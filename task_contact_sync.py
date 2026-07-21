"""
联系人昵称统一同步任务（独立进程，与 task_group_sync.py 同一形态）

职责：维护统一联系人信息表 wecom_contacts（全系统联系人昵称唯一权威来源）
  - 增量发现（默认每 10 分钟）：messages 里新出现的 sender → 插入并立即解析
  - 全量刷新（默认每 6 小时）：逐个联系人重新拉取昵称，捕捉改名
    连续失败 >= 5 次的联系人退避为每天试 1 次；解析失败不覆盖旧昵称

背景：core/contacts.py 的 get_name() 原来只查 contacts_cache 永久缓存，命中就直接
返回、永不刷新，联系人改名后系统里显示的还是旧名字（如 FlowBot @提醒功能用的客服
账号改名后，@不到人也不会报错，只会静默失效）。现在改用本任务维护的 wecom_contacts
表做权威来源，contacts_cache 降级为兜底（同 wecom_groups vs contacts_cache 的关系）。

运行方式（与 web 服务完全解耦，互不影响）：
    cd /home/wxbot && nohup python3 task_contact_sync.py >> logs/contact_sync.log 2>&1 &

配置（config.yaml 可选，均有默认值）：
    contact_sync:
      incremental_interval: 600    # 增量发现间隔（秒）
      full_interval: 21600         # 全量刷新间隔（秒）
      api_delay: 0.3               # 每次企微 API 调用间隔（秒）
      sync_corp_names: ["午虎科技"]  # 只用这些企业的接口查昵称，不配=全部企业
"""

import json
import logging
import time
import urllib.request

import yaml

from storage.db import Database
from storage.contact_db import (
    init_contact_table, discover_new_contacts, get_contacts_to_sync,
    update_contact_success, update_contact_failure, get_sync_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("contact_sync")

# MySQL 命名锁：防止误起多个进程重复同步
_LOCK_NAME = "wxbot_contact_sync"


class CorpClient:
    """单个企业的企微 API 客户端（token 缓存 + 联系人昵称拉取），与 task_group_sync.CorpClient 同构"""

    def __init__(self, corpid: str, secret: str, external_secret: str = "", name: str = ""):
        self.corpid = corpid
        self.secret = secret
        self.external_secret = external_secret
        self.name = name or corpid
        self._token = ""
        self._token_expires = 0
        self._ext_token = ""
        self._ext_token_expires = 0

    def _get_token(self, secret: str, cache_attr: str, expire_attr: str) -> str:
        now = int(time.time())
        if getattr(self, cache_attr) and now < getattr(self, expire_attr):
            return getattr(self, cache_attr)
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corpid}&corpsecret={secret}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errcode") == 0:
                setattr(self, cache_attr, data["access_token"])
                setattr(self, expire_attr, now + data.get("expires_in", 7200) - 300)
                return data["access_token"]
            logger.error("[%s] 获取token失败: %s", self.name, data.get("errmsg"))
        except Exception as e:
            logger.error("[%s] 获取token异常: %s", self.name, e)
        return ""

    def access_token(self) -> str:
        return self._get_token(self.secret, "_token", "_token_expires")

    def external_token(self) -> str:
        if not self.external_secret:
            return ""
        return self._get_token(self.external_secret, "_ext_token", "_ext_token_expires")

    def fetch_contact(self, userid: str, contact_type: str):
        """
        拉取单个联系人昵称。
        返回:
          dict {"name": ...}                成功
          {"permanent": True}                联系人不存在/已离职（不可恢复）
          {"error": "失败原因"}               本企业解析不到（可换企业再试）
        """
        if contact_type == "external":
            token = self.external_token() or self.access_token()
            if not token:
                return {"error": "external_secret未配置或token获取失败"}
            url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get?access_token={token}&external_userid={userid}"
            try:
                with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("errcode") == 0:
                    name = (data.get("external_contact") or {}).get("name", "")
                    return {"name": name} if name else {"error": "接口返回昵称为空"}
                errcode = data.get("errcode")
                if errcode == 40096:
                    # 40096 = 无效的 external_userid（ID 截断或已失效）
                    return {"permanent": True}
                return {"error": f"externalcontact/get errcode={errcode}({data.get('errmsg', '')})"}
            except Exception as e:
                return {"error": f"externalcontact/get异常: {e}"}
        else:
            token = self.access_token()
            if not token:
                return {"error": "获取access_token失败"}
            url = f"https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token={token}&userid={userid}"
            try:
                with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("errcode") == 0:
                    name = data.get("name", "")
                    return {"name": name} if name else {"error": "接口返回昵称为空"}
                errcode = data.get("errcode")
                if errcode == 60111:
                    # 60111 = userid不存在（离职/删除）
                    return {"permanent": True}
                return {"error": f"user/get errcode={errcode}({data.get('errmsg', '')})"}
            except Exception as e:
                return {"error": f"user/get异常: {e}"}


def build_corps(config: dict) -> list:
    """从 config.yaml 组装企业客户端列表（主企业在前），与 task_group_sync.build_corps 同构"""
    wecom = config["wecom"]
    corps = [CorpClient(
        corpid=wecom["corpid"], secret=wecom["secret"],
        external_secret=wecom.get("external_secret", ""), name="车物家",
    )]
    seen = {wecom["corpid"]}
    for i, cb in enumerate(config.get("extra_callbacks", []) or []):
        cid, sec = cb.get("corpid", ""), cb.get("secret", "")
        if cid and sec and cid not in seen:
            seen.add(cid)
            corps.append(CorpClient(
                corpid=cid, secret=sec,
                external_secret=cb.get("external_secret", ""),
                name=cb.get("name", f"企业{i + 2}"),
            ))
    return corps


def resolve_contact(corps: list, userid: str, contact_type: str, corpid_hint: str, api_delay: float):
    """逐企业解析联系人昵称。已知所属企业的优先试（省 API 调用）。
    返回 (corp, detail) / (None, {"permanent":True}) / (None, {"errors": [各企业失败原因]})"""
    ordered = sorted(corps, key=lambda c: 0 if c.corpid == corpid_hint else 1) \
        if corpid_hint else corps
    errors = []
    for corp in ordered:
        detail = corp.fetch_contact(userid, contact_type)
        time.sleep(api_delay)
        if detail and detail.get("permanent"):
            return None, detail
        if detail and detail.get("name"):
            return corp, detail
        if detail and detail.get("error"):
            errors.append(f"{corp.name}: {detail['error']}")
        else:
            errors.append(f"{corp.name}: 接口返回昵称为空")
    return None, {"errors": errors}


def run_sync(db, corps: list, full: bool, api_delay: float, force: bool = False,
             corpids: list = None):
    """执行一轮同步（增量 full=False / 全量 full=True / force=True 忽略失败退避强制全量）"""
    label = "强制全量刷新" if (full and force) else ("全量刷新" if full else "增量发现")
    lock_conn = db.pool.connection()
    got_lock = False
    try:
        lock_cursor = lock_conn.cursor()
        lock_cursor.execute(f"SELECT GET_LOCK('{_LOCK_NAME}', 0)")
        got_lock = lock_cursor.fetchone()[0] == 1
        if not got_lock:
            logger.info("%s：另一个同步进程正在执行，本轮跳过", label)
            return

        new_cnt = discover_new_contacts(db)
        if new_cnt:
            logger.info("发现 %d 个新联系人", new_cnt)

        contacts = get_contacts_to_sync(db, full=full, force=force, corpids=corpids)
        if not contacts:
            if full:
                logger.info("%s：无待同步的联系人", label)
            return

        total = len(contacts)
        logger.info("%s开始: 本轮待同步 %d 个联系人", label, total)
        ok = failed = changed = done = 0
        for c in contacts:
            done += 1
            corp, detail = resolve_contact(
                corps, c["userid"], c.get("contact_type") or "user", c.get("corpid", ""), api_delay)
            if corp and detail:
                name_changed = update_contact_success(db, c["userid"], corp.corpid, detail["name"])
                ok += 1
                if name_changed:
                    changed += 1
                    logger.info("[%d/%d] 昵称更新: %s 「%s」->「%s」(%s)",
                                done, total, c["userid"],
                                c.get("name") or "(空)", detail["name"], corp.name)
                else:
                    logger.info("[%d/%d] 同步成功: %s 「%s」(%s)",
                                done, total, c["userid"], detail["name"], corp.name)
            elif detail and detail.get("permanent"):
                update_contact_failure(db, c["userid"], permanent=True)
                failed += 1
                logger.info("[%d/%d] 联系人不存在/已离职: %s（保留最后昵称「%s」）",
                            done, total, c["userid"], c.get("name") or "")
            else:
                update_contact_failure(db, c["userid"])
                failed += 1
                reasons = "; ".join((detail or {}).get("errors") or ["未知原因"])
                logger.info("[%d/%d] 同步失败: %s 「%s」| %s",
                            done, total, c["userid"], c.get("name") or "(空)", reasons)

        logger.info("%s完成: 共 %d 个联系人, 成功 %d, 失败 %d, 昵称变化 %d | 总体状态: %s",
                    label, len(contacts), ok, failed, changed, get_sync_stats(db))
    except Exception:
        logger.exception("%s执行异常", label)
    finally:
        try:
            if got_lock:
                lock_cursor.execute(f"SELECT RELEASE_LOCK('{_LOCK_NAME}')")
        except Exception:
            pass
        lock_conn.close()


def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sync_cfg = config.get("contact_sync", {}) or {}
    incremental_interval = sync_cfg.get("incremental_interval", 600)
    full_interval = sync_cfg.get("full_interval", 6 * 3600)
    # 联系人解析企业白名单：只用这些企业的接口查昵称（不配=全部企业）
    sync_corp_names = sync_cfg.get("sync_corp_names", ["午虎科技"])
    api_delay = sync_cfg.get("api_delay", 0.3)

    db = Database(config["mysql"], main_corpid=config["wecom"]["corpid"])
    init_contact_table(db)
    corps = build_corps(config)
    if sync_corp_names:
        sync_corps = [c for c in corps if c.name in sync_corp_names] or corps
    else:
        sync_corps = corps
    logger.info("联系人同步任务启动: 发现企业 %s / 解析企业 %s | 增量 %ds / 全量 %ds",
                [c.name for c in corps], [c.name for c in sync_corps],
                incremental_interval, full_interval)

    sync_corpids = [c.corpid for c in sync_corps]

    def sync(full, force=False):
        run_sync(db, sync_corps, full=full, api_delay=api_delay, force=force, corpids=sync_corpids)

    sync(full=True, force=True)
    # 联系人没有等价于"客户群列表"的批量发现接口，新联系人只能靠 messages 发现，
    # 发现时 corpid 还是空的，进不了上面那次全量（按 corpid 过滤）。这里紧接着补一次
    # 增量（不按 corpid 过滤），让首次启动时发现的联系人立刻解析，不用等第一个增量周期。
    sync(full=False)

    now = time.time()
    next_incremental = now + incremental_interval
    next_full = now + full_interval
    while True:
        time.sleep(30)
        now = time.time()
        if now >= next_full:
            sync(full=True)
            next_full = time.time() + full_interval
            next_incremental = time.time() + incremental_interval  # 全量已含增量
        elif now >= next_incremental:
            sync(full=False)
            next_incremental = time.time() + incremental_interval


if __name__ == "__main__":
    main()

"""
群信息统一同步任务（独立进程，参考 wuhuApi/task.py 形态）

职责：维护统一群信息表 wecom_groups（全系统群名唯一权威来源）
  - 增量发现（默认每 10 分钟）：messages 里新出现的群 → 插入并立即解析
  - 全量刷新（默认每 6 小时）：逐群重新拉取群名/群主/成员数，捕捉群改名
    连续失败 >= 5 次的群退避为每天试 1 次；解析失败不覆盖旧群名
  - 客户群列表发现（默认每天 1 次）：调客户群列表 API 全量发现新群，
    还没发过消息的群也能提前入表（配监控群不再需要先发一条消息）

运行方式（与 web 服务完全解耦，互不影响）：
    cd /home/wxbot && nohup python3 task_group_sync.py >> logs/group_sync.log 2>&1 &
或用 systemd/supervisor 托管。误起多个进程时由 MySQL 命名锁保证不重复同步。

配置（config.yaml 可选，均有默认值）：
    group_sync:
      incremental_interval: 600    # 增量发现间隔（秒）
      full_interval: 21600         # 全量刷新间隔（秒）
      list_interval: 86400         # 客户群列表发现间隔（秒），0=关闭
      list_corp_names: ["午虎科技"]  # 列表发现只跑这些企业（按 name），不配=全部企业
      api_delay: 0.3               # 每次企微 API 调用间隔（秒）
"""

import json
import logging
import time
import urllib.request

import yaml

from storage.db import Database
from storage.group_db import (
    init_group_table, discover_new_rooms, get_groups_to_sync,
    update_group_success, update_group_failure, get_sync_stats,
    insert_room_ids, cleanup_pending_external,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("group_sync")

# MySQL 命名锁：防止误起多个进程重复同步
_LOCK_NAME = "wxbot_group_sync"


class CorpClient:
    """单个企业的企微 API 客户端（token 缓存 + 群详情拉取）"""

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

    @staticmethod
    def _post(url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch_group(self, roomid: str):
        """
        拉取单个群详情。降级链与 core/contacts.py 一致：
        会话存档接口（内部群）→ 客户群接口（外部群）。

        返回:
          dict {"name","owner","member_count","group_type"}  成功
          {"permanent": True}                                群不存在/已解散
          {"error": "失败原因"}                               本企业解析不到（可换企业再试）
        """
        # 1. 会话存档接口（内部群）
        # 无论内部群接口因何失败（301059非内部群/301052存档到期等），都继续降级试客户群接口
        internal_err = ""
        token = self.access_token()
        if token:
            try:
                data = self._post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/msgaudit/groupchat/get?access_token={token}",
                    {"roomid": roomid},
                )
                if data.get("errcode") == 0:
                    name = data.get("roomname", "")
                    members = data.get("members", [])
                    if not name and members:
                        name = "、".join(m.get("memberid", "") for m in members[:3]) + "..."
                    return {"name": name, "owner": "",
                            "member_count": len(members), "group_type": "internal"}
                if data.get("errcode") != 301059:  # 301059=非内部群，属正常降级不算错误
                    internal_err = f"内部群接口errcode={data.get('errcode')}({data.get('errmsg', '')})"
            except Exception as e:
                internal_err = f"内部群接口异常: {e}"
        else:
            internal_err = "获取access_token失败"

        # 2. 客户群接口（外部群）
        ext_token = self.external_token()
        if not ext_token:
            return {"error": ((internal_err + "; ") if internal_err else "")
                    + "客户群接口: external_secret未配置或token获取失败"}
        try:
            data = self._post(
                f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/get?access_token={ext_token}",
                {"chat_id": roomid, "need_name": 1},
            )
            errcode = data.get("errcode")
            if errcode == 0:
                chat = data.get("group_chat", {})
                members = chat.get("member_list", [])
                name = chat.get("name", "")
                if not name and members:
                    picked = [m.get("name", "") for m in members[:3] if m.get("name")]
                    name = ("、".join(picked) + "...") if picked else ""
                return {"name": name, "owner": chat.get("owner", ""),
                        "member_count": len(members), "group_type": "external"}
            if errcode == 40050:
                # 40050 = chat_id 无效（群已解散/不存在）
                return {"permanent": True}
            # 90501=不是外部群 / 92002=跨企业 等：本企业解析不到，换下一个企业
            return {"error": ((internal_err + "; ") if internal_err else "")
                    + f"客户群接口errcode={errcode}({data.get('errmsg', '')})"}
        except Exception as e:
            return {"error": ((internal_err + "; ") if internal_err else "")
                    + f"客户群接口异常: {e}"}

    def list_customer_groups(self, api_delay: float = 0.3) -> list:
        """
        客户群列表 API（externalcontact/groupchat/list）翻页拉取本企业全部客户群 chat_id。
        参考 wuhuApi/task.py 的 get_group_chat_list。
        需要「客户联系」权限且应用可见范围覆盖群主；未配置 external_secret 时返回空。
        """
        token = self.external_token()
        if not token:
            return []
        chat_ids = []
        cursor = ""
        url = f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/list?access_token={token}"
        while True:
            body = {"status_filter": 0, "limit": 1000}
            if cursor:
                body["cursor"] = cursor
            try:
                data = self._post(url, body)
            except Exception as e:
                logger.warning("[%s] 客户群列表接口异常: %s", self.name, e)
                break
            if data.get("errcode") != 0:
                logger.warning("[%s] 客户群列表接口失败: errcode=%s, errmsg=%s",
                               self.name, data.get("errcode"), data.get("errmsg"))
                break
            chat_ids.extend(
                g.get("chat_id") for g in data.get("group_chat_list", []) if g.get("chat_id")
            )
            cursor = data.get("next_cursor", "")
            if not cursor:
                break
            time.sleep(api_delay)
        return chat_ids


def build_corps(config: dict) -> list:
    """从 config.yaml 组装企业客户端列表（主企业在前）"""
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


def resolve_group(corps: list, roomid: str, corpid_hint: str, api_delay: float):
    """
    逐企业解析群详情。已知所属企业的优先试（省 API 调用）。
    返回 (corp, detail) / (None, {"permanent":True}) / (None, {"errors": [各企业失败原因]})
    """
    ordered = sorted(corps, key=lambda c: 0 if c.corpid == corpid_hint else 1) \
        if corpid_hint else corps
    errors = []
    for corp in ordered:
        detail = corp.fetch_group(roomid)
        time.sleep(api_delay)
        if detail and detail.get("permanent"):
            return None, detail
        if detail and detail.get("name"):
            return corp, detail
        if detail and detail.get("error"):
            errors.append(f"{corp.name}: {detail['error']}")
        else:
            errors.append(f"{corp.name}: 接口返回群名为空")
    return None, {"errors": errors}


def run_sync(db, corps: list, full: bool, api_delay: float, force: bool = False):
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

        new_cnt = discover_new_rooms(db)
        if new_cnt:
            logger.info("发现 %d 个新群", new_cnt)

        groups = get_groups_to_sync(db, full=full, force=force)
        if not groups:
            if full:
                logger.info("%s：无待同步的群", label)
            return

        total = len(groups)
        logger.info("%s开始: 本轮待同步 %d 个群", label, total)
        ok = failed = changed = done = 0
        for g in groups:
            done += 1
            corp, detail = resolve_group(corps, g["roomid"], g.get("corpid", ""), api_delay)
            if corp and detail:
                name_changed = update_group_success(
                    db, g["roomid"], corp.corpid, detail["name"],
                    owner=detail.get("owner", ""),
                    member_count=detail.get("member_count", 0),
                    group_type=detail.get("group_type", ""),
                )
                ok += 1
                if name_changed:
                    changed += 1
                    logger.info("[%d/%d] 群名更新: %s 「%s」->「%s」(%s)",
                                done, total, g["roomid"],
                                g.get("name") or "(空)", detail["name"], corp.name)
                else:
                    logger.info("[%d/%d] 同步成功: %s 「%s」(%s)",
                                done, total, g["roomid"], detail["name"], corp.name)
            elif detail and detail.get("permanent"):
                update_group_failure(db, g["roomid"], permanent=True)
                failed += 1
                logger.info("[%d/%d] 群已解散/不存在: %s（保留最后群名「%s」）",
                            done, total, g["roomid"], g.get("name") or "")
            else:
                update_group_failure(db, g["roomid"])
                failed += 1
                reasons = "; ".join((detail or {}).get("errors") or ["未知原因"])
                logger.info("[%d/%d] 同步失败: %s 「%s」| %s",
                            done, total, g["roomid"], g.get("name") or "(空)", reasons)

        logger.info("%s完成: 共 %d 个群, 成功 %d, 失败 %d, 群名变化 %d | 总体状态: %s",
                    label, len(groups), ok, failed, changed, get_sync_stats(db))
    except Exception:
        logger.exception("%s执行异常", label)
    finally:
        try:
            if got_lock:
                lock_cursor.execute(f"SELECT RELEASE_LOCK('{_LOCK_NAME}')")
        except Exception:
            pass
        lock_conn.close()


def run_list_discovery(db, corps: list, api_delay: float, allowed_names: list = None):
    """客户群列表全量发现：各企业拉客户群列表，新 chat_id 插入为 pending（随后由增量解析补详情）。
    allowed_names 非空时只跑白名单内的企业，并清理此前误插入的其他企业待解析群。"""
    if allowed_names:
        allowed = [c for c in corps if c.name in allowed_names]
        if not allowed:
            logger.warning("列表发现白名单 %s 未匹配到任何企业，跳过", allowed_names)
            return
        deleted = cleanup_pending_external(db, [c.corpid for c in allowed])
        if deleted:
            logger.info("已清理白名单外企业的待解析群 %d 个", deleted)
        corps = allowed
    total_new = 0
    for corp in corps:
        chat_ids = corp.list_customer_groups(api_delay)
        if not chat_ids:
            continue
        new_cnt = insert_room_ids(db, chat_ids, corpid=corp.corpid, group_type="external")
        total_new += new_cnt
        logger.info("[%s] 客户群列表发现: 共 %d 个群, 新增 %d", corp.name, len(chat_ids), new_cnt)
    if total_new:
        logger.info("客户群列表发现完成: 新增 %d 个群，转增量解析", total_new)


def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sync_cfg = config.get("group_sync", {}) or {}
    incremental_interval = sync_cfg.get("incremental_interval", 600)
    full_interval = sync_cfg.get("full_interval", 6 * 3600)
    list_interval = sync_cfg.get("list_interval", 24 * 3600)  # 0=关闭客户群列表发现
    # 列表发现企业白名单（按企业 name）：不配置默认只跑「午虎科技」，配 [] 表示全部企业
    list_corp_names = sync_cfg.get("list_corp_names", ["午虎科技"])
    # 群详情解析企业白名单：只用这些企业的接口查群名（车物家会话存档已到期，查了也是浪费）
    sync_corp_names = sync_cfg.get("sync_corp_names", ["午虎科技"])
    api_delay = sync_cfg.get("api_delay", 0.3)

    db = Database(config["mysql"], main_corpid=config["wecom"]["corpid"])
    init_group_table(db)
    corps = build_corps(config)
    if sync_corp_names:
        sync_corps = [c for c in corps if c.name in sync_corp_names] or corps
    else:
        sync_corps = corps
    logger.info("群同步任务启动: 发现企业 %s / 解析企业 %s | 增量 %ds / 全量 %ds / 列表发现 %ds (白名单 %s)",
                [c.name for c in corps], [c.name for c in sync_corps],
                incremental_interval, full_interval,
                list_interval, list_corp_names or "全部")

    # 启动先跑一轮列表发现 + 强制全量（每次启动都把所有群重新同步一遍群名，忽略失败退避）
    if list_interval:
        run_list_discovery(db, corps, api_delay, list_corp_names)
    run_sync(db, sync_corps, full=True, api_delay=api_delay, force=True)

    now = time.time()
    next_incremental = now + incremental_interval
    next_full = now + full_interval
    next_list = now + list_interval if list_interval else float("inf")
    while True:
        time.sleep(30)
        now = time.time()
        if now >= next_list:
            run_list_discovery(db, corps, api_delay, list_corp_names)
            run_sync(db, sync_corps, full=False, api_delay=api_delay)  # 立即解析新发现的群
            next_list = time.time() + list_interval
            next_incremental = time.time() + incremental_interval
        elif now >= next_full:
            run_sync(db, sync_corps, full=True, api_delay=api_delay)
            next_full = time.time() + full_interval
            next_incremental = time.time() + incremental_interval  # 全量已含增量
        elif now >= next_incremental:
            run_sync(db, sync_corps, full=False, api_delay=api_delay)
            next_incremental = time.time() + incremental_interval


if __name__ == "__main__":
    main()

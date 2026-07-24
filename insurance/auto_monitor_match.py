"""
识别群自动匹配 —— 群名命中「识别群 + TZxxx 编号」规则时，
自动把该群加入对应企业的识别群监控配置（monitor_configs）。

两个触发入口共用本模块的 check_and_sync_group：
  1. task_group_sync.py 定时任务：全量/增量同步检测到群名变化(name_changed)时
  2. callback/event_callback.py 事件回调：change_external_chat 群名变化时

规则：群名须同时满足
  - 包含字面量「识别群」
  - 能提取出 TZ + 数字 编号（如 TZ050 / TZ-050）
编号归一化后（去符号、转大写、数字补齐 3 位）与 enterprises.enterprise_no 匹配：
  - 命中企业 → 把群加入其 enabled 且 id 最大的一条监控配置；无配置则新建一条
  - 未命中企业 → 自动创建「预备群TZxxx」占位企业并发钉钉通知，再加入其新建配置

设计详见 docs/review/2026-07-22-群名称与客户变更事件回调-方案原型.html
"""

import logging
import re

logger = logging.getLogger(__name__)

# 识别群关键词（群名须包含此字面量）
_RECOGNIZE_KEYWORD = "识别群"
# TZ 编号：TZ + 可选横杠 + 数字，如 TZ050 / TZ-050 / tz 050（大小写不敏感）
_TZ_PATTERN = re.compile(r"TZ-?(\d+)", re.IGNORECASE)


def check_and_sync_group(db, roomid, name, corpid=""):
    """群名命中规则时，检查/补加识别群到对应企业的监控配置。

    幂等：该群已在企业名下任意配置中则跳过。
    任何异常都在内部吞掉并记日志，绝不影响调用方（定时任务/事件回调）主流程。

    返回状态字符串，供调用方统计汇总：
      None      非识别群（群名不含「识别群」或无 TZ 编号，未处理）
      "already" 命中规则但已在监控配置，跳过
      "added"   命中规则且本次新加入监控配置（含新建配置 / 建占位企业后加入）
      "error"   处理过程中异常
    """
    try:
        return _do_check_and_sync(db, roomid, name or "", corpid or "")
    except Exception:
        logger.exception("识别群自动匹配失败: roomid=%s name=%s", roomid, name)
        return "error"


def _do_check_and_sync(db, roomid, name, corpid):
    # 延迟导入，避免模块级循环依赖（task_group_sync 独立进程也能安全 import）
    from auth.db import get_enterprise_by_no, create_enterprise, update_enterprise
    from insurance.monitor_config_db import (
        list_monitor_configs_by_enterprise,
        create_monitor_config,
        update_monitor_config,
    )
    from core.notify import notify_new_enterprise_placeholder

    # 1. 关键词 + 编号规则，缺一不满足直接跳过（非识别群）
    if _RECOGNIZE_KEYWORD not in name:
        return None
    m = _TZ_PATTERN.search(name)
    if not m:
        return None
    # 数字补齐 3 位，保证 TZ50 / TZ050 归一到同一编号（与既有 TZ008 约定一致）
    norm_digits = m.group(1).zfill(3)
    tz_key = "TZ" + norm_digits           # 查询用归一化编号，如 TZ050

    # 2. 按归一化编号查企业
    enterprise = get_enterprise_by_no(db, tz_key)
    if not enterprise:
        # 2.1 未匹配到企业：自动建「预备群」占位企业（不 return，继续把群加进它的配置）
        enterprise_no = f"TZ-{norm_digits}"      # 存带横杠三位补零，如 TZ-050
        placeholder_name = f"预备群{tz_key}"      # 如 预备群TZ050
        eid = create_enterprise(db, placeholder_name)
        update_enterprise(db, eid, {"enterprise_no": enterprise_no})
        enterprise = {"id": eid, "name": placeholder_name}
        logger.info("识别群自动匹配: 编号 %s 未匹配企业，已自动创建占位企业 %s(id=%s)",
                    tz_key, placeholder_name, eid)
        notify_new_enterprise_placeholder(placeholder_name, enterprise_no, roomid, name)

    enterprise_id = enterprise["id"]

    # 3. 判重：该企业名下任意一条配置（不限 enabled）的 rooms 已含该群则跳过
    configs = list_monitor_configs_by_enterprise(db, enterprise_id)
    for c in configs:
        room_ids = {r.get("id") for r in (c.get("rooms") or []) if isinstance(r, dict)}
        if roomid in room_ids:
            return "already"

    # 4. 选目标配置：enabled=1 且 id 最大（最新）；没有则新建一条
    enabled = [c for c in configs if c.get("enabled")]
    target = max(enabled, key=lambda c: c["id"]) if enabled else None
    if target:
        rooms = list(target.get("rooms") or []) + [{"id": roomid, "name": name}]
        update_monitor_config(db, target["id"], {"rooms": rooms})
        logger.info("识别群自动匹配: 群「%s」(%s) 加入企业 %s 的监控配置 id=%s",
                    name, roomid, enterprise["name"], target["id"])
    else:
        new_id = create_monitor_config(db, {
            "name": f"{enterprise['name']}-自动创建",
            "enabled": 1,
            "enterprise_id": enterprise_id,
            "rooms": [{"id": roomid, "name": name}],
        })
        logger.info("识别群自动匹配: 群「%s」(%s) 新建企业 %s 的监控配置 id=%s",
                    name, roomid, enterprise["name"], new_id)
    return "added"

"""
FlowBot 群消息通知（识别失败/群内回填/未开通群等场景向群发送提示并 @ 相关人）。

走第三方 FlowBot（飞流机器人）HTTP 接口，按「群名称」定位群、按「微信名」@ 人。
前提：对应的 FlowBot 机器人必须已在目标微信群内，否则找不到群、发送失败。

送达确认：sendTask 的 code=200 仅代表"指令已接收"（异步执行），真实执行结果
由 FlowBot 回调（insurance/flowbot_callback.py）按 taskId 推送。发送成功后将
taskId + 原始 payload 写入 flowbot_push_log 表，回调失败时用 payload 重发。

本模块所有异常均自行吞掉（旁路通知，绝不影响保单识别主流程）。
"""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

FLOWBOT_API = "https://flowbot.feiliu.run/api/sendTask"
FLOWBOT_ROBOT_INFO_API = "https://flowbot.feiliu.run/api/getRobotInfo"

# FlowBot 消息类型码
_TYPE_TEXT = 50009  # 文字（支持 atList @人）

# 推送日志落库用的 db 引用（main.py 启动时注入；为 None 时跳过落库，行为向后兼容）
_db = None


def init_flowbot_db(db):
    """注入数据库引用，启用推送日志落库（配合回调做送达确认与失败重发）。"""
    global _db
    _db = db


def post_task(task_list: list, robot_id: str) -> dict:
    """
    调用 FlowBot sendTask 接口（单次，不重试）。
    返回接口 JSON（异常时返回 {"code": -1, "message": <错误>}）。
    """
    try:
        resp = requests.post(
            FLOWBOT_API,
            params={"robotId": robot_id},
            json={"taskList": task_list},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"code": -1, "message": str(e)}


def get_robot_info(robot_id: str):
    """
    查询机器人信息（文档：/api/getRobotInfo）。

    返回 data 字典（含 isOnline: 0离线/1在线、lastLogin_at 等）；
    接口异常或非 200 时返回 None（调用方按"无法核实"处理）。
    """
    try:
        resp = requests.post(
            FLOWBOT_ROBOT_INFO_API,
            params={"robotId": robot_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data") or {}
        logger.warning("FlowBot getRobotInfo 返回非200: %s", data)
    except Exception as e:
        logger.warning("FlowBot getRobotInfo 请求异常: %s", e)
    return None


def _save_push_log(task_id: str, robot_id: str, group_name: str,
                   message: str, task_list: list,
                   source: str = "notify", roomid: str = ""):
    """
    发送成功后落库推送日志（供回调更新结果/失败重发）。失败只记日志。

    Returns:
        新插入记录的自增 id（落库失败或未注入 db 时返回 None）
    """
    if not _db:
        return None
    try:
        import pymysql.cursors
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "INSERT INTO flowbot_push_log "
                "(task_id, robot_id, source, roomid, group_name, message, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (task_id, robot_id, source, roomid, group_name, message,
                 json.dumps(task_list, ensure_ascii=False)),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logger.warning("FlowBot 推送日志落库失败（不影响发送）: %s", e)
        return None


def send_flowbot_group_message(group_name: str, wechat_name, message: str, robot_id: str) -> bool:
    """
    通过 FlowBot 向指定微信群发送一条文字消息（可 @ 一人或多人）。

    Args:
        group_name:  目标群名称（FlowBot 按名称模糊定位群）
        wechat_name: 要 @ 的成员微信名；单人传字符串，多人传列表（空/None 则不 @）。
                     @全员传中文"所有人"（微信 @ 面板的原生快捷项，命中微信自身"所有人"
                     候选，而非按昵称查找某个真实成员）——传英文"@all"不是任何成员的
                     昵称，FlowBot 找不到匹配项，会静默不 @ 任何人（消息仍会正常发出，
                     2026-07-28 踩过）。另外，微信平台层面只有群主/管理员账号才能真正
                     触发 @所有人，机器人账号若不是对应群的群主/管理员，即使传对了
                     "所有人"也不会生效，需要在群里确认机器人的身份。
        message:     消息正文
        robot_id:    FlowBot 机器人 ID（台账专用，不同于报价系统）

    Returns:
        是否发送成功（失败只记日志、返回 False，不抛异常）
    """
    if not group_name or not robot_id:
        logger.warning("FlowBot 群通知缺少群名或 robotId，跳过（group=%r robot=%r）",
                       group_name, robot_id)
        return False

    if isinstance(wechat_name, (list, tuple)):
        at_list = [n for n in wechat_name if n]
    else:
        at_list = [wechat_name] if wechat_name else []
    task_list = [{
        "type": _TYPE_TEXT,
        "atList": at_list,
        "searchText": group_name,
        "message": message,
    }]

    # 发送失败重试最多3次（接口异常/非200都重试），1s/2s 递增退避
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        result = post_task(task_list, robot_id)
        if result.get("code") == 200:
            task_ids = [t.get("taskId", "") for t in (result.get("taskList") or [])]
            task_id = next(filter(None, task_ids), "")
            logger.info("✅ 已发送群通知到【%s】@%s (taskId=%s): %s",
                        group_name, "、".join(at_list) or "-", task_id or "-", message)
            _save_push_log(task_id, robot_id, group_name, message, task_list)
            return True
        logger.warning("⚠ 群通知发送失败(第%d/%d次): %s", attempt, max_attempts, result)
        if attempt < max_attempts:
            time.sleep(attempt)  # 1s, 2s 递增退避
    return False


def send_monitor_message(group_name: str, message: str, robot_id: str, roomid: str) -> dict:
    """
    监控页手动发送群消息：单次调用不重试（失败由用户在页面手动重发），
    落库 source='monitor'（回调失败时不走自动重发，只标记状态）。

    Returns:
        {"ok": True, "task_id": xx, "log_id": xx}
        或 {"ok": False, "error": <原因>}
    """
    task_list = [{
        "type": _TYPE_TEXT,
        "atList": [],
        "searchText": group_name,
        "message": message,
    }]
    result = post_task(task_list, robot_id)
    if result.get("code") != 200:
        error = result.get("message") or f"FlowBot 接口返回异常(code={result.get('code')})"
        logger.warning("监控页群消息发送失败 群=%s: %s", group_name, error)
        return {"ok": False, "error": error}

    task_ids = [t.get("taskId", "") for t in (result.get("taskList") or [])]
    task_id = next(filter(None, task_ids), "")
    log_id = _save_push_log(task_id, robot_id, group_name, message, task_list,
                            source="monitor", roomid=roomid)
    logger.info("监控页群消息已提交 群=%s taskId=%s logId=%s", group_name, task_id or "-", log_id)
    return {"ok": True, "task_id": task_id, "log_id": log_id}

"""
FlowBot 机器人回调接收端（参考 wuhuApi 实现，文档 https://flowbot.apifox.cn/doc-5369842）。

注册方式：POST https://flowbot.feiliu.run/api/updateCallBackUrl?robotId=<robotId>
          body: {"callBackUrl": "https://wxbot.wuhuxiche.com/flowbot/callback"}

回调类型（mode）：
    init     初始化回调配置
    logs     收到聊天记录（量大，只记 debug 日志）
    receipt  指令已接收（v2.5.5+）
    callBack 指令执行回调（真实送达结果：data.taskId / data.status=success|fail / data.message）
    online   机器人上线
    offline  机器人离线（先调 getRobotInfo 核实：真离线才报离线，在线则按发送失败告警）

约束：必须 3 秒内返回 {"code": 200}，耗时处理（失败重发）放后台线程。

callBack 失败处理：按 taskId 查 flowbot_push_log，重发次数 < 3 时用原 payload
重发（更新为新 taskId、状态回到推送中）；重发额度用尽则标记最终失败 + 钉钉告警。
"""

import json
import logging
import threading

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

flowbot_bp = Blueprint("flowbot", __name__)

_db = None

# 回调失败后的最大重发次数
_MAX_RETRY = 3


def init_flowbot_callback(db):
    """注入数据库引用（main.py 启动时调用）。"""
    global _db
    _db = db


@flowbot_bp.route("/flowbot/callback", methods=["POST"])
def flowbot_callback():
    """FlowBot 回调入口：快速应答，耗时逻辑放后台线程。"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "")
        robot_id = data.get("robotId", "")

        if mode == "callBack":
            cb = data.get("data", {}) or {}
            task_id = cb.get("taskId", "")
            status = cb.get("status", "")
            message = cb.get("message", "")
            logger.info("FlowBot回调: 指令执行结果 taskId=%s status=%s msg=%s",
                        task_id, status, message)
            if task_id:
                threading.Thread(
                    target=_handle_task_result,
                    args=(task_id, status, message, robot_id),
                    daemon=True,
                ).start()
        elif mode == "receipt":
            cb = data.get("data", {}) or {}
            logger.info("FlowBot回调: 指令已接收 taskId=%s 队列剩余=%s",
                        cb.get("taskId", ""), cb.get("taskCount", ""))
        elif mode == "offline":
            # 飞流有时消息发送失败也会推 offline 回调，不能直接断言已离线；
            # 放后台线程调 getRobotInfo 核实真实在线状态后再告警（回调须3秒内应答）
            logger.warning("FlowBot回调: 收到离线通知 robot=%s（待核实）", robot_id)
            threading.Thread(
                target=_handle_offline, args=(robot_id,), daemon=True,
            ).start()
        elif mode == "online":
            logger.info("FlowBot回调: 机器人上线 robot=%s", robot_id)
        elif mode == "init":
            logger.info("FlowBot回调: 回调地址初始化成功 robot=%s", robot_id)
        elif mode == "logs":
            # 聊天记录推送量大，只记 debug（wxbot 的消息来源是会话存档，不依赖这里）
            logger.debug("FlowBot回调: logs from=%s 共%d条",
                         data.get("searchText", ""), len(data.get("data") or []))
        else:
            logger.info("FlowBot回调: 未知mode=%s robot=%s", mode, robot_id)
    except Exception as e:
        # 任何异常都不影响应答（3秒内必须返回200，否则飞流会视为回调失败）
        logger.warning("FlowBot回调处理异常: %s", e, exc_info=True)

    return jsonify({"code": 200, "message": "回调接收成功"})


def _query_pending_pushes(robot_id: str, limit: int = 5):
    """查最近1小时内仍在推送中(status=0)的通知，用于告警时说明受影响的群和内容。"""
    if not _db:
        return []
    try:
        import pymysql.cursors
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT group_name, message FROM flowbot_push_log "
                "WHERE robot_id = %s AND status = 0 "
                "AND created_at >= NOW() - INTERVAL 1 HOUR "
                "ORDER BY id DESC LIMIT %s", (robot_id, limit))
            return cursor.fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询FlowBot待送达通知失败: %s", e)
        return []


def _handle_offline(robot_id: str):
    """
    离线回调后台处理：先调 getRobotInfo 核实真实在线状态，再决定告警口径。

      - 核实在线   → 只是消息发送失败，按「发送失败」告警，列出失败的群和内容
      - 核实已离线 → 离线告警，附最后登录时间和受影响的待送达通知
      - 无法核实   → 按离线回调原样告警，注明未能核实
    """
    try:
        from insurance.flowbot_notify import get_robot_info
        from core.notify import notify_error

        info = get_robot_info(robot_id)
        is_online = info.get("isOnline") if info is not None else None

        pending = _query_pending_pushes(robot_id)
        pending_desc = "；".join(
            f"群【{p.get('group_name') or '未知'}】{str(p.get('message') or '')[:60]}"
            for p in pending) or "无"

        if is_online == 1:
            logger.warning("FlowBot离线回调核实为在线（疑似发送失败） robot=%s 待送达%d条",
                           robot_id, len(pending))
            notify_error(
                "FlowBot群通知", "send_fail",
                "收到离线回调，但核实机器人当前在线，实际为消息发送失败（非离线）",
                f"robotId={robot_id} | 待送达通知{len(pending)}条: {pending_desc}")
        elif is_online == 0:
            last_login = (info or {}).get("lastLogin_at") or "未知"
            logger.warning("FlowBot机器人已离线（已核实） robot=%s 最后登录=%s",
                           robot_id, last_login)
            notify_error(
                "FlowBot机器人", "offline",
                "机器人已离线（getRobotInfo 已核实），群通知将无法送达，"
                "请尽快检查手机/登录状态",
                f"robotId={robot_id} | 最后登录: {last_login} | "
                f"待送达通知{len(pending)}条: {pending_desc}")
        else:
            logger.warning("FlowBot离线回调无法核实在线状态 robot=%s", robot_id)
            notify_error(
                "FlowBot机器人", "offline",
                "收到机器人离线回调（getRobotInfo 核实失败，实际状态未知），"
                "群通知可能无法送达",
                f"robotId={robot_id} | 待送达通知{len(pending)}条: {pending_desc}")
    except Exception as e:
        logger.warning("FlowBot离线回调处理异常 robot=%s: %s", robot_id, e, exc_info=True)


def _handle_task_result(task_id: str, status: str, message: str, robot_id: str):
    """后台处理指令执行结果：成功标记 / 失败重发（最多3次）/ 最终失败告警。"""
    if not _db:
        return
    try:
        import pymysql.cursors
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            if status == "success":
                cursor.execute(
                    "UPDATE flowbot_push_log SET status = 1 WHERE task_id = %s", (task_id,))
                conn.commit()
                logger.info("FlowBot推送成功 taskId=%s", task_id)
                return

            # 失败：查重发额度与原始 payload
            cursor.execute(
                "SELECT id, robot_id, group_name, message, payload, retry_count "
                "FROM flowbot_push_log WHERE task_id = %s LIMIT 1", (task_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning("FlowBot推送失败但无落库记录 taskId=%s msg=%s", task_id, message)
                return

            if row["retry_count"] < _MAX_RETRY and row.get("payload"):
                retry_count = row["retry_count"] + 1
                from insurance.flowbot_notify import post_task
                task_list = json.loads(row["payload"])
                result = post_task(task_list, row["robot_id"] or robot_id)
                new_task_id = ""
                if result.get("code") == 200:
                    ids = [t.get("taskId", "") for t in (result.get("taskList") or [])]
                    new_task_id = next(filter(None, ids), "")
                if new_task_id:
                    cursor.execute(
                        "UPDATE flowbot_push_log SET task_id = %s, retry_count = %s, "
                        "status = 0, error_msg = %s WHERE id = %s",
                        (new_task_id, retry_count, (message or "")[:500], row["id"]))
                    conn.commit()
                    logger.info("FlowBot推送失败已重发(第%d/%d次) old=%s new=%s",
                                retry_count, _MAX_RETRY, task_id, new_task_id)
                    return
                # 重发请求本身失败 → 直接记一次重发并落最终失败
                cursor.execute(
                    "UPDATE flowbot_push_log SET status = 2, retry_count = %s, "
                    "error_msg = %s WHERE id = %s",
                    (retry_count, f"重发请求失败: {result.get('message', '')}"[:500], row["id"]))
                conn.commit()
                _notify_final_fail(row, retry_count, message)
                return

            # 重发额度用尽 → 最终失败
            cursor.execute(
                "UPDATE flowbot_push_log SET status = 2, error_msg = %s WHERE id = %s",
                ((message or "")[:500], row["id"]))
            conn.commit()
            _notify_final_fail(row, row["retry_count"], message)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("FlowBot回调结果处理异常 taskId=%s: %s", task_id, e, exc_info=True)


def _notify_final_fail(row: dict, retry_used: int, message: str):
    """推送最终失败 → 钉钉告警技术侧跟进。"""
    logger.warning("FlowBot推送最终失败 群=%s 已重试%d次 原因=%s",
                   row.get("group_name", ""), retry_used, message)
    try:
        from core.notify import notify_error
        notify_error(
            "FlowBot群通知", "push_final_fail",
            f"推送最终失败（已重试{retry_used}次）: {message or '未知原因'}",
            f"群={row.get('group_name', '')} | 消息={str(row.get('message', ''))[:100]}")
    except Exception:
        pass

"""
FlowBot 机器人回调接收端（参考 wuhuApi 实现，文档 https://flowbot.apifox.cn/doc-5369842）。

注册方式：POST https://flowbot.feiliu.run/api/updateCallBackUrl?robotId=<robotId>
          body: {"callBackUrl": "https://wxbot.wuhuxiche.com/flowbot/callback"}

回调类型（mode）：
    init     初始化回调配置
    logs     收到聊天记录（量大，只记 debug 日志）
    receipt  指令已接收（v2.5.5+）
    callBack 指令执行回调（真实送达结果：data.taskId / data.status=success|fail / data.message）
    online   机器人上线（清掉线状态；此前发过离线告警则补「已恢复」通知）
    offline  机器人离线（掉线满5分钟且 getRobotInfo 核实仍离线才告警，避免抖动误报）

约束：必须 3 秒内返回 {"code": 200}，耗时处理（失败重发）放后台线程。

callBack 失败处理：按 taskId 查 flowbot_push_log，重发次数 < 3 时用原 payload
重发（更新为新 taskId、状态回到推送中）；重发额度用尽则标记最终失败 + 钉钉告警。
"""

import json
import logging
import threading
import time

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
            # 飞流有时秒级抖动/发送失败也会推 offline 回调，不能直接断言已离线；
            # 放后台线程等待满阈值后调 getRobotInfo 核实再告警（回调须3秒内应答）
            logger.warning("FlowBot回调: 收到离线通知 robot=%s（%d秒后核实）",
                           robot_id, _OFFLINE_ALERT_DELAY)
            threading.Thread(
                target=_handle_offline, args=(robot_id,), daemon=True,
            ).start()
        elif mode == "online":
            logger.info("FlowBot回调: 机器人上线 robot=%s", robot_id)
            threading.Thread(
                target=_handle_online, args=(robot_id,), daemon=True,
            ).start()
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


# 掉线持续超过该秒数才发离线告警（短暂抖动只记日志，避免来回通知）
_OFFLINE_ALERT_DELAY = 300


def _offline_state_key(robot_id: str) -> str:
    return f"flowbot_offline_state_{robot_id}"


def _get_offline_state(robot_id: str):
    """读掉线状态（存 insurance_config，跨 gunicorn worker 共享）。无状态返回 None。"""
    if not _db:
        return None
    try:
        from insurance.db import get_insurance_config
        state = get_insurance_config(_db, _offline_state_key(robot_id))
        return state if isinstance(state, dict) else None
    except Exception as e:
        logger.warning("读取FlowBot掉线状态失败 robot=%s: %s", robot_id, e)
        return None


def _set_offline_state(robot_id: str, state):
    """写掉线状态；传 None 表示清除（机器人已恢复在线）。"""
    if not _db:
        return
    try:
        from insurance.db import set_insurance_config
        set_insurance_config(_db, _offline_state_key(robot_id), state)
    except Exception as e:
        logger.warning("写入FlowBot掉线状态失败 robot=%s: %s", robot_id, e)


def _handle_offline(robot_id: str):
    """
    离线回调后台处理：记录掉线时间，等满 _OFFLINE_ALERT_DELAY 后核实再告警。

      - 等待期间收到 online 回调（状态被清除）→ 短暂掉线，只记日志不告警
      - 等满后 getRobotInfo 核实仍离线（或核实失败）→ 离线告警（附最后登录
        时间 + 待送达通知清单），并标记 alerted 供恢复时发「已恢复」通知
      - 等满后核实为在线（online 回调丢失）→ 清状态；有滞留通知才按发送失败告警
    """
    try:
        now = time.time()
        state = _get_offline_state(robot_id)
        if state:
            # 重复 offline 回调：沿用最早掉线时间，剩余等待时间相应缩短
            offline_at = float(state.get("offline_at") or now)
        else:
            offline_at = now
            _set_offline_state(robot_id, {"offline_at": now, "alerted": False})

        time.sleep(max(0, _OFFLINE_ALERT_DELAY - (now - offline_at)))

        state = _get_offline_state(robot_id)
        if not state:
            logger.info("FlowBot掉线%d秒内已恢复在线，不告警 robot=%s",
                        _OFFLINE_ALERT_DELAY, robot_id)
            return
        if state.get("alerted"):
            return  # 已由其他线程/worker 告警过

        from insurance.flowbot_notify import get_robot_info
        from core.notify import notify_error

        info = get_robot_info(robot_id)
        is_online = info.get("isOnline") if info is not None else None
        pending = _query_pending_pushes(robot_id)
        pending_desc = "；".join(
            f"群【{p.get('group_name') or '未知'}】{str(p.get('message') or '')[:60]}"
            for p in pending) or "无"

        if is_online == 1:
            # 实际在线但没收到 online 回调：清状态；有滞留通知说明发送失败
            _set_offline_state(robot_id, None)
            if pending:
                logger.warning("FlowBot核实在线但有滞留通知（疑似发送失败） robot=%s %d条",
                               robot_id, len(pending))
                notify_error(
                    "FlowBot群通知", "send_fail",
                    "机器人在线，但有群通知超过5分钟未确认送达，疑似发送失败",
                    f"robotId={robot_id} | 待送达通知{len(pending)}条: {pending_desc}")
            else:
                logger.info("FlowBot离线回调核实为在线且无滞留通知，不告警 robot=%s",
                            robot_id)
            return

        # 仍离线（is_online==0）或核实失败（None）→ 离线告警
        _set_offline_state(robot_id, {"offline_at": offline_at, "alerted": True})
        minutes = int((time.time() - offline_at) / 60)
        verify_desc = ("getRobotInfo 已核实" if is_online == 0
                       else "getRobotInfo 核实失败，按离线回调处理")
        last_login = (info or {}).get("lastLogin_at") or "未知"
        logger.warning("FlowBot机器人离线超过%d分钟（%s） robot=%s",
                       minutes, verify_desc, robot_id)
        notify_error(
            "FlowBot机器人", "offline",
            f"机器人已离线超过{minutes}分钟（{verify_desc}），群通知将无法送达，"
            "请尽快检查手机/登录状态",
            f"robotId={robot_id} | 最后登录: {last_login} | "
            f"待送达通知{len(pending)}条: {pending_desc}")
    except Exception as e:
        logger.warning("FlowBot离线回调处理异常 robot=%s: %s", robot_id, e, exc_info=True)


def _handle_online(robot_id: str):
    """
    上线回调后台处理：清除掉线状态；若此前已发过离线告警，补发「已恢复」通知闭环。
    短暂掉线（未达告警阈值）只记日志。
    """
    try:
        state = _get_offline_state(robot_id)
        if not state:
            return
        _set_offline_state(robot_id, None)

        offline_at = float(state.get("offline_at") or 0)
        duration = int(time.time() - offline_at) if offline_at else 0
        dur_desc = (f"{duration // 60}分{duration % 60}秒"
                    if duration >= 60 else f"{duration}秒")

        if state.get("alerted"):
            logger.info("FlowBot机器人恢复在线（本次离线%s） robot=%s", dur_desc, robot_id)
            from core.notify import notify_recovery
            notify_recovery(
                "FlowBot机器人",
                f"机器人已恢复在线，本次离线约{dur_desc}，群通知恢复正常发送",
                f"robotId={robot_id}")
        else:
            logger.info("FlowBot机器人短暂掉线%s后恢复（未达告警阈值，不通知） robot=%s",
                        dur_desc, robot_id)
    except Exception as e:
        logger.warning("FlowBot上线回调处理异常 robot=%s: %s", robot_id, e, exc_info=True)


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
                "SELECT id, robot_id, source, group_name, message, payload, retry_count "
                "FROM flowbot_push_log WHERE task_id = %s LIMIT 1", (task_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning("FlowBot推送失败但无落库记录 taskId=%s msg=%s", task_id, message)
                return

            # 监控页手动发送的消息：失败只标记状态（页面显示❗+重发按钮），
            # 不自动重发、不钉钉告警，重发由用户手动触发
            if row.get("source") == "monitor":
                cursor.execute(
                    "UPDATE flowbot_push_log SET status = 2, error_msg = %s WHERE id = %s",
                    ((message or "发送失败")[:500], row["id"]))
                conn.commit()
                logger.info("FlowBot监控页消息发送失败（等待用户手动重发） taskId=%s 群=%s: %s",
                            task_id, row.get("group_name", ""), message)
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

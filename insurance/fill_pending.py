# insurance/fill_pending.py
"""
群内补充信息「待匹配池」。

背景：客户在群里发补充信息（车牌+业务经理/业务专员等）时，同车牌保单
可能还在识别队列里（PDF 识别较慢），按车牌命中 0 条就立即提示「未录入」
会误报。改为：
  1. 命中 0 条 → 入池（policy_fill_pending 表，持久化，进程重启不丢）
  2. 延迟 N 分钟（默认10）后仍未匹配 → 才发「未录入」群提醒 + 钉钉通知
     （发完条目留池继续等匹配）
  3. 保单识别成功时按 roomid+车牌 反查池子 → 自动把补充字段写入该记录
     并标记 matched；若已发过「未录入」提醒，补发「补录成功」群通知闭环
  4. 过期（默认48小时）未匹配 → 作废，不再参与匹配

多保单 PDF 兼容：matched 后 10 分钟宽限期内，同车牌新记录（交强+商业
两条记录）仍复用该条目的 fill 回填，但通知只发一次（靠状态抢占保证）。

多 worker 安全：状态流转用「带条件 UPDATE 抢占」保证同一条目只被一个
worker 通知/匹配一次；时间比较全部用 DB NOW()，不依赖各机器本地时钟。

所有入口旁路化：异常只记日志，绝不影响消息拉取与保单识别主流程。

配置（insurance_config 表 quote_fill_miss_notify 键，沿用现有）：
  enabled            「未录入」提醒开关（默认关；不影响入池与自动匹配回填）
  robot_id           FlowBot 机器人 ID，缺省复用 flowbot_fail_notify
  tech_support_name  客服微信名，缺省"技术客服（午虎保单台账系统）"
  template           未录入提醒文案（{plate} 占位）
  delay_minutes      延迟提醒分钟数，默认 10
  pool_expire_hours  池子有效期小时数，默认 48
  template_matched   补录成功文案（{plate} 占位）
"""

import json
import logging
import re
import threading

import pymysql

logger = logging.getLogger(__name__)

# 扫描间隔（秒）与 matched 后宽限期（分钟，多保单PDF后续记录复用回填）
_SCAN_INTERVAL = 30
_MATCHED_GRACE_MINUTES = 10

# 默认配置
_DEFAULT_DELAY_MINUTES = 10
_DEFAULT_EXPIRE_HOURS = 48
_DEFAULT_TECH_NAME = "技术客服（午虎保单台账系统）"

# 「未录入」默认提醒文案（{plate} 动态替换车牌），原 core/fetcher.py 迁入
MISS_TEMPLATE = (
    "⚠️ 补充信息未录入\n"
    "车牌：{plate}\n"
    "系统暂未找到该车牌的保单识别记录，本次补充的信息（业务经理、业务专员、保险公司经办人等）未能生效。\n"
    "请检查：\n"
    "1️⃣ 该车牌的保单是否已发到本群并识别成功\n"
    "2️⃣ 车牌号是否输入有误\n"
    "确认保单已识别后，请重新发送一次补充信息即可。\n"
    "若车牌无误但仍提示未录入，请联系客服处理（已@客服跟进）。"
)

# 「补录成功」默认文案（仅在已发过「未录入」提醒后匹配上才发，闭环客户疑问）
MATCHED_TEMPLATE = (
    "✅ 补充信息已录入\n"
    "车牌：{plate} 的保单已识别完成，您此前发送的补充信息已自动录入，无需重复发送。"
)

_scan_thread_started = False


def init_fill_pending(db):
    """建表并启动扫描线程（main.py 启动时调用一次）。"""
    _ensure_table(db)
    _start_scan_thread(db)


def _ensure_table(db):
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS policy_fill_pending (
                id INT AUTO_INCREMENT PRIMARY KEY,
                roomid VARCHAR(128) NOT NULL COMMENT '群id',
                room_name VARCHAR(255) DEFAULT '' COMMENT '群名（入池时解析，FlowBot按群名发送）',
                sender VARCHAR(128) DEFAULT '' COMMENT '发送人userid',
                sender_name VARCHAR(255) DEFAULT '' COMMENT '发送人名称（@用，空=不@）',
                enterprise_id INT DEFAULT NULL COMMENT '群所属企业',
                plate VARCHAR(16) NOT NULL COMMENT '归一化车牌（去-/空格，大写）',
                fill_fields TEXT COMMENT '补充字段JSON',
                raw_text TEXT COMMENT '原始补充文本',
                status VARCHAR(16) DEFAULT 'pending' COMMENT 'pending/notified/matched/expired',
                notify_at DATETIME NOT NULL COMMENT '延迟提醒时间点',
                expire_at DATETIME NOT NULL COMMENT '过期时间点',
                matched_record_id INT DEFAULT NULL COMMENT '匹配到的识别记录id',
                matched_at DATETIME DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_room_plate_status (roomid, plate, status),
                INDEX idx_status_notify (status, notify_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群内补充信息待匹配池'
        """)
        conn.commit()
    finally:
        conn.close()


def _get_cfg(db) -> dict:
    """读取 quote_fill_miss_notify 配置，robot_id 缺省复用 flowbot_fail_notify。"""
    from insurance.db import get_insurance_config
    cfg = get_insurance_config(db, "quote_fill_miss_notify", {}) or {}
    if not cfg.get("robot_id"):
        fail_cfg = get_insurance_config(db, "flowbot_fail_notify", {}) or {}
        cfg["robot_id"] = fail_cfg.get("robot_id")
    return cfg


def _norm_plate(plate: str) -> str:
    """车牌归一化，与 insurance.db.find_records_by_plate 同规则。"""
    return re.sub(r"[-－\s　·.]", "", plate or "").upper()


# ------------------------------------------------------------------ #
# 入池
# ------------------------------------------------------------------ #

def enqueue_pending(db, roomid: str, room_name: str, sender: str, sender_name: str,
                    plate: str, fill: dict, raw_text: str = "",
                    enterprise_id: int = None) -> bool:
    """
    补充信息命中 0 条识别记录时入池。

    同一群+同车牌+同发送人已有未决（pending/notified）条目时合并更新
    字段（新值覆盖同名旧值），不重复入池：
      - pending：不刷新 notify_at（重复发送不无限顺延提醒）
      - notified：保持 notified，不再二次提醒
    返回是否成功（失败只记日志）。
    """
    plate = _norm_plate(plate)
    if not roomid or not plate:
        return False
    fill = {k: v for k, v in (fill or {}).items()
            if k != "plate" and v and str(v).strip()}
    if not fill:
        return False

    cfg = _get_cfg(db)

    def _int_cfg(key, default):
        # 注意 0 是合法值（如 delay_minutes=0 立即提醒），不能用 or 取默认
        val = cfg.get(key)
        try:
            return default if val in (None, "") else int(val)
        except (TypeError, ValueError):
            return default

    delay_min = _int_cfg("delay_minutes", _DEFAULT_DELAY_MINUTES)
    expire_hours = _int_cfg("pool_expire_hours", _DEFAULT_EXPIRE_HOURS)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, fill_fields FROM policy_fill_pending "
            "WHERE roomid = %s AND plate = %s AND sender = %s "
            "AND status IN ('pending', 'notified') "
            "ORDER BY id DESC LIMIT 1",
            (roomid, plate, sender),
        )
        row = cursor.fetchone()
        if row:
            # 合并已有未决条目
            old_fill = {}
            try:
                old_fill = json.loads(row["fill_fields"] or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            old_fill.update(fill)
            cursor.execute(
                "UPDATE policy_fill_pending SET fill_fields = %s, raw_text = %s, "
                "room_name = %s, sender_name = %s WHERE id = %s",
                (json.dumps(old_fill, ensure_ascii=False), raw_text or "",
                 room_name or "", sender_name or "", row["id"]),
            )
            conn.commit()
            logger.info("待匹配池合并更新: id=%d plate=%s fill=%s", row["id"], plate, fill)
            return True

        cursor.execute(
            "INSERT INTO policy_fill_pending "
            "(roomid, room_name, sender, sender_name, enterprise_id, plate, "
            " fill_fields, raw_text, status, notify_at, expire_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', "
            "        DATE_ADD(NOW(), INTERVAL %s MINUTE), "
            "        DATE_ADD(NOW(), INTERVAL %s HOUR))",
            (roomid, room_name or "", sender or "", sender_name or "",
             enterprise_id, plate, json.dumps(fill, ensure_ascii=False),
             raw_text or "", delay_min, expire_hours),
        )
        conn.commit()
        logger.info("补充信息入待匹配池: plate=%s room=%s sender=%s fill=%s "
                    "(%d分钟后未匹配将提醒)", plate, roomid, sender, fill, delay_min)
        return True
    except Exception as e:
        logger.error("补充信息入池失败 plate=%s room=%s: %s", plate, roomid, e, exc_info=True)
        return False
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 保单识别成功 → 匹配回填
# ------------------------------------------------------------------ #

def match_and_apply(db, record_id: int, roomid: str, parsed_fields: dict) -> bool:
    """
    保单识别成功后调用：按 roomid+车牌 查池，把补充字段写入 parsed_fields（原地）。

    在 handler 字段映射/钉钉同步之前调用，回填结果随记录一起落库并同步。
    匹配范围：pending/notified，以及 matched 后 10 分钟宽限期内的条目
    （多保单 PDF 的交强/商业两条记录都要回填，通知只发一次）。
    首次抢占到 notified 条目时补发「补录成功」群通知。
    返回是否有字段被回填。
    """
    if not roomid or not isinstance(parsed_fields, dict):
        return False
    plate = _norm_plate(parsed_fields.get("车牌号", ""))
    if not plate:
        return False

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, status, sender_name, room_name, fill_fields "
            "FROM policy_fill_pending "
            "WHERE roomid = %s AND plate = %s AND ("
            "  (status IN ('pending', 'notified') AND expire_at > NOW()) "
            "  OR (status = 'matched' AND matched_at > DATE_SUB(NOW(), INTERVAL %s MINUTE))"
            ") ORDER BY id",
            (roomid, plate, _MATCHED_GRACE_MINUTES),
        )
        rows = cursor.fetchall()
        if not rows:
            return False

        applied = False
        for row in rows:
            try:
                fill = json.loads(row["fill_fields"] or "{}")
            except (TypeError, json.JSONDecodeError):
                fill = {}
            if not fill:
                continue

            was_notified = False
            if row["status"] in ("pending", "notified"):
                # 抢占标记 matched（多 worker/多记录下只有一个抢到，负责发成功通知）
                cursor.execute(
                    "UPDATE policy_fill_pending SET status = 'matched', "
                    "matched_record_id = %s, matched_at = NOW() "
                    "WHERE id = %s AND status = %s",
                    (record_id, row["id"], row["status"]),
                )
                claimed = cursor.rowcount == 1
                conn.commit()
                if claimed:
                    was_notified = row["status"] == "notified"
                # 没抢到（并发已被别的记录标记）也照常回填字段

            parsed_fields.update(fill)
            applied = True
            logger.info("待匹配池回填成功: pending_id=%d record_id=%d plate=%s fill=%s",
                        row["id"], record_id, plate, fill)

            # 已发过「未录入」提醒的条目匹配上 → 补发「补录成功」通知闭环
            if was_notified:
                _send_matched_notify(db, row, plate)
        return applied
    except Exception as e:
        logger.error("待匹配池回填异常 record_id=%s plate=%s: %s",
                     record_id, plate, e, exc_info=True)
        return False
    finally:
        conn.close()


def _send_matched_notify(db, row: dict, plate: str):
    """已提醒过「未录入」的条目匹配成功后，FlowBot 群通知 @发送人。旁路化。"""
    try:
        cfg = _get_cfg(db)
        robot_id = cfg.get("robot_id")
        room_name = row.get("room_name") or ""
        if not robot_id or not room_name:
            logger.info("补录成功通知跳过（缺 robot_id 或群名）: pending_id=%s", row.get("id"))
            return
        template = cfg.get("template_matched") or MATCHED_TEMPLATE
        msg = template.replace("{plate}", plate or "未知")
        at_list = [n for n in (row.get("sender_name"),) if n]
        from insurance.flowbot_notify import send_flowbot_group_message
        send_flowbot_group_message(room_name, at_list, msg, robot_id)
        logger.info("补录成功通知已发送: pending_id=%s plate=%s room=%s",
                    row.get("id"), plate, room_name)
    except Exception as e:
        logger.warning("补录成功通知发送失败（不影响主流程）pending_id=%s: %s",
                       row.get("id"), e)


# ------------------------------------------------------------------ #
# 扫描线程：延迟提醒 + 过期作废
# ------------------------------------------------------------------ #

def _start_scan_thread(db):
    global _scan_thread_started
    if _scan_thread_started:
        return
    _scan_thread_started = True
    threading.Thread(target=_scan_loop, args=(db,), daemon=True,
                     name="fill-pending-scan").start()
    logger.info("待匹配池扫描线程已启动（%ds/次）", _SCAN_INTERVAL)


def _scan_loop(db):
    import time
    while True:
        time.sleep(_SCAN_INTERVAL)
        try:
            _scan_once(db)
        except Exception as e:
            logger.warning("待匹配池扫描异常: %s", e, exc_info=True)


def _scan_once(db):
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 1. 过期作废（幂等，多 worker 重复执行无害）
        cursor.execute(
            "UPDATE policy_fill_pending SET status = 'expired' "
            "WHERE status IN ('pending', 'notified') AND expire_at <= NOW()"
        )
        expired_cnt = cursor.rowcount
        conn.commit()
        if expired_cnt:
            logger.info("待匹配池过期作废 %d 条", expired_cnt)

        # 2. 到点的延迟提醒；开关关闭时条目留 pending 只做匹配不提醒
        cfg = _get_cfg(db)
        if not cfg.get("enabled"):
            return
        cursor.execute(
            "SELECT id, roomid, room_name, sender_name, plate FROM policy_fill_pending "
            "WHERE status = 'pending' AND notify_at <= NOW() ORDER BY id LIMIT 50"
        )
        rows = cursor.fetchall()
        for row in rows:
            # 逐条抢占（多 worker 下只有一个 worker 抢到并发送）
            cursor.execute(
                "UPDATE policy_fill_pending SET status = 'notified' "
                "WHERE id = %s AND status = 'pending'",
                (row["id"],),
            )
            claimed = cursor.rowcount == 1
            conn.commit()
            if not claimed:
                continue
            _send_miss_notify(db, cfg, row)
    finally:
        conn.close()


def _send_miss_notify(db, cfg: dict, row: dict):
    """发「未录入」群提醒（FlowBot @发送人+@技术客服）+ 钉钉通知。旁路化。"""
    plate = row.get("plate") or "未知"
    room_name = row.get("room_name") or ""
    sender_name = row.get("sender_name") or ""

    # FlowBot 群内提醒
    try:
        robot_id = cfg.get("robot_id")
        if robot_id and room_name:
            tech_name = cfg.get("tech_support_name") or _DEFAULT_TECH_NAME
            template = cfg.get("template") or MISS_TEMPLATE
            msg = template.replace("{plate}", plate)
            # 去重：发送人本身就是技术客服账号时只@一次
            at_list = list(dict.fromkeys(n for n in (sender_name, tech_name) if n))
            from insurance.flowbot_notify import send_flowbot_group_message
            send_flowbot_group_message(room_name, at_list, msg, robot_id)
            logger.info("延迟未录入提醒已发送: pending_id=%s plate=%s room=%s",
                        row.get("id"), plate, room_name)
        else:
            logger.info("延迟未录入提醒跳过群消息（缺 robot_id 或群名）: pending_id=%s",
                        row.get("id"))
    except Exception as e:
        logger.warning("延迟未录入提醒发送失败（不影响主流程）pending_id=%s: %s",
                       row.get("id"), e)

    # 钉钉同步通知技术客服（与群提醒互相独立，各自兜底）
    try:
        from core.notify import notify_fill_pending_miss
        notify_fill_pending_miss(room_name or row.get("roomid", ""),
                                 sender_name, plate)
    except Exception as e:
        logger.warning("未录入钉钉通知发送失败 pending_id=%s: %s", row.get("id"), e)

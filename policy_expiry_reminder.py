# -*- coding: utf-8 -*-
"""
保单到期提醒——每日巡检 insurance_policy_fields.end_date_iso 在未来30天内(含今天)的保单，
按上传群汇总成一条消息，通过 FlowBot 发到群里并 @ 相应上传人。

独立进程，由 crontab 每天调度一次执行；不要挂进 Flask app（gunicorn -w 2 会导致
同一份提醒被两个 worker 各发一遍）。
"""
import logging
import sys
from datetime import date

sys.path.insert(0, "/home/wxbot")

import pymysql  # noqa: E402

from main import load_config  # noqa: E402
from storage.db import Database  # noqa: E402
from core.contacts import ContactsManager  # noqa: E402
from insurance.db import get_insurance_config  # noqa: E402
from insurance.flowbot_notify import send_flowbot_group_message  # noqa: E402
from auth.jwt_utils import generate_token, init_jwt  # noqa: E402
from auth.db import get_user_by_id  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/wxbot/logs/policy_expiry_reminder.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

REMIND_WINDOW_DAYS = 30
BASE_URL = "https://wxbot.wuhuxiche.com"


def ensure_reminder_log_table(db):
    """建"已提醒"记录表(幂等)。按 (record_id, remind_date) 唯一，防止同一保单同一天被重复提醒。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_reminder_log (
                id          BIGINT PRIMARY KEY AUTO_INCREMENT,
                record_id   INT NOT NULL COMMENT '关联 insurance_records.id',
                remind_date DATE NOT NULL COMMENT '提醒日期',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_record_date (record_id, remind_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='保单到期提醒发送记录(按天去重，30天窗口内每天提醒一次)'
        """)
        conn.commit()
    finally:
        conn.close()


def fetch_expiring_records(db):
    """
    查逾期 REMIND_WINDOW_DAYS 天内 ~ 未来 REMIND_WINDOW_DAYS 天内到期、今天还没提醒过的保单，
    并排除 renewal_tasks 中已闭环(续保/停保/暂不考虑)的任务对应保单（只保留 pending 或尚无任务的）。
    同一 policy_no 只保留 record_id 最大(最新，覆盖"重新识别"产生的旧记录)的一条；
    空 policy_no 各自独立，不参与去重（与台账去重口径一致）。
    只推送 users.renewal_enabled=1 的账号跟单(insurance_records.user_id)的保单——续保提醒
    功能改为客户按个人账号自助开启（不是按企业），跟单人自己没点"开启功能"之前，他跟单的
    保单不推送到期提醒；同企业下其他已开启的员工不受影响（2026-07-15 从企业级改为账号级）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT r.id AS record_id, r.roomid, r.sender, r.sender_name, r.user_id,
                   pf.plate_no, pf.policy_no, pf.policy_type, pf.end_date_iso
            FROM insurance_records r
            JOIN insurance_policy_fields pf ON r.id = pf.record_id
            JOIN users u ON u.id = r.user_id
            LEFT JOIN renewal_tasks rt
                ON (rt.customer_key = pf.plate_no OR rt.customer_key = UPPER(pf.vin))
               AND rt.policy_type = pf.policy_type
               AND rt.enterprise_id = r.enterprise_id
            WHERE r.deleted_at IS NULL
              AND r.status = 'done'
              AND r.doc_category = '保单'
              AND r.roomid IS NOT NULL AND r.roomid != ''
              AND u.renewal_enabled = 1
              AND pf.end_date_iso IS NOT NULL
              AND pf.end_date_iso BETWEEN DATE_SUB(CURDATE(), INTERVAL %s DAY) AND DATE_ADD(CURDATE(), INTERVAL %s DAY)
              AND (rt.id IS NULL OR rt.status = 'pending')
              AND NOT EXISTS (
                  SELECT 1 FROM insurance_reminder_log rl
                  WHERE rl.record_id = r.id AND rl.remind_date = CURDATE()
              )
            ORDER BY r.id DESC
        """, (REMIND_WINDOW_DAYS, REMIND_WINDOW_DAYS))
        rows = cursor.fetchall()
    finally:
        conn.close()

    # 去重键 = (policy_no, plate_no)，不能只用 policy_no：
    # 驾乘意外险等"批量保单"是一个 policy_no 覆盖多辆车各一份，同 policy_no 不同车牌
    # 是各自独立的保单，必须分别提醒；只有 policy_no+plate_no 都相同才是"重新识别"产生的
    # 同一份保单的重复记录，才应该只保留最新一条。
    seen = {}
    for r in rows:
        pno = r.get("policy_no") or ""
        plate = r.get("plate_no") or ""
        key = (pno, plate) if pno else f"__norno_{r['record_id']}"
        if key not in seen or r["record_id"] > seen[key]["record_id"]:
            seen[key] = r
    return list(seen.values())


def mark_reminded(db, record_ids):
    if not record_ids:
        return
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT IGNORE INTO insurance_reminder_log (record_id, remind_date) VALUES (%s, CURDATE())",
            [(rid,) for rid in record_ids],
        )
        conn.commit()
    finally:
        conn.close()


def _build_contacts_instances(cfg, db):
    """
    构造「主企业 + 全部额外企业」的 ContactsManager 实例列表，与 main.py 里
    Flask app 启动时的路由方式保持一致。群/保单可能属于任意一个企业的监控群，
    只用主企业通讯录会导致跨企业的群/发送人解析失败（退化成原始ID）。
    """
    main_contacts = ContactsManager(
        corpid=cfg["wecom"]["corpid"],
        secret=cfg["wecom"]["secret"],
        db=db,
        external_secret=cfg["wecom"].get("external_secret", ""),
        enterprise_name="车物家",
    )
    instances = [main_contacts]
    for cb in cfg.get("extra_callbacks", []):
        cb_corpid = cb.get("corpid", cfg["wecom"]["corpid"])
        cb_secret = cb.get("secret", "")
        if not cb_secret or cb_corpid == cfg["wecom"]["corpid"]:
            continue
        extra_contacts = ContactsManager(
            corpid=cb_corpid,
            secret=cb_secret,
            db=db,
            external_secret=cb.get("external_secret", ""),
            enterprise_name=cb.get("name", cb_corpid),
        )
        extra_contacts.set_cache_prefix(cb_corpid)
        extra_contacts.set_fallback_contacts(main_contacts)
        instances.append(extra_contacts)
    return instances


def _resolve_room_name(instances, roomid):
    """依次尝试每个企业的通讯录解析群名，取第一个成功且不等于roomid本身的结果。"""
    for c in instances:
        try:
            name = c.get_room_name(roomid) or ""
        except Exception as e:
            logger.warning("解析群名失败(企业=%s) roomid=%s: %s", c.enterprise_name, roomid, e)
            continue
        # get_room_name 解析失败时会把 roomid 原样返回兜底，这种"退化值"不能当真群名用
        if name and name != roomid:
            return name
    return ""


def _resolve_sender_name(instances, sender, stored_name):
    """优先用落库快照昵称；快照为空或等于原始ID(未解析成功)时依次尝试各企业通讯录兜底。"""
    if stored_name and stored_name != sender:
        return stored_name
    for c in instances:
        try:
            resolved = c.get_name(sender) or ""
        except Exception as e:
            logger.warning("实时解析发送人昵称失败(企业=%s) sender=%s: %s", c.enterprise_name, sender, e)
            continue
        if resolved and resolved != sender:
            return resolved
    return stored_name or sender


def main():
    cfg = load_config("/home/wxbot/config.yaml")
    db = Database(cfg["mysql"], main_corpid=cfg["wecom"]["corpid"])
    jwt_secret = cfg.get("jwt_secret", "") or get_insurance_config(db, "jwt_secret", "")
    init_jwt(jwt_secret)
    contacts_instances = _build_contacts_instances(cfg, db)

    ensure_reminder_log_table(db)

    fb_cfg = get_insurance_config(db, "flowbot_fail_notify", {}) or {}
    robot_id = fb_cfg.get("robot_id")
    if not robot_id:
        logger.warning("未配置 flowbot robot_id，跳过本次到期提醒")
        return

    records = fetch_expiring_records(db)
    if not records:
        logger.info("今日无需提醒的到期保单")
        return

    today = date.today()
    by_room = {}
    for r in records:
        by_room.setdefault(r["roomid"], []).append(r)

    total_sent_record_ids = []
    for roomid, items in by_room.items():
        room_name = _resolve_room_name(contacts_instances, roomid)
        if not room_name:
            logger.warning("群名解析为空，跳过 roomid=%s（%d条待提醒）", roomid, len(items))
            continue

        # 按发送人分组，组内按剩余天数升序（越紧急越靠前）
        by_sender = {}
        for r in items:
            by_sender.setdefault(r["sender"], []).append(r)

        lines = ["【今日续保提醒】以下保单需尽快跟进续保：", ""]
        at_names = []
        for sender, plist in by_sender.items():
            plist.sort(key=lambda x: x["end_date_iso"])
            sender_name = _resolve_sender_name(contacts_instances, sender, plist[0].get("sender_name") or "")
            at_names.append(sender_name)
            lines.append(f"@{sender_name}")
            # 按逾期🔴 / 即将到期🟠 分组展示
            overdue_lines, soon_lines = [], []
            for r in plist:
                days_left = (r["end_date_iso"] - today).days
                plate = r.get("plate_no") or "未知车牌"
                ptype = r.get("policy_type") or "保单"
                end_str = r["end_date_iso"].strftime("%Y-%m-%d")
                if days_left < 0:
                    overdue_lines.append(f"· {plate} {ptype} —— 已逾期{-days_left}天({end_str})")
                else:
                    due_str = "今天到期" if days_left == 0 else f"还剩{days_left}天到期"
                    soon_lines.append(f"· {plate} {ptype} —— {due_str}({end_str})")
            if overdue_lines:
                lines.append("🔴 已逾期：")
                lines.extend(overdue_lines)
            if soon_lines:
                lines.append("🟠 即将到期：")
                lines.extend(soon_lines)
            # 免登录直达链接：带这个跟单人自己账号的登录token，点开直接进续保管理系统页面
            _uid = plist[0].get("user_id")
            if _uid:
                try:
                    _u = get_user_by_id(db, _uid)
                    if _u:
                        _tk = generate_token(_uid, _u.get("phone", ""), _u.get("role", ""))
                        lines.append(f"👉 点击查看续保详情：{BASE_URL}/insurance?tab=renewal&token={_tk}")
                except Exception as e:
                    logger.warning("生成续保免登录链接失败, sender=%s: %s", sender, e)
            lines.append("")

        message = "\n".join(lines).rstrip()
        ok = send_flowbot_group_message(room_name, at_names, message, robot_id)
        if ok:
            ids = [r["record_id"] for r in items]
            total_sent_record_ids.extend(ids)
            logger.info("到期提醒已发送 room=%s(%s) 涉及%d条保单 @%s",
                        room_name, roomid, len(items), at_names)
        else:
            logger.warning("到期提醒发送失败 room=%s(%s)，本轮不标记已提醒，明天会重试", room_name, roomid)

    mark_reminded(db, total_sent_record_ids)
    logger.info("本次到期提醒完成，共%d个群命中，成功发送%d个群，%d条保单",
                len(by_room), len({r["roomid"] for r in records if r["record_id"] in total_sent_record_ids}),
                len(total_sent_record_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("到期提醒任务异常退出")
        raise

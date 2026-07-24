# -*- coding: utf-8 -*-
"""
公户车盖章跟进——每日汇总提醒。

遍历开启了「公户车盖章跟进」的企业，统计其待跟进付费(seal_paid=0)的公户车保单，
按企业在企业管理页绑定的推送群(复用续保推送群绑定)各发一条汇总消息 + 免登录直达链接。

独立进程，由 crontab 每天 9:00 调度一次执行；不要挂进 Flask app（gunicorn -w 2 会导致
同一份汇总被两个 worker 各发一遍）。
"""
import logging
import sys

sys.path.insert(0, "/home/wxbot")

import pymysql  # noqa: E402

from main import load_config  # noqa: E402
from storage.db import Database  # noqa: E402
from core.contacts import ContactsManager  # noqa: E402
from insurance.db import get_insurance_config  # noqa: E402
from insurance.flowbot_notify import send_flowbot_group_message  # noqa: E402
from insurance import renewal_push_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/wxbot/logs/seal_daily_reminder.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

BASE_URL = "https://wxbot.wuhuxiche.com"

# 公户车判断：被保险人匹配公司特征（与 insurance/api.py 的 _CORP_INSURED_REGEXP 保持一致）
_CORP_INSURED_REGEXP = ("公司|有限|集团|物流|贸易|运输|租赁|车队|工厂|商行|中心|合作社|服务部|"
                        "经营部|事务所|门市|建筑|工程|科技|实业|电子|商贸|银行|医院|学校|幼儿园|合伙")


def _build_contacts_instances(cfg, db):
    """构造「主企业 + 全部额外企业」的 ContactsManager 实例列表，用于跨企业解析群名。"""
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
    """依次尝试每个企业的通讯录解析群名，取第一个成功且不等于 roomid 本身的结果。"""
    for c in instances:
        try:
            name = c.get_room_name(roomid) or ""
        except Exception as e:
            logger.warning("解析群名失败(企业=%s) roomid=%s: %s", c.enterprise_name, roomid, e)
            continue
        if name and name != roomid:
            return name
    return ""


def fetch_seal_enterprises(db):
    """开启了「公户车盖章跟进」且启用中的企业。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, name FROM enterprises WHERE seal_tracking_enabled = 1 AND enabled = 1"
        )
        return cursor.fetchall() or []
    finally:
        conn.close()


def fetch_unpaid_seal(db, eid):
    """某企业待跟进付费(seal_paid=0)的公户车保单（被保险人为公司）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            """
            SELECT r.id AS record_id, pf.plate_no, pf.insured, pf.company_short, pf.policy_type
            FROM insurance_records r
            JOIN insurance_policy_fields pf ON pf.record_id = r.id
            WHERE r.enterprise_id = %s
              AND r.deleted_at IS NULL
              AND r.status = 'done'
              AND (r.doc_category = '保单' OR r.doc_category = '' OR r.doc_category IS NULL)
              AND pf.insured REGEXP %s
              AND (r.seal_paid IS NULL OR r.seal_paid = 0)
            ORDER BY r.id DESC
            """,
            (eid, _CORP_INSURED_REGEXP),
        )
        return cursor.fetchall() or []
    finally:
        conn.close()


def main():
    cfg = load_config("/home/wxbot/config.yaml")
    db = Database(cfg["mysql"], main_corpid=cfg["wecom"]["corpid"])
    contacts_instances = _build_contacts_instances(cfg, db)

    fb_cfg = get_insurance_config(db, "flowbot_fail_notify", {}) or {}
    robot_id = fb_cfg.get("robot_id")
    if not robot_id:
        logger.warning("未配置 flowbot robot_id，跳过本次盖章汇总")
        return

    ents = fetch_seal_enterprises(db)
    if not ents:
        logger.info("无开启盖章跟进的企业")
        return

    for ent in ents:
        eid = ent["id"]
        unpaid = fetch_unpaid_seal(db, eid)
        if not unpaid:
            logger.info("企业 %s(%s) 无待付费公户车保单", eid, ent.get("name"))
            continue

        push_rooms = renewal_push_db.get_enabled_rooms_by_enterprise(db, eid)
        if not push_rooms:
            logger.info("企业 %s 未配置推送群，跳过 %d 单待付费", eid, len(unpaid))
            continue

        # 直达链接：跳到公户车盖章跟进页（点开后需登录查看）
        link = f"{BASE_URL}/insurance?tab=seal"

        # 汇总文案
        lines = ["🏢 公户车盖章跟进 · 每日提醒", ""]
        lines.append(f"截至今天，还有 {len(unpaid)} 单公户车保单待盖章：")
        for r in unpaid[:10]:
            plate = r.get("plate_no") or "未知车牌"
            insured = (r.get("insured") or "").strip()
            lines.append(f"· {plate}　{insured}")
        if len(unpaid) > 10:
            lines.append(f"…… 等共 {len(unpaid)} 单")
        lines.append("")
        lines.append(f"👉 点击查看并标记盖章：{link}")
        message = "\n".join(lines)

        for pr in push_rooms:
            room_name = _resolve_room_name(contacts_instances, pr["roomid"]) or pr.get("room_name") or pr["roomid"]
            ok = send_flowbot_group_message(room_name, ["@all"], message, robot_id)
            if ok:
                logger.info("盖章汇总已发送 企业=%s room=%s(%s) 待付费=%d",
                            eid, room_name, pr["roomid"], len(unpaid))
            else:
                logger.warning("盖章汇总发送失败 企业=%s room=%s(%s)",
                               eid, room_name, pr["roomid"])

    logger.info("本次盖章每日汇总完成")


if __name__ == "__main__":
    main()

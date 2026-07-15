# -*- coding: utf-8 -*-
"""
续保任务扫描——每日巡检 insurance_policy_fields.end_date_iso 在 [今天-30天, 今天+90天]
内的保单，按车牌/VIN+企业聚合生成/更新 renewal_tasks，供续保提醒页面使用。

独立进程，由 crontab 每天调度一次执行（与 policy_expiry_reminder.py 错开时间）；
不要挂进 Flask app（gunicorn -w 2 会导致同一次扫描被两个 worker 各跑一遍）。
run_scan(db) 同时被本脚本的 main() 和 insurance/renewal_api.py 的 POST /scan
手动触发端点复用，避免两处逻辑分叉。
"""
import json
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "/home/wxbot")

import pymysql  # noqa: E402

from main import load_config  # noqa: E402
from storage.db import Database  # noqa: E402
from insurance import renewal_db as rdb  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/wxbot/logs/renewal_task_scan.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WINDOW_PAST_DAYS = 30
WINDOW_FUTURE_DAYS = 90
EXPIRE_AFTER_DAYS = 30  # 到期后超过这么多天未成交/未流失，自动标记 expired


def _fetch_window_records(db, today):
    """
    查窗口内(今天-30, 今天+90)的保单记录，含企业/车牌/VIN等聚合所需字段。
    只处理 users.renewal_enabled=1 的账号跟单的保单——续保提醒功能按个人账号自助开启，
    不是按企业；同企业下没开的员工，他跟单的保单不参与扫描/推送（2026-07-15 从企业级
    改为账号级，几个人开就只影响这几个人自己的数据）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT r.id AS record_id, r.enterprise_id, r.sender, r.sender_name,
                   pf.plate_no, pf.vin, pf.insured, pf.applicant, pf.company_short,
                   pf.policy_type, pf.end_date_iso, pf.total_premium
            FROM insurance_records r
            JOIN insurance_policy_fields pf ON r.id = pf.record_id
            JOIN users u ON u.id = r.user_id
            WHERE r.deleted_at IS NULL
              AND r.status = 'done'
              AND r.doc_category = '保单'
              AND r.enterprise_id IS NOT NULL
              AND u.renewal_enabled = 1
              AND pf.end_date_iso IS NOT NULL
              AND pf.end_date_iso BETWEEN %s AND %s
            ORDER BY r.id DESC
        """, (today - timedelta(days=WINDOW_PAST_DAYS), today + timedelta(days=WINDOW_FUTURE_DAYS)))
        return cursor.fetchall()
    finally:
        conn.close()


def _group_by_customer(rows):
    """按 (customer_key, enterprise_id) 聚合；车牌优先，否则VIN(归一化大写)；两者都空跳过。"""
    groups = {}
    for row in rows:
        plate = (row.get("plate_no") or "").strip()
        vin = (row.get("vin") or "").strip().upper()
        customer_key = plate if plate else vin
        if not customer_key:
            continue
        key = (customer_key, row["enterprise_id"])
        groups.setdefault(key, []).append(row)
    return groups


def _resolve_default_assignee(db, sender, enterprise_id):
    """新建任务时的默认跟进人：按最新保单的 sender 走 user_sender_binding 解析 user_id（按企业精确匹配）。"""
    if not sender:
        return None, ""
    try:
        from auth.db import get_binding_by_sender
        bound = get_binding_by_sender(db, sender, enterprise_id)
        if bound:
            from auth.db import get_user_by_id
            user = get_user_by_id(db, bound["user_id"])
            return bound["user_id"], (user or {}).get("name") or ""
    except Exception as e:
        logger.warning("解析默认跟进人失败 sender=%s: %s", sender, e)
    return None, ""


def _build_task_payload(db, customer_key, enterprise_id, rows):
    """把同一客户的多条保单记录聚合成一条 renewal_tasks 的 upsert payload。"""
    rows_sorted = sorted(rows, key=lambda r: r["end_date_iso"])
    soonest = rows_sorted[0]
    latest_record = max(rows, key=lambda r: r["record_id"])  # 最新上传的一条，用于解析默认跟进人

    policy_types = []
    for r in rows_sorted:
        pt = (r.get("policy_type") or "").strip()
        if pt and pt not in policy_types:
            policy_types.append(pt)

    assignee, assignee_name = _resolve_default_assignee(db, latest_record.get("sender"), enterprise_id)

    days_left = (soonest["end_date_iso"] - date.today()).days
    priority = 2 if days_left <= 7 else (1 if days_left <= 30 else 0)

    return {
        "customer_key": customer_key,
        "enterprise_id": enterprise_id,
        "plate_no": soonest.get("plate_no") or "",
        "vin": soonest.get("vin") or "",
        "insured": soonest.get("insured") or "",
        "applicant": soonest.get("applicant") or "",
        "company_short": soonest.get("company_short") or "",
        "policy_type": "、".join(policy_types),
        "end_date": soonest["end_date_iso"],
        "total_premium": soonest.get("total_premium") or "",
        "policy_record_ids": [r["record_id"] for r in rows],
        "primary_record_id": soonest["record_id"],
        "priority": priority,
        "assignee": assignee,
        "assignee_name": assignee_name,
    }


def _detect_renewal_hints(db, enterprise_id_scope=None):
    """
    续保检测提示（非全自动）：对状态在 pending/following 的存量任务，查一次「同客户 +
    不在该任务当前 policy_record_ids 里 + 起保日期紧接着当前任务到期日」的保单记录，
    命中写入 hint 字段，不改变任务状态；由人工在页面上确认「标记已成交」或「忽略」。

    两层过滤缺一不可：
    1) 排除 policy_record_ids 里已有的记录——否则同一辆车交强+商业两份保单到期日不同，
       会把任务自己名下的另一份保单误判成"检测到新保单"。
    2) 只认"起保日期落在当前任务到期日前后一小段区间"的记录——否则任何到期日更晚的
       不相关保单（比如同时买的多年期驾意险）都会被误判成续保信号，命中率会高到失去意义
       （实测未加这层过滤时命中率高达98%，加上后才是真正的"接续投保"信号）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, customer_key, enterprise_id, end_date, policy_record_ids FROM renewal_tasks "
            "WHERE status IN ('pending','following')"
        )
        tasks = cursor.fetchall()
    finally:
        conn.close()

    hint_count = 0
    for t in tasks:
        try:
            known_ids = json.loads(t["policy_record_ids"]) if t.get("policy_record_ids") else []
        except (TypeError, ValueError):
            known_ids = []
        if not known_ids:
            known_ids = [0]  # NOT IN () 语法非法，占位一个不存在的id

        # 续保新单的起保日期应紧接当前任务的到期日：容忍提前7天投保、最多间隔35天(留出补录时间)
        start_window_from = t["end_date"] - timedelta(days=7)
        start_window_to = t["end_date"] + timedelta(days=35)

        conn = db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            ph = ",".join(["%s"] * len(known_ids))
            cursor.execute(f"""
                SELECT r.id AS record_id, pf.end_date_iso
                FROM insurance_records r JOIN insurance_policy_fields pf ON r.id = pf.record_id
                WHERE (pf.plate_no = %s OR pf.vin = %s)
                  AND r.enterprise_id = %s AND r.deleted_at IS NULL
                  AND r.status = 'done' AND r.doc_category = '保单'
                  AND r.id NOT IN ({ph})
                  AND pf.start_date_iso BETWEEN %s AND %s
                ORDER BY pf.end_date_iso DESC LIMIT 1
            """, [t["customer_key"], t["customer_key"], t["enterprise_id"]] + known_ids
                 + [start_window_from, start_window_to])
            hit = cursor.fetchone()
        finally:
            conn.close()
        if hit:
            rdb.set_hint(db, t["id"], hit["record_id"], hit["end_date_iso"])
            hint_count += 1
    return hint_count


def sync_customer_task(db, plate_no, vin, enterprise_id, user_id=None):
    """
    单客户级实时同步（供"重新识别"/手动编辑字段等场景调用，不必等下一次全量扫描）。
    customer_key 取值与批量扫描一致：车牌优先，否则VIN(大写)。

    窗口内（今天-30~今天+90到期）：按批量扫描同样的口径聚合并 upsert，与全量扫描收敛到
    同一结果，只是提前生效。
    窗口外：若该客户已有存量任务(非won/lost)，仍用该客户全部保单重新计算快照并 upsert——
    否则数据修正会把到期日推出窗口，全量扫描从此不再碰这条任务，旧快照永久残留成"僵尸数据"
    （2026-07-14实测：一条国寿驾乘险的止期解析bug修复后，对应续保任务的到期日一直不刷新，
    根因就是修正后的到期日跳出了90天扫描窗口）。窗口外且从无存量任务时不新建，维持"仅追踪
    近期到期客户"的原有语义不变。
    """
    customer_key = (plate_no or "").strip() or (vin or "").strip().upper()
    if not customer_key or not enterprise_id:
        return
    today = date.today()
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 续保提醒按个人账号自助开关，不是按企业(与批量扫描的过滤口径保持一致)——触发这次
        # 同步的那条记录如果没有绑定跟单人、或跟单人没开，不参与实时同步
        if not user_id:
            return
        cursor.execute("SELECT renewal_enabled FROM users WHERE id = %s", (user_id,))
        _u = cursor.fetchone()
        if not _u or not _u.get("renewal_enabled"):
            return
        cursor.execute("""
            SELECT r.id AS record_id, r.enterprise_id, r.sender, r.sender_name,
                   pf.plate_no, pf.vin, pf.insured, pf.applicant, pf.company_short,
                   pf.policy_type, pf.end_date_iso, pf.total_premium
            FROM insurance_records r
            JOIN insurance_policy_fields pf ON r.id = pf.record_id
            WHERE r.deleted_at IS NULL AND r.status = 'done' AND r.doc_category = '保单'
              AND r.enterprise_id = %s AND (pf.plate_no = %s OR pf.vin = %s)
        """, (enterprise_id, customer_key, customer_key))
        all_rows = [r for r in cursor.fetchall() if r.get("end_date_iso")]
    finally:
        conn.close()
    if not all_rows:
        return

    window_rows = [
        r for r in all_rows
        if (today - timedelta(days=WINDOW_PAST_DAYS)) <= r["end_date_iso"] <= (today + timedelta(days=WINDOW_FUTURE_DAYS))
    ]
    if not window_rows:
        existing = rdb.get_task_by_customer(db, customer_key, enterprise_id)
        if not existing:
            return  # 窗口外且从无存量任务，不需要新建
        source_rows = all_rows  # 窗口外但已有存量任务：用全量兜底刷新快照，避免残留旧值
    else:
        source_rows = window_rows

    payload = _build_task_payload(db, customer_key, enterprise_id, source_rows)
    rdb.upsert_task(db, payload)


def run_scan(db) -> dict:
    """执行一次完整扫描，返回统计结果字典。供 crontab 脚本和 API 手动触发端点复用。"""
    today = date.today()
    rows = _fetch_window_records(db, today)
    groups = _group_by_customer(rows)

    upserted = 0
    for (customer_key, enterprise_id), group_rows in groups.items():
        payload = _build_task_payload(db, customer_key, enterprise_id, group_rows)
        rdb.upsert_task(db, payload)
        upserted += 1

    expired_count = rdb.mark_expired(db, today - timedelta(days=EXPIRE_AFTER_DAYS))
    hint_count = _detect_renewal_hints(db)

    result = {
        "scanned_records": len(rows),
        "customers": len(groups),
        "upserted": upserted,
        "expired": expired_count,
        "hints": hint_count,
    }
    logger.info("续保扫描完成: %s", result)
    return result


def main():
    cfg = load_config("/home/wxbot/config.yaml")
    db = Database(cfg["mysql"], main_corpid=cfg["wecom"]["corpid"])

    # 接入钉钉错误告警：与 main.py 启动时相同的初始化方式，失败时不能像
    # policy_expiry_reminder.py 那样静默无人知道——续保数据新鲜度直接影响跟进人能否看到应跟进客户
    try:
        from core.notify import init_notifier
        notify_cfg = cfg.get("notify", {})
        init_notifier(webhook_url=notify_cfg.get("webhook_url", ""), secret=notify_cfg.get("secret", ""))
    except Exception:
        logger.warning("初始化钉钉告警失败，本次扫描异常将不会有告警", exc_info=True)

    rdb.init_renewal_tables(db)
    run_scan(db)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("续保任务扫描异常退出")
        try:
            from core.notify import notify_error
            notify_error("续保任务扫描", "renewal_task_scan.main", str(e))
        except Exception:
            pass
        raise

# -*- coding: utf-8 -*-
"""
保单「内部管理备注」——仅超级管理员可见/可写的每日追加日志。

每条备注 = 一行(insurance_internal_notes),不可覆盖;带问题归属分类。
异常保单要求每天核对并说明:record 当天有备注即视为「今日已核对」。

归属分类 attribution:
  our_pending  我方问题·待修复
  our_fixed    我方问题·已修复
  customer     客户问题·已说明
  ''/None      普通备注(不分类)
"""
import logging
import pymysql

logger = logging.getLogger(__name__)

# 合法的问题归属值(空串表示不分类)
VALID_ATTRIBUTIONS = {"", "our_pending", "our_fixed", "customer"}


def ensure_table(db):
    """建表(幂等)。可在 init_insurance_tables 中调用,或独立调用。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_internal_notes (
                id          BIGINT PRIMARY KEY AUTO_INCREMENT,
                record_id   INT NOT NULL COMMENT '关联 insurance_records.id',
                content     TEXT COMMENT '备注内容',
                attribution VARCHAR(16) NOT NULL DEFAULT '' COMMENT '问题归属: our_pending/our_fixed/customer/空',
                author_id   INT DEFAULT NULL COMMENT '操作人 user_id',
                author_name VARCHAR(64) DEFAULT NULL COMMENT '操作人姓名',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_notes_record (record_id),
                KEY idx_notes_record_day (record_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='保单内部管理备注(仅超管,每日追加日志)'
        """)
        conn.commit()
    finally:
        conn.close()


def add_note(db, record_id: int, content: str, attribution: str = "",
             author_id: int = None, author_name: str = "") -> int:
    """追加一条备注,返回新 id。"""
    if attribution not in VALID_ATTRIBUTIONS:
        attribution = ""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO insurance_internal_notes "
            "(record_id, content, attribution, author_id, author_name) "
            "VALUES (%s, %s, %s, %s, %s)",
            (record_id, content, attribution, author_id, author_name or ""),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_notes(db, record_id: int) -> list:
    """某保单的全部备注,最新在前。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, record_id, content, attribution, author_id, author_name, created_at "
            "FROM insurance_internal_notes WHERE record_id = %s ORDER BY id DESC",
            (record_id,),
        )
        rows = cursor.fetchall() or []
        for r in rows:
            r["created_at"] = str(r["created_at"])
        return rows
    finally:
        conn.close()


def get_summaries(db, record_ids: list) -> dict:
    """
    批量取多个保单的备注摘要,供列表接口注入。
    返回 {record_id: {count, latest, latest_attribution, latest_at, reviewed_today}}。
    未命中的 record_id 不在返回 dict 中(前端按缺省=无备注处理)。
    """
    if not record_ids:
        return {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        ph = ",".join(["%s"] * len(record_ids))
        # 每条备注按记录聚合:总数、今日是否有备注、最新一条 id
        cursor.execute(
            f"SELECT record_id, COUNT(*) AS cnt, "
            f"       MAX(CASE WHEN DATE(created_at) = CURDATE() THEN 1 ELSE 0 END) AS today, "
            f"       MAX(id) AS latest_id "
            f"FROM insurance_internal_notes WHERE record_id IN ({ph}) GROUP BY record_id",
            list(record_ids),
        )
        agg = {r["record_id"]: r for r in (cursor.fetchall() or [])}
        if not agg:
            return {}
        # 取每条记录最新一条备注的内容/归属/时间
        latest_ids = [r["latest_id"] for r in agg.values()]
        lph = ",".join(["%s"] * len(latest_ids))
        cursor.execute(
            f"SELECT id, record_id, content, attribution, created_at "
            f"FROM insurance_internal_notes WHERE id IN ({lph})",
            latest_ids,
        )
        latest = {r["record_id"]: r for r in (cursor.fetchall() or [])}
        out = {}
        for rid, a in agg.items():
            lt = latest.get(rid, {})
            out[rid] = {
                "count": int(a["cnt"]),
                "latest": lt.get("content") or "",
                "latest_attribution": lt.get("attribution") or "",
                "latest_at": str(lt.get("created_at") or ""),
                "reviewed_today": bool(a["today"]),
            }
        return out
    finally:
        conn.close()

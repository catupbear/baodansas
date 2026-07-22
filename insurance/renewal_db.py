# -*- coding: utf-8 -*-
"""
续保任务模块 — 数据库层
负责 renewal_tasks / renewal_followups 两张表的建表与 CRUD 操作。
"""
import json
import logging

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


def init_renewal_tables(db):
    """
    创建 renewal_tasks / renewal_followups 表（幂等，已存在则跳过）。
    db 是 storage.db.Database 实例，通过 db.pool 获取连接。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS renewal_tasks (
                id                 INT PRIMARY KEY AUTO_INCREMENT,
                customer_key       VARCHAR(128) NOT NULL COMMENT '车牌号或VIN(车牌优先)',
                enterprise_id      INT DEFAULT NULL COMMENT '所属企业',
                plate_no           VARCHAR(32) DEFAULT '',
                vin                VARCHAR(64) DEFAULT '',
                insured            VARCHAR(128) DEFAULT '',
                applicant          VARCHAR(128) DEFAULT '',
                company_short      VARCHAR(64) DEFAULT '',
                policy_type        VARCHAR(64) NOT NULL DEFAULT '' COMMENT '险种(参与唯一键,每险种一条任务)',
                policy_no          VARCHAR(64) DEFAULT '' COMMENT '该险种最近一张保单的保单号',
                end_date           DATE DEFAULT NULL COMMENT '聚合内最近到期日',
                total_premium      VARCHAR(64) DEFAULT '' COMMENT '展示快照,不做数值聚合',
                policy_record_ids  TEXT COMMENT '关联insurance_records.id,JSON数组,每次扫描全量覆盖',
                primary_record_id  INT DEFAULT NULL COMMENT '聚合内最近到期日对应的原始record_id,用于前端预览原件',
                assignee           INT DEFAULT NULL COMMENT '跟进人 user_id',
                assignee_name      VARCHAR(128) DEFAULT '',
                status             VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/following/won/lost/expired',
                priority           TINYINT NOT NULL DEFAULT 0 COMMENT '0/1/2,纯自动计算,不支持手动覆盖',
                remark             TEXT,
                remind_at          DATE DEFAULT NULL COMMENT '与最新一条followup.next_remind_at同步',
                hint_record_id     INT DEFAULT NULL COMMENT '疑似已续保的新保单record_id(续保检测提示)',
                hint_end_date      DATE DEFAULT NULL,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at         DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_customer_type_ent (customer_key, policy_type, enterprise_id),
                KEY idx_ent_status_end (enterprise_id, status, end_date),
                KEY idx_assignee_status (assignee, status),
                KEY idx_remind_at (remind_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='续保任务(按车牌/VIN+险种+企业,每险种一条)'
        """)
        # 幂等迁移：旧表加 policy_no 列
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='renewal_tasks' AND COLUMN_NAME='policy_no'"
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE renewal_tasks ADD COLUMN policy_no VARCHAR(64) DEFAULT '' "
                "COMMENT '该险种最近一张保单的保单号' AFTER policy_type"
            )
        # 幂等迁移：唯一键从 (customer_key, enterprise_id) 改为 (customer_key, policy_type, enterprise_id)
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='renewal_tasks' AND INDEX_NAME='uk_customer_type_ent'"
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='renewal_tasks' AND INDEX_NAME='uk_customer_ent'"
            )
            if cursor.fetchone()["cnt"] > 0:
                cursor.execute("ALTER TABLE renewal_tasks DROP INDEX uk_customer_ent")
            cursor.execute(
                "ALTER TABLE renewal_tasks ADD UNIQUE KEY uk_customer_type_ent "
                "(customer_key, policy_type, enterprise_id)"
            )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS renewal_followups (
                id              INT PRIMARY KEY AUTO_INCREMENT,
                task_id         INT NOT NULL,
                operator        INT DEFAULT NULL,
                operator_name   VARCHAR(128) DEFAULT '',
                action          VARCHAR(32) NOT NULL DEFAULT 'followup' COMMENT 'followup/assign/status_change',
                content         TEXT,
                next_remind_at  DATE DEFAULT NULL,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_task (task_id),
                KEY idx_operator (operator)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='续保跟进记录(时间线)'
        """)
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='renewal_tasks' AND COLUMN_NAME='primary_record_id'"
        )
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE renewal_tasks ADD COLUMN primary_record_id INT DEFAULT NULL "
                "COMMENT '聚合内最近到期日对应的原始record_id,用于前端预览原件' AFTER policy_record_ids"
            )
        conn.commit()
        logger.info("renewal_tasks / renewal_followups 表初始化完成")
    finally:
        conn.close()


# ============================================================
# renewal_tasks CRUD
# ============================================================

def upsert_task(db, data: dict) -> int:
    """
    按 (customer_key, policy_type, enterprise_id) 原子 upsert 一条续保任务（每险种一条）。
    已存在且 status IN (won,lost) 时不覆盖任何字段（尊重已关闭状态），
    仅返回其 id；否则插入或更新 end_date/policy_record_ids/priority 等快照字段。

    data 需含: customer_key, enterprise_id(必须是非空int，MySQL唯一索引对NULL不去重，
               调用方必须先过滤掉没有企业归属的记录，不要传None), plate_no, vin, insured,
               applicant, company_short, policy_type, end_date, total_premium,
               policy_record_ids(list，会自动json序列化), priority,
               assignee(可选), assignee_name(可选)
    返回: 该任务的 id。
    """
    record = dict(data)
    if not record.get("enterprise_id"):
        raise ValueError("upsert_task: enterprise_id 不能为空（唯一索引对NULL不去重，调用前需过滤）")
    if isinstance(record.get("policy_record_ids"), (list, dict)):
        record["policy_record_ids"] = json.dumps(record["policy_record_ids"], ensure_ascii=False)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 已关闭(won/lost)的任务不覆盖：先查现状
        cursor.execute(
            "SELECT id, status FROM renewal_tasks WHERE customer_key=%s AND policy_type=%s AND enterprise_id=%s",
            (record["customer_key"], record.get("policy_type") or "", record["enterprise_id"]),
        )
        existing = cursor.fetchone()
        if existing and existing["status"] in ("won", "lost"):
            return existing["id"]

        columns = ["customer_key", "enterprise_id", "plate_no", "vin", "insured", "applicant",
                   "company_short", "policy_type", "policy_no", "end_date", "total_premium",
                   "policy_record_ids", "primary_record_id", "priority"]
        values = [record.get(c) for c in columns]
        update_cols = ["plate_no", "vin", "insured", "applicant", "company_short", "policy_no",
                       "end_date", "total_premium", "policy_record_ids", "primary_record_id", "priority"]
        # 新建任务时才写默认跟进人；已存在(pending/following/expired)不覆盖已有 assignee
        if not existing:
            columns += ["assignee", "assignee_name", "status"]
            values += [record.get("assignee"), record.get("assignee_name") or "", "pending"]

        col_sql = ", ".join(columns)
        ph_sql = ", ".join(["%s"] * len(columns))
        update_sql = ", ".join([f"{c}=VALUES({c})" for c in update_cols])
        # status 若之前是 expired，遇到窗口内数据重新出现应回退成 pending（重新可跟进）
        update_sql += ", status=IF(status='expired','pending',status)"

        cursor.execute(
            f"INSERT INTO renewal_tasks ({col_sql}) VALUES ({ph_sql}) "
            f"ON DUPLICATE KEY UPDATE {update_sql}",
            values,
        )
        conn.commit()

        cursor.execute(
            "SELECT id FROM renewal_tasks WHERE customer_key=%s AND policy_type=%s AND enterprise_id=%s",
            (record["customer_key"], record.get("policy_type") or "", record["enterprise_id"]),
        )
        row = cursor.fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def mark_expired(db, cutoff_date) -> int:
    """end_date 早于 cutoff_date 且未关闭的任务标记为 expired。返回受影响行数。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE renewal_tasks SET status='expired' "
            "WHERE end_date < %s AND status NOT IN ('won','lost','expired')",
            (cutoff_date,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def set_hint(db, task_id: int, hint_record_id, hint_end_date):
    """写入/清空续保检测提示(hint_record_id/hint_end_date 传 None 即清空)。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE renewal_tasks SET hint_record_id=%s, hint_end_date=%s WHERE id=%s",
            (hint_record_id, hint_end_date, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_task(db, task_id: int) -> dict:
    """获取单条续保任务，不存在返回 None。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM renewal_tasks WHERE id=%s", (task_id,))
        row = cursor.fetchone()
        if row and isinstance(row.get("policy_record_ids"), str):
            try:
                row["policy_record_ids"] = json.loads(row["policy_record_ids"])
            except (TypeError, json.JSONDecodeError):
                row["policy_record_ids"] = []
        return row
    finally:
        conn.close()


def get_task_by_customer(db, customer_key: str, enterprise_id) -> dict:
    """按 (customer_key, enterprise_id) 取该客户任意一条任务，不存在返回 None（供实时同步判断存量用；每险种一条后同客户可能多条）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM renewal_tasks WHERE customer_key=%s AND enterprise_id=%s LIMIT 1",
            (customer_key, enterprise_id),
        )
        row = cursor.fetchone()
        if row and isinstance(row.get("policy_record_ids"), str):
            try:
                row["policy_record_ids"] = json.loads(row["policy_record_ids"])
            except (TypeError, json.JSONDecodeError):
                row["policy_record_ids"] = []
        return row
    finally:
        conn.close()


def list_tasks(db, page: int = 1, page_size: int = 20, enterprise_id=None,
               assignee_ids=None, status: str = "", date_start: str = "",
               date_end: str = "", keyword: str = "") -> dict:
    """
    分页查询续保任务列表。
    enterprise_id: None=不过滤(超管)；否则按企业过滤
    assignee_ids: None=不过滤；[]=空列表返回空结果(员工无可见任务)；[uid,...]=按跟进人过滤
    status: 空=不过滤，'!expired'等暂不支持排除语法，直接传具体状态值
    date_start/date_end: 按 end_date 范围过滤（'YYYY-MM-DD'）
    keyword: 车牌/被保人/投保人/保单号模糊搜索
    返回: {"total":N, "pages":N, "page":N, "tasks":[...]}
    """
    conditions = []
    params = []
    if enterprise_id is not None:
        conditions.append("enterprise_id = %s")
        params.append(enterprise_id)
    if assignee_ids is not None:
        if not assignee_ids:
            return {"total": 0, "pages": 1, "page": page, "tasks": []}
        ph = ",".join(["%s"] * len(assignee_ids))
        conditions.append(f"assignee IN ({ph})")
        params.extend(assignee_ids)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if date_start:
        conditions.append("end_date >= %s")
        params.append(date_start)
    if date_end:
        conditions.append("end_date <= %s")
        params.append(date_end)
    if keyword:
        conditions.append("(plate_no LIKE %s OR insured LIKE %s OR applicant LIKE %s OR policy_no LIKE %s)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM renewal_tasks {where_clause}", params)
        total = cursor.fetchone()["cnt"]
        pages = max(1, (total + page_size - 1) // page_size)
        offset = (page - 1) * page_size
        cursor.execute(
            f"SELECT * FROM renewal_tasks {where_clause} "
            f"ORDER BY priority DESC, end_date ASC LIMIT %s OFFSET %s",
            params + [page_size, offset],
        )
        rows = cursor.fetchall()
        for r in rows:
            if isinstance(r.get("policy_record_ids"), str):
                try:
                    r["policy_record_ids"] = json.loads(r["policy_record_ids"])
                except (TypeError, json.JSONDecodeError):
                    r["policy_record_ids"] = []
            for f in ("end_date", "remind_at", "hint_end_date", "created_at", "updated_at"):
                if r.get(f) is not None:
                    r[f] = str(r[f])
        return {"total": total, "pages": pages, "page": page, "tasks": rows}
    finally:
        conn.close()


def get_stats(db, enterprise_id=None, assignee_ids=None) -> dict:
    """看板统计：过期未处理/7天内到期/本月到期/下月到期/本月已成交。"""
    base_conditions = []
    params = []
    if enterprise_id is not None:
        base_conditions.append("enterprise_id = %s")
        params.append(enterprise_id)
    if assignee_ids is not None:
        if not assignee_ids:
            return {"overdue": 0, "due_7d": 0, "due_30d": 0, "due_this_month": 0, "due_next_month": 0, "won_this_month": 0}
        ph = ",".join(["%s"] * len(assignee_ids))
        base_conditions.append(f"assignee IN ({ph})")
        params.extend(assignee_ids)
    base_where = (" AND " + " AND ".join(base_conditions)) if base_conditions else ""

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        def _count(extra_sql, extra_params=()):
            cursor.execute(
                f"SELECT COUNT(*) AS cnt FROM renewal_tasks WHERE {extra_sql} {base_where}",
                list(extra_params) + params,
            )
            return cursor.fetchone()["cnt"]

        overdue = _count("end_date < CURDATE() AND status NOT IN ('won','lost')")
        due_7d = _count("status NOT IN ('won','lost','expired') AND end_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 7 DAY)")
        due_30d = _count("status NOT IN ('won','lost','expired') AND end_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 30 DAY)")
        due_this_month = _count("status NOT IN ('won','lost','expired') AND end_date BETWEEN CURDATE() AND LAST_DAY(CURDATE())")
        due_next_month = _count(
            "status NOT IN ('won','lost','expired') AND end_date BETWEEN "
            "DATE_ADD(LAST_DAY(CURDATE()), INTERVAL 1 DAY) AND LAST_DAY(DATE_ADD(CURDATE(), INTERVAL 1 MONTH))"
        )
        won_this_month = _count("status='won' AND updated_at >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')")

        return {
            "overdue": overdue, "due_7d": due_7d, "due_30d": due_30d, "due_this_month": due_this_month,
            "due_next_month": due_next_month, "won_this_month": won_this_month,
        }
    finally:
        conn.close()


# ============================================================
# renewal_followups CRUD
# ============================================================

def add_followup(db, task_id: int, operator, operator_name: str, action: str,
                  content: str = "", next_remind_at=None) -> int:
    """
    追加一条跟进记录；若传 next_remind_at 同步回写 renewal_tasks.remind_at；
    若 action='followup' 且任务当前状态为 pending，自动置为 following。
    返回新记录 id。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "INSERT INTO renewal_followups (task_id, operator, operator_name, action, content, next_remind_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (task_id, operator, operator_name or "", action, content or "", next_remind_at),
        )
        followup_id = cursor.lastrowid

        if next_remind_at:
            cursor.execute("UPDATE renewal_tasks SET remind_at=%s WHERE id=%s", (next_remind_at, task_id))
        if action == "followup":
            cursor.execute(
                "UPDATE renewal_tasks SET status='following' WHERE id=%s AND status='pending'",
                (task_id,),
            )
        conn.commit()
        return followup_id
    finally:
        conn.close()


def get_followups(db, task_id: int) -> list:
    """某任务的全部跟进记录，最新在前。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM renewal_followups WHERE task_id=%s ORDER BY id DESC",
            (task_id,),
        )
        rows = cursor.fetchall()
        for r in rows:
            r["created_at"] = str(r["created_at"])
            if r.get("next_remind_at"):
                r["next_remind_at"] = str(r["next_remind_at"])
        return rows
    finally:
        conn.close()


def update_status(db, task_id: int, status: str):
    """变更任务状态。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE renewal_tasks SET status=%s WHERE id=%s", (status, task_id))
        conn.commit()
    finally:
        conn.close()


def assign_tasks(db, task_ids: list, assignee: int, assignee_name: str):
    """批量分配跟进人。"""
    if not task_ids:
        return
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        ph = ",".join(["%s"] * len(task_ids))
        cursor.execute(
            f"UPDATE renewal_tasks SET assignee=%s, assignee_name=%s WHERE id IN ({ph})",
            [assignee, assignee_name or ""] + task_ids,
        )
        conn.commit()
    finally:
        conn.close()

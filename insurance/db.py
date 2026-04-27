"""
保单识别模块 — 数据库层
负责保单识别记录和配置项的增删改查（MySQL 版本）
"""

import json
import logging

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# JSON 字段列表，保存时自动序列化，读取时由调用方自行处理
_JSON_FIELDS = {"parsed_fields", "mapped_fields"}


def init_insurance_tables(db):
    """
    建立保单识别所需的数据库表（MySQL 语法）。
    db 是 storage.db.Database 实例，通过 db.pool 获取连接。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 保单识别记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_records (
                id                 INT PRIMARY KEY AUTO_INCREMENT,
                msg_seq            INT,
                roomid             VARCHAR(64),
                room_name          VARCHAR(128),
                sender             VARCHAR(64),
                sender_name        VARCHAR(128),
                filename           VARCHAR(256),
                filesize           INT DEFAULT 0,
                cos_url            TEXT,
                ocr_engine         VARCHAR(32),
                doc_category       VARCHAR(64),
                confidence         FLOAT DEFAULT 0,
                parsed_fields      LONGTEXT,
                mapped_fields      LONGTEXT,
                dingtalk_synced    TINYINT DEFAULT 0,
                dingtalk_target_id VARCHAR(64),
                dingtalk_record_id VARCHAR(128),
                status             VARCHAR(32) DEFAULT 'pending',
                error_message      TEXT,
                source             VARCHAR(32) DEFAULT 'auto',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)

        # 前端可配置项表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS insurance_config (
                id           INT PRIMARY KEY AUTO_INCREMENT,
                config_key   VARCHAR(128) UNIQUE,
                config_value LONGTEXT,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)

        # 索引（忽略已存在的索引错误）
        for idx_sql in [
            "CREATE INDEX idx_insurance_roomid ON insurance_records(roomid)",
            "CREATE INDEX idx_insurance_status ON insurance_records(status)",
            "CREATE INDEX idx_insurance_created_at ON insurance_records(created_at)",
        ]:
            try:
                cursor.execute(idx_sql)
            except Exception:
                pass  # 索引已存在，忽略

        # 为 insurance_records 表添加新字段（幂等，已存在则跳过）
        new_columns = [
            ("raw_text", "LONGTEXT COMMENT 'OCR提取原文'"),
        ]
        for col_name, col_def in new_columns:
            try:
                cursor.execute(f"ALTER TABLE insurance_records ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass  # 字段已存在，忽略

        conn.commit()
        logger.info("保单识别数据库表初始化完成")
    finally:
        conn.close()


def save_insurance_record(db, record: dict) -> int:
    """
    插入一条保单识别记录，返回新记录的 id。
    JSON 字段（parsed_fields / mapped_fields）若传入 dict/list，自动序列化。
    """
    data = dict(record)
    for field in _JSON_FIELDS:
        if field in data and not isinstance(data[field], str):
            data[field] = json.dumps(data[field], ensure_ascii=False)

    # created_at / updated_at 由 MySQL DEFAULT 自动填充，无需手动传入
    data.pop("created_at", None)
    data.pop("updated_at", None)

    columns = ", ".join(data.keys())
    # MySQL 参数占位符为 %s
    placeholders = ", ".join(["%s"] * len(data))
    values = list(data.values())

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"INSERT INTO insurance_records ({columns}) VALUES ({placeholders})",
            values
        )
        conn.commit()
        record_id = cursor.lastrowid
        logger.debug("保单记录已插入, id=%d", record_id)
        return record_id
    finally:
        conn.close()


def update_insurance_record(db, record_id: int, updates: dict):
    """
    更新指定 id 的保单识别记录。
    updates 中若包含 dict/list 类型的值，自动 json.dumps 序列化。
    updated_at 由 MySQL ON UPDATE CURRENT_TIMESTAMP 自动维护。
    """
    data = {}
    for k, v in updates.items():
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v, ensure_ascii=False)
        else:
            data[k] = v

    # MySQL 的 ON UPDATE CURRENT_TIMESTAMP 会自动更新 updated_at，无需手动传入
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [record_id]

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"UPDATE insurance_records SET {set_clause} WHERE id = %s",
            values
        )
        conn.commit()
        logger.debug("保单记录已更新, id=%d, 字段=%s", record_id, list(updates.keys()))
    finally:
        conn.close()


def find_records_by_plate(db, plate: str, exclude_id: int = None) -> list:
    """
    查找同车牌的已完成保单记录，用于跨保单字段互补。
    返回 parsed_fields 非空且 status=done 的记录列表（按时间倒序）。
    """
    if not plate:
        return []
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = (
            "SELECT id, parsed_fields FROM insurance_records "
            "WHERE status = 'done' AND parsed_fields IS NOT NULL "
            "AND JSON_UNQUOTE(JSON_EXTRACT(parsed_fields, '$.车牌号')) = %s"
        )
        params = [plate]
        if exclude_id:
            sql += " AND id != %s"
            params.append(exclude_id)
        sql += " ORDER BY id DESC LIMIT 10"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            if isinstance(row.get("parsed_fields"), str):
                try:
                    row["parsed_fields"] = json.loads(row["parsed_fields"])
                except (TypeError, json.JSONDecodeError):
                    row["parsed_fields"] = {}
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def query_insurance_records(
    db,
    page: int = 1,
    page_size: int = 20,
    roomid: str = "",
    status: str = "",
    keyword: str = "",
    source: str = "",
    source_type: str = "",
    sender: str = "",
    ocr_engine: str = "",
    doc_category: str = "",
) -> dict:
    """
    分页查询保单识别记录，支持按群、状态、关键词、来源、识别方式、文档类型筛选。
    source_type: 'room' 仅群聊, 'user' 仅私聊（roomid 为空的记录）
    sender: 按发送人 ID 筛选
    ocr_engine: 按识别方式筛选（pdfplumber / baidu）
    doc_category: 按文档类型筛选（保单 / 电子标志 等）
    返回: {"total": 总数, "pages": 总页数, "page": 当前页, "records": [...]}
    """
    conditions = []
    params = []

    # 来源类型筛选
    if source_type == "room":
        conditions.append("roomid IS NOT NULL AND roomid != ''")
        if roomid:
            conditions.append("roomid = %s")
            params.append(roomid)
    elif source_type == "user":
        conditions.append("(roomid IS NULL OR roomid = '')")
        if sender:
            conditions.append("sender = %s")
            params.append(sender)
    else:
        if roomid:
            conditions.append("roomid LIKE %s")
            params.append(f"%{roomid}%")
    if status:
        conditions.append("status = %s")
        params.append(status)
    if keyword:
        # 在文件名、群名、发送人名中模糊匹配
        conditions.append(
            "(filename LIKE %s OR room_name LIKE %s OR sender_name LIKE %s)"
        )
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if source:
        conditions.append("source = %s")
        params.append(source)
    if ocr_engine:
        conditions.append("ocr_engine = %s")
        params.append(ocr_engine)
    if doc_category:
        conditions.append("doc_category = %s")
        params.append(doc_category)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 查询总数
        cursor.execute(
            f"SELECT COUNT(*) as cnt FROM insurance_records {where_clause}",
            params
        )
        total = cursor.fetchone()["cnt"]
        pages = max(1, (total + page_size - 1) // page_size)

        # 延迟关联：先查 id（轻量排序），再用 id 取完整数据（避免 LONGTEXT 拖慢排序）
        offset = (page - 1) * page_size
        cursor.execute(
            f"SELECT id FROM insurance_records {where_clause} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [page_size, offset]
        )
        id_rows = cursor.fetchall()
        if id_rows:
            ids = [r["id"] for r in id_rows]
            placeholders = ",".join(["%s"] * len(ids))
            select_cols = (
                "id, msg_seq, roomid, room_name, sender, sender_name, "
                "filename, filesize, cos_url, ocr_engine, doc_category, confidence, "
                "parsed_fields, dingtalk_synced, dingtalk_target_id, "
                "status, source, created_at, updated_at"
            )
            cursor.execute(
                f"SELECT {select_cols} FROM insurance_records "
                f"WHERE id IN ({placeholders}) ORDER BY created_at DESC",
                ids
            )
            records = list(cursor.fetchall())
        else:
            records = []

        return {"total": total, "pages": pages, "page": page, "records": records}
    finally:
        conn.close()


def get_insurance_record(db, record_id: int) -> dict:
    """获取单条保单识别记录，不存在时返回 None。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM insurance_records WHERE id = %s",
            (record_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_insurance_config(db, key: str, default=None):
    """
    获取配置项。
    config_value 若是合法 JSON 字符串则自动反序列化，否则直接返回字符串。
    key 不存在时返回 default。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT config_value FROM insurance_config WHERE config_key = %s",
            (key,)
        )
        row = cursor.fetchone()
        if row is None:
            return default
        raw = row["config_value"]
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
    finally:
        conn.close()


def set_insurance_config(db, key: str, value):
    """
    设置配置项（存在则更新，不存在则插入）。
    value 若为 dict/list/int/float/bool/None 等非字符串类型，自动 json.dumps 序列化。
    MySQL 使用 REPLACE INTO 实现 upsert。
    """
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # REPLACE INTO：config_key 为 UNIQUE，存在则替换，不存在则插入
        cursor.execute(
            "REPLACE INTO insurance_config (config_key, config_value) VALUES (%s, %s)",
            (key, value)
        )
        conn.commit()
        logger.debug("保单配置已更新, key=%s", key)
    finally:
        conn.close()


def get_all_insurance_config(db) -> dict:
    """
    获取全部配置项，返回 {key: value} 字典。
    每个 value 自动尝试 JSON 反序列化。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT config_key, config_value FROM insurance_config")
        result = {}
        for row in cursor.fetchall():
            raw = row["config_value"]
            try:
                result[row["config_key"]] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result[row["config_key"]] = raw
        return result
    finally:
        conn.close()


def get_insurance_stats(db) -> dict:
    """
    获取保单识别统计信息。
    返回:
        {
            "total": 总记录数,
            "status_stats": [{"status": ..., "cnt": ...}, ...],
            "room_stats":   [{"roomid": ..., "room_name": ..., "cnt": ...}, ...]
        }
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 单次全表扫描，同时统计状态和引擎（避免多次扫描 LONGTEXT 表）
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(status = 'done' OR status = 'success') as done_cnt,
                SUM(status = 'failed') as failed_cnt,
                SUM(status = 'pending') as pending_cnt,
                SUM(status = 'processing') as processing_cnt
            FROM insurance_records
        """)
        summary = cursor.fetchone()
        total = summary["total"] or 0

        # 按状态统计（从汇总结果构造，不再单独查询）
        status_stats = []
        for s, c in [("done", summary["done_cnt"]), ("failed", summary["failed_cnt"]),
                      ("pending", summary["pending_cnt"]), ("processing", summary["processing_cnt"])]:
            if c:
                status_stats.append({"status": s, "cnt": int(c)})

        # 按识别方式统计（轻量查询，ocr_engine 是 VARCHAR(32)）
        cursor.execute(
            "SELECT ocr_engine, COUNT(*) as cnt "
            "FROM insurance_records GROUP BY ocr_engine ORDER BY cnt DESC"
        )
        engine_stats = list(cursor.fetchall())

        # 按群统计（轻量查询，只用索引列）
        cursor.execute(
            "SELECT roomid, MAX(room_name) as room_name, COUNT(*) as cnt "
            "FROM insurance_records GROUP BY roomid ORDER BY cnt DESC LIMIT 20"
        )
        room_stats = list(cursor.fetchall())

        return {
            "total": total,
            "status_stats": status_stats,
            "room_stats": room_stats,
            "engine_stats": engine_stats,
        }
    finally:
        conn.close()


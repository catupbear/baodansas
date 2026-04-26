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


def query_insurance_records(
    db,
    page: int = 1,
    page_size: int = 20,
    roomid: str = "",
    status: str = "",
    keyword: str = "",
    source: str = "",
) -> dict:
    """
    分页查询保单识别记录，支持按群、状态、关键词、来源筛选。
    返回: {"total": 总数, "pages": 总页数, "page": 当前页, "records": [...]}
    """
    conditions = []
    params = []

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

        # 分页查询
        offset = (page - 1) * page_size
        cursor.execute(
            f"SELECT * FROM insurance_records {where_clause} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [page_size, offset]
        )
        records = list(cursor.fetchall())

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

        # 总数
        cursor.execute("SELECT COUNT(*) as cnt FROM insurance_records")
        total = cursor.fetchone()["cnt"]

        # 按状态统计
        cursor.execute(
            "SELECT status, COUNT(*) as cnt "
            "FROM insurance_records GROUP BY status ORDER BY cnt DESC"
        )
        status_stats = list(cursor.fetchall())

        # 按群统计（取前 20 个）
        cursor.execute(
            "SELECT roomid, room_name, COUNT(*) as cnt "
            "FROM insurance_records GROUP BY roomid ORDER BY cnt DESC LIMIT 20"
        )
        room_stats = list(cursor.fetchall())

        return {
            "total": total,
            "status_stats": status_stats,
            "room_stats": room_stats,
        }
    finally:
        conn.close()

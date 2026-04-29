"""
备份来源: insurance/db.py
备份日期: 2026-04-29
备份原因: sync_policy_fields / query_policy_fields 从未被调用，内部逻辑已在 save/update_insurance_record 中自动处理
恢复方式: 见同目录 README.md
"""


def sync_policy_fields(db, record_id: int, parsed_fields: dict):
    """
    公开方法：同步关联表（外部调用用，如手动重新识别场景）。
    parsed_fields 为 dict 类型。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        _upsert_policy_fields(cursor, record_id, parsed_fields)
        conn.commit()
    finally:
        conn.close()


def query_policy_fields(db, record_id: int) -> dict:
    """查询单条关联表记录，返回 OCR 字段名为 key 的字典"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM insurance_policy_fields WHERE record_id = %s",
            (record_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {}
        # 列名转回 OCR 字段名
        result = {}
        for col_name, ocr_key in _COLUMN_TO_OCR.items():
            val = row.get(col_name, "")
            if val:
                result[ocr_key] = val
        return result
    finally:
        conn.close()

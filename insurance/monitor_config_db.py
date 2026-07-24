"""
监控配置模块 — 数据库层
负责 monitor_configs 表的建表与 CRUD 操作
"""

import json
import logging

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# 需要 JSON 序列化/反序列化的字段
_JSON_FIELDS = {"rooms", "users", "field_mapping"}


def init_monitor_config_table(db):
    """
    创建 monitor_configs 表（幂等，已存在则跳过）。
    db 是 storage.db.Database 实例，通过 db.pool 获取连接。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_configs (
                id               INT PRIMARY KEY AUTO_INCREMENT,
                name             VARCHAR(128) NOT NULL DEFAULT '',
                enabled          TINYINT NOT NULL DEFAULT 1,
                rooms            LONGTEXT COMMENT '群聊列表 JSON: [{"id":"wr...","name":"群名"},...]',
                users            LONGTEXT COMMENT '私聊列表 JSON: [{"id":"wm...","name":"人名"},...]',
                dingtalk_doc_url VARCHAR(512) DEFAULT '',
                dingtalk_base_id VARCHAR(128) DEFAULT '',
                dingtalk_sheet_id VARCHAR(128) DEFAULT '',
                field_mapping    LONGTEXT COMMENT '字段映射 JSON: {"OCR字段":"钉钉列名",...}',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_monitor_enabled (enabled)
            )
        """)
        # 添加 user_id 列（幂等，已存在则跳过）
        try:
            cursor.execute("ALTER TABLE monitor_configs ADD COLUMN user_id INT DEFAULT NULL COMMENT '绑定的用户ID'")
            cursor.execute("CREATE INDEX idx_mc_user_id ON monitor_configs(user_id)")
        except Exception:
            pass  # 列已存在

        # 添加 enterprise_id 列（幂等，已存在则跳过）
        try:
            cursor.execute("ALTER TABLE monitor_configs ADD COLUMN enterprise_id INT DEFAULT NULL COMMENT '绑定的企业ID'")
            cursor.execute("CREATE INDEX idx_mc_enterprise_id ON monitor_configs(enterprise_id)")
        except Exception:
            pass  # 列已存在

        conn.commit()
        logger.info("monitor_configs 表初始化完成")
    finally:
        conn.close()


def _serialize_json(data: dict) -> dict:
    """
    写入前序列化 JSON 字段。
    将 data 中属于 _JSON_FIELDS 且值为 dict/list 的字段转为 JSON 字符串。
    返回处理后的副本，不修改原始 data。
    """
    result = dict(data)
    for field in _JSON_FIELDS:
        if field in result and not isinstance(result[field], str):
            result[field] = json.dumps(result[field], ensure_ascii=False)
    return result


def _deserialize_json(row: dict) -> dict:
    """
    读取后反序列化 JSON 字段。
    将 row 中属于 _JSON_FIELDS 的字符串字段尝试解析为 Python 对象。
    返回处理后的副本，不修改原始 row。
    """
    if row is None:
        return None
    result = dict(row)
    for field in _JSON_FIELDS:
        if field in result and isinstance(result[field], str):
            try:
                result[field] = json.loads(result[field])
            except (TypeError, json.JSONDecodeError):
                pass  # 解析失败保持原始字符串
    return result


def list_monitor_configs(db) -> list:
    """查询全部监控配置，按 id 降序排列。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM monitor_configs ORDER BY id DESC")
        rows = cursor.fetchall()
        return [_deserialize_json(row) for row in rows]
    finally:
        conn.close()


def get_monitor_config(db, config_id: int) -> dict:
    """获取单条监控配置，不存在时返回 None。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM monitor_configs WHERE id = %s",
            (config_id,)
        )
        row = cursor.fetchone()
        return _deserialize_json(row)
    finally:
        conn.close()


def create_monitor_config(db, data: dict) -> int:
    """
    新增监控配置，返回新记录的 id。
    JSON 字段（rooms/users/field_mapping）若传入 dict/list，自动序列化。
    """
    record = _serialize_json(data)
    # created_at / updated_at 由 MySQL DEFAULT 自动填充
    record.pop("id", None)
    record.pop("created_at", None)
    record.pop("updated_at", None)

    columns = ", ".join(record.keys())
    placeholders = ", ".join(["%s"] * len(record))
    values = list(record.values())

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"INSERT INTO monitor_configs ({columns}) VALUES ({placeholders})",
            values
        )
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("监控配置已创建, id=%d, name=%s", new_id, data.get("name", ""))
        return new_id
    finally:
        conn.close()


def update_monitor_config(db, config_id: int, data: dict) -> bool:
    """
    更新指定 id 的监控配置，返回是否成功（即是否存在该记录）。
    JSON 字段自动序列化，updated_at 由 MySQL 自动维护。
    """
    record = _serialize_json(data)
    # 不允许修改 id 和时间戳
    record.pop("id", None)
    record.pop("created_at", None)
    record.pop("updated_at", None)

    if not record:
        return False

    set_clause = ", ".join([f"{k} = %s" for k in record.keys()])
    values = list(record.values()) + [config_id]

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"UPDATE monitor_configs SET {set_clause} WHERE id = %s",
            values
        )
        conn.commit()
        # 检查记录是否存在（rowcount=0 可能是数据未变化，不代表不存在）
        cursor.execute("SELECT id FROM monitor_configs WHERE id = %s", (config_id,))
        exists = cursor.fetchone() is not None
        if exists:
            logger.info("监控配置已更新, id=%d", config_id)
        return exists
    finally:
        conn.close()


def delete_monitor_config(db, config_id: int) -> bool:
    """删除指定 id 的监控配置，返回是否成功（即是否存在该记录）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "DELETE FROM monitor_configs WHERE id = %s",
            (config_id,)
        )
        conn.commit()
        affected = cursor.rowcount
        if affected > 0:
            logger.info("监控配置已删除, id=%d", config_id)
        return affected > 0
    finally:
        conn.close()


def get_enabled_monitor_configs(db) -> list:
    """获取所有启用的监控配置（enabled=1），按 id 降序排列。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM monitor_configs WHERE enabled = 1 ORDER BY id DESC"
        )
        rows = cursor.fetchall()
        return [_deserialize_json(row) for row in rows]
    finally:
        conn.close()


def list_monitor_configs_by_enterprise(db, enterprise_id) -> list:
    """按企业查其名下全部监控配置（不限 enabled），按 id 降序排列。
    供识别群自动匹配判重/选目标配置使用。enterprise_id 为空返回空列表。
    """
    if enterprise_id is None:
        return []
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM monitor_configs WHERE enterprise_id = %s ORDER BY id DESC",
            (enterprise_id,)
        )
        rows = cursor.fetchall()
        return [_deserialize_json(row) for row in rows]
    finally:
        conn.close()


def migrate_from_watch_list(db):
    """
    一次性迁移：将旧 insurance_config 中的 watch_list + dingtalk_targets 迁移到 monitor_configs 表。
    如果 monitor_configs 已有数据则跳过（说明已迁移过）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT COUNT(*) as cnt FROM monitor_configs")
        if cursor.fetchone()["cnt"] > 0:
            return  # 已有数据，跳过迁移

        # 读取旧配置
        cursor.execute("SELECT config_value FROM insurance_config WHERE config_key = 'watch_list'")
        row = cursor.fetchone()
        if not row:
            return
        try:
            watch_list = json.loads(row["config_value"])
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(watch_list, list) or not watch_list:
            return

        # 读取旧钉钉目标
        cursor.execute("SELECT config_value FROM insurance_config WHERE config_key = 'dingtalk_targets'")
        row2 = cursor.fetchone()
        dingtalk_targets = []
        if row2:
            try:
                dingtalk_targets = json.loads(row2["config_value"])
            except (TypeError, json.JSONDecodeError):
                pass

        target_map = {t["id"]: t for t in dingtalk_targets if isinstance(t, dict) and t.get("id")}

        # 按 target_ids 分组：相同 target_ids 组合的监控项合并为一条 monitor_config
        from collections import defaultdict
        groups = defaultdict(lambda: {"rooms": [], "users": []})

        for w in watch_list:
            if not isinstance(w, dict) or not w.get("id"):
                continue
            tids = tuple(sorted(w.get("target_ids", [])))
            if w.get("type") == "room":
                groups[tids]["rooms"].append({"id": w["id"], "name": w.get("name", w["id"])})
            elif w.get("type") == "user":
                groups[tids]["users"].append({"id": w["id"], "name": w.get("name", w["id"])})

        # 为每个分组创建一条 monitor_config
        idx = 0
        for tids, sources in groups.items():
            idx += 1
            doc_url, base_id, sheet_id = "", "", ""
            if tids and tids[0] in target_map:
                t = target_map[tids[0]]
                doc_url = t.get("doc_url", "")
                base_id = t.get("base_id", "")
                sheet_id = t.get("sheet_id", "")

            cursor.execute("""
                INSERT INTO monitor_configs (name, enabled, rooms, users,
                    dingtalk_doc_url, dingtalk_base_id, dingtalk_sheet_id, field_mapping)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s)
            """, (
                f"迁移配置{idx}",
                json.dumps(sources["rooms"], ensure_ascii=False),
                json.dumps(sources["users"], ensure_ascii=False),
                doc_url, base_id, sheet_id,
                json.dumps({}, ensure_ascii=False),
            ))

        conn.commit()
        if idx > 0:
            logger.info("已从旧 watch_list 迁移 %d 条监控配置到 monitor_configs", idx)
    except Exception as e:
        logger.warning("迁移旧监控配置失败（可忽略）: %s", e)
    finally:
        conn.close()

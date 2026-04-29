"""
账号模块 — 数据库层
负责 users 表的建表与 CRUD 操作
"""

import logging
from datetime import datetime

import pymysql
import pymysql.cursors
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# 角色常量
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ENTERPRISE = "enterprise"
ROLE_EMPLOYEE = "employee"


def init_users_table(db):
    """
    创建 users 表（幂等）+ 初始超管账号。
    db 是 storage.db.Database 实例。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id           INT PRIMARY KEY AUTO_INCREMENT,
                phone        VARCHAR(64) NOT NULL UNIQUE COMMENT '手机号（登录凭证）',
                password_hash VARCHAR(256) NOT NULL,
                name         VARCHAR(128) DEFAULT '' COMMENT '姓名',
                role         VARCHAR(32) NOT NULL DEFAULT 'employee',
                parent_id    INT DEFAULT NULL COMMENT '所属企业账号ID，员工必填',
                enabled      TINYINT NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_users_role (role),
                INDEX idx_users_parent (parent_id)
            )
        """)

        # 兼容旧表：username → phone, display_name → name
        for old_col, new_col, col_def in [
            ("username", "phone", "VARCHAR(64) NOT NULL COMMENT '手机号（登录凭证）'"),
            ("display_name", "name", "VARCHAR(128) DEFAULT '' COMMENT '姓名'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users CHANGE COLUMN {old_col} {new_col} {col_def}")
                logger.info("users 表列 %s → %s 迁移完成", old_col, new_col)
            except Exception:
                pass  # 列不存在或已迁移

        # 初始超管账号（仅在表为空时插入）
        cursor.execute("SELECT COUNT(*) AS cnt FROM users")
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "INSERT INTO users (phone, password_hash, role, name) VALUES (%s, %s, %s, %s)",
                ("admin", generate_password_hash("admin123"), ROLE_SUPER_ADMIN, "超级管理员"),
            )
            logger.info("已创建默认超管账号: admin / admin123")

        conn.commit()
        logger.info("users 表初始化完成")
    finally:
        conn.close()


def get_user_by_phone(db, phone: str) -> dict | None:
    """根据手机号查找用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE phone = %s", (phone,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_by_id(db, user_id: int) -> dict | None:
    """根据 ID 查找用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT id, phone, role, parent_id, name, enabled, created_at, updated_at FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def verify_password(user: dict, password: str) -> bool:
    """校验密码"""
    return check_password_hash(user["password_hash"], password)


def list_users(db, role: str = "", parent_id: int = None) -> list:
    """查询用户列表，可按角色/所属企业筛选"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = "SELECT id, phone, role, parent_id, name, enabled, created_at, updated_at FROM users WHERE 1=1"
        params = []
        if role:
            sql += " AND role = %s"
            params.append(role)
        if parent_id is not None:
            sql += " AND parent_id = %s"
            params.append(parent_id)
        sql += " ORDER BY created_at DESC"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            for f in ("created_at", "updated_at"):
                if row.get(f) and not isinstance(row[f], str):
                    row[f] = str(row[f])
        return rows
    finally:
        conn.close()


def create_user(db, phone: str, password: str, role: str, parent_id: int = None, name: str = "") -> int:
    """创建用户，返回新用户 ID"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "INSERT INTO users (phone, password_hash, role, parent_id, name) VALUES (%s, %s, %s, %s, %s)",
            (phone, generate_password_hash(password), role, parent_id, name),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_user(db, user_id: int, data: dict):
    """更新用户信息（支持 name, role, parent_id, enabled）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        allowed = {"name", "role", "parent_id", "enabled"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [user_id]
        cursor.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)
        conn.commit()
    finally:
        conn.close()


def reset_password(db, user_id: int, new_password: str):
    """重置密码"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_password), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_enterprise_employee_ids(db, enterprise_id: int) -> list[int]:
    """获取企业下所有员工 ID（含企业自身）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT id FROM users WHERE parent_id = %s", (enterprise_id,))
        ids = [row["id"] for row in cursor.fetchall()]
        ids.append(enterprise_id)
        return ids
    finally:
        conn.close()

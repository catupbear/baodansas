"""
账号模块 — 数据库层
负责 users 表的建表与 CRUD 操作
"""

import logging
from datetime import datetime

import pymysql
import pymysql.cursors
from werkzeug.security import generate_password_hash as _gen_hash, check_password_hash


def generate_password_hash(password: str) -> str:
    """使用 pbkdf2:sha256 生成密码哈希（兼容低配服务器，scrypt 可能极慢）"""
    return _gen_hash(password, method="pbkdf2:sha256")

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


def init_enterprises_table(db):
    """
    创建 enterprises 表（幂等）。
    db 是 storage.db.Database 实例。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS enterprises (
                id             INT PRIMARY KEY AUTO_INCREMENT,
                name           VARCHAR(128) NOT NULL COMMENT '企业名称',
                contact_person VARCHAR(64) DEFAULT '' COMMENT '联系人',
                contact_phone  VARCHAR(64) DEFAULT '' COMMENT '联系电话',
                enabled        TINYINT NOT NULL DEFAULT 1,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_enterprises_name (name)
            )
        """)
        conn.commit()
        logger.info("enterprises 表初始化完成")
    finally:
        conn.close()


def list_enterprises(db, enabled: int = None) -> list:
    """查询企业列表，可按启用状态筛选"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = "SELECT * FROM enterprises WHERE 1=1"
        params = []
        if enabled is not None:
            sql += " AND enabled = %s"
            params.append(enabled)
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


def get_enterprise_by_id(db, enterprise_id: int) -> dict | None:
    """根据 ID 查找企业"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM enterprises WHERE id = %s", (enterprise_id,))
        row = cursor.fetchone()
        if row:
            for f in ("created_at", "updated_at"):
                if row.get(f) and not isinstance(row[f], str):
                    row[f] = str(row[f])
        return row
    finally:
        conn.close()


def create_enterprise(db, name: str, contact_person: str = "", contact_phone: str = "") -> int:
    """创建企业，返回新企业 ID"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "INSERT INTO enterprises (name, contact_person, contact_phone) VALUES (%s, %s, %s)",
            (name, contact_person, contact_phone),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_enterprise(db, enterprise_id: int, data: dict):
    """更新企业信息（支持 name, contact_person, contact_phone, enabled）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        allowed = {"name", "contact_person", "contact_phone", "enabled"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [enterprise_id]
        cursor.execute(f"UPDATE enterprises SET {set_clause} WHERE id = %s", values)
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


# ============================================================
# 发送人绑定（user_sender_binding）
# ============================================================

def init_sender_binding_table(db):
    """
    创建 user_sender_binding 表（幂等）。
    记录员工账号与企业微信发送人的绑定关系。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sender_binding (
                id            INT PRIMARY KEY AUTO_INCREMENT,
                user_id       INT NOT NULL COMMENT '用户ID → users.id',
                sender        VARCHAR(64) NOT NULL COMMENT '企业微信发送人ID',
                sender_name   VARCHAR(128) DEFAULT '' COMMENT '发送人显示名',
                enterprise_id INT DEFAULT NULL COMMENT '企业ID',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_sender_ent (sender, enterprise_id),
                INDEX idx_binding_user (user_id)
            )
        """)
        conn.commit()
        logger.info("user_sender_binding 表初始化完成")
    finally:
        conn.close()


def get_bindings_by_user(db, user_id: int) -> list:
    """查询某用户的所有发送人绑定"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, user_id, sender, sender_name, enterprise_id, created_at "
            "FROM user_sender_binding WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            if row.get("created_at") and not isinstance(row["created_at"], str):
                row["created_at"] = str(row["created_at"])
        return rows
    except pymysql.err.ProgrammingError as e:
        if e.args[0] == 1146:
            return []
        raise
    finally:
        conn.close()


def get_binding_by_sender(db, sender: str, enterprise_id: int = None) -> dict | None:
    """
    按 sender 查找绑定的用户。
    如果传了 enterprise_id 则精确匹配，否则先匹配 enterprise_id 为 NULL 的全局绑定。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if enterprise_id is not None:
            # 先精确匹配企业，再兜底查全局绑定
            cursor.execute(
                "SELECT * FROM user_sender_binding WHERE sender = %s AND enterprise_id = %s LIMIT 1",
                (sender, enterprise_id),
            )
            row = cursor.fetchone()
            if row:
                return row
        # 查全局绑定（enterprise_id IS NULL）或任意匹配
        cursor.execute(
            "SELECT * FROM user_sender_binding WHERE sender = %s ORDER BY enterprise_id IS NULL, id LIMIT 1",
            (sender,),
        )
        return cursor.fetchone()
    except pymysql.err.ProgrammingError as e:
        if e.args[0] == 1146:
            return None
        raise
    finally:
        conn.close()


def set_user_bindings(db, user_id: int, senders: list, enterprise_id: int = None):
    """
    全量设置用户的发送人绑定列表（先删后插）。
    senders: [{"sender": "xxx", "sender_name": "xxx"}, ...] 或 ["sender_id", ...]
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 删除该用户的旧绑定
        cursor.execute("DELETE FROM user_sender_binding WHERE user_id = %s", (user_id,))
        # 插入新绑定
        for item in senders:
            if isinstance(item, dict):
                sender = item.get("sender", "")
                sender_name = item.get("sender_name", "")
            else:
                sender = str(item)
                sender_name = ""
            if not sender:
                continue
            try:
                cursor.execute(
                    "INSERT INTO user_sender_binding (user_id, sender, sender_name, enterprise_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_id, sender, sender_name, enterprise_id),
                )
            except pymysql.err.IntegrityError:
                logger.warning("发送人 %s 已绑定其他用户，跳过", sender)
        conn.commit()
    finally:
        conn.close()


def get_sender_user_name_map(db, enterprise_id: int = None) -> dict:
    """
    查询 sender → 用户姓名 的映射表。
    通过 user_sender_binding JOIN users 表，返回 {sender: user_name} 字典。
    用于列表/导出时实时关联"跟单人"字段。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if enterprise_id is not None:
            cursor.execute(
                "SELECT b.sender, u.name AS user_name "
                "FROM user_sender_binding b JOIN users u ON b.user_id = u.id "
                "WHERE b.enterprise_id = %s",
                (enterprise_id,),
            )
        else:
            cursor.execute(
                "SELECT b.sender, u.name AS user_name "
                "FROM user_sender_binding b JOIN users u ON b.user_id = u.id"
            )
        return {row["sender"]: row["user_name"] for row in cursor.fetchall() if row.get("user_name")}
    except pymysql.err.ProgrammingError as e:
        if e.args[0] == 1146:
            return {}
        raise
    finally:
        conn.close()


# ============================================================
# 短信验证码（sms_verifications）
# ============================================================

def init_sms_verification_table(db):
    """创建短信验证码表（幂等）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sms_verifications (
                id         INT PRIMARY KEY AUTO_INCREMENT,
                phone      VARCHAR(20) NOT NULL,
                code       VARCHAR(10) NOT NULL,
                purpose    VARCHAR(32) NOT NULL DEFAULT 'register',
                used       TINYINT NOT NULL DEFAULT 0,
                expires_at DATETIME NOT NULL,
                sent_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_sms_phone_purpose (phone, purpose)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
        logger.info("sms_verifications 表初始化完成")
    finally:
        conn.close()


def save_verification_code(db, phone: str, code: str, purpose: str = "register", ttl: int = 300):
    """保存验证码，ttl 秒后过期"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sms_verifications (phone, code, purpose, expires_at) "
            "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s SECOND))",
            (phone, code, purpose, ttl),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_sms_sent_at(db, phone: str, purpose: str = "register"):
    """返回该手机号最近一次发送时间（datetime 或 None）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT sent_at FROM sms_verifications WHERE phone = %s AND purpose = %s "
            "ORDER BY id DESC LIMIT 1",
            (phone, purpose),
        )
        row = cursor.fetchone()
        return row["sent_at"] if row else None
    finally:
        conn.close()


def verify_sms_code(db, phone: str, code: str, purpose: str = "register") -> bool:
    """
    验证验证码。成功则标记为已用，返回 True；失败返回 False。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id FROM sms_verifications "
            "WHERE phone = %s AND code = %s AND purpose = %s AND used = 0 AND expires_at > NOW() "
            "ORDER BY id DESC LIMIT 1",
            (phone, code, purpose),
        )
        row = cursor.fetchone()
        if not row:
            return False
        cursor.execute("UPDATE sms_verifications SET used = 1 WHERE id = %s", (row["id"],))
        conn.commit()
        return True
    finally:
        conn.close()


def list_all_bindings(db, enterprise_id: int = None) -> list:
    """列出所有绑定记录（前端用户列表展示用）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if enterprise_id is not None:
            cursor.execute(
                "SELECT * FROM user_sender_binding WHERE enterprise_id = %s ORDER BY user_id, created_at",
                (enterprise_id,),
            )
        else:
            cursor.execute("SELECT * FROM user_sender_binding ORDER BY user_id, created_at")
        rows = cursor.fetchall()
        for row in rows:
            if row.get("created_at") and not isinstance(row["created_at"], str):
                row["created_at"] = str(row["created_at"])
        return rows
    except pymysql.err.ProgrammingError as e:
        if e.args[0] == 1146:  # Table doesn't exist
            logger.warning("user_sender_binding 表不存在，请重启服务自动建表")
            return []
        raise
    finally:
        conn.close()

"""
账号模块 — 数据库层
负责 users 表的建表与 CRUD 操作
"""

import json
import logging
import secrets
import string
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
ROLE_SALES = "sales"              # 兼容旧数据
ROLE_CHANNEL_SALES = "channel_sales"   # 渠道销售
ROLE_PERSONAL_SALES = "personal_sales" # 个人销售


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
                activated    TINYINT NOT NULL DEFAULT 0 COMMENT '是否已激活（自助注册需管理员激活）',
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
                "INSERT INTO users (phone, password_hash, role, name, activated) VALUES (%s, %s, %s, %s, 1)",
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
        cursor.execute("SELECT id, phone, role, parent_id, name, enabled, activated, is_sales, sales_type, created_at, updated_at FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def verify_password(user: dict, password: str) -> bool:
    """校验密码"""
    return check_password_hash(user["password_hash"], password)


def list_users(db, role: str = "", parent_id: int = None, sales_type: str = "") -> list:
    """查询用户列表，可按角色/所属企业/销售类型筛选"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = "SELECT id, phone, role, parent_id, name, enabled, activated, is_sales, sales_type, created_at, updated_at FROM users WHERE 1=1"
        params = []
        if role:
            sql += " AND role = %s"
            params.append(role)
        if sales_type:
            sql += " AND sales_type = %s"
            params.append(sales_type)
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


def _generate_referral_code() -> str:
    """生成8位大写字母+数字邀请码"""
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(8))


def create_user(db, phone: str, password: str, role: str, parent_id: int = None, name: str = "", referrer_id: int = None, is_sales: bool = False, activated: int = 1) -> int:
    """创建用户，返回新用户 ID。activated 默认 1（管理员创建即激活），自助注册传 0。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 生成唯一邀请码
        referral_code = None
        for _ in range(10):
            code = _generate_referral_code()
            cursor.execute("SELECT id FROM users WHERE referral_code = %s", (code,))
            if not cursor.fetchone():
                referral_code = code
                break
        cursor.execute(
            "INSERT INTO users (phone, password_hash, role, parent_id, name, referral_code, referrer_id, is_sales, sales_type, activated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (phone, generate_password_hash(password), role, parent_id, name, referral_code, referrer_id, 1 if is_sales else 0, "", 1 if activated else 0),
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
        allowed = {"name", "role", "parent_id", "enabled", "is_sales", "sales_type", "activated", "renewal_enabled"}
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


def delete_user(db, user_id: int):
    """删除用户（同时清理绑定关系）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        # 清理发送人绑定（表名为 user_sender_binding；表不存在时忽略，不阻塞删除）
        try:
            cursor.execute("DELETE FROM user_sender_binding WHERE user_id = %s", (user_id,))
        except Exception:
            pass
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
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
                enterprise_no  VARCHAR(50) DEFAULT '' COMMENT '企业编号（可编辑业务编号）',
                survey_token   VARCHAR(64) DEFAULT '' COMMENT '企业信息收集表专属链接token',
                survey_data    TEXT COMMENT '企业信息收集表提交内容(JSON)',
                contact_person VARCHAR(64) DEFAULT '' COMMENT '联系人',
                contact_phone  VARCHAR(64) DEFAULT '' COMMENT '联系电话',
                enabled        TINYINT NOT NULL DEFAULT 1,
                merge_by_plate TINYINT NOT NULL DEFAULT 0 COMMENT '识别记录默认按车牌合并显示',
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_enterprises_name (name)
            )
        """)
        conn.commit()
        logger.info("enterprises 表初始化完成")
    finally:
        conn.close()


def init_enterprise_follow_status_column(db):
    """幂等：给 enterprises 表加 follow_status 列（客户跟进状态，如"正常使用"/"待使用"/"无使用（体量太小）"）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER TABLE enterprises ADD COLUMN follow_status VARCHAR(64) DEFAULT '' COMMENT '客户跟进状态'")
            logger.info("enterprises 表添加列 follow_status 完成")
        except Exception:
            pass  # 列已存在，跳过
    finally:
        conn.close()


def init_enterprise_remark_column(db):
    """幂等：给 enterprises 表加 remark 列（客户档案备注，销售跟进记录用）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER TABLE enterprises ADD COLUMN remark TEXT COMMENT '客户档案备注'")
            logger.info("enterprises 表添加列 remark 完成")
        except Exception:
            pass  # 列已存在，跳过
    finally:
        conn.close()


def init_enterprise_onboarded_column(db):
    """幂等：给 enterprises 表加 onboarded_at 列（正式接入时间）。
    "预备群X"占位企业创建时不记录，等改名为正式客户名称后才首次写入(见 update_enterprise)。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER TABLE enterprises ADD COLUMN onboarded_at DATETIME DEFAULT NULL COMMENT '正式接入时间(预备群改名后才记录)'")
            logger.info("enterprises 表添加列 onboarded_at 完成")
        except Exception:
            pass  # 列已存在，跳过
    finally:
        conn.close()


def backfill_enterprise_onboarded_at(db):
    """一次性回填：本来就不是"预备群"占位的老企业，用创建时间兜底接入时间"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE enterprises SET onboarded_at = created_at "
            "WHERE onboarded_at IS NULL AND name NOT LIKE '预备群%'"
        )
        conn.commit()
    except Exception:
        logger.exception("回填 enterprises.onboarded_at 失败")
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
            for f in ("created_at", "updated_at", "plan_start_at", "next_plan_start_at", "onboarded_at"):
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
            for f in ("created_at", "updated_at", "plan_start_at", "next_plan_start_at", "onboarded_at"):
                if row.get(f) and not isinstance(row[f], str):
                    row[f] = str(row[f])
        return row
    finally:
        conn.close()


def get_enterprise_admin_user(db, enterprise_id: int) -> dict | None:
    """获取企业的管理员账号（role=enterprise 且 parent_id=该企业ID）。
    用于超管代该企业查看/导出数据时，按该企业自己的模板配置（费率公式/固定值等）解析，
    而不是误用超管自己的账号配置。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, phone, role, parent_id FROM users WHERE role = %s AND parent_id = %s "
            "ORDER BY id ASC LIMIT 1",
            (ROLE_ENTERPRISE, enterprise_id)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def create_enterprise(db, name: str, contact_person: str = "", contact_phone: str = "") -> int:
    """创建企业，返回新企业 ID。
    "预备群"开头的占位企业不记录接入时间(onboarded_at 留空)，等改名为正式客户名称后才首次写入；
    非"预备群"开头则视为直接正式接入，创建时立即记录。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        onboarded_at = None if name.startswith("预备群") else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # plan_type 显式置空串表示"未开通"：列为 NOT NULL DEFAULT 'standard'，
        # 若不写该列，数据库会默认成 standard，导致新企业被自动开通。开通由管理员后续操作。
        cursor.execute(
            "INSERT INTO enterprises (name, contact_person, contact_phone, plan_type, onboarded_at) VALUES (%s, %s, %s, '', %s)",
            (name, contact_person, contact_phone, onboarded_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_enterprise(db, enterprise_id: int, data: dict):
    """更新企业信息"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        allowed = {"name", "enterprise_no", "contact_person", "contact_phone", "enabled", "merge_by_plate", "renewal_enabled", "plan_type", "plan_months", "plan_start_at", "next_plan_type", "next_plan_months", "next_plan_start_at", "referrer_id", "survey_token", "survey_data", "follow_status", "remark", "onboarded_at"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return None
        # "预备群"占位企业改名为正式客户名称时，首次记录正式接入时间(onboarded_at)
        if "name" in fields and not fields["name"].startswith("预备群"):
            cursor.execute("SELECT name, onboarded_at FROM enterprises WHERE id = %s", (enterprise_id,))
            row = cursor.fetchone()
            if row and (row.get("name") or "").startswith("预备群") and not row.get("onboarded_at"):
                fields["onboarded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 跟进状态改为"正常使用"时，接入时间实时刷新为当前时间
        if fields.get("follow_status") == "正常使用":
            fields["onboarded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [enterprise_id]
        cursor.execute(f"UPDATE enterprises SET {set_clause} WHERE id = %s", values)
        conn.commit()
        return fields
    finally:
        conn.close()


def delete_enterprise(db, enterprise_id: int):
    """删除企业"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM enterprises WHERE id = %s", (enterprise_id,))
        conn.commit()
    finally:
        conn.close()


def get_enterprise_by_survey_token(db, token: str):
    """按信息收集表 token 查企业。"""
    if not token:
        return None
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM enterprises WHERE survey_token = %s", (token,))
        return cursor.fetchone()
    finally:
        conn.close()


def ensure_survey_token(db, enterprise_id: int) -> str:
    """确保企业有信息收集表 token，没有则生成并保存，返回 token。"""
    import secrets
    ent = get_enterprise_by_id(db, enterprise_id)
    if not ent:
        return ""
    token = (ent.get("survey_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
        update_enterprise(db, enterprise_id, {"survey_token": token})
    return token


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


def _get_binding_by_sender_once(db, sender: str, enterprise_id: int = None) -> dict | None:
    """单次查询，供 get_binding_by_sender 调用（含重试时复用）。"""
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


def get_binding_by_sender(db, sender: str, enterprise_id: int = None) -> dict | None:
    """
    按 sender 查找绑定的用户。
    如果传了 enterprise_id 则精确匹配，否则先匹配 enterprise_id 为 NULL 的全局绑定。

    2026-07-14：曾出现过一段时间内大批量已绑定sender的查询全部返回空（无异常日志、
    重启服务后自愈，怀疑连接池给到了异常/过期连接）导致对应记录user_id未正确关联，
    事后手动回填过497条历史数据。这里加一次"空结果时用全新连接重试"的保险，命中
    不一致时打WARNING留痕，方便下次复现时第一时间定位，不改变正常场景下的行为。
    """
    result = _get_binding_by_sender_once(db, sender, enterprise_id)
    if result is None:
        retry_result = _get_binding_by_sender_once(db, sender, enterprise_id)
        if retry_result is not None:
            logger.warning(
                "get_binding_by_sender: sender=%s(enterprise_id=%s) 首次查询为空但重试后命中"
                "(user_id=%s)——疑似连接池返回了异常连接，本次已用重试结果",
                sender, enterprise_id, retry_result.get("user_id"),
            )
            return retry_result
    return result


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


def get_sender_alias_map(db, enterprise_id: int = None) -> dict:
    """
    查询 sender → 绑定别名(sender_name) 的映射表。
    管理员在「绑定发送人」处为每个 sender 设置的别名，用于列表/导出时覆盖记录的发送人显示名。
    仅返回 sender_name 非空的绑定。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if enterprise_id is not None:
            cursor.execute(
                "SELECT sender, sender_name FROM user_sender_binding "
                "WHERE enterprise_id = %s AND sender_name IS NOT NULL AND sender_name != ''",
                (enterprise_id,),
            )
        else:
            cursor.execute(
                "SELECT sender, sender_name FROM user_sender_binding "
                "WHERE sender_name IS NOT NULL AND sender_name != ''"
            )
        return {row["sender"]: row["sender_name"] for row in cursor.fetchall() if row.get("sender_name")}
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


def sms_in_cooldown(db, phone: str, purpose: str, cooldown_seconds: int) -> int:
    """
    检查手机号是否仍在冷却期内（时间比较在 MySQL 侧完成，规避时区问题）。
    返回剩余冷却秒数（>0 表示冷却中，0 表示可发送）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT GREATEST(0, %s - TIMESTAMPDIFF(SECOND, sent_at, NOW())) AS remaining "
            "FROM sms_verifications "
            "WHERE phone = %s AND purpose = %s "
            "ORDER BY id DESC LIMIT 1",
            (cooldown_seconds, phone, purpose),
        )
        row = cursor.fetchone()
        return int(row["remaining"]) if row else 0
    finally:
        conn.close()


def try_insert_sms_record(db, phone: str, code: str, purpose: str,
                          cooldown_seconds: int, ttl: int) -> int:
    """
    原子操作：仅当冷却期已过时才插入验证码记录（在 MySQL 侧完成检查+插入，防并发）。
    返回 0 表示插入成功（可以发短信），>0 表示剩余冷却秒数（不可发送）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 先检查冷却（同一连接内，避免跨连接竞态）
        cursor.execute(
            "SELECT GREATEST(0, %s - TIMESTAMPDIFF(SECOND, sent_at, NOW())) AS remaining "
            "FROM sms_verifications WHERE phone = %s AND purpose = %s "
            "ORDER BY id DESC LIMIT 1",
            (cooldown_seconds, phone, purpose),
        )
        row = cursor.fetchone()
        remaining = int(row["remaining"]) if row else 0
        if remaining > 0:
            return remaining
        # 冷却已过，插入记录（先占位，再发短信，防止并发重复发送）
        cursor.execute(
            "INSERT INTO sms_verifications (phone, code, purpose, expires_at) "
            "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s SECOND))",
            (phone, code, purpose, ttl),
        )
        conn.commit()
        return 0
    finally:
        conn.close()


def delete_sms_record(db, phone: str, code: str, purpose: str):
    """发短信失败时回滚：删除刚才插入的记录，让用户可以重试"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM sms_verifications WHERE phone=%s AND code=%s AND purpose=%s "
            "ORDER BY id DESC LIMIT 1",
            (phone, code, purpose),
        )
        conn.commit()
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


def init_is_sales_column(db):
    """为 users 表添加 is_sales / sales_type 字段（幂等迁移）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        for col, definition in [
            ("is_sales", "TINYINT NOT NULL DEFAULT 0 COMMENT '是否同时拥有销售权限（兼容旧数据）'"),
            ("sales_type", "VARCHAR(32) NOT NULL DEFAULT '' COMMENT '附加销售类型: channel/personal'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                logger.info("users 表添加列 %s 完成", col)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def init_users_renewal_column(db):
    """为 users 表添加 renewal_enabled 字段（幂等迁移）。
    续保提醒功能改为按个人账号自助开启（不是按企业），每个员工/企业管理员账号
    自己点"开启功能"才会：(1) 自己的续保管理系统页面解除磨砂/打码 (2) 自己跟单的保单
    才会参与续保任务扫描和到期提醒推送。互不影响，同企业下没开的员工不受影响。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN renewal_enabled TINYINT NOT NULL DEFAULT 0 "
                "COMMENT '是否已自助开启续保提醒功能(按账号，非按企业)'"
            )
            logger.info("users 表添加列 renewal_enabled 完成")
        except Exception:
            pass  # 列已存在，跳过
        conn.commit()
    finally:
        conn.close()


def init_activated_column(db):
    """为 users 表添加 activated 字段（幂等迁移）。
    首次添加时把所有存量用户置为已激活（activated=1），
    此后仅新自助注册的用户为未激活（activated=0），需管理员在账号管理激活。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN activated TINYINT NOT NULL DEFAULT 0 "
                "COMMENT '是否已激活（自助注册需管理员激活）'"
            )
            # 列首次创建：存量用户全部视为已激活，避免现有用户被锁在外面
            cursor.execute("UPDATE users SET activated = 1")
            conn.commit()
            logger.info("users 表添加列 activated 完成，存量用户已全部置为已激活")
        except Exception:
            pass  # 列已存在，跳过（不再重复激活）
    finally:
        conn.close()


def init_enterprise_plan_column(db):
    """为 enterprises 表添加套餐相关字段（幂等迁移）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        for col, definition in [
            ("plan_type", "VARCHAR(32) NOT NULL DEFAULT 'standard' COMMENT '套餐类型: trial/standard/enterprise'"),
            ("plan_months", "INT NOT NULL DEFAULT 1 COMMENT '套餐时长（月）'"),
            ("plan_start_at", "DATETIME DEFAULT NULL COMMENT '套餐开始时间'"),
            ("next_plan_type", "VARCHAR(32) DEFAULT NULL COMMENT '下一个套餐类型'"),
            ("next_plan_months", "INT DEFAULT NULL COMMENT '下一个套餐时长（月）'"),
            ("next_plan_start_at", "DATETIME DEFAULT NULL COMMENT '下一个套餐开始时间'"),
            ("referrer_id", "INT DEFAULT NULL COMMENT '推荐人用户ID'"),
            ("enterprise_no", "VARCHAR(50) DEFAULT '' COMMENT '企业编号（可编辑业务编号）'"),
            ("survey_token", "VARCHAR(64) DEFAULT '' COMMENT '企业信息收集表专属链接token'"),
            ("survey_data", "TEXT COMMENT '企业信息收集表提交内容(JSON)'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE enterprises ADD COLUMN {col} {definition}")
                logger.info("enterprises 表添加列 %s 完成", col)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def init_referral_columns(db):
    """为 users 表添加邀请码字段（幂等迁移）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        for col, definition in [
            ("referral_code", "VARCHAR(16) NULL UNIQUE COMMENT '邀请码'"),
            ("referrer_id", "INT NULL COMMENT '邀请人ID'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                logger.info("users 表添加列 %s 完成", col)
            except Exception:
                pass  # 已存在
        conn.commit()
    finally:
        conn.close()


def get_user_by_referral_code(db, code: str) -> dict | None:
    """根据邀请码查找用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT id, phone, role, name, referral_code FROM users WHERE referral_code = %s AND enabled = 1", (code,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_referral_downlines(db, user_id: int) -> list:
    """获取该用户的直接下线列表"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, phone, name, created_at FROM users WHERE referrer_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            if row.get("created_at") and not isinstance(row["created_at"], str):
                row["created_at"] = str(row["created_at"])
            # 手机号脱敏
            p = row.get("phone", "")
            if len(p) == 11:
                row["phone_masked"] = p[:3] + "****" + p[7:]
            else:
                row["phone_masked"] = p
        return rows
    finally:
        conn.close()


def get_or_create_referral_code(db, user_id: int) -> str:
    """获取用户邀请码，若不存在则自动生成并写入"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT referral_code FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return ""
        if row.get("referral_code"):
            return row["referral_code"]
        # 旧账号没有邀请码，补生成
        for _ in range(10):
            code = _generate_referral_code()
            cursor.execute("SELECT id FROM users WHERE referral_code = %s", (code,))
            if not cursor.fetchone():
                cursor.execute("UPDATE users SET referral_code = %s WHERE id = %s", (code, user_id))
                conn.commit()
                return code
        return ""
    finally:
        conn.close()


def check_enterprise_quota(db, enterprise_id: int) -> dict:
    """
    检查企业配额状态，同时检查套餐是否到期。
    返回字段：exceeded / expired / plan_type / usage / limit / remaining
    """
    ent = get_enterprise_by_id(db, enterprise_id)
    if not ent:
        return {"exceeded": False, "expired": False, "plan_type": None}

    # 自动激活排队套餐
    if ent.get("next_plan_type") and ent.get("next_plan_start_at"):
        conn2 = db.pool.connection()
        try:
            cur2 = conn2.cursor(pymysql.cursors.DictCursor)
            cur2.execute(
                "SELECT TIMESTAMPDIFF(SECOND, %s, NOW()) AS over_sec",
                (ent["next_plan_start_at"],),
            )
            r2 = cur2.fetchone()
            if r2 and r2["over_sec"] is not None and r2["over_sec"] >= 0:
                # 下一个套餐已到开始时间，自动激活
                cur2.execute(
                    "UPDATE enterprises SET plan_type=%s, plan_months=%s, plan_start_at=%s, "
                    "next_plan_type=NULL, next_plan_months=NULL, next_plan_start_at=NULL WHERE id=%s",
                    (ent["next_plan_type"], ent["next_plan_months"], ent["next_plan_start_at"], enterprise_id),
                )
                conn2.commit()
                # 重新读取企业信息
                ent = get_enterprise_by_id(db, enterprise_id)
        finally:
            conn2.close()

    plan_type = ent.get("plan_type", "standard")

    # 套餐到期检查（在 MySQL 侧完成，规避时区问题）
    expired = False
    if ent.get("plan_start_at"):
        conn = db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT TIMESTAMPDIFF(SECOND, DATE_ADD(%s, INTERVAL %s MONTH), NOW()) AS over_sec",
                (ent["plan_start_at"], ent.get("plan_months", 1) or 1),
            )
            row = cursor.fetchone()
            if row and row["over_sec"] is not None and row["over_sec"] > 0:
                expired = True
        finally:
            conn.close()

    if expired:
        return {"exceeded": False, "expired": True, "plan_type": plan_type}

    # 企业版/企业试用：不限额度
    if plan_type in ("enterprise", "enterprise_trial"):
        return {"exceeded": False, "expired": False, "plan_type": plan_type}

    # 标准版：检查月度额度
    usage = get_enterprise_monthly_pdf_usage(db, enterprise_id)
    remaining = max(0, 3000 - usage)
    return {
        "exceeded": usage >= 3000,
        "expired": False,
        "plan_type": plan_type,
        "usage": usage,
        "limit": 3000,
        "remaining": remaining,
    }


def get_enterprise_monthly_pdf_usage(db, enterprise_id: int) -> int:
    """获取指定企业当月PDF识别使用量"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM insurance_records "
            "WHERE enterprise_id = %s "
            "AND YEAR(created_at) = YEAR(NOW()) "
            "AND MONTH(created_at) = MONTH(NOW()) "
            "AND deleted_at IS NULL",
            (enterprise_id,)
        )
        row = cursor.fetchone()
        return row["cnt"] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def get_monthly_pdf_usage_batch(db) -> dict:
    """批量获取所有企业当月PDF识别使用量，返回 {enterprise_id: count}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT enterprise_id, COUNT(*) as cnt FROM insurance_records "
            "WHERE enterprise_id IS NOT NULL "
            "AND YEAR(created_at) = YEAR(NOW()) "
            "AND MONTH(created_at) = MONTH(NOW()) "
            "AND deleted_at IS NULL "
            "GROUP BY enterprise_id"
        )
        return {row["enterprise_id"]: row["cnt"] for row in cursor.fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def get_member_count_batch(db) -> dict:
    """批量获取各企业人员数（按 parent_id 分组），返回 {enterprise_id: count}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT parent_id, COUNT(*) as cnt FROM users WHERE parent_id IS NOT NULL GROUP BY parent_id"
        )
        return {row["parent_id"]: row["cnt"] for row in cursor.fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def get_monitor_count_batch(db) -> dict:
    """批量获取各企业监控的群聊数（统计 rooms 内不同群，去重），返回 {enterprise_id: 群数}"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT enterprise_id, rooms FROM monitor_configs WHERE enterprise_id IS NOT NULL"
        )
        ent_rooms = {}  # {enterprise_id: set(room_id)}
        for row in cursor.fetchall():
            eid = row["enterprise_id"]
            try:
                rooms = json.loads(row["rooms"]) if row.get("rooms") else []
            except (TypeError, ValueError):
                rooms = []
            s = ent_rooms.setdefault(eid, set())
            for r in (rooms or []):
                rid = r.get("id") if isinstance(r, dict) else r
                if rid:
                    s.add(rid)
        return {eid: len(s) for eid, s in ent_rooms.items()}
    except Exception:
        return {}
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


# ==================== 钱包 ====================

PLAN_PRICES = {
    "standard": 299,
    "enterprise": 999,
    "enterprise_trial": 0,
}


def init_wallet_tables(db):
    """创建钱包和流水表（幂等）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id     INT PRIMARY KEY,
                balance     DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id              INT PRIMARY KEY AUTO_INCREMENT,
                user_id         INT NOT NULL,
                amount          DECIMAL(10,2) NOT NULL,
                type            VARCHAR(32) NOT NULL COMMENT 'commission/withdraw',
                description     VARCHAR(256),
                enterprise_id   INT,
                status          VARCHAR(16) DEFAULT 'done' COMMENT 'done/pending/rejected',
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_wt_user (user_id),
                KEY idx_wt_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
        logger.info("钱包表初始化完成")
    finally:
        conn.close()


def get_wallet_balance(db, user_id: int) -> float:
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT balance FROM wallets WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        conn.close()


def add_wallet_commission(db, user_id: int, amount: float, description: str, enterprise_id: int = None):
    """给用户钱包加佣金（原子操作）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE balance = balance + %s",
            (user_id, amount, amount)
        )
        cursor.execute(
            "INSERT INTO wallet_transactions (user_id, amount, type, description, enterprise_id, status) "
            "VALUES (%s, %s, 'commission', %s, %s, 'done')",
            (user_id, amount, description, enterprise_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_wallet_transactions(db, user_id: int = None, limit: int = 100) -> list:
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if user_id:
            cursor.execute(
                "SELECT * FROM wallet_transactions WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            )
        else:
            cursor.execute(
                "SELECT wt.*, u.name as user_name, u.phone FROM wallet_transactions wt "
                "LEFT JOIN users u ON u.id = wt.user_id "
                "ORDER BY wt.created_at DESC LIMIT %s",
                (limit,)
            )
        rows = cursor.fetchall()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M")
            r["amount"] = float(r["amount"])
        return rows
    finally:
        conn.close()


def request_withdrawal(db, user_id: int, amount: float) -> int:
    """申请提现（冻结余额，状态 pending）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT balance FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))
        row = cursor.fetchone()
        balance = float(row["balance"]) if row else 0.0
        if amount > balance:
            raise ValueError("余额不足")
        cursor.execute("UPDATE wallets SET balance = balance - %s WHERE user_id=%s", (amount, user_id))
        cursor.execute(
            "INSERT INTO wallet_transactions (user_id, amount, type, description, status) "
            "VALUES (%s, %s, 'withdraw', '申请提现', 'pending')",
            (user_id, amount)
        )
        conn.commit()
        cursor.execute("SELECT LAST_INSERT_ID() as id")
        return cursor.fetchone()["id"]
    finally:
        conn.close()


def approve_withdrawal(db, txn_id: int):
    """超管批准提现"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE wallet_transactions SET status='done' WHERE id=%s AND type='withdraw'", (txn_id,))
        conn.commit()
    finally:
        conn.close()


def reject_withdrawal(db, txn_id: int):
    """超管拒绝提现，退回余额"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT user_id, amount FROM wallet_transactions WHERE id=%s AND type='withdraw' AND status='pending'", (txn_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE wallets SET balance = balance + %s WHERE user_id=%s", (row["amount"], row["user_id"]))
            cursor.execute("UPDATE wallet_transactions SET status='rejected' WHERE id=%s", (txn_id,))
            conn.commit()
    finally:
        conn.close()

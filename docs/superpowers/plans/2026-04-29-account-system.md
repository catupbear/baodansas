# 账号系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为保单识别系统添加三级账号体系（超管/企业/员工），所有 API 加 JWT token 验证，数据按账号权限隔离。

**Architecture:** 新增 `auth/` 模块处理用户管理和 JWT 鉴权。通过 Flask decorator 在现有 API 上加鉴权层，通过 `user_id` 字段关联保单记录和监控配置到具体账号。前端新增登录页和账号管理页，现有页面增加 token 传递逻辑。

**Tech Stack:** Flask + PyJWT + werkzeug.security(密码哈希) + MySQL + Vue 3 + Tailwind CSS

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|------|------|
| `auth/__init__.py` | 模块初始化 |
| `auth/db.py` | users 表建表 + CRUD |
| `auth/jwt_utils.py` | JWT token 生成/验证 |
| `auth/decorators.py` | `@login_required` / `@admin_required` 装饰器 |
| `auth/api.py` | 登录、账号管理 Blueprint |
| `web/templates/login.html` | 登录页 |
| `web/templates/admin_users.html` | 账号管理页（超管专属） |

### 修改文件

| 文件 | 变更 |
|------|------|
| `requirements.txt` | 添加 `PyJWT>=2.8` |
| `main.py` | 初始化 auth 模块，注册 auth Blueprint，所有页面路由加登录检查 |
| `insurance/api.py` | 所有路由加 `@login_required`，查询接口加 user_id 数据过滤 |
| `insurance/db.py` | `insurance_records` 表加 `user_id` 列，查询函数支持 user_id 过滤 |
| `insurance/handler.py` | 自动识别时根据 monitor_config 的 user_id 写入记录 |
| `insurance/monitor_config_db.py` | `monitor_configs` 表加 `user_id` 列 |
| `web/api.py` | 所有路由加 `@login_required` |
| `web/templates/insurance.html` | 请求头携带 token，增加导航栏（登出/账号管理入口） |
| `web/templates/monitor_config.html` | 请求头携带 token，监控配置增加绑定账号选择 |

---

### Task 1: 依赖 + users 表

**Files:**
- Modify: `requirements.txt`
- Create: `auth/__init__.py`
- Create: `auth/db.py`

- [ ] **Step 1: 添加 PyJWT 依赖**

在 `requirements.txt` 末尾添加：
```
PyJWT>=2.8
```

- [ ] **Step 2: 创建 auth/__init__.py**

```python
"""
账号认证模块
提供用户管理、JWT 鉴权、权限控制
"""
```

- [ ] **Step 3: 创建 auth/db.py — 建表 + CRUD**

```python
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
                username     VARCHAR(64) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                role         VARCHAR(32) NOT NULL DEFAULT 'employee',
                parent_id    INT DEFAULT NULL COMMENT '所属企业账号ID，员工必填',
                display_name VARCHAR(128) DEFAULT '',
                enabled      TINYINT NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_users_role (role),
                INDEX idx_users_parent (parent_id)
            )
        """)

        # 初始超管账号（仅在表为空时插入）
        cursor.execute("SELECT COUNT(*) AS cnt FROM users")
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s, %s, %s, %s)",
                ("admin", generate_password_hash("admin123"), ROLE_SUPER_ADMIN, "超级管理员"),
            )
            logger.info("已创建默认超管账号: admin / admin123")

        conn.commit()
        logger.info("users 表初始化完成")
    finally:
        conn.close()


def get_user_by_username(db, username: str) -> dict | None:
    """根据用户名查找用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        return cursor.fetchone()
    finally:
        conn.close()


def get_user_by_id(db, user_id: int) -> dict | None:
    """根据 ID 查找用户"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT id, username, role, parent_id, display_name, enabled, created_at, updated_at FROM users WHERE id = %s", (user_id,))
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
        sql = "SELECT id, username, role, parent_id, display_name, enabled, created_at, updated_at FROM users WHERE 1=1"
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
        # 时间戳转字符串
        for row in rows:
            for f in ("created_at", "updated_at"):
                if row.get(f) and not isinstance(row[f], str):
                    row[f] = str(row[f])
        return rows
    finally:
        conn.close()


def create_user(db, username: str, password: str, role: str, parent_id: int = None, display_name: str = "") -> int:
    """创建用户，返回新用户 ID"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, parent_id, display_name) VALUES (%s, %s, %s, %s, %s)",
            (username, generate_password_hash(password), role, parent_id, display_name),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_user(db, user_id: int, data: dict):
    """更新用户信息（支持 display_name, role, parent_id, enabled）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        allowed = {"display_name", "role", "parent_id", "enabled"}
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
```

- [ ] **Step 4: 安装依赖**

Run: `pip install PyJWT>=2.8`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt auth/
git commit -m "feat: 添加 auth 模块 — users 表建表与 CRUD"
```

---

### Task 2: JWT 工具 + 鉴权装饰器

**Files:**
- Create: `auth/jwt_utils.py`
- Create: `auth/decorators.py`

- [ ] **Step 1: 创建 auth/jwt_utils.py**

```python
"""
JWT Token 工具
负责 token 的签发与验证
"""

import time
import logging

import jwt

logger = logging.getLogger(__name__)

# 模块级配置，由 main.py 初始化时设置
_secret_key = "wxbot-default-jwt-secret"
_expire_seconds = 86400 * 7  # 默认 7 天


def init_jwt(secret_key: str, expire_seconds: int = 86400 * 7):
    """初始化 JWT 配置"""
    global _secret_key, _expire_seconds
    _secret_key = secret_key
    _expire_seconds = expire_seconds


def generate_token(user_id: int, username: str, role: str) -> str:
    """签发 JWT token"""
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": int(time.time()) + _expire_seconds,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, _secret_key, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """
    验证并解码 token。
    成功返回 payload dict，失败返回 None。
    """
    try:
        payload = jwt.decode(token, _secret_key, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug("Token 无效: %s", e)
        return None
```

- [ ] **Step 2: 创建 auth/decorators.py**

```python
"""
鉴权装饰器
提供 @login_required 和 @admin_required
"""

from functools import wraps

from flask import jsonify, request, g

from .jwt_utils import verify_token
from .db import ROLE_SUPER_ADMIN

# 模块级 db 引用，由 init 时设置
_db = None


def init_auth_decorators(db):
    """注入数据库实例"""
    global _db
    _db = db


def _extract_token() -> str | None:
    """从请求头或查询参数提取 token"""
    # 优先 Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    # 降级：查询参数 ?token=xxx
    return request.args.get("token")


def login_required(f):
    """要求登录（任何角色）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"code": 401, "msg": "未登录"}), 401

        payload = verify_token(token)
        if not payload:
            return jsonify({"code": 401, "msg": "登录已过期，请重新登录"}), 401

        # 检查用户是否仍存在且启用
        from .db import get_user_by_id
        user = get_user_by_id(_db, payload["user_id"])
        if not user or not user.get("enabled"):
            return jsonify({"code": 401, "msg": "账号已禁用"}), 401

        # 存到 g 上下文，后续路由函数可通过 g.current_user 获取
        g.current_user = {
            "user_id": payload["user_id"],
            "username": payload["username"],
            "role": payload["role"],
            "parent_id": user.get("parent_id"),
        }
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """要求超级管理员"""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.current_user["role"] != ROLE_SUPER_ADMIN:
            return jsonify({"code": 403, "msg": "权限不足"}), 403
        return f(*args, **kwargs)
    return decorated
```

- [ ] **Step 3: Commit**

```bash
git add auth/jwt_utils.py auth/decorators.py
git commit -m "feat: JWT 工具 + login_required/admin_required 装饰器"
```

---

### Task 3: 登录 + 账号管理 API

**Files:**
- Create: `auth/api.py`

- [ ] **Step 1: 创建 auth/api.py**

```python
"""
账号认证模块 — Flask API 蓝图
提供登录、当前用户信息、账号 CRUD（超管专属）
"""

import logging

from flask import Blueprint, jsonify, request, g

from .db import (
    ROLE_SUPER_ADMIN,
    ROLE_ENTERPRISE,
    ROLE_EMPLOYEE,
    get_user_by_username,
    get_user_by_id,
    verify_password,
    list_users,
    create_user,
    update_user,
    reset_password,
)
from .jwt_utils import generate_token
from .decorators import login_required, admin_required

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

_db = None


def init_auth_api(db):
    """注入数据库实例"""
    global _db
    _db = db


# ============================================================
# 登录
# ============================================================

@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    """
    用户登录，返回 JWT token。
    请求体: {"username": "xxx", "password": "xxx"}
    """
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"code": 400, "msg": "用户名和密码不能为空"}), 400

    user = get_user_by_username(_db, username)
    if not user:
        return jsonify({"code": 401, "msg": "用户名或密码错误"}), 401

    if not user.get("enabled"):
        return jsonify({"code": 403, "msg": "账号已禁用，请联系管理员"}), 403

    if not verify_password(user, password):
        return jsonify({"code": 401, "msg": "用户名或密码错误"}), 401

    token = generate_token(user["id"], user["username"], user["role"])

    return jsonify({
        "code": 0,
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "display_name": user.get("display_name", ""),
            },
        },
    })


# ============================================================
# 当前用户
# ============================================================

@auth_bp.route("/api/auth/me", methods=["GET"])
@login_required
def get_me():
    """获取当前登录用户信息"""
    user = get_user_by_id(_db, g.current_user["user_id"])
    if not user:
        return jsonify({"code": 404, "msg": "用户不存在"}), 404
    return jsonify({"code": 0, "data": user})


# ============================================================
# 账号管理（超管专属）
# ============================================================

@auth_bp.route("/api/auth/users", methods=["GET"])
@admin_required
def api_list_users():
    """查询用户列表，可按 role / parent_id 筛选"""
    role = request.args.get("role", "")
    parent_id = request.args.get("parent_id")
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = None

    users = list_users(_db, role=role, parent_id=parent_id)
    return jsonify({"code": 0, "data": users})


@auth_bp.route("/api/auth/users", methods=["POST"])
@admin_required
def api_create_user():
    """
    创建用户。
    请求体: {"username", "password", "role", "parent_id"(可选), "display_name"(可选)}
    """
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", ROLE_EMPLOYEE)
    parent_id = data.get("parent_id")
    display_name = data.get("display_name", "")

    if not username or not password:
        return jsonify({"code": 400, "msg": "用户名和密码不能为空"}), 400

    if role not in (ROLE_SUPER_ADMIN, ROLE_ENTERPRISE, ROLE_EMPLOYEE):
        return jsonify({"code": 400, "msg": f"无效角色: {role}"}), 400

    if role == ROLE_EMPLOYEE and not parent_id:
        return jsonify({"code": 400, "msg": "员工账号必须指定所属企业"}), 400

    # 检查用户名唯一
    existing = get_user_by_username(_db, username)
    if existing:
        return jsonify({"code": 400, "msg": "用户名已存在"}), 400

    # 校验 parent_id 指向的企业账号确实存在
    if parent_id:
        parent = get_user_by_id(_db, int(parent_id))
        if not parent or parent["role"] != ROLE_ENTERPRISE:
            return jsonify({"code": 400, "msg": "所属企业账号不存在"}), 400

    try:
        user_id = create_user(_db, username, password, role, parent_id, display_name)
        return jsonify({"code": 0, "data": {"id": user_id}})
    except Exception as e:
        logger.exception("创建用户失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_update_user(user_id):
    """更新用户信息（display_name, role, parent_id, enabled）"""
    data = request.get_json(silent=True) or {}
    try:
        update_user(_db, user_id, data)
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("更新用户 %d 失败", user_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def api_reset_password(user_id):
    """重置密码"""
    data = request.get_json(silent=True) or {}
    new_password = data.get("password", "")
    if not new_password:
        return jsonify({"code": 400, "msg": "新密码不能为空"}), 400
    try:
        reset_password(_db, user_id, new_password)
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("重置密码 %d 失败", user_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/change-password", methods=["POST"])
@login_required
def api_change_password():
    """当前用户修改自己的密码"""
    data = request.get_json(silent=True) or {}
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")
    if not old_password or not new_password:
        return jsonify({"code": 400, "msg": "旧密码和新密码不能为空"}), 400

    user = get_user_by_username(_db, g.current_user["username"])
    if not verify_password(user, old_password):
        return jsonify({"code": 400, "msg": "旧密码错误"}), 400

    reset_password(_db, g.current_user["user_id"], new_password)
    return jsonify({"code": 0, "msg": "密码修改成功"})
```

- [ ] **Step 2: Commit**

```bash
git add auth/api.py
git commit -m "feat: 登录 + 账号管理 API（含修改密码）"
```

---

### Task 4: main.py 集成 auth 模块

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 在 main.py 顶部添加 import**

在现有 import 区域（`from insurance.api import ...` 之后）添加：
```python
from auth.db import init_users_table
from auth.jwt_utils import init_jwt
from auth.decorators import init_auth_decorators
from auth.api import auth_bp, init_auth_api
```

- [ ] **Step 2: 在 create_app 的 init_insurance_tables(db) 之前添加 auth 初始化**

在 `# 初始化保单识别模块` 注释前插入：
```python
    # 初始化账号认证模块
    init_users_table(db)
    jwt_secret = config.get("secret_key", "wxbot-monitor-secret-key")
    init_jwt(jwt_secret)
    init_auth_decorators(db)
    init_auth_api(db)
    app.register_blueprint(auth_bp)
```

- [ ] **Step 3: 修改页面路由 — 添加登录页 + 改造现有路由**

将 main.py 中的路由部分替换为：
```python
    # 登录页（无需认证）
    @app.route("/login")
    def login_page():
        return render_template("login.html")

    # 首页 → 保单识别（需登录，前端通过 JS 检查 token）
    @app.route("/")
    def index():
        return render_template("insurance.html")

    # 保单识别兼容旧路径
    @app.route("/insurance")
    def insurance_page():
        return redirect("/")

    # 监控配置管理
    @app.route("/monitor-config")
    def monitor_config_page():
        return render_template("monitor_config.html")

    # 模板训练管理
    @app.route("/training")
    def training_page():
        return render_template("training.html")

    # 账号管理（超管专属，前端校验权限）
    @app.route("/admin/users")
    def admin_users_page():
        return render_template("admin_users.html")

    # 消息监控（保留密码验证兼容）
    @app.route("/monitor")
    def monitor_page():
        return render_template("monitor_login.html")

    @app.route("/monitor/chat")
    def monitor_chat():
        from flask import session
        if not session.get("monitor_auth"):
            return redirect("/monitor")
        return render_template("index.html")

    @app.route("/monitor/login", methods=["POST"])
    def monitor_login():
        from flask import session
        password = request.form.get("password", "")
        if password == config.get("monitor", {}).get("password", "wuhu2025"):
            session["monitor_auth"] = True
            return redirect("/monitor/chat")
        return render_template("monitor_login.html", error="密码错误")
```

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: main.py 集成 auth 模块初始化 + 登录/管理路由"
```

---

### Task 5: 登录页前端

**Files:**
- Create: `web/templates/login.html`

- [ ] **Step 1: 创建登录页**

简约登录页，使用 Vue 3 + Tailwind CSS，风格与现有 insurance.html 一致（暖色系）。登录成功后将 token 和用户信息存入 localStorage，跳转到首页。

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - 保单识别系统</title>
    <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
        body { background: #faf8f5; }
        [v-cloak] { display: none; }
    </style>
</head>
<body>
<div id="app" v-cloak>
    <div class="min-h-screen flex items-center justify-center">
        <div class="bg-white rounded-xl shadow-lg p-10 w-96">
            <h1 class="text-xl font-semibold text-gray-800 text-center mb-2">保单识别系统</h1>
            <p class="text-sm text-gray-400 text-center mb-8">请登录以继续</p>

            <div v-if="error" class="bg-red-50 text-red-600 text-sm rounded-lg px-4 py-2 mb-4">{{ error }}</div>

            <form @submit.prevent="handleLogin">
                <div class="mb-5">
                    <label class="block text-sm text-gray-500 mb-1">用户名</label>
                    <input v-model="username" type="text" placeholder="请输入用户名" autofocus required
                        class="w-full h-11 px-4 border border-gray-200 rounded-lg text-sm outline-none focus:border-orange-400 transition">
                </div>
                <div class="mb-6">
                    <label class="block text-sm text-gray-500 mb-1">密码</label>
                    <input v-model="password" type="password" placeholder="请输入密码" required
                        class="w-full h-11 px-4 border border-gray-200 rounded-lg text-sm outline-none focus:border-orange-400 transition">
                </div>
                <button type="submit" :disabled="loading"
                    class="w-full h-11 bg-orange-600 hover:bg-orange-700 text-white rounded-lg text-sm font-medium transition disabled:opacity-50">
                    {{ loading ? '登录中...' : '登 录' }}
                </button>
            </form>
        </div>
    </div>
</div>

<script>
const { createApp, ref } = Vue;
createApp({
    setup() {
        const username = ref('');
        const password = ref('');
        const error = ref('');
        const loading = ref(false);

        // 已登录则跳转首页
        if (localStorage.getItem('token')) {
            window.location.href = '/';
        }

        async function handleLogin() {
            error.value = '';
            loading.value = true;
            try {
                const resp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: username.value, password: password.value }),
                });
                const data = await resp.json();
                if (data.code === 0) {
                    localStorage.setItem('token', data.data.token);
                    localStorage.setItem('user', JSON.stringify(data.data.user));
                    window.location.href = '/';
                } else {
                    error.value = data.msg || '登录失败';
                }
            } catch (e) {
                error.value = '网络错误，请重试';
            } finally {
                loading.value = false;
            }
        }

        return { username, password, error, loading, handleLogin };
    }
}).mount('#app');
</script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add web/templates/login.html
git commit -m "feat: 登录页前端（Vue 3 + Tailwind）"
```

---

### Task 6: 账号管理页前端

**Files:**
- Create: `web/templates/admin_users.html`

- [ ] **Step 1: 创建账号管理页**

超管专属页面，展示用户列表，支持创建/编辑/禁用/重置密码。使用与 insurance.html 一致的 UI 风格。

包含功能：
- 用户列表（角色筛选）
- 创建用户弹窗（用户名、密码、角色、所属企业选择、显示名）
- 编辑用户弹窗
- 重置密码弹窗
- 启用/禁用切换

前端使用 `localStorage.getItem('token')` 作为 Authorization header。

页面需包含导航栏（首页/监控配置/账号管理/登出），与其他页面保持一致。

（完整 HTML 太长，实现时按 insurance.html 的模式编写，核心是 CRUD 表格 + 弹窗表单）

- [ ] **Step 2: Commit**

```bash
git add web/templates/admin_users.html
git commit -m "feat: 账号管理页前端（超管专属）"
```

---

### Task 7: 现有 API 加鉴权 + 数据过滤

**Files:**
- Modify: `insurance/db.py` — insurance_records 加 user_id 列，查询支持 user_id 过滤
- Modify: `insurance/monitor_config_db.py` — monitor_configs 加 user_id 列
- Modify: `insurance/api.py` — 所有路由加 @login_required，查询加数据过滤
- Modify: `web/api.py` — 所有路由加 @login_required

- [ ] **Step 1: insurance/db.py — 建表加 user_id 列**

在 `init_insurance_tables` 函数中，建表语句后添加 ALTER TABLE（安全幂等）：
```python
        # 为 insurance_records 添加 user_id 列（存量兼容）
        try:
            cursor.execute("ALTER TABLE insurance_records ADD COLUMN user_id INT DEFAULT NULL COMMENT '所属用户ID'")
            cursor.execute("CREATE INDEX idx_ir_user_id ON insurance_records(user_id)")
            conn.commit()
        except Exception:
            pass  # 列已存在则忽略
```

- [ ] **Step 2: insurance/db.py — query_insurance_records 增加 user_ids 参数**

在 `query_insurance_records` 函数的过滤条件中增加：
```python
    # user_ids: 按账号过滤（企业看子账号，员工看自己）
    user_ids = kwargs.get("user_ids")
    if user_ids is not None:
        if user_ids:
            placeholders = ",".join(["%s"] * len(user_ids))
            where_parts.append(f"r.user_id IN ({placeholders})")
            params.extend(user_ids)
        else:
            where_parts.append("0")  # 空列表不返回数据
```

- [ ] **Step 3: insurance/monitor_config_db.py — 加 user_id 列**

在 `init_monitor_config_table` 中，建表后添加：
```python
        try:
            cursor.execute("ALTER TABLE monitor_configs ADD COLUMN user_id INT DEFAULT NULL COMMENT '绑定的用户ID'")
            cursor.execute("CREATE INDEX idx_mc_user_id ON monitor_configs(user_id)")
            conn.commit()
        except Exception:
            pass
```

- [ ] **Step 4: insurance/api.py — 所有路由加装饰器 + 数据过滤**

在文件顶部 import 添加：
```python
from auth.decorators import login_required, admin_required
from auth.db import ROLE_SUPER_ADMIN, ROLE_ENTERPRISE, get_enterprise_employee_ids
```

为每个 `@insurance_bp.route(...)` 下的函数添加 `@login_required` 装饰器。

在 `list_records()` 函数中，构建查询前根据角色添加 user_ids 过滤：
```python
    # 按登录用户角色过滤数据
    user_ids = None
    role = g.current_user["role"]
    uid = g.current_user["user_id"]
    if role == ROLE_ENTERPRISE:
        user_ids = get_enterprise_employee_ids(_db, uid)
    elif role != ROLE_SUPER_ADMIN:
        user_ids = [uid]
```

然后将 `user_ids` 传给 `query_insurance_records(..., user_ids=user_ids)`。

同样的逻辑应用到 `get_stats()` 等统计接口。

- [ ] **Step 5: web/api.py — 加鉴权装饰器**

在文件顶部 import 添加：
```python
from auth.decorators import login_required
```

为每个 `@api_bp.route(...)` 下的函数添加 `@login_required` 装饰器。

- [ ] **Step 6: Commit**

```bash
git add insurance/db.py insurance/monitor_config_db.py insurance/api.py web/api.py
git commit -m "feat: 现有 API 加 JWT 鉴权 + 保单数据按账号权限过滤"
```

---

### Task 8: 手动上传关联用户 + 自动识别关联用户

**Files:**
- Modify: `insurance/api.py` — 手动上传时写入 user_id
- Modify: `insurance/handler.py` — 自动识别时从 monitor_config 获取 user_id

- [ ] **Step 1: insurance/api.py — 手动上传写入 user_id**

在手动上传接口（`upload_and_recognize`）中，保存记录时传入当前登录用户 ID：
```python
    user_id = g.current_user["user_id"]
    # 在 save_insurance_record 调用时传入 user_id
```

- [ ] **Step 2: insurance/handler.py — 自动识别关联用户**

在 `_process_message` 方法中，获取匹配的 monitor_config，取其 `user_id` 字段写入 insurance_record：
```python
    # 从监控配置中获取绑定的 user_id
    config_user_id = matched_config.get("user_id") if matched_config else None
    # 保存记录时传入 user_id=config_user_id
```

- [ ] **Step 3: insurance/db.py — save_insurance_record 支持 user_id**

在 `save_insurance_record` 函数的 INSERT 语句中加入 `user_id` 字段。

- [ ] **Step 4: Commit**

```bash
git add insurance/api.py insurance/handler.py insurance/db.py
git commit -m "feat: 手动上传+自动识别记录关联用户ID"
```

---

### Task 9: 前端改造 — token 传递 + 导航栏 + 登出

**Files:**
- Modify: `web/templates/insurance.html` — API 请求携带 token，添加导航栏
- Modify: `web/templates/monitor_config.html` — API 请求携带 token，添加绑定账号

- [ ] **Step 1: 创建公共的 auth 辅助 JS 函数**

在 insurance.html 和其他页面的 `<script>` 区域添加公共函数：
```javascript
// 鉴权工具
function getToken() { return localStorage.getItem('token'); }
function getUser() { try { return JSON.parse(localStorage.getItem('user')); } catch { return null; } }
function checkAuth() {
    if (!getToken()) { window.location.href = '/login'; return false; }
    return true;
}
function logout() {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    window.location.href = '/login';
}
function authHeaders() {
    return { 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' };
}
// 封装 fetch，自动处理 401
async function authFetch(url, options = {}) {
    options.headers = { ...authHeaders(), ...options.headers };
    const resp = await fetch(url, options);
    if (resp.status === 401) { logout(); throw new Error('未登录'); }
    return resp;
}
```

- [ ] **Step 2: insurance.html — 所有 fetch 调用替换为 authFetch**

将 `fetch('/api/insurance/...')` 替换为 `authFetch('/api/insurance/...')`，移除手动设置 headers 的地方。

- [ ] **Step 3: insurance.html — 添加顶部导航栏**

在页面顶部添加导航栏，包含：
- 首页（保单识别）、监控配置、账号管理（仅超管可见）
- 右侧显示当前用户名 + 登出按钮

- [ ] **Step 4: monitor_config.html — 同样改造（authFetch + 导航栏）**

- [ ] **Step 5: monitor_config.html — 监控配置增加绑定账号下拉**

在监控配置的编辑/新建弹窗中添加"绑定账号"下拉选择，调用 `/api/auth/users?role=employee` 获取员工列表。

- [ ] **Step 6: Commit**

```bash
git add web/templates/insurance.html web/templates/monitor_config.html
git commit -m "feat: 前端加 token 鉴权 + 导航栏 + 监控配置绑定账号"
```

---

### Task 10: 更新日志

**Files:**
- Create: `docs/changelog/2026-04/2026-04-29-账号系统更新记录.md`

- [ ] **Step 1: 编写更新记录**

包含：需求说明、数据库变更（users 表、insurance_records 加 user_id、monitor_configs 加 user_id）、代码变更清单、部署步骤（pip install PyJWT、重启服务、默认超管 admin/admin123）。

- [ ] **Step 2: Commit**

```bash
git add docs/changelog/
git commit -m "docs: 账号系统更新记录"
```

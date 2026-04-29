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

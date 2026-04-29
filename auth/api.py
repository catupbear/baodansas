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

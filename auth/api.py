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
    get_user_by_phone,
    get_user_by_id,
    verify_password,
    list_users,
    create_user,
    update_user,
    reset_password,
    list_enterprises,
    get_enterprise_by_id,
    create_enterprise,
    update_enterprise,
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
    请求体: {"phone": "xxx", "password": "xxx"}
    """
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    password = data.get("password", "")

    if not phone or not password:
        return jsonify({"code": 400, "msg": "手机号和密码不能为空"}), 400

    user = get_user_by_phone(_db, phone)
    if not user:
        return jsonify({"code": 401, "msg": "手机号或密码错误"}), 401

    if not user.get("enabled"):
        return jsonify({"code": 403, "msg": "账号已禁用，请联系管理员"}), 403

    if not verify_password(user, password):
        return jsonify({"code": 401, "msg": "手机号或密码错误"}), 401

    token = generate_token(user["id"], user["phone"], user["role"])

    return jsonify({
        "code": 0,
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "phone": user["phone"],
                "role": user["role"],
                "name": user.get("name", ""),
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
    # 填充所属企业名称
    ent_cache = {}
    for u in users:
        pid = u.get("parent_id")
        if pid:
            if pid not in ent_cache:
                ent = get_enterprise_by_id(_db, pid)
                ent_cache[pid] = ent["name"] if ent else ""
            u["parent_name"] = ent_cache[pid]
        else:
            u["parent_name"] = ""
    return jsonify({"code": 0, "data": users})


@auth_bp.route("/api/auth/users", methods=["POST"])
@admin_required
def api_create_user():
    """
    创建用户。
    请求体: {"phone", "password", "role", "parent_id"(可选), "name"(可选)}
    """
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    password = data.get("password", "")
    role = data.get("role", ROLE_EMPLOYEE)
    parent_id = data.get("parent_id")
    name = data.get("name", "")

    if not phone or not password:
        return jsonify({"code": 400, "msg": "手机号和密码不能为空"}), 400

    if role not in (ROLE_SUPER_ADMIN, ROLE_ENTERPRISE, ROLE_EMPLOYEE):
        return jsonify({"code": 400, "msg": f"无效角色: {role}"}), 400

    if role in (ROLE_ENTERPRISE, ROLE_EMPLOYEE) and not parent_id:
        return jsonify({"code": 400, "msg": "企业管理员和员工必须指定所属企业"}), 400

    # 检查手机号唯一
    existing = get_user_by_phone(_db, phone)
    if existing:
        return jsonify({"code": 400, "msg": "该手机号已注册"}), 400

    # 校验 parent_id 指向的企业确实存在
    if parent_id:
        ent = get_enterprise_by_id(_db, int(parent_id))
        if not ent:
            return jsonify({"code": 400, "msg": "所属企业不存在"}), 400

    try:
        user_id = create_user(_db, phone, password, role, parent_id, name)
        return jsonify({"code": 0, "data": {"id": user_id}})
    except Exception as e:
        logger.exception("创建用户失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_update_user(user_id):
    """更新用户信息（name, role, parent_id, enabled）"""
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


# ============================================================
# 企业管理（超管专属）
# ============================================================

@auth_bp.route("/api/auth/enterprises", methods=["GET"])
@admin_required
def api_list_enterprises():
    """查询企业列表"""
    enterprises = list_enterprises(_db)
    return jsonify({"code": 0, "data": enterprises})


@auth_bp.route("/api/auth/enterprises", methods=["POST"])
@admin_required
def api_create_enterprise():
    """
    创建企业。
    请求体: {"name", "contact_person"(可选), "contact_phone"(可选)}
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    contact_person = data.get("contact_person", "").strip()
    contact_phone = data.get("contact_phone", "").strip()

    if not name:
        return jsonify({"code": 400, "msg": "企业名称不能为空"}), 400

    try:
        eid = create_enterprise(_db, name, contact_person, contact_phone)
        return jsonify({"code": 0, "data": {"id": eid}})
    except Exception as e:
        logger.exception("创建企业失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/enterprises/<int:eid>", methods=["PUT"])
@admin_required
def api_update_enterprise(eid):
    """更新企业信息（name, contact_person, contact_phone, enabled）"""
    data = request.get_json(silent=True) or {}
    try:
        update_enterprise(_db, eid, data)
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("更新企业 %d 失败", eid)
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

    user = get_user_by_phone(_db, g.current_user["phone"])
    if not verify_password(user, old_password):
        return jsonify({"code": 400, "msg": "旧密码错误"}), 400

    reset_password(_db, g.current_user["user_id"], new_password)
    return jsonify({"code": 0, "msg": "密码修改成功"})

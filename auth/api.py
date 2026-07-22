"""
账号认证模块 — Flask API 蓝图
提供登录、当前用户信息、账号 CRUD（超管专属）
"""

import logging
import secrets

from flask import Blueprint, jsonify, request, g

import re
from datetime import datetime, timezone

from .db import (
    ROLE_SUPER_ADMIN,
    ROLE_ENTERPRISE,
    ROLE_EMPLOYEE,
    ROLE_SALES,
    ROLE_CHANNEL_SALES,
    ROLE_PERSONAL_SALES,
    get_user_by_phone,
    get_user_by_id,
    verify_password,
    list_users,
    create_user,
    update_user,
    reset_password,
    delete_user,
    list_enterprises,
    get_enterprise_by_id,
    create_enterprise,
    update_enterprise,
    delete_enterprise,
    ensure_survey_token,
    get_enterprise_by_survey_token,
    get_bindings_by_user,
    set_user_bindings,
    list_all_bindings,
    save_verification_code,
    get_last_sms_sent_at,
    sms_in_cooldown,
    try_insert_sms_record,
    delete_sms_record,
    verify_sms_code,
    get_user_by_referral_code,
    get_referral_downlines,
    get_or_create_referral_code,
    get_monthly_pdf_usage_batch,
    get_member_count_batch,
    get_monitor_count_batch,
    get_enterprise_monthly_pdf_usage,
)
from .jwt_utils import generate_token
from .decorators import login_required, admin_required
from .sms import generate_code, send_sms_code

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_SMS_COOLDOWN = 60   # 同一手机号发送间隔（秒）
_SMS_TTL = 300       # 验证码有效期（秒）

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

_db = None
_insurance_db = None


def init_auth_api(db, insurance_db=None):
    """注入数据库实例（insurance_db 用于保单记录回填）"""
    global _db, _insurance_db
    _db = db
    _insurance_db = insurance_db or db


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
                "activated": int(user.get("activated", 1)),
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
    # 附带企业级功能开关（前端入口显示控制用）
    if user.get("role") != ROLE_SUPER_ADMIN and user.get("parent_id"):
        try:
            _ent = get_enterprise_by_id(_db, user["parent_id"])
            user["seal_tracking_enabled"] = int((_ent or {}).get("seal_tracking_enabled") or 0)
        except Exception:
            user["seal_tracking_enabled"] = 0
    return jsonify({"code": 0, "data": user})


@auth_bp.route("/api/auth/my-quota", methods=["GET"])
@login_required
def get_my_quota():
    """获取当前用户所属企业的套餐和本月PDF识别用量"""
    role = g.current_user.get("role", "")
    user_id = g.current_user["user_id"]

    # 确定企业ID
    if role == ROLE_SUPER_ADMIN:
        return jsonify({"code": 0, "data": {"plan_type": None, "monthly_usage": 0, "monthly_limit": None}})

    user = get_user_by_id(_db, user_id)
    enterprise_id = user.get("parent_id") if user else None

    if not enterprise_id:
        return jsonify({"code": 0, "data": {
            "no_enterprise": True,
            "plan_type": None,
            "monthly_usage": 0,
            "monthly_limit": None,
        }})

    ent = get_enterprise_by_id(_db, enterprise_id)
    plan_type = ent.get("plan_type", "standard") if ent else "standard"
    plan_months = ent.get("plan_months", 1) if ent else 1
    plan_start_at = str(ent["plan_start_at"]) if ent and ent.get("plan_start_at") else None
    monthly_usage = get_enterprise_monthly_pdf_usage(_db, enterprise_id)
    monthly_limit = None if plan_type in ("enterprise", "enterprise_trial") else 3000

    from .db import get_boost_balance
    _boost = get_boost_balance(_db, enterprise_id)
    return jsonify({"code": 0, "data": {
        "plan_type": plan_type,
        "plan_months": plan_months,
        "plan_start_at": plan_start_at,
        "monthly_usage": monthly_usage,
        "monthly_limit": monthly_limit,
        "boost_remaining": _boost["remaining"],
        "boost_nearest_expire": _boost["nearest_expire"],
    }})


# ============================================================
# 识别加油包（99元/1200次/3个月，可叠加；超管开通管理）
# ============================================================

@auth_bp.route("/api/auth/enterprises/<int:eid>/boost-packs", methods=["GET", "POST"])
@admin_required
def api_enterprise_boost_packs(eid):
    from .db import list_boost_packs, create_boost_pack, get_boost_balance
    if request.method == "GET":
        return jsonify({"code": 0, "data": {
            "packs": list_boost_packs(_db, eid),
            "balance": get_boost_balance(_db, eid),
        }})
    body = request.get_json(silent=True) or {}
    pack_id = create_boost_pack(
        _db, eid,
        created_by=g.current_user["user_id"],
        remark=(body.get("remark") or "").strip()[:200])
    return jsonify({"code": 0, "msg": "加油包已开通", "data": {"id": pack_id}})


@auth_bp.route("/api/auth/boost-packs/<int:pack_id>", methods=["DELETE"])
@admin_required
def api_delete_boost_pack(pack_id):
    from .db import delete_boost_pack
    n = delete_boost_pack(_db, pack_id)
    return jsonify({"code": 0, "msg": "已删除" if n else "记录不存在"})


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

    # channel_sales / personal_sales 是附加销售类型，存在 sales_type 字段而非 role
    sales_type = ""
    _filter_agent = False
    if role == "channel_sales":
        sales_type = "channel"
        role = ""
    elif role == "personal_sales":
        sales_type = "personal"
        role = ""
    elif role == "agent_sales":
        # 代理销售：统一筛选所有具备销售能力的账号（含历史 channel/personal）
        _filter_agent = True
        role = ""

    users = list_users(_db, role=role, parent_id=parent_id, sales_type=sales_type)
    if _filter_agent:
        users = [u for u in users
                 if u.get("role") in ("channel_sales", "personal_sales", "sales")
                 or u.get("sales_type") in ("channel", "personal")]
    # 关键词搜索（姓名/手机号）
    q = request.args.get("q", "").strip()
    limit = request.args.get("limit", 0)
    if q:
        users = [u for u in users if q in (u.get("name") or "") or q in (u.get("phone") or "")]
    if limit:
        try: users = users[:int(limit)]
        except: pass
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

    # 填充绑定的发送人信息
    all_bindings = list_all_bindings(_db)
    # 按 user_id 分组
    binding_map = {}
    for b in all_bindings:
        uid = b["user_id"]
        if uid not in binding_map:
            binding_map[uid] = []
        binding_map[uid].append({
            "sender": b["sender"],
            "sender_name": b.get("sender_name", ""),
        })
    for u in users:
        u["bound_senders"] = binding_map.get(u["id"], [])

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
    is_sales = bool(data.get("is_sales", False))
    sales_type = data.get("sales_type", "")

    if not phone or not password:
        return jsonify({"code": 400, "msg": "手机号和密码不能为空"}), 400

    _SALES_ROLES = (ROLE_SALES, ROLE_CHANNEL_SALES, ROLE_PERSONAL_SALES)
    if role not in (ROLE_SUPER_ADMIN, ROLE_ENTERPRISE, ROLE_EMPLOYEE) + _SALES_ROLES:
        return jsonify({"code": 400, "msg": f"无效角色: {role}"}), 400

    # 企业管理员和员工必须指定企业；销售角色可以不挂企业（独立销售）
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
        user_id = create_user(_db, phone, password, role, parent_id, name, is_sales=is_sales)

        # 处理发送人绑定
        bound_senders = data.get("bound_senders", [])
        if bound_senders and user_id:
            ent_id = int(parent_id) if parent_id else None
            set_user_bindings(_db, user_id, bound_senders, ent_id)
            # 回填历史保单记录
            _backfill_records_by_senders(user_id, bound_senders, ent_id)

        return jsonify({"code": 0, "data": {"id": user_id}})
    except Exception as e:
        logger.exception("创建用户失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_update_user(user_id):
    """更新用户信息（name, role, parent_id, enabled, is_sales, bound_senders）"""
    data = request.get_json(silent=True) or {}
    if "is_sales" in data:
        data["is_sales"] = 1 if data["is_sales"] else 0
    if "sales_type" in data:
        data["sales_type"] = data["sales_type"] or ""
    try:
        update_user(_db, user_id, data)

        # 处理发送人绑定（仅当请求中包含 bound_senders 字段时才更新）
        if "bound_senders" in data:
            bound_senders = data["bound_senders"] or []
            # 获取用户的 enterprise_id（parent_id）
            user = get_user_by_id(_db, user_id)
            ent_id = data.get("parent_id") or (user.get("parent_id") if user else None)
            if ent_id is not None:
                ent_id = int(ent_id)
            set_user_bindings(_db, user_id, bound_senders, ent_id)
            # 回填历史保单记录
            _backfill_records_by_senders(user_id, bound_senders, ent_id)

        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("更新用户 %d 失败", user_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    """删除用户"""
    cur = get_user_by_id(_db, user_id)
    if not cur:
        return jsonify({"code": 404, "msg": "用户不存在"}), 404
    if cur.get("role") == ROLE_SUPER_ADMIN:
        return jsonify({"code": 403, "msg": "不允许删除超级管理员"}), 403
    try:
        delete_user(_db, user_id)
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("删除用户 %d 失败", user_id)
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
# 发送人列表（供绑定选择）
# ============================================================

@auth_bp.route("/api/auth/senders", methods=["GET"])
@admin_required
def api_list_senders():
    """从 insurance_records 去重取出所有发送人列表，供前端绑定选择"""
    try:
        import pymysql.cursors
        conn = _insurance_db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute("""
                SELECT sender, sender_name, COUNT(*) AS record_count
                FROM insurance_records
                WHERE sender IS NOT NULL AND sender != ''
                GROUP BY sender, sender_name
                ORDER BY record_count DESC
            """)
            rows = cursor.fetchall()
            # 同一个 sender 可能有不同的 sender_name（名称变化），去重取最新的
            sender_map = {}
            for row in rows:
                sid = row["sender"]
                if sid not in sender_map:
                    sender_map[sid] = {
                        "sender": sid,
                        "sender_name": row.get("sender_name", ""),
                        "record_count": row["record_count"],
                    }
                else:
                    sender_map[sid]["record_count"] += row["record_count"]
                    # 优先取非空名称
                    if not sender_map[sid]["sender_name"] and row.get("sender_name"):
                        sender_map[sid]["sender_name"] = row["sender_name"]
            senders = list(sender_map.values())
            senders.sort(key=lambda x: x["record_count"], reverse=True)
            return jsonify({"code": 0, "data": senders})
        finally:
            conn.close()
    except Exception as e:
        logger.exception("获取发送人列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


def _backfill_records_by_senders(user_id: int, senders: list, enterprise_id: int = None):
    """回填历史保单记录：将匹配 sender 的记录归属到指定用户"""
    if not senders:
        return
    try:
        import pymysql.cursors
        # 提取 sender ID 列表
        sender_ids = []
        for item in senders:
            if isinstance(item, dict):
                sid = item.get("sender", "")
            else:
                sid = str(item)
            if sid:
                sender_ids.append(sid)
        if not sender_ids:
            return

        conn = _insurance_db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            placeholders = ", ".join(["%s"] * len(sender_ids))
            params = [user_id] + sender_ids
            if enterprise_id is not None:
                sql = (
                    f"UPDATE insurance_records SET user_id = %s "
                    f"WHERE sender IN ({placeholders}) AND enterprise_id = %s"
                )
                params.append(enterprise_id)
            else:
                sql = (
                    f"UPDATE insurance_records SET user_id = %s "
                    f"WHERE sender IN ({placeholders})"
                )
            cursor.execute(sql, params)
            affected = cursor.rowcount
            conn.commit()
            if affected > 0:
                logger.info("回填保单记录: user_id=%d, senders=%s, 更新%d条", user_id, sender_ids, affected)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("回填保单记录失败: user_id=%d, %s", user_id, e)


# ============================================================
# 企业管理（超管专属）
# ============================================================

@auth_bp.route("/api/auth/enterprises", methods=["GET"])
@admin_required
def api_list_enterprises():
    """查询企业列表（附带当月PDF识别使用量 + 推荐人名称）"""
    enterprises = list_enterprises(_db)
    usage_map = get_monthly_pdf_usage_batch(_db)
    member_map = get_member_count_batch(_db)
    monitor_map = get_monitor_count_batch(_db)
    # 批量查推荐人名称
    referrer_ids = list({e["referrer_id"] for e in enterprises if e.get("referrer_id")})
    referrer_map = {}
    if referrer_ids:
        from auth.db import get_user_by_id
        for rid in referrer_ids:
            u = get_user_by_id(_db, rid)
            if u:
                referrer_map[rid] = u.get("name") or u.get("phone", "")
    for ent in enterprises:
        ent["monthly_pdf_usage"] = usage_map.get(ent["id"], 0)
        ent["member_count"] = member_map.get(ent["id"], 0)
        ent["monitor_count"] = monitor_map.get(ent["id"], 0)
        ent["referrer_name"] = referrer_map.get(ent.get("referrer_id"), "")
        # 附带该企业自动创建的管理员账号(登录名=企业编号小写去横线)的密码明文，供客户管理页展示
        ent["admin_account"] = ""
        ent["admin_password"] = ""
        no = (ent.get("enterprise_no") or "").strip()
        if no:
            au = get_user_by_phone(_db, no.lower().replace("-", ""))
            if au:
                ent["admin_account"] = au.get("phone") or ""
                ent["admin_password"] = au.get("plain_password") or ""
    return jsonify({"code": 0, "data": enterprises})


@auth_bp.route("/api/auth/enterprises", methods=["POST"])
@admin_required
def api_create_enterprise():
    """创建企业"""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    contact_person = data.get("contact_person", "").strip()
    contact_phone = data.get("contact_phone", "").strip()
    if not name:
        return jsonify({"code": 400, "msg": "企业名称不能为空"}), 400
    enterprise_no = data.get("enterprise_no", "").strip()
    if not enterprise_no:
        return jsonify({"code": 400, "msg": "企业编号不能为空"}), 400

    # 企业编号(小写、去掉横线)将作为该企业管理员的登录账号，需提前确保账号未被占用
    admin_phone = enterprise_no.lower().replace("-", "")
    if get_user_by_phone(_db, admin_phone):
        return jsonify({"code": 400, "msg": f"企业编号「{enterprise_no}」对应的账号已存在，请更换编号"}), 400

    try:
        eid = create_enterprise(_db, name, contact_person, contact_phone)
        update_enterprise(_db, eid, {"enterprise_no": enterprise_no})
        # 同步创建该企业的管理员账号：登录账号=企业编号(小写去横线)，姓名=企业管理员，初始密码=4位随机数
        admin_password = f"{secrets.randbelow(10000):04d}"
        create_user(_db, admin_phone, admin_password, ROLE_ENTERPRISE, eid, "企业管理员", activated=1)
        # 初始化企业默认导出列配置：包含全部识别字段（都勾选导出）
        try:
            from insurance.field_config_db import init_enterprise_default_template
            init_enterprise_default_template(_db, eid)
        except Exception:
            logger.exception("初始化企业默认导出模板失败")
        if data.get("referrer_id"):
            update_enterprise(_db, eid, {"referrer_id": data["referrer_id"]})
        # 默认不自动开通套餐：新企业为"未开通"状态，由管理员手动「开通套餐」配置。
        # 仅当显式传入 plan_type 时才开通（兼容将来在创建表单里直接选套餐的场景）。
        if data.get("plan_type"):
            update_enterprise(_db, eid, {
                "plan_type": data["plan_type"],
                "plan_months": int(data.get("plan_months", 1)),
                "plan_start_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        return jsonify({"code": 0, "data": {"id": eid}})
    except Exception as e:
        logger.exception("创建企业失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ==================== 企业信息收集表 ====================

@auth_bp.route("/api/auth/enterprises/<int:eid>/survey-link", methods=["POST"])
@admin_required
def api_enterprise_survey_link(eid):
    """生成/获取企业信息收集表的专属链接（超管）。重复调用返回同一个 token。"""
    ent = get_enterprise_by_id(_db, eid)
    if not ent:
        return jsonify({"code": 404, "msg": "企业不存在"}), 404
    token = ensure_survey_token(_db, eid)
    submitted = bool((ent.get("survey_data") or "").strip())
    return jsonify({"code": 0, "data": {"token": token, "path": f"/enterprise-survey/{token}", "submitted": submitted}})


@auth_bp.route("/api/enterprise-survey/<token>", methods=["GET"])
def api_get_survey(token):
    """公开：客户打开收集表时拉取企业当前名称（用于页面展示）。"""
    ent = get_enterprise_by_survey_token(_db, token)
    if not ent:
        return jsonify({"code": 404, "msg": "链接无效或已失效"}), 404
    import json as _json
    prev = {}
    if (ent.get("survey_data") or "").strip():
        try:
            prev = _json.loads(ent["survey_data"])
        except Exception:
            prev = {}
    return jsonify({"code": 0, "data": {
        "enterprise_name": ent.get("name", ""),
        "submitted": bool(prev),
        "data": prev,
    }})


@auth_bp.route("/api/enterprise-survey/<token>", methods=["POST"])
def api_submit_survey(token):
    """公开：客户提交收集表 → 更新企业名称 + 按手机号建管理员账号(密码888888) + 存档全部内容。"""
    import json as _json
    ent = get_enterprise_by_survey_token(_db, token)
    if not ent:
        return jsonify({"code": 404, "msg": "链接无效或已失效"}), 404
    data = request.get_json(silent=True) or {}
    company = (data.get("company_full_name") or "").strip()
    contact_name = (data.get("contact_name") or "").strip()
    contact_phone = (data.get("contact_phone") or "").strip()
    # 必填：企业全称、联系人姓名+手机号
    if not company:
        return jsonify({"code": 400, "msg": "请填写企业全称"}), 400
    if not contact_name or not contact_phone:
        return jsonify({"code": 400, "msg": "请填写联系人姓名和手机号"}), 400
    if not _PHONE_RE.match(contact_phone):
        return jsonify({"code": 400, "msg": "联系人手机号格式不正确（需11位）"}), 400

    eid = ent["id"]
    # 1. 用企业全称更新企业名称、联系人
    update_enterprise(_db, eid, {"name": company, "contact_person": contact_name, "contact_phone": contact_phone})
    # 2. 按联系人手机号创建企业管理员账号（密码 888888）；手机号已注册则跳过创建
    admin_created = False
    if not get_user_by_phone(_db, contact_phone):
        try:
            create_user(_db, contact_phone, "888888", ROLE_ENTERPRISE, eid, contact_name, activated=1)
            admin_created = True
        except Exception:
            logger.exception("信息收集表创建管理员账号失败")
    # 3. 存档全部收集内容
    update_enterprise(_db, eid, {"survey_data": _json.dumps(data, ensure_ascii=False)})
    return jsonify({"code": 0, "msg": "提交成功，我们会尽快为您配置", "data": {"admin_created": admin_created}})


@auth_bp.route("/api/auth/enterprises/<int:eid>", methods=["PUT"])
@admin_required
def api_update_enterprise(eid):
    """更新企业信息"""
    from auth.db import PLAN_PRICES, add_wallet_commission, get_user_by_id
    data = request.get_json(silent=True) or {}
    try:
        commission_triggered = False
        commission_type = None
        commission_months = 1

        if "plan_type" in data or "plan_months" in data:
            stack = data.pop("stack_plan", False)
            new_type = data.get("plan_type")
            new_months = int(data.get("plan_months", 1))
            if stack:
                cur = get_enterprise_by_id(_db, eid)
                if cur and cur.get("plan_start_at"):
                    cur_start = cur["plan_start_at"]
                    if isinstance(cur_start, str):
                        cur_start = datetime.fromisoformat(cur_start.replace(" ", "T"))
                    cur_months = int(cur.get("plan_months") or 1)
                    m = cur_start.month - 1 + cur_months
                    expiry = cur_start.replace(year=cur_start.year + m // 12, month=m % 12 + 1)
                    if expiry > datetime.now():
                        data.pop("plan_type", None)
                        data.pop("plan_months", None)
                        data["next_plan_type"] = new_type
                        data["next_plan_months"] = new_months
                        data["next_plan_start_at"] = expiry.strftime("%Y-%m-%d %H:%M:%S")
                        update_enterprise(_db, eid, data)
                        # 叠加续费也触发佣金
                        commission_triggered = True
                        commission_type = new_type
                        commission_months = new_months
                        _try_pay_commission(eid, commission_type, commission_months, add_wallet_commission, get_enterprise_by_id, get_user_by_id)
                        return jsonify({"code": 0})
            data["plan_start_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data["next_plan_type"] = None
            data["next_plan_months"] = None
            data["next_plan_start_at"] = None
            commission_triggered = True
            commission_type = new_type
            commission_months = new_months

        updated_fields = update_enterprise(_db, eid, data)
        if commission_triggered:
            _try_pay_commission(eid, commission_type, commission_months, add_wallet_commission, get_enterprise_by_id, get_user_by_id)
        resp_data = {}
        if updated_fields and "onboarded_at" in updated_fields:
            resp_data["onboarded_at"] = updated_fields["onboarded_at"]
        return jsonify({"code": 0, "data": resp_data})
    except Exception as e:
        logger.exception("更新企业 %d 失败", eid)
        return jsonify({"code": 500, "msg": str(e)}), 500


def _try_pay_commission(eid, plan_type, plan_months, add_wallet_commission, get_enterprise_by_id, get_user_by_id):
    """查找企业推荐人并结算佣金"""
    from auth.db import PLAN_PRICES
    try:
        if not plan_type or plan_type == "enterprise_trial":
            return
        unit_price = PLAN_PRICES.get(plan_type, 0)
        if unit_price <= 0:
            return
        ent = get_enterprise_by_id(_db, eid)
        if not ent:
            return
        referrer_id = ent.get("referrer_id")
        if not referrer_id:
            return
        referrer = get_user_by_id(_db, referrer_id)
        if not referrer:
            return
        # 12 的整数倍按年费计算
        ANNUAL_PRICES = {"standard": 2999, "enterprise": 9999}
        if plan_months % 12 == 0:
            years = plan_months // 12
            total_price = ANNUAL_PRICES.get(plan_type, unit_price * 12) * years
            billing = f"{years}年（年费）"
        else:
            total_price = unit_price * plan_months
            billing = f"{plan_months}个月"
        commission = round(total_price * 0.2, 2)
        plan_label = "企业版" if plan_type == "enterprise" else "标准版"
        ent_name = ent.get("name", f"企业#{eid}")
        desc = f"推荐佣金：{ent_name} 开通{plan_label} {billing}"
        add_wallet_commission(_db, referrer_id, commission, desc, eid)
        logger.info("佣金结算：推荐人 %d 获得 %.2f 元（%s）", referrer_id, commission, desc)
    except Exception:
        logger.exception("佣金结算失败 enterprise=%d", eid)


@auth_bp.route("/api/auth/enterprises/<int:eid>", methods=["DELETE"])
@admin_required
def api_delete_enterprise(eid):
    """删除企业"""
    try:
        delete_enterprise(_db, eid)
        return jsonify({"code": 0})
    except Exception as e:
        logger.exception("删除企业 %d 失败", eid)
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/profile", methods=["PUT"])
@login_required
def api_update_profile():
    """当前用户更新自己的姓名"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"code": 400, "msg": "姓名不能为空"}), 400
    try:
        update_user(_db, g.current_user["user_id"], {"name": name})
        return jsonify({"code": 0, "msg": "用户名已更新"})
    except Exception as e:
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


# ============================================================
# 注册：发送验证码 + 完成注册
# ============================================================

@auth_bp.route("/api/auth/sms/send", methods=["POST"])
def api_send_sms():
    """发送注册验证码"""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()

    if not _PHONE_RE.match(phone):
        return jsonify({"code": 400, "msg": "请输入有效的11位手机号"}), 400

    code = generate_code(6)
    # 原子操作：冷却检查 + 插入记录一步完成，防并发重复发送
    remaining = try_insert_sms_record(_db, phone, code, "register", _SMS_COOLDOWN, _SMS_TTL)
    if remaining > 0:
        return jsonify({"code": 429, "msg": f"发送过于频繁，请 {remaining} 秒后重试"}), 429

    # 记录已入库，再发短信；发送失败则删除记录让用户可重试
    ok, err = send_sms_code(phone, code)
    if not ok:
        delete_sms_record(_db, phone, code, "register")
        return jsonify({"code": 500, "msg": f"短信发送失败：{err}"}), 500

    return jsonify({"code": 0, "msg": "验证码已发送"})


@auth_bp.route("/api/auth/sms/send-reset", methods=["POST"])
def api_send_sms_reset():
    """发送找回密码验证码"""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()

    if not _PHONE_RE.match(phone):
        return jsonify({"code": 400, "msg": "请输入有效的11位手机号"}), 400

    # 检查手机号是否注册
    user = get_user_by_phone(_db, phone)
    if not user:
        return jsonify({"code": 404, "msg": "该手机号未注册"}), 404

    code = generate_code(6)
    remaining = try_insert_sms_record(_db, phone, code, "reset", _SMS_COOLDOWN, _SMS_TTL)
    if remaining > 0:
        return jsonify({"code": 429, "msg": f"发送过于频繁，请 {remaining} 秒后重试"}), 429

    ok, err = send_sms_code(phone, code)
    if not ok:
        delete_sms_record(_db, phone, code, "reset")
        return jsonify({"code": 500, "msg": f"短信发送失败：{err}"}), 500

    return jsonify({"code": 0, "msg": "验证码已发送"})


@auth_bp.route("/api/auth/forgot-password", methods=["POST"])
def api_forgot_password():
    """通过短信验证码重置密码"""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    new_password = data.get("password") or ""

    if not _PHONE_RE.match(phone):
        return jsonify({"code": 400, "msg": "请输入有效的11位手机号"}), 400
    if not code:
        return jsonify({"code": 400, "msg": "请输入验证码"}), 400
    if len(new_password) < 6:
        return jsonify({"code": 400, "msg": "密码长度不能少于6位"}), 400

    if not verify_sms_code(_db, phone, code, "reset"):
        return jsonify({"code": 400, "msg": "验证码错误或已过期"}), 400

    user = get_user_by_phone(_db, phone)
    if not user:
        return jsonify({"code": 404, "msg": "用户不存在"}), 404

    reset_password(_db, user["id"], new_password)
    return jsonify({"code": 0, "msg": "密码已重置"})


@auth_bp.route("/api/auth/register", methods=["POST"])
def api_register():
    """用验证码完成注册（注册后自动登录）"""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    password = data.get("password", "")
    name = (data.get("name") or "").strip()
    ref = (data.get("ref") or "").strip()

    if not _PHONE_RE.match(phone):
        return jsonify({"code": 400, "msg": "请输入有效的11位手机号"}), 400
    if not code:
        return jsonify({"code": 400, "msg": "请输入验证码"}), 400
    if len(password) < 6:
        return jsonify({"code": 400, "msg": "密码长度不能少于6位"}), 400

    # 校验验证码
    if not verify_sms_code(_db, phone, code, "register"):
        return jsonify({"code": 400, "msg": "验证码错误或已过期"}), 400

    # 手机号唯一检查
    if get_user_by_phone(_db, phone):
        return jsonify({"code": 400, "msg": "该手机号已注册，请直接登录"}), 400

    # 解析邀请码
    referrer_id = None
    if ref:
        referrer = get_user_by_referral_code(_db, ref)
        if referrer:
            referrer_id = referrer["id"]

    try:
        # 自助注册：默认未激活，需管理员在账号管理激活后才能进入工作台
        user_id = create_user(_db, phone, password, ROLE_EMPLOYEE, None, name or phone, referrer_id=referrer_id, activated=0)
        user = get_user_by_id(_db, user_id)
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
                    "activated": int(user.get("activated", 0)),
                },
            },
        })
    except Exception as e:
        logger.exception("注册失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/invite-info", methods=["GET"])
@login_required
def api_invite_info():
    """获取当前用户的邀请码、邀请链接、下线列表"""
    user_id = g.current_user["user_id"]
    referral_code = get_or_create_referral_code(_db, user_id)
    downlines = get_referral_downlines(_db, user_id)
    from flask import request as _req
    base = _req.host_url.rstrip("/")
    invite_link = f"{base}/login?ref={referral_code}" if referral_code else ""
    return jsonify({
        "code": 0,
        "data": {
            "referral_code": referral_code,
            "invite_link": invite_link,
            "downline_count": len(downlines),
            "downlines": downlines,
        },
    })


# ==================== 钱包 ====================

@auth_bp.route("/api/auth/wallet", methods=["GET"])
@login_required
def api_get_wallet():
    """获取当前用户钱包余额和流水"""
    from auth.db import get_wallet_balance, get_wallet_transactions
    user_id = g.current_user["user_id"]
    balance = get_wallet_balance(_db, user_id)
    transactions = get_wallet_transactions(_db, user_id=user_id, limit=50)
    return jsonify({"code": 0, "data": {"balance": balance, "transactions": transactions}})


@auth_bp.route("/api/auth/wallet/withdraw", methods=["POST"])
@login_required
def api_wallet_withdraw():
    """申请提现"""
    from auth.db import request_withdrawal
    user_id = g.current_user["user_id"]
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"code": 400, "msg": "提现金额必须大于0"}), 400
    try:
        request_withdrawal(_db, user_id, amount)
        return jsonify({"code": 0, "msg": "提现申请已提交"})
    except ValueError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500


@auth_bp.route("/api/auth/wallet/all", methods=["GET"])
@admin_required
def api_get_all_wallet_transactions():
    """超管查看所有钱包流水"""
    from auth.db import get_wallet_transactions
    txns = get_wallet_transactions(_db, user_id=None, limit=200)
    return jsonify({"code": 0, "data": txns})


@auth_bp.route("/api/auth/wallet/withdraw/<int:txn_id>/approve", methods=["POST"])
@admin_required
def api_approve_withdrawal(txn_id):
    """超管批准提现"""
    from auth.db import approve_withdrawal
    approve_withdrawal(_db, txn_id)
    return jsonify({"code": 0, "msg": "已批准"})


@auth_bp.route("/api/auth/wallet/withdraw/<int:txn_id>/reject", methods=["POST"])
@admin_required
def api_reject_withdrawal(txn_id):
    """超管拒绝提现（退回余额）"""
    from auth.db import reject_withdrawal
    reject_withdrawal(_db, txn_id)
    return jsonify({"code": 0, "msg": "已拒绝"})


@auth_bp.route("/api/auth/enterprises/<int:eid>/referrer", methods=["PUT"])
@admin_required
def api_set_enterprise_referrer(eid):
    """超管设置企业推荐人"""
    from auth.db import update_enterprise
    data = request.get_json() or {}
    referrer_id = data.get("referrer_id")  # None 表示清除
    update_enterprise(_db, eid, {"referrer_id": referrer_id})
    return jsonify({"code": 0, "msg": "ok"})

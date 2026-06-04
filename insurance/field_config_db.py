"""
用户字段配置数据库操作层

负责 user_field_config（字段配置模板）和 user_active_template（启用模板）两张表的增删改查。
支持三级作用域：global（全局默认）、enterprise（企业级）、user（员工个人）。
同时提供公式引擎和配置应用函数，供导出时调用。
"""

import json
import logging
import re
from datetime import datetime

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# 支持的 config_type 类型
CONFIG_TYPES = ["company_alias", "policy_type_alias", "date_format", "fee_formula",
                "list_columns", "export_columns"]

# 默认模板名
DEFAULT_TEMPLATE_NAME = "默认模板"

# 默认列配置（与 field_mapping.py 的 OUTPUT_COLUMNS 保持一致）
DEFAULT_COLUMNS = [
    {"key": "文件名",   "visible": True, "order": 0, "display_name": "文件名",   "type": "record", "frozen": True},
    {"key": "文档类型", "visible": True, "order": 1, "display_name": "文档类型", "type": "record"},
    {"key": "承保公司", "visible": True, "order": 2, "display_name": "承保公司"},
    {"key": "保单号",   "visible": True, "order": 3, "display_name": "保单号"},
    {"key": "险种",     "visible": True, "order": 4, "display_name": "险种"},
    {"key": "车牌",     "visible": True, "order": 5, "display_name": "车牌"},
    {"key": "投保人",   "visible": True, "order": 6, "display_name": "投保人"},
    {"key": "被保人",   "visible": True, "order": 7, "display_name": "被保人"},
    {"key": "车主",     "visible": True, "order": 8, "display_name": "车主"},
    {"key": "签单日期", "visible": True, "order": 9, "display_name": "签单日期"},
    {"key": "起保日期", "visible": True, "order": 10, "display_name": "起保日期"},
    {"key": "终保日期", "visible": True, "order": 11, "display_name": "终保日期"},
    {"key": "保费",     "visible": True, "order": 12, "display_name": "保费"},
    {"key": "业务员",   "visible": True, "order": 13, "display_name": "业务员"},
    {"key": "跟单人",   "visible": True, "order": 14, "display_name": "跟单人"},
    {"key": "车船税",   "visible": True, "order": 15, "display_name": "车船税"},
    {"key": "状态",     "visible": True, "order": 16, "display_name": "状态",     "type": "record"},
    {"key": "创建时间", "visible": True, "order": 17, "display_name": "创建时间", "type": "record"},
    {"key": "识别时间", "visible": True, "order": 18, "display_name": "识别时间", "type": "record"},
    {"key": "群名",     "visible": True, "order": 19, "display_name": "群名",     "type": "record"},
    {"key": "发送人",   "visible": True, "order": 20, "display_name": "发送人",   "type": "record"},
    {"key": "被保险人身份证号码", "visible": True, "order": 21, "display_name": "被保险人身份证号码"},
    {"key": "投保人身份证号码", "visible": True, "order": 22, "display_name": "投保人身份证号码"},
    {"key": "车架号",     "visible": True, "order": 23, "display_name": "车架号"},
    {"key": "保司公司名称", "visible": True, "order": 24, "display_name": "保司公司名称"},
    {"key": "保司地址",     "visible": True, "order": 25, "display_name": "保司地址"},
    {"key": "保司（带地区）", "visible": True, "order": 26, "display_name": "保司（带地区）"},
    {"key": "交强到期时间", "visible": False, "order": 27, "display_name": "交强到期时间"},
]

# 记录级字段的 key 集合（用于前端区分渲染方式）
RECORD_LEVEL_FIELDS = {"文件名", "文档类型", "状态", "创建时间", "识别时间", "群名", "发送人"}

# ---- 按车牌合并导出 ----

# 共享字段（合并后不拆分，多条记录互相补充）
MERGE_SHARED_FIELDS = ["承保公司", "车牌", "投保人", "被保人", "车主", "业务员", "跟单人", "出单渠道", "签单日期", "起保日期", "终保日期"]

# 需要按险种拆分的差异字段
MERGE_SPLIT_FIELDS = ["保单号", "保费",
                       "佣金率", "佣金", "采购费率", "公司利润", "跟单人利润"]

# 险种前缀（与 policy_parser.get_policy_type_code 的分类对应）
MERGE_PREFIXES = {
    "commercial": "商业险",
    "compulsory": "交强险",
    "accident": "非车险",
    "non_vehicle": "非车险",
}

# 合并后自动生成的差异列名列表
MERGE_EXTRA_COLUMNS = []
for _field in MERGE_SPLIT_FIELDS:
    for _prefix in ["商业险", "交强险", "非车险"]:
        MERGE_EXTRA_COLUMNS.append(f"{_prefix}{_field}")


# ------------------------------------------------------------------ #
# 内部工具函数
# ------------------------------------------------------------------ #

def _scope_id_condition(scope_id):
    """
    生成 scope_id 的 SQL 条件片段和参数。
    scope_id 为 None 时使用 IS NULL，否则使用 = %s。
    返回 (condition_str, params_list)
    """
    if scope_id is None:
        return "scope_id IS NULL", []
    return "scope_id = %s", [scope_id]


def _convert_fmt_to_strftime(fmt: str) -> str:
    """将前端日期格式（如 YYYY-MM-DD HH:mm）转为 Python strftime 格式（如 %Y-%m-%d %H:%M）"""
    return (fmt
            .replace("YYYY", "%Y")
            .replace("MM", "%m")
            .replace("DD", "%d")
            .replace("HH", "%H")
            .replace("mm", "%M"))


def _format_plate_with_hyphen(plate: str) -> str:
    """车牌格式化：在省份简称+字母后插入连字符（粤BB446T → 粤B-B446T）"""
    if not plate or len(plate) < 3 or "-" in plate:
        return plate
    PROVINCE_CHARS = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁"
    if plate[0] in PROVINCE_CHARS and plate[1].isalpha() and plate[1].isupper():
        return plate[0] + plate[1] + "-" + plate[2:]
    return plate


def _build_date_str(dt: datetime, fmt: str, no_pad: bool = False) -> str:
    """
    根据前端日期格式（YYYY/MM/DD、YYYY-MM-DD HH:mm 等）构建日期字符串。
    no_pad=True 时月/日/时/分不补前导零（如 2026/5/7）；否则补零至两位（与 strftime 一致）。
    按 token 替换，YYYY 先于其它，MM(月)/mm(分) 区分大小写，互不干扰。
    """
    def _n(v: int) -> str:
        return str(v) if no_pad else f"{v:02d}"
    return (fmt
            .replace("YYYY", str(dt.year))
            .replace("MM", _n(dt.month))
            .replace("DD", _n(dt.day))
            .replace("HH", _n(dt.hour))
            .replace("mm", _n(dt.minute)))


def _format_date(raw: str, fmt: str, no_pad: bool = False) -> str:
    """
    日期格式转换（内部函数）。
    支持解析：YYYY-MM-DD[ HH:mm]、YYYY/MM/DD、YYYY年MM月DD日、YYYYMMDD、ISO 8601
    fmt 支持前端格式（YYYY-MM-DD / YYYY-MM-DD HH:mm）或 strftime 格式，自动识别。
    no_pad=True 时（仅前端格式生效）月/日/时/分不补前导零（如 2026/5/7）。
    解析失败则原样返回。
    """
    if not raw or not fmt:
        return raw
    # 是否前端格式（含 YYYY 或 MM+DD）：是则走可控补零的手动构建，否则用 strftime（始终补零）
    is_frontend_fmt = "YYYY" in fmt or ("MM" in fmt and "DD" in fmt)
    strftime_fmt = fmt if not is_frontend_fmt else _convert_fmt_to_strftime(fmt)

    def _render(dt: datetime) -> str:
        return _build_date_str(dt, fmt, no_pad) if is_frontend_fmt else dt.strftime(strftime_fmt)

    # 尝试多种格式解析（含时间的优先匹配）
    raw_str = str(raw).strip()
    # ISO 8601 / datetime 格式（如 2026-05-26T14:30:00 或 2026-05-26 14:30:00）
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?', raw_str)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
            return _render(dt)
        except (ValueError, OverflowError):
            pass
    # 纯日期格式
    patterns = [
        r'(\d{4})-(\d{1,2})-(\d{1,2})',
        r'(\d{4})/(\d{1,2})/(\d{1,2})',
        r'(\d{4})年(\d{1,2})月(\d{1,2})日?',
        r'(\d{4})(\d{2})(\d{2})',
    ]
    for pat in patterns:
        m = re.match(pat, raw_str)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return _render(dt)
            except (ValueError, OverflowError):
                pass
    return raw


# ------------------------------------------------------------------ #
# 模板管理
# ------------------------------------------------------------------ #

def list_templates(db, user_id: int, role: str, parent_id=None) -> dict:
    """
    列出用户可见的模板列表。

    返回格式：
    {
        "my": [{"name": "xxx", "visible": bool}],
        "enterprise": [{"name": "xxx"}]   # 仅 employee 角色有此键
    }
    如果 my 没有任何模板，返回默认的 [{"name": "默认模板", "visible": False}]

    role: "super_admin" 时 scope=global, scope_id=None
          "enterprise" 时 scope=enterprise, scope_id=parent_id（企业管理员）
          "employee" 时 scope=user, scope_id=user_id（普通员工）
    """
    # 确定 my 的查询范围
    if role == "super_admin":
        my_scope = "global"
        my_scope_id = None
    elif role == "enterprise":
        my_scope = "enterprise"
        my_scope_id = parent_id
    else:
        my_scope = "user"
        my_scope_id = user_id

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 查询 my 模板（DISTINCT template_name + visible_to_employees）
        sid_cond, sid_params = _scope_id_condition(my_scope_id)
        cursor.execute(
            f"SELECT DISTINCT template_name, visible_to_employees "
            f"FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} "
            f"ORDER BY template_name",
            [my_scope] + sid_params
        )
        my_rows = cursor.fetchall()

        # 每个模板名只保留一行（取 visible_to_employees 最大值，即有一条可见则视为可见）
        my_map = {}
        for row in my_rows:
            name = row["template_name"]
            visible = bool(row["visible_to_employees"])
            if name not in my_map:
                my_map[name] = visible
            else:
                my_map[name] = my_map[name] or visible

        my_list = [{"name": k, "visible": v} for k, v in my_map.items()]

        # 如果没有任何模板，返回默认模板
        if not my_list:
            my_list = [{"name": DEFAULT_TEMPLATE_NAME, "visible": False}]

        result = {"my": my_list}

        # employee 角色额外查询企业可见模板
        if role == "employee" and parent_id is not None:
            ent_sid_cond, ent_sid_params = _scope_id_condition(parent_id)
            cursor.execute(
                f"SELECT DISTINCT template_name "
                f"FROM user_field_config "
                f"WHERE scope = 'enterprise' AND {ent_sid_cond} AND visible_to_employees = 1 "
                f"ORDER BY template_name",
                ent_sid_params
            )
            ent_rows = cursor.fetchall()
            result["enterprise"] = [{"name": row["template_name"]} for row in ent_rows]

        # 非超管角色查询系统模板（global scope，只读）
        if role != "super_admin":
            cursor.execute(
                "SELECT DISTINCT template_name "
                "FROM user_field_config "
                "WHERE scope = 'global' AND scope_id IS NULL "
                "ORDER BY template_name"
            )
            sys_rows = cursor.fetchall()
            if sys_rows:
                result["system"] = [{"name": row["template_name"]} for row in sys_rows]
            else:
                result["system"] = [{"name": DEFAULT_TEMPLATE_NAME}]

        return result
    finally:
        conn.close()


def list_all_templates(db) -> dict:
    """
    超管专用：列出系统所有模板，按 scope 分组返回。
    返回格式：
    {
        "global": [{"name": "xxx", "visible": bool}],
        "enterprises": [
            {"enterprise_id": 1, "enterprise_name": "A公司", "templates": [{"name": "xxx", "visible": bool}]}
        ],
        "users": [
            {"user_id": 1, "user_name": "张三", "enterprise_name": "A公司", "templates": [{"name": "xxx", "visible": bool}]}
        ]
    }
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 1. 查询所有模板（按 scope 分组）
        cursor.execute(
            "SELECT DISTINCT scope, scope_id, template_name, MAX(visible_to_employees) as visible "
            "FROM user_field_config "
            "GROUP BY scope, scope_id, template_name "
            "ORDER BY scope, scope_id, template_name"
        )
        rows = cursor.fetchall()

        # 2. 收集需要查询名称的 enterprise 和 user ID
        ent_ids = set()
        user_ids = set()
        for row in rows:
            if row["scope"] == "enterprise" and row["scope_id"]:
                ent_ids.add(row["scope_id"])
            elif row["scope"] == "user" and row["scope_id"]:
                user_ids.add(row["scope_id"])

        # 3. 批量查询企业名称
        ent_names = {}
        if ent_ids:
            placeholders = ",".join(["%s"] * len(ent_ids))
            cursor.execute(f"SELECT id, name FROM enterprises WHERE id IN ({placeholders})", list(ent_ids))
            for r in cursor.fetchall():
                ent_names[r["id"]] = r["name"]

        # 4. 批量查询用户名称和所属企业
        user_info = {}
        if user_ids:
            placeholders = ",".join(["%s"] * len(user_ids))
            cursor.execute(
                f"SELECT u.id, u.name, u.phone, u.parent_id, e.name as enterprise_name "
                f"FROM users u LEFT JOIN enterprises e ON u.parent_id = e.id "
                f"WHERE u.id IN ({placeholders})",
                list(user_ids)
            )
            for r in cursor.fetchall():
                user_info[r["id"]] = {
                    "name": r["name"] or r["phone"] or str(r["id"]),
                    "enterprise_name": r["enterprise_name"] or "",
                    "parent_id": r["parent_id"],
                }

        # 5. 组装结果
        global_templates = []
        ent_map = {}  # enterprise_id -> [templates]
        user_map = {}  # user_id -> [templates]

        for row in rows:
            tpl = {"name": row["template_name"], "visible": bool(row["visible"])}
            if row["scope"] == "global":
                global_templates.append(tpl)
            elif row["scope"] == "enterprise":
                eid = row["scope_id"]
                ent_map.setdefault(eid, []).append(tpl)
            elif row["scope"] == "user":
                uid = row["scope_id"]
                user_map.setdefault(uid, []).append(tpl)

        if not global_templates:
            global_templates = [{"name": DEFAULT_TEMPLATE_NAME, "visible": False}]

        enterprises = []
        for eid, templates in ent_map.items():
            enterprises.append({
                "enterprise_id": eid,
                "enterprise_name": ent_names.get(eid, f"企业#{eid}"),
                "templates": templates,
            })

        users = []
        for uid, templates in user_map.items():
            info = user_info.get(uid, {"name": f"用户#{uid}", "enterprise_name": "", "parent_id": None})
            users.append({
                "user_id": uid,
                "user_name": info["name"],
                "enterprise_name": info["enterprise_name"],
                "templates": templates,
            })

        return {
            "global": global_templates,
            "enterprises": enterprises,
            "users": users,
        }
    finally:
        conn.close()


def rename_template(db, scope: str, scope_id, old_name: str, new_name: str):
    """
    重命名模板（更新 user_field_config 中的 template_name）。
    同时更新 user_active_template 中引用该模板名的记录。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sid_cond, sid_params = _scope_id_condition(scope_id)

        # 更新配置表中的模板名
        cursor.execute(
            f"UPDATE user_field_config SET template_name = %s "
            f"WHERE scope = %s AND {sid_cond} AND template_name = %s",
            [new_name, scope] + sid_params + [old_name]
        )

        # 同步更新启用模板表
        if scope == "user":
            # 个人模板：只更新自己的
            cursor.execute(
                "UPDATE user_active_template SET template_name = %s "
                "WHERE user_id = %s AND active_source = 'own' AND template_name = %s",
                (new_name, scope_id, old_name)
            )
        elif scope == "enterprise":
            # 企业模板：更新使用了该企业模板的所有员工
            cursor.execute(
                "UPDATE user_active_template SET template_name = %s "
                "WHERE active_source = 'enterprise' AND template_name = %s "
                "AND user_id IN (SELECT id FROM users WHERE parent_id = %s)",
                (new_name, old_name, scope_id)
            )

        conn.commit()
        logger.debug("模板重命名: %s → %s (scope=%s, scope_id=%s)", old_name, new_name, scope, scope_id)
    finally:
        conn.close()


def update_template_visibility(db, scope: str, scope_id, template_name: str, visible: bool):
    """
    更新模板的对员工可见性（仅 enterprise scope 有实际意义）。
    将该模板下所有配置行的 visible_to_employees 统一设置。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"UPDATE user_field_config SET visible_to_employees = %s "
            f"WHERE scope = %s AND {sid_cond} AND template_name = %s",
            [1 if visible else 0, scope] + sid_params + [template_name]
        )
        conn.commit()
        logger.debug("模板可见性更新: template=%s visible=%s", template_name, visible)
    finally:
        conn.close()


def delete_template(db, scope: str, scope_id, template_name: str):
    """
    删除模板（删除 user_field_config 中该模板的所有配置行）。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND template_name = %s",
            [scope] + sid_params + [template_name]
        )

        # 清理启用模板表中对该模板的引用，回退到默认模板
        if scope == "user":
            cursor.execute(
                "UPDATE user_active_template SET template_name = %s "
                "WHERE user_id = %s AND active_source = 'own' AND template_name = %s",
                (DEFAULT_TEMPLATE_NAME, scope_id, template_name)
            )
        elif scope == "enterprise":
            cursor.execute(
                "UPDATE user_active_template SET template_name = %s, active_source = 'own' "
                "WHERE active_source = 'enterprise' AND template_name = %s "
                "AND user_id IN (SELECT id FROM users WHERE parent_id = %s)",
                (DEFAULT_TEMPLATE_NAME, template_name, scope_id)
            )

        conn.commit()
        logger.debug("模板已删除: template=%s (scope=%s, scope_id=%s)", template_name, scope, scope_id)
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 配置读写
# ------------------------------------------------------------------ #

def get_template_config(db, scope: str, scope_id, template_name: str) -> dict:
    """
    获取指定模板的所有配置，按 config_type 分组返回。

    返回格式：
    {
        "company_alias": [{"key": "xxx", "value": "yyy"}],
        "policy_type_alias": [...],
        "date_format": [...],
        "fee_formula": [...]
    }
    config_value 自动尝试 json.loads，失败则返回原字符串。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"SELECT config_type, config_key, config_value "
            f"FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND template_name = %s "
            f"ORDER BY config_type, id",
            [scope] + sid_params + [template_name]
        )
        rows = cursor.fetchall()

        # 按 config_type 分组，初始化所有类型为空列表
        result = {ct: [] for ct in CONFIG_TYPES}
        for row in rows:
            ct = row["config_type"]
            raw_val = row["config_value"]
            # 自动尝试 JSON 反序列化
            try:
                value = json.loads(raw_val)
            except (TypeError, json.JSONDecodeError, ValueError):
                value = raw_val
            if ct not in result:
                result[ct] = []
            result[ct].append({"key": row["config_key"], "value": value})

        return result
    finally:
        conn.close()


def save_template_config(db, scope: str, scope_id, template_name: str,
                         config_type: str, items: list, visible: bool = False):
    """
    保存指定 config_type 的配置（先删后插，整体替换）。

    items: [{"key": "xxx", "value": "yyy"}, ...]
    value 如果不是字符串则自动 json.dumps。
    visible: 是否对员工可见（visible_to_employees）
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sid_cond, sid_params = _scope_id_condition(scope_id)

        # 先删除该 config_type 下的所有旧配置
        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND template_name = %s AND config_type = %s",
            [scope] + sid_params + [template_name, config_type]
        )

        # 批量插入新配置
        visible_val = 1 if visible else 0
        for item in items:
            key = item.get("key", "")
            value = item.get("value", "")
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            if not key:
                continue

            # scope_id 为 None 时需要特殊处理 INSERT
            if scope_id is None:
                cursor.execute(
                    "INSERT INTO user_field_config "
                    "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                    "VALUES (%s, NULL, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE config_value = VALUES(config_value), "
                    "visible_to_employees = VALUES(visible_to_employees)",
                    [scope, template_name, config_type, key, value, visible_val]
                )
            else:
                cursor.execute(
                    "INSERT INTO user_field_config "
                    "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE config_value = VALUES(config_value), "
                    "visible_to_employees = VALUES(visible_to_employees)",
                    [scope, scope_id, template_name, config_type, key, value, visible_val]
                )

        conn.commit()
        logger.debug("模板配置已保存: scope=%s template=%s config_type=%s items=%d",
                     scope, template_name, config_type, len(items))
    finally:
        conn.close()


def save_full_template(db, scope: str, scope_id, template_name: str,
                       config: dict, visible: bool = False):
    """
    保存完整模板配置（所有 4 种 config_type）。

    config 格式：
    {
        "company_alias": [{"key": "xxx", "value": "yyy"}],
        "policy_type_alias": [...],
        "date_format": [...],
        "fee_formula": [...]
    }
    """
    # 只保存前端传入的 config_type，不传的不动（避免误删 list_columns / export_columns）
    for config_type in CONFIG_TYPES:
        if config_type not in config:
            continue
        items = config[config_type]
        save_template_config(db, scope, scope_id, template_name, config_type, items, visible)


# ------------------------------------------------------------------ #
# 启用模板
# ------------------------------------------------------------------ #

def get_active_template(db, user_id: int) -> dict:
    """
    获取用户当前启用的模板信息。
    无记录时返回默认值 {"source": "own", "template_name": "默认模板"}
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT active_source, template_name FROM user_active_template WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return {"source": "own", "template_name": DEFAULT_TEMPLATE_NAME}
        return {"source": row["active_source"], "template_name": row["template_name"]}
    finally:
        conn.close()


def set_active_template(db, user_id: int, source: str, template_name: str):
    """
    设置用户启用的模板（REPLACE INTO，存在则替换，不存在则插入）。

    source: "own"（使用自己的模板）或 "enterprise"（使用企业模板）
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "REPLACE INTO user_active_template (user_id, active_source, template_name) "
            "VALUES (%s, %s, %s)",
            (user_id, source, template_name)
        )
        conn.commit()
        logger.debug("用户启用模板已更新: user_id=%s source=%s template=%s",
                     user_id, source, template_name)
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 导出时用：获取有效配置
# ------------------------------------------------------------------ #

def get_effective_config(db, user_id: int, role: str, parent_id=None) -> dict:
    """
    获取用户当前启用模板的配置。

    先调用 get_active_template 获取启用信息，再根据 source 决定查询哪个作用域：
    - source="own": 查询用户自己的模板（scope=user, scope_id=user_id）
    - source="enterprise": 查询企业模板（scope=enterprise, scope_id=parent_id）

    返回与 get_template_config 相同格式的 dict。
    """
    active = get_active_template(db, user_id)
    source = active.get("source", "own")
    template_name = active.get("template_name", DEFAULT_TEMPLATE_NAME)

    if source == "system":
        scope = "global"
        scope_id = None
    elif source == "enterprise":
        scope = "enterprise"
        scope_id = parent_id
    else:
        # 根据角色确定 scope
        if role == "super_admin":
            scope = "global"
            scope_id = None
        elif role == "enterprise":
            scope = "enterprise"
            scope_id = parent_id
        else:
            scope = "user"
            scope_id = user_id

    config = get_template_config(db, scope, scope_id, template_name)

    # 注入模板级开关：平安车牌格式化
    if get_plate_format_pingan(db, user_id, role, parent_id):
        if config is None:
            config = {}
        config["plate_format_pingan"] = True

    # 注入「百分比」标记字段集合：列配置中勾选了 is_percent 的自定义字段
    # 用于 apply_user_config_to_fields 把用户填的百分数（10）按 10% 处理
    percent_keys = get_percent_fields(db, user_id, role, parent_id)
    if percent_keys:
        if config is None:
            config = {}
        config["percent_fields"] = percent_keys

    return config


# ------------------------------------------------------------------ #
# 列配置读写（list_columns / export_columns）
# ------------------------------------------------------------------ #

def _fix_missing_defaults(db, config_type, columns, scope, scope_id):
    """
    自动补充 DEFAULT_COLUMNS 中有但用户配置里不存在的字段到末尾。
    例如新增了「车主」「文件名」等字段后，已保存的配置自动补上。
    同时迁移已废弃的列名（如「时间」→「创建时间」）。
    """
    changed = False
    # 迁移旧列名：「时间」→「创建时间」
    for c in columns:
        if c["key"] == "时间":
            c["key"] = "创建时间"
            c["display_name"] = c.get("display_name", "").replace("时间", "创建时间") or "创建时间"
            changed = True

    # 去重：保留每个 key 第一次出现的条目
    seen_keys = set()
    deduped = []
    for c in columns:
        if c["key"] not in seen_keys:
            seen_keys.add(c["key"])
            deduped.append(c)
    if len(deduped) < len(columns):
        removed = len(columns) - len(deduped)
        columns = deduped
        changed = True
        logger.debug("自动去重 %s 重复字段 %d 个", config_type, removed)

    existing_keys = {c["key"] for c in columns}
    max_order = len(columns)
    added = 0
    # 已有配置的用户，新补充的字段默认不显示，避免影响已有模板
    has_existing_config = len(columns) > 0
    for dc in DEFAULT_COLUMNS:
        if dc["key"] not in existing_keys:
            new_col = {**dc, "order": max_order}
            if has_existing_config:
                new_col["visible"] = False
            columns.append(new_col)
            max_order += 1
            added += 1

    if added or changed:
        save_column_config(db, config_type, scope, scope_id, columns)
        if added:
            logger.debug("自动补充 %s 默认字段 %d 个", config_type, added)
        if changed:
            logger.debug("迁移 %s 旧列名（时间→创建时间）", config_type)
    return columns


def _fix_merge_columns(db, columns, scope, scope_id, user_id, role, parent_id):
    """
    自动修正 export_columns 中的合并字段：
    - 合并开启时：清理不在 MERGE_EXTRA_COLUMNS 中的旧 merge 字段，补充缺失的
    - 合并关闭时：不处理
    修正后自动回写数据库。
    """
    merge_enabled = get_merge_by_plate(db, user_id, role, parent_id)
    if not merge_enabled:
        return columns

    valid_set = set(MERGE_EXTRA_COLUMNS)
    shared_set = set(MERGE_SHARED_FIELDS)
    original_json = json.dumps(columns, ensure_ascii=False)

    # 清理前端临时标记
    for c in columns:
        for k in list(c.keys()):
            if k.startswith("_"):
                del c[k]

    # 清理旧版 merge 字段
    columns = [c for c in columns if not c.get("merge") or c["key"] in valid_set]

    # 补充缺失的 merge 字段
    existing_keys = {c["key"] for c in columns}
    max_order = len(columns)
    for col_name in MERGE_EXTRA_COLUMNS:
        if col_name not in existing_keys:
            columns.append({"key": col_name, "visible": True, "order": max_order,
                            "display_name": col_name, "merge": True})
            max_order += 1

    # 有变更则回写数据库
    new_json = json.dumps(columns, ensure_ascii=False)
    if new_json != original_json:
        save_column_config(db, "export_columns", scope, scope_id, columns)
        logger.debug("自动修正 export_columns 合并字段")

    return columns


def get_column_config(db, config_type: str, user_id: int, role: str, parent_id=None) -> dict:
    """
    获取列配置（list_columns 或 export_columns），按优先级查找：
    user → enterprise → global → 代码默认值。

    当用户启用了企业/系统模板时，优先从对应 scope 查询。

    返回：{"source": "enterprise", "columns": [...]}
    """
    if config_type not in ("list_columns", "export_columns"):
        return {"source": "default", "columns": list(DEFAULT_COLUMNS)}

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 获取当前活跃模板
        active = get_active_template(db, user_id)
        active_tpl = active.get("template_name", DEFAULT_TEMPLATE_NAME)
        active_source = active.get("source", "own")

        # 根据 active_source 决定搜索顺序
        search_order = []
        if active_source == "system":
            # 用户选了系统模板，直接查全局
            search_order.append(("global", None, "global"))
        elif active_source == "enterprise" and parent_id is not None:
            # 用户选了企业模板，直接查企业级
            search_order.append(("enterprise", parent_id, "enterprise"))
        else:
            # 用户选了自己的模板，按角色优先级查
            if role == "employee" and parent_id is not None:
                search_order.append(("user", user_id, "user"))
                search_order.append(("enterprise", parent_id, "enterprise"))
            elif role == "enterprise":
                search_order.append(("enterprise", parent_id or user_id, "enterprise"))
            elif role == "super_admin":
                search_order.append(("global", None, "global"))

        for scope, scope_id, source_label in search_order:
            sid_cond, sid_params = _scope_id_condition(scope_id)
            # 优先查活跃模板，找不到再查默认模板
            for tpl in ([active_tpl, DEFAULT_TEMPLATE_NAME] if active_tpl != DEFAULT_TEMPLATE_NAME else [DEFAULT_TEMPLATE_NAME]):
                cursor.execute(
                    f"SELECT config_value FROM user_field_config "
                    f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns' "
                    f"AND template_name = %s LIMIT 1",
                    [scope] + sid_params + [config_type, tpl]
                )
                row = cursor.fetchone()
                if row:
                    break
            else:
                row = None
            if row:
                try:
                    columns = json.loads(row["config_value"])
                    # 自动补充 DEFAULT_COLUMNS 中新增的字段
                    columns = _fix_missing_defaults(db, config_type, columns, scope, scope_id)
                    # export_columns 自动修正合并字段（清理旧版 + 补充缺失）
                    if config_type == "export_columns":
                        columns = _fix_merge_columns(db, columns, scope, scope_id, user_id, role, parent_id)
                    return {"source": source_label, "columns": columns}
                except (TypeError, json.JSONDecodeError):
                    pass

        return {"source": "default", "columns": list(DEFAULT_COLUMNS)}
    finally:
        conn.close()


def get_percent_fields(db, user_id: int, role: str, parent_id=None) -> set:
    """
    返回被标记为「百分比」的自定义字段 key 集合。

    取导出列配置 + 页面列配置中 is_percent=True 的列（并集，前端两侧同步，
    取并集可兼容任一侧先勾选/同步未及时的情况）。这类字段：
    - 用户填的是百分数（如填 "10" 表示 10%）
    - 公式计算时按 0.1 参与，显示时转回 "10%"
    """
    keys = set()
    for config_type in ("export_columns", "list_columns"):
        try:
            cfg = get_column_config(db, config_type, user_id, role, parent_id)
            for c in cfg.get("columns", []):
                if c.get("is_percent") and c.get("key"):
                    keys.add(c["key"])
        except Exception:
            logger.debug("读取百分比字段配置失败: %s", config_type)
    return keys


def save_column_config(db, config_type: str, scope: str, scope_id, columns: list,
                       template_name: str = None):
    """
    保存列配置。columns 为 [{"key":"xxx","visible":true,"order":0,"display_name":"xxx"}, ...]
    使用 config_key = 'columns'，整体存为一条 JSON 记录。
    template_name 为 None 时使用默认模板名。
    """
    tpl_name = template_name or DEFAULT_TEMPLATE_NAME
    visible_val = 0 if template_name else 1
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)

        # 先删旧记录
        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns' "
            f"AND template_name = %s",
            [scope] + sid_params + [config_type, tpl_name]
        )

        # 清理前端 UI 临时标记，不持久化
        clean_columns = []
        for c in columns:
            col = {k: v for k, v in c.items() if not k.startswith("_")}
            clean_columns.append(col)

        # 插入新记录
        value = json.dumps(clean_columns, ensure_ascii=False)
        if scope_id is None:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, NULL, %s, %s, 'columns', %s, %s)",
                [scope, tpl_name, config_type, value, visible_val]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, %s, %s, %s, 'columns', %s, %s)",
                [scope, scope_id, tpl_name, config_type, value, visible_val]
            )
        conn.commit()
        logger.debug("列配置已保存: scope=%s config_type=%s", scope, config_type)
    finally:
        conn.close()


def delete_column_config(db, config_type: str, scope: str, scope_id):
    """删除当前模板的列配置（不影响备份模板），回退到上一级默认。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)
        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = %s AND config_key = 'columns' "
            f"AND template_name = %s",
            [scope] + sid_params + [config_type, DEFAULT_TEMPLATE_NAME]
        )
        conn.commit()
        logger.debug("列配置已删除: scope=%s config_type=%s", scope, config_type)
    finally:
        conn.close()


def get_merge_by_plate(db, user_id: int, role: str, parent_id=None) -> bool:
    """获取合并导出开关状态，按优先级查找，默认关闭。当用户启用企业/系统模板时直接查对应 scope。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 根据 active_source 决定搜索顺序
        active = get_active_template(db, user_id)
        active_source = active.get("source", "own")

        search_order = []
        if active_source == "system":
            search_order.append(("global", None))
        elif active_source == "enterprise" and parent_id is not None:
            search_order.append(("enterprise", parent_id))
        else:
            if role == "employee" and parent_id is not None:
                search_order.append(("user", user_id))
                search_order.append(("enterprise", parent_id))
            elif role == "enterprise":
                search_order.append(("enterprise", parent_id or user_id))
            elif role == "super_admin":
                search_order.append(("global", None))

        for scope, scope_id in search_order:
            sid_cond, sid_params = _scope_id_condition(scope_id)
            cursor.execute(
                f"SELECT config_value FROM user_field_config "
                f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
                f"AND config_key = 'merge_by_plate' LIMIT 1",
                [scope] + sid_params
            )
            row = cursor.fetchone()
            if row:
                return row["config_value"] == "1"
        return False
    finally:
        conn.close()


def save_merge_by_plate(db, scope: str, scope_id, enabled: bool):
    """保存合并导出开关。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
            f"AND config_key = 'merge_by_plate'",
            [scope] + sid_params
        )

        value = "1" if enabled else "0"
        if scope_id is None:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, NULL, %s, 'export_columns', 'merge_by_plate', %s, 1)",
                [scope, DEFAULT_TEMPLATE_NAME, value]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, %s, %s, 'export_columns', 'merge_by_plate', %s, 1)",
                [scope, scope_id, DEFAULT_TEMPLATE_NAME, value]
            )
        conn.commit()
        logger.debug("合并开关已保存: scope=%s enabled=%s", scope, enabled)
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 平安车牌格式化开关（模板级配置）
# ------------------------------------------------------------------ #

def get_plate_format_pingan(db, user_id: int, role: str, parent_id=None) -> bool:
    """获取平安车牌格式化开关状态，按优先级查找，默认关闭。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        active = get_active_template(db, user_id)
        active_source = active.get("source", "own")

        search_order = []
        if active_source == "system":
            search_order.append(("global", None))
        elif active_source == "enterprise" and parent_id is not None:
            search_order.append(("enterprise", parent_id))
        else:
            if role == "employee" and parent_id is not None:
                search_order.append(("user", user_id))
                search_order.append(("enterprise", parent_id))
            elif role == "enterprise":
                search_order.append(("enterprise", parent_id or user_id))
            elif role == "super_admin":
                search_order.append(("global", None))

        for scope, scope_id in search_order:
            sid_cond, sid_params = _scope_id_condition(scope_id)
            cursor.execute(
                f"SELECT config_value FROM user_field_config "
                f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
                f"AND config_key = 'plate_format_pingan' LIMIT 1",
                [scope] + sid_params
            )
            row = cursor.fetchone()
            if row:
                return row["config_value"] == "1"
        return False
    finally:
        conn.close()


def save_plate_format_pingan(db, scope: str, scope_id, enabled: bool):
    """保存平安车牌格式化开关。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        sid_cond, sid_params = _scope_id_condition(scope_id)

        cursor.execute(
            f"DELETE FROM user_field_config "
            f"WHERE scope = %s AND {sid_cond} AND config_type = 'export_columns' "
            f"AND config_key = 'plate_format_pingan'",
            [scope] + sid_params
        )

        value = "1" if enabled else "0"
        if scope_id is None:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, NULL, %s, 'export_columns', 'plate_format_pingan', %s, 1)",
                [scope, DEFAULT_TEMPLATE_NAME, value]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config "
                "(scope, scope_id, template_name, config_type, config_key, config_value, visible_to_employees) "
                "VALUES (%s, %s, %s, 'export_columns', 'plate_format_pingan', %s, 1)",
                [scope, scope_id, DEFAULT_TEMPLATE_NAME, value]
            )
        conn.commit()
        logger.debug("平安车牌格式化开关已保存: scope=%s enabled=%s", scope, enabled)
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 自定义字段固定值（独立于模板，始终按用户个人存储）
# ------------------------------------------------------------------ #

def get_user_fixed_values(db, user_id: int) -> dict:
    """
    获取用户个人的自定义字段固定值。
    始终从 scope=user 读取，不受模板切换影响。
    返回 {字段名: 固定值}。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT config_value FROM user_field_config "
            "WHERE scope = 'user' AND scope_id = %s AND config_type = 'custom_fixed_values' "
            "AND config_key = 'values' LIMIT 1",
            [user_id]
        )
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row["config_value"])
            except (TypeError, json.JSONDecodeError):
                pass
        return {}
    finally:
        conn.close()


def save_user_fixed_values(db, user_id: int, values: dict):
    """
    保存用户个人的自定义字段固定值。
    始终写入 scope=user，不受模板切换影响。
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        json_val = json.dumps(values, ensure_ascii=False)
        cursor.execute(
            "SELECT id FROM user_field_config "
            "WHERE scope = 'user' AND scope_id = %s AND config_type = 'custom_fixed_values' "
            "AND config_key = 'values' LIMIT 1",
            [user_id]
        )
        if cursor.fetchone():
            cursor.execute(
                "UPDATE user_field_config SET config_value = %s, updated_at = NOW() "
                "WHERE scope = 'user' AND scope_id = %s AND config_type = 'custom_fixed_values' "
                "AND config_key = 'values'",
                [json_val, user_id]
            )
        else:
            cursor.execute(
                "INSERT INTO user_field_config (scope, scope_id, config_type, config_key, config_value, template_name) "
                "VALUES ('user', %s, 'custom_fixed_values', 'values', %s, %s)",
                [user_id, json_val, DEFAULT_TEMPLATE_NAME]
            )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 公式引擎
# ------------------------------------------------------------------ #

def evaluate_formula(formula: str, fields: dict) -> str:
    """
    计算公式结果。

    - 替换 × → *, ÷ → /
    - 按字段名（从长到短）替换为实际值（float 转换，失败用 0）
    - 正则校验安全（只允许数字 + 运算符 + 括号 + 空格 + 小数点）
    - eval 计算，失败返回空字符串

    示例：
        formula = "保费合计 × 0.8 + 手续费"
        fields = {"保费合计": "1000", "手续费": "50"}
        → "850.0"
    """
    if not formula:
        return ""

    expr = formula
    # 替换中文运算符
    expr = expr.replace("×", "*").replace("÷", "/")

    # 按字段名长度降序替换（避免短字段名替换掉长字段名的一部分）
    sorted_keys = sorted(fields.keys(), key=lambda k: len(k), reverse=True)
    for key in sorted_keys:
        if key in expr:
            raw_val = fields.get(key, "")
            try:
                # 尝试提取数字（去除非数字字符如"元"、"，"等）
                clean_val = re.sub(r"[^\d.]", "", str(raw_val))
                num_val = float(clean_val) if clean_val else 0.0
            except (ValueError, TypeError):
                num_val = 0.0
            expr = expr.replace(key, str(num_val))

    # 安全校验：只允许数字、运算符、括号、空格、小数点
    if not re.match(r'^[\d\s\+\-\*\/\.\(\)]+$', expr):
        logger.warning("公式安全校验失败，拒绝执行: %s", expr)
        return ""

    try:
        result = eval(expr)  # noqa: S307 # 已通过正则白名单校验
        if isinstance(result, float):
            # 截断到2位小数（不四舍五入）
            import math
            result = math.floor(result * 100) / 100
            if result == int(result):
                return str(int(result))
            return f"{result:.2f}"
        return str(result)
    except Exception as e:
        logger.debug("公式计算失败: %s，expr=%s", e, expr)
        return ""


def match_fee_formulas(config: dict, fields: dict) -> list:
    """
    筛选出匹配当前记录的公式列表。

    返回 [(target_field, formula, is_computed), ...]：
    - target_field：公式目标字段名
    - formula：公式表达式（display，按字段名引用）
    - is_computed：是否为「计算公式」（引用了记录中的其他字段，如"保费 × 应付费率"）。
                   False 表示「常量公式」（纯数字，如"0"、"500"）。

    公式 key 格式："目标字段:公司简称:险种简称"，按公司/险种匹配当前记录。
    供 apply_user_config_to_fields 与导出（生成 Excel 活公式）共用，避免匹配逻辑重复。
    """
    record_company = fields.get("保险公司简称") or fields.get("承保公司") or fields.get("保险公司") or ""
    record_policy_type = fields.get("险种类型") or fields.get("险种") or ""
    all_field_keys = set(fields.keys())

    matched = []
    for item in (config or {}).get("fee_formula", []):
        raw_key = item.get("key", "")
        raw_value = item.get("value", "")
        if not raw_key:
            continue
        parts = raw_key.split(":", 2)
        target_field = parts[0]
        rule_company = parts[1] if len(parts) > 1 else ""
        rule_policy_type = parts[2] if len(parts) > 2 else ""
        if rule_company and rule_company != "全部" and rule_company not in record_company and record_company not in rule_company:
            continue
        if rule_policy_type and rule_policy_type != "全部" and rule_policy_type not in record_policy_type and record_policy_type not in rule_policy_type:
            continue
        if isinstance(raw_value, dict):
            formula = raw_value.get("display", "")
        else:
            formula = str(raw_value)
        if not (target_field and formula):
            continue
        # 计算公式 vs 常量公式：公式中是否出现记录里的其他字段名
        normalized = formula.replace("×", "*").replace("÷", "/")
        is_computed = any(k in normalized for k in all_field_keys)
        matched.append((target_field, formula, is_computed))
    return matched


def apply_user_config_to_fields(config: dict, fields: dict, formula_only: bool = False) -> dict:
    """
    将用户字段配置应用到一条保单记录，返回 fields 的副本。

    config 格式（同 get_template_config 返回值）：
    {
        "company_alias": [{"key": "原名", "value": "简称"}],
        "policy_type_alias": [{"key": "原名", "value": "简称"}],
        "date_format": [{"key": "字段名", "value": "%Y年%m月%d日"}],
        "fee_formula": [{"key": "目标字段名", "value": "公式"}]
    }

    处理顺序：
    1. 公司简称替换（company_alias）
    2. 险种简称替换（policy_type_alias）
    3. 日期格式化（date_format）
    4. 公式计算（fee_formula）

    formula_only=True 时跳过 1-3 步，仅执行公式计算和固定值填充。
    用于数据已经过完整处理但需要重新计算公式的场景（如导出）。
    """
    result = dict(fields)

    # 手动修改过的字段不被简称替换/公式覆盖
    manual_fields = config.get("_manual_fields") or set()

    if formula_only:
        # 跳过别名/日期格式，仅执行公式计算（从步骤4开始）
        # 先将"率"字段中的百分比格式还原为数值，便于公式引用
        for key, val in result.items():
            if "率" in key and isinstance(val, str) and val.endswith("%"):
                try:
                    num = float(val[:-1])
                    if abs(num) < 100:
                        result[key] = str(num / 100)
                    else:
                        result[key] = str(num)
                except (ValueError, TypeError):
                    pass
    else:
        # 1. 公司简称替换（支持包含匹配：配置key是公司全称的子串即可命中）
        company_aliases = {item["key"]: item["value"] for item in config.get("company_alias", [])}
        company_val = result.get("保险公司") or result.get("承保公司") or result.get("保险公司简称") or ""
        if company_val:
            alias = None
            # 优先精确匹配
            if company_val in company_aliases:
                alias = company_aliases[company_val]
            else:
                # 包含匹配：按key长度从长到短，避免短key误命中
                for key in sorted(company_aliases.keys(), key=len, reverse=True):
                    if key and key in company_val:
                        alias = company_aliases[key]
                        break
            if alias:
                if "保险公司简称" not in manual_fields:
                    result["保险公司简称"] = alias
                if "承保公司" in result and "承保公司" not in manual_fields:
                    result["承保公司"] = alias
                # 同步更新"保司（带地区）"：用简称替换原公司名；无地址时直接用简称
                region_key = "保司（带地区）"
                alt_region_key = "保司带地区"
                for rk in (region_key, alt_region_key):
                    if rk in result and rk not in manual_fields:
                        _addr = result.get("保司地址", "")
                        if _addr:
                            from insurance.policy_parser import extract_city_from_address
                            _city = extract_city_from_address(_addr)
                            result[rk] = (_city + alias) if _city else alias
                        else:
                            result[rk] = alias

        # 2. 险种简称替换
        policy_type_items = config.get("policy_type_alias", [])
        # 从列表中提取快捷映射开关
        _quick_alias_on = any(item.get("key") == "__quick_alias__" and item.get("value") for item in policy_type_items)
        policy_type_val = result.get("险种类型") or result.get("险种") or ""
        if policy_type_val:
            alias = None
            if _quick_alias_on:
                # 快捷映射：直接按分类映射为 交强险/商业险/驾意险/非车险
                from insurance.policy_parser import get_policy_type_code
                _, short_name = get_policy_type_code(policy_type_val)
                _QUICK_DISPLAY = {"交强险": "交强险", "商业险": "商业险", "驾乘/意外险": "驾意险", "非车险": "非车险"}
                alias = _QUICK_DISPLAY.get(short_name, short_name)
            else:
                # 逐条匹配（支持包含匹配：配置key是险种全称的子串即可命中）
                type_aliases = {item["key"]: item["value"] for item in policy_type_items if item.get("key") != "__quick_alias__"}
                if policy_type_val in type_aliases:
                    alias = type_aliases[policy_type_val]
                else:
                    for key in sorted(type_aliases.keys(), key=len, reverse=True):
                        if key and key in policy_type_val:
                            alias = type_aliases[key]
                            break
            if alias:
                if "险种类型" not in manual_fields:
                    result["险种类型"] = alias
                if "险种" in result and "险种" not in manual_fields:
                    result["险种"] = alias

        # 3. 日期格式化
        date_items = config.get("date_format", [])
        # __no_pad__：全局开关，开启时月/日/时/分不补前导零
        _date_no_pad = any(item.get("key") == "__no_pad__" and item.get("value") for item in date_items)
        date_fmt_map = {item.get("key", ""): item.get("value", "") for item in date_items
                        if item.get("key") != "__no_pad__"}
        for field_name, fmt in date_fmt_map.items():
            if field_name and fmt and field_name in result:
                raw_date = result[field_name]
                if raw_date:
                    result[field_name] = _format_date(str(raw_date), fmt, _date_no_pad)
        # 创建时间/识别时间：如果没有用户配置格式，默认保留时间部分
        for ts_field in ("创建时间", "识别时间"):
            if ts_field in result and ts_field not in date_fmt_map and result[ts_field]:
                result[ts_field] = _format_date(str(result[ts_field]), "YYYY-MM-DD HH:mm", _date_no_pad)

    # 3.4 固定值填充（来自列配置中自定义字段的 fixed_value）
    # 必须在「百分比归一化 / 公式计算 / 百分比显示」之前执行：
    # 否则靠固定值取值的字段（如 应付费率 固定值=0）在公式计算时还是空的，
    # 会导致 合计=保费×应付费率 算不出（留空），且固定值不会被转成百分比显示。
    fixed_values = config.get("fixed_values", {})
    for field_name, fixed_val in fixed_values.items():
        if fixed_val and not result.get(field_name):
            result[field_name] = fixed_val

    # 3.5 百分比标记字段归一化（is_percent）
    # 用户在这类列填的是「百分数」（如填 "10" 表示 10%），统一还原为小数（0.1）：
    #   - 供下方公式按 0.1 参与计算（如 应付手续费 = 保费 × 费率）
    #   - 末尾随 rate_fields 一起转回 "10%" 显示
    # 兼容已带 "%" 的值（"10%" 同样还原为 0.1），保证幂等。
    percent_fields = config.get("percent_fields") or set()
    for _pf in percent_fields:
        raw = result.get(_pf)
        if raw in (None, ""):
            continue
        txt = str(raw).strip().rstrip("%").replace(",", "")
        if re.fullmatch(r"-?\d+(\.\d+)?", txt):
            result[_pf] = str(float(txt) / 100)

    # 4. 公式计算（key格式："目标字段:公司简称:险种简称"）
    # 先用原始数值计算所有公式，最后再转百分比显示，避免"率"字段被提前转成"10%"影响后续公式
    # 多轮计算：公式之间可能有依赖（如"合计应付手续费"依赖"应付费率"），
    # 第一轮计算后，用更新的值再算一轮，确保依赖链正确传递
    rate_fields = []  # 记录需要转百分比的字段

    # 预处理：筛选出匹配当前记录的公式列表（含 is_computed 标记，逻辑统一在 match_fee_formulas）
    matched_formulas = match_fee_formulas(config, result)

    for round_idx in range(2):
        changed = False
        for target_field, formula, is_computed in matched_formulas:
            if target_field in manual_fields:
                # 计算公式（引用其他字段）→ 强制重算，覆盖手动值
                # 常量公式（纯数字）→ 尊重手动值，跳过
                if not is_computed:
                    if round_idx == 0 and "率" in target_field:
                        rate_fields.append(target_field)
                    continue
            old_val = result.get(target_field)
            calculated = evaluate_formula(formula, result)
            if calculated:
                result[target_field] = calculated
                if round_idx == 0 and "率" in target_field:
                    rate_fields.append(target_field)
                if calculated != old_val:
                    changed = True
        if not changed:
            break

    # 所有公式算完后，再将"率"字段转为百分比显示
    for field in rate_fields:
        try:
            if field not in result:
                continue
            val = float(result[field])
            if abs(val) < 1:
                result[field] = f"{val * 100:g}%"
            else:
                result[field] = f"{val:g}%"
        except (ValueError, TypeError):
            pass

    # 百分比标记字段（is_percent）：已归一化为小数，统一 ×100 转回百分比显示。
    # 这里不复用上面的 abs<1 启发式，避免 ">=100%" 的值（如 1.5）被误判。
    # 若值已是 "10%"（float 失败）则原样保留，保证与 rate_fields 不冲突、幂等。
    for _pf in percent_fields:
        raw = result.get(_pf)
        if raw in (None, ""):
            continue
        try:
            result[_pf] = f"{float(raw) * 100:g}%"
        except (ValueError, TypeError):
            pass

    # 6. 平安车牌格式化（粤BB446T → 粤B-B446T）
    if config.get("plate_format_pingan"):
        company = result.get("保险公司简称") or result.get("承保公司") or result.get("保险公司") or ""
        if "平安" in company:
            plate = result.get("车牌号") or result.get("车牌") or ""
            formatted = _format_plate_with_hyphen(plate)
            if formatted != plate:
                if "车牌号" in result and "车牌号" not in manual_fields:
                    result["车牌号"] = formatted
                if "车牌" in result and "车牌" not in manual_fields:
                    result["车牌"] = formatted

    return result

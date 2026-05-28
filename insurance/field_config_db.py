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
    {"key": "文件名",   "visible": True, "order": 0, "display_name": "文件名",   "type": "record"},
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
    {"key": "佣金率",   "visible": True, "order": 13, "display_name": "佣金率"},
    {"key": "佣金",     "visible": True, "order": 14, "display_name": "佣金"},
    {"key": "业务员",   "visible": True, "order": 15, "display_name": "业务员"},
    {"key": "采购费率", "visible": True, "order": 16, "display_name": "采购费率"},
    {"key": "公司利润", "visible": True, "order": 17, "display_name": "公司利润"},
    {"key": "跟单人",   "visible": True, "order": 18, "display_name": "跟单人"},
    {"key": "跟单人利润", "visible": True, "order": 19, "display_name": "跟单人利润"},
    {"key": "出单渠道", "visible": True, "order": 20, "display_name": "出单渠道"},
    {"key": "车船税",   "visible": True, "order": 21, "display_name": "车船税"},
    {"key": "状态",     "visible": True, "order": 22, "display_name": "状态",     "type": "record"},
    {"key": "创建时间", "visible": True, "order": 23, "display_name": "创建时间", "type": "record"},
    {"key": "识别时间", "visible": True, "order": 24, "display_name": "识别时间", "type": "record"},
    {"key": "群名",     "visible": True, "order": 25, "display_name": "群名",     "type": "record"},
    {"key": "发送人",   "visible": True, "order": 26, "display_name": "发送人",   "type": "record"},
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


def _format_date(raw: str, fmt: str) -> str:
    """
    日期格式转换（内部函数）。
    支持解析：YYYY-MM-DD[ HH:mm]、YYYY/MM/DD、YYYY年MM月DD日、YYYYMMDD、ISO 8601
    fmt 支持前端格式（YYYY-MM-DD / YYYY-MM-DD HH:mm）或 strftime 格式，自动识别。
    解析失败则原样返回。
    """
    if not raw or not fmt:
        return raw
    # 自动转换前端格式为 strftime 格式
    if "YYYY" in fmt or ("MM" in fmt and "DD" in fmt):
        fmt = _convert_fmt_to_strftime(fmt)
    # 尝试多种格式解析（含时间的优先匹配）
    raw_str = str(raw).strip()
    # ISO 8601 / datetime 格式（如 2026-05-26T14:30:00 或 2026-05-26 14:30:00）
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?', raw_str)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
            return dt.strftime(fmt)
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
                return dt.strftime(fmt)
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

        return result
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

    if source == "enterprise":
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

    return get_template_config(db, scope, scope_id, template_name)


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
    for dc in DEFAULT_COLUMNS:
        if dc["key"] not in existing_keys:
            columns.append({**dc, "order": max_order})
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

    返回：{"source": "enterprise", "columns": [...]}
    """
    if config_type not in ("list_columns", "export_columns"):
        return {"source": "default", "columns": list(DEFAULT_COLUMNS)}

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 按优先级依次查找
        search_order = []
        if role == "employee" and parent_id is not None:
            search_order.append(("user", user_id, "user"))
            search_order.append(("enterprise", parent_id, "enterprise"))
        elif role == "enterprise":
            search_order.append(("enterprise", parent_id or user_id, "enterprise"))
        elif role == "super_admin":
            search_order.append(("global", None, "global"))

        # 获取当前活跃模板名
        active = get_active_template(db, user_id)
        active_tpl = active.get("template_name", DEFAULT_TEMPLATE_NAME)

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
    """获取合并导出开关状态，按优先级查找，默认关闭。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        search_order = []
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
        # 格式化输出：整数则不显示小数点
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        return str(result)
    except Exception as e:
        logger.debug("公式计算失败: %s，expr=%s", e, expr)
        return ""


def apply_user_config_to_fields(config: dict, fields: dict) -> dict:
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
    """
    result = dict(fields)

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
            # 写入简称（同时更新保险公司简称字段）
            result["保险公司简称"] = alias
            # 如果已有承保公司字段则一并更新
            if "承保公司" in result:
                result["承保公司"] = alias

    # 2. 险种简称替换（支持包含匹配：配置key是险种全称的子串即可命中）
    type_aliases = {item["key"]: item["value"] for item in config.get("policy_type_alias", [])}
    policy_type_val = result.get("险种类型") or result.get("险种") or ""
    if policy_type_val:
        alias = None
        # 优先精确匹配
        if policy_type_val in type_aliases:
            alias = type_aliases[policy_type_val]
        else:
            # 包含匹配：按key长度从长到短匹配，避免短key误命中
            for key in sorted(type_aliases.keys(), key=len, reverse=True):
                if key and key in policy_type_val:
                    alias = type_aliases[key]
                    break
        if alias:
            result["险种类型"] = alias
            if "险种" in result:
                result["险种"] = alias

    # 3. 日期格式化
    date_fmt_map = {item.get("key", ""): item.get("value", "") for item in config.get("date_format", [])}
    for field_name, fmt in date_fmt_map.items():
        if field_name and fmt and field_name in result:
            raw_date = result[field_name]
            if raw_date:
                result[field_name] = _format_date(str(raw_date), fmt)
    # 创建时间/识别时间：如果没有用户配置格式，默认保留时间部分
    for ts_field in ("创建时间", "识别时间"):
        if ts_field in result and ts_field not in date_fmt_map and result[ts_field]:
            result[ts_field] = _format_date(str(result[ts_field]), "YYYY-MM-DD HH:mm")

    # 4. 公式计算（key格式："目标字段:公司简称:险种简称"）
    # 先用原始数值计算所有公式，最后再转百分比显示，避免"率"字段被提前转成"10%"影响后续公式
    record_company = result.get("保险公司简称") or result.get("承保公司") or result.get("保险公司") or ""
    record_policy_type = result.get("险种类型") or result.get("险种") or ""
    rate_fields = []  # 记录需要转百分比的字段
    for item in config.get("fee_formula", []):
        raw_key = item.get("key", "")
        raw_value = item.get("value", "")
        if not raw_key:
            continue
        # 解析 key："目标字段:公司:险种" 或旧格式 "目标字段"
        parts = raw_key.split(":", 2)
        target_field = parts[0]
        rule_company = parts[1] if len(parts) > 1 else ""
        rule_policy_type = parts[2] if len(parts) > 2 else ""
        # 匹配公司和险种（包含匹配）
        if rule_company and rule_company not in record_company and record_company not in rule_company:
            continue
        if rule_policy_type and rule_policy_type not in record_policy_type and record_policy_type not in rule_policy_type:
            continue
        # 取公式字符串（value 可能是 dict{display,tokens} 或直接字符串）
        if isinstance(raw_value, dict):
            formula = raw_value.get("display", "")
        else:
            formula = str(raw_value)
        if target_field and formula:
            calculated = evaluate_formula(formula, result)
            if calculated:
                result[target_field] = calculated
                if "率" in target_field:
                    rate_fields.append(target_field)

    # 所有公式算完后，再将"率"字段转为百分比显示
    for field in rate_fields:
        try:
            val = float(result[field])
            if abs(val) < 1:
                result[field] = f"{val * 100:g}%"
            else:
                result[field] = f"{val:g}%"
        except (ValueError, TypeError):
            pass

    return result

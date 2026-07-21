# -*- coding: utf-8 -*-
"""
销售看板模块 — 数据库层
负责 dashboard_config（看板配置模板）/ dashboard_targets（业绩目标）两张表的
建表与 CRUD，以及看板聚合计算（指标卡 / 图表 / 排行 / 目标完成率）。

设计说明：
- 配置解析顺序：enterprise（企业自定义）→ global（系统默认模板）
- 聚合在应用层内存完成：台账字段存于 display_fields JSON 列，且佣金/提成等
  公式字段由 field_config_db 的费率公式在应用层计算，无法下推 SQL；
  单企业数据量（数千条/年）内存聚合为毫秒级
- 目标周期 month/quarter/year 为滚动周期（按当前自然月/季/年计算），custom 为固定区间
"""
import calendar
import copy
import json
import logging
import re
from datetime import date, timedelta

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# 筛选条件支持的操作符
_FILTER_OPS = {"contains", "not_contains", "eq", "ne"}
_OP_LABEL = {"contains": "包含", "not_contains": "不包含", "eq": "等于", "ne": "不等于"}
_AGG_LABEL = {"sum": "求和", "count": "计数", "avg": "平均"}
_PERIOD_LABEL = {"month": "当月", "quarter": "当季度", "year": "今年", "custom": "自定义区间"}

# 系统默认模板（global scope 无记录时的兜底配置）
DEFAULT_CONFIG = {
    "time_field": "签单日期",
    "cards": [
        {"id": "c1", "title": "总保费", "agg": "sum", "field": "保费"},
        {"id": "c2", "title": "总佣金", "agg": "sum", "field": "佣金"},
        {"id": "c3", "title": "保单量", "agg": "count"},
    ],
    "charts": [
        {"type": "trend", "title": "保费趋势", "granularity": "auto",
         "metrics": [{"label": "保费", "agg": "sum", "field": "保费"}]},
        {"type": "donut", "title": "保司保费占比", "dimension": "保险公司",
         "metric": {"agg": "sum", "field": "保费"}, "top": 0},
        {"type": "bar", "title": "险种保费分布", "dimension": "险种",
         "metric": {"agg": "sum", "field": "保费"}},
    ],
    "ranking": {
        "title": "发送人业绩排行", "dimension": "发送人", "sort_by": "保费",
        "columns": [
            {"title": "保费", "agg": "sum", "field": "保费"},
            {"title": "保单量", "agg": "count"},
        ],
        "target_columns": [],
    },
}


def init_dashboard_tables(db):
    """创建 dashboard_config / dashboard_targets 表（幂等，已存在则跳过）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                scope VARCHAR(16) NOT NULL COMMENT 'global=系统默认模板 / enterprise=企业自定义',
                scope_id INT NOT NULL DEFAULT 0 COMMENT 'enterprise 时为企业ID，global 固定 0',
                config_json LONGTEXT COMMENT '看板配置JSON（cards/charts/ranking）',
                enabled TINYINT DEFAULT 1 COMMENT '是否启用看板（超管可按企业开关）',
                updated_by INT DEFAULT NULL COMMENT '最后修改人user_id',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_scope (scope, scope_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='销售看板配置'
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_targets (
                id INT AUTO_INCREMENT PRIMARY KEY,
                enterprise_id INT NOT NULL COMMENT '所属企业',
                target_type VARCHAR(16) NOT NULL COMMENT 'enterprise=企业整体 / condition=条件目标 / employee=员工目标',
                name VARCHAR(64) NOT NULL COMMENT '目标名称；员工目标中同名记录=同一目标项',
                metric_agg VARCHAR(8) DEFAULT 'sum' COMMENT '聚合方式 sum/count',
                metric_field VARCHAR(64) DEFAULT '保费' COMMENT '统计字段',
                filters_json TEXT COMMENT '筛选条件JSON数组',
                employee_name VARCHAR(64) NOT NULL DEFAULT '' COMMENT '员工目标：业务员姓名；空串=该目标项的全员统一值',
                period_type VARCHAR(16) NOT NULL COMMENT 'month/quarter/year=滚动周期 / custom=固定区间',
                period_start DATE DEFAULT NULL COMMENT 'custom 起始',
                period_end DATE DEFAULT NULL COMMENT 'custom 截止',
                target_value DECIMAL(14,2) NOT NULL DEFAULT 0 COMMENT '目标值',
                enabled TINYINT DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_ent (enterprise_id, target_type),
                UNIQUE KEY uk_target (enterprise_id, target_type, name, employee_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='销售看板业绩目标'
        """)
        conn.commit()
        logger.info("销售看板表初始化完成")
    finally:
        conn.close()


# ==================== 配置读写 ====================

def get_dashboard_config(db, enterprise_id: int) -> dict:
    """
    解析企业的看板配置：enterprise（企业自定义）→ global（系统默认模板）→ 内置 DEFAULT_CONFIG。
    返回 {"config": dict, "source": "enterprise"|"global"|"default", "enabled": bool}
    """
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT scope, config_json, enabled FROM dashboard_config "
            "WHERE (scope='enterprise' AND scope_id=%s) OR (scope='global' AND scope_id=0)",
            (enterprise_id,))
        rows = {r["scope"]: r for r in cursor.fetchall()}
    finally:
        conn.close()

    # 看板开关以企业行为准（无企业行时默认启用）
    ent_row = rows.get("enterprise")
    enabled = bool(ent_row["enabled"]) if ent_row is not None else True

    for scope in ("enterprise", "global"):
        row = rows.get(scope)
        if row and row.get("config_json"):
            try:
                cfg = json.loads(row["config_json"])
                if isinstance(cfg, dict) and cfg:
                    return {"config": cfg, "source": scope, "enabled": enabled}
            except (TypeError, json.JSONDecodeError):
                logger.warning("dashboard_config JSON 解析失败 scope=%s ent=%s", scope, enterprise_id)
    return {"config": copy.deepcopy(DEFAULT_CONFIG), "source": "default", "enabled": enabled}


def save_dashboard_config(db, scope: str, scope_id: int, config: dict, user_id=None):
    """保存看板配置。scope=global 时 scope_id 固定 0"""
    if scope not in ("global", "enterprise"):
        raise ValueError("scope 必须为 global 或 enterprise")
    if scope == "global":
        scope_id = 0
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO dashboard_config (scope, scope_id, config_json, updated_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE config_json=VALUES(config_json), updated_by=VALUES(updated_by)",
            (scope, scope_id, json.dumps(config, ensure_ascii=False), user_id))
        conn.commit()
    finally:
        conn.close()


def reset_dashboard_config(db, enterprise_id: int):
    """删除企业自定义配置，恢复使用系统默认模板（保留 enabled 开关行为：整行删除即恢复默认启用）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dashboard_config WHERE scope='enterprise' AND scope_id=%s",
            (enterprise_id,))
        conn.commit()
    finally:
        conn.close()


def set_dashboard_enabled(db, enterprise_id: int, enabled: bool, user_id=None):
    """超管按企业开关看板。企业无自定义配置时插入空配置行仅承载开关"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO dashboard_config (scope, scope_id, config_json, enabled, updated_by) "
            "VALUES ('enterprise', %s, NULL, %s, %s) "
            "ON DUPLICATE KEY UPDATE enabled=VALUES(enabled), updated_by=VALUES(updated_by)",
            (enterprise_id, 1 if enabled else 0, user_id))
        conn.commit()
    finally:
        conn.close()


# ==================== 目标 CRUD ====================

def list_targets(db, enterprise_id: int) -> list:
    """返回企业全部目标（含禁用），按类型/名称排序"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT * FROM dashboard_targets WHERE enterprise_id=%s "
            "ORDER BY target_type, name, employee_name",
            (enterprise_id,))
        rows = cursor.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["filters"] = _parse_filters(r.pop("filters_json", None))
        r["target_value"] = float(r["target_value"] or 0)
        for k in ("period_start", "period_end"):
            r[k] = r[k].strftime("%Y-%m-%d") if r.get(k) else None
        for k in ("created_at", "updated_at"):
            if r.get(k) and not isinstance(r[k], str):
                r[k] = str(r[k])
    return rows


def save_target(db, enterprise_id: int, target: dict) -> int:
    """
    新增/更新一条目标记录（按唯一键 upsert）。
    target: {target_type, name, metric_agg, metric_field, filters, employee_name,
             period_type, period_start, period_end, target_value, enabled}
    """
    target_type = target.get("target_type") or "enterprise"
    if target_type not in ("enterprise", "condition", "employee"):
        raise ValueError("target_type 非法")
    name = (target.get("name") or "").strip()
    if not name:
        raise ValueError("目标名称不能为空")
    period_type = target.get("period_type") or "month"
    if period_type not in ("month", "quarter", "year", "custom"):
        raise ValueError("period_type 非法")
    if period_type == "custom" and not (target.get("period_start") and target.get("period_end")):
        raise ValueError("自定义区间必须提供起止日期")
    filters = _sanitize_filters(target.get("filters"))
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO dashboard_targets "
            "(enterprise_id, target_type, name, metric_agg, metric_field, filters_json, "
            " employee_name, period_type, period_start, period_end, target_value, enabled) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE metric_agg=VALUES(metric_agg), metric_field=VALUES(metric_field), "
            " filters_json=VALUES(filters_json), period_type=VALUES(period_type), "
            " period_start=VALUES(period_start), period_end=VALUES(period_end), "
            " target_value=VALUES(target_value), enabled=VALUES(enabled)",
            (enterprise_id, target_type, name,
             target.get("metric_agg") or "sum",
             target.get("metric_field") or "保费",
             json.dumps(filters, ensure_ascii=False),
             (target.get("employee_name") or "").strip(),
             period_type,
             target.get("period_start") or None,
             target.get("period_end") or None,
             float(target.get("target_value") or 0),
             1 if target.get("enabled", True) else 0))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def delete_target(db, enterprise_id: int, target_id: int = None,
                  name: str = None, target_type: str = None) -> int:
    """删除目标：按 id 删单条；按 name+target_type 删整个目标项（员工目标项含所有按人记录）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        if target_id:
            cursor.execute(
                "DELETE FROM dashboard_targets WHERE enterprise_id=%s AND id=%s",
                (enterprise_id, target_id))
        elif name and target_type:
            cursor.execute(
                "DELETE FROM dashboard_targets WHERE enterprise_id=%s AND target_type=%s AND name=%s",
                (enterprise_id, target_type, name))
        else:
            raise ValueError("需提供 target_id 或 name+target_type")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def delete_target_person(db, enterprise_id: int, name: str, employee_name: str) -> int:
    """删除员工目标项中某人的单独设置（恢复跟随全员统一值）"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dashboard_targets WHERE enterprise_id=%s AND target_type='employee' "
            "AND name=%s AND employee_name=%s",
            (enterprise_id, name, employee_name))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ==================== 聚合计算 ====================

def _parse_filters(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return _sanitize_filters(raw)
    try:
        return _sanitize_filters(json.loads(raw))
    except (TypeError, json.JSONDecodeError):
        return []


def _sanitize_filters(filters) -> list:
    """清洗筛选条件：仅保留合法 {field, op, value}"""
    result = []
    for f in (filters or []):
        if not isinstance(f, dict):
            continue
        field = (f.get("field") or "").strip()
        op = f.get("op") or "contains"
        value = str(f.get("value") or "").strip()
        if field and value and op in _FILTER_OPS:
            result.append({"field": field, "op": op, "value": value})
    return result


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_num(val):
    """金额/数字文本 → float；无法解析返回 None。支持 '1,234.56元'、'¥1234'、'12.5%' 等"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").strip()
    if not s:
        return None
    is_pct = s.endswith("%")
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        num = float(m.group())
        return num / 100 if is_pct else num
    except ValueError:
        return None


def _match_filters(fields: dict, filters: list) -> bool:
    """记录字段是否满足全部筛选条件（AND 关系）"""
    for f in filters:
        val = str(fields.get(f["field"]) or "")
        target = f["value"]
        op = f["op"]
        if op == "contains" and target not in val:
            return False
        if op == "not_contains" and target in val:
            return False
        if op == "eq" and val != target:
            return False
        if op == "ne" and val == target:
            return False
    return True


def _aggregate(rows: list, agg: str, field: str = None, filters: list = None) -> float:
    """对记录列表做聚合。rows 为 fields dict 列表"""
    if filters:
        rows = [r for r in rows if _match_filters(r, filters)]
    if agg == "count":
        return float(len(rows))
    nums = [n for n in (_to_num(r.get(field)) for r in rows) if n is not None]
    if agg == "avg":
        return round(sum(nums) / len(nums), 2) if nums else 0.0
    return round(sum(nums), 2)  # sum


def _filters_text(filters: list) -> str:
    if not filters:
        return ""
    return "；".join(f"{f['field']} {_OP_LABEL.get(f['op'], f['op'])}「{f['value']}」" for f in filters)


def _build_explain(agg: str, field: str, filters: list, date_start: str, date_end: str,
                   time_field: str, user_config: dict = None) -> str:
    """生成「?」悬停的数据来源与计算公式说明文案"""
    parts = []
    if agg == "count":
        parts.append("数据来源：台账保单记录条数（COUNT）")
    else:
        parts.append(f"数据来源：台账「{field}」列{_AGG_LABEL.get(agg, agg)}")
        # 公式字段：附费率公式来源
        for item in (user_config or {}).get("fee_formula", []) or []:
            if item.get("key") == field and item.get("value"):
                parts.append(f"计算公式：{field} = {item['value']}（来自字段配置-费率公式）")
                break
    ft = _filters_text(filters)
    if ft:
        parts.append(f"筛选条件：{ft}")
    parts.append(f"统计范围：{date_start} ~ {date_end}（按{time_field}），已识别完成、未删除的保单")
    return "\n".join(parts)


def _period_range(period_type: str, period_start, period_end, today: date = None) -> tuple:
    """目标周期 → (start_date_str, end_date_str)。滚动周期按当前自然月/季/年"""
    today = today or date.today()
    if period_type == "month":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif period_type == "quarter":
        q_start_month = (today.month - 1) // 3 * 3 + 1
        start = date(today.year, q_start_month, 1)
        end_month = q_start_month + 2
        end = date(today.year, end_month, calendar.monthrange(today.year, end_month)[1])
    elif period_type == "year":
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
    else:  # custom
        start = period_start if isinstance(period_start, date) else _parse_date(str(period_start))
        end = period_end if isinstance(period_end, date) else _parse_date(str(period_end))
        if not start or not end:
            return None, None
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _parse_date(s):
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, TypeError, IndexError):
        return None


# 时间字段 → insurance_policy_fields 的 ISO 日期列
_TIME_FIELD_COL = {
    "签单日期": "sign_date_iso",
    "起保日期": "start_date_iso",
    "终保日期": "end_date_iso",
}


def load_records(db, enterprise_id: int, date_start: str, date_end: str,
                 time_field: str = "签单日期", user_config: dict = None,
                 apply_config_fn=None) -> list:
    """
    加载企业时间范围内的保单记录并返回 fields dict 列表（已应用用户配置/公式）。
    时间按 time_field 对应的 ISO 日期列过滤，无值回退 created_at 日期。
    apply_config_fn: field_config_db.apply_user_config_to_fields（由调用方传入避免循环依赖）
    """
    iso_col = _TIME_FIELD_COL.get(time_field, "sign_date_iso")
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"""
            SELECT r.id, r.display_fields, r.manual_fields, r.company_short,
                   r.sender, r.user_id,
                   DATE(r.created_at) AS created_day,
                   pf.{iso_col} AS time_day,
                   pf.salesperson, pf.policy_type, pf.company, pf.total_premium, pf.sign_date
            FROM insurance_records r
            LEFT JOIN insurance_policy_fields pf ON pf.record_id = r.id
            WHERE r.enterprise_id = %s AND r.deleted_at IS NULL AND r.status = 'done'
              AND (r.doc_category = '保单' OR r.doc_category = '' OR r.doc_category IS NULL)
              AND COALESCE(pf.{iso_col}, DATE(r.created_at)) BETWEEN %s AND %s
        """, (enterprise_id, date_start, date_end))
        rows = cursor.fetchall()
        # 无 sender 的手动上传记录：批量查上传人姓名，用作「发送人」
        _upload_ids = {r["user_id"] for r in rows
                       if not (r.get("sender") or "").strip() and r.get("user_id")}
        upload_name_map = {}
        if _upload_ids:
            ph = ",".join(["%s"] * len(_upload_ids))
            cursor.execute(f"SELECT id, name FROM users WHERE id IN ({ph})", list(_upload_ids))
            for u in cursor.fetchall():
                if u.get("name"):
                    upload_name_map[u["id"]] = u["name"]
    finally:
        conn.close()

    # 发送人显示名映射：管理员绑定别名 > 绑定用户真实姓名 > 微信原名
    try:
        from auth.db import get_sender_alias_map, get_sender_user_name_map
        sender_alias_map = get_sender_alias_map(db, enterprise_id) or {}
        sender_name_map = get_sender_user_name_map(db, enterprise_id) or {}
    except Exception:
        sender_alias_map, sender_name_map = {}, {}

    records = []
    for row in rows:
        try:
            fields = json.loads(row["display_fields"]) if row.get("display_fields") else {}
            if not isinstance(fields, dict):
                fields = {}
        except (TypeError, json.JSONDecodeError):
            fields = {}
        # 关键维度兜底：display_fields 缺失时从关联表/记录列补充
        if not fields.get("保险公司"):
            fields["保险公司"] = row.get("company_short") or row.get("company") or ""
        if not fields.get("险种"):
            fields["险种"] = row.get("policy_type") or ""
        if not fields.get("业务员"):
            fields["业务员"] = row.get("salesperson") or ""
        if not fields.get("保费"):
            fields["保费"] = row.get("total_premium") or ""
        # 聚合用时间桶：ISO 时间列，无值回退创建日期
        td = row.get("time_day") or row.get("created_day")
        fields["_time_day"] = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td or "")
        # 应用用户配置（简称/公式），手动修改过的字段不被公式覆盖
        if apply_config_fn and user_config:
            try:
                cfg = dict(user_config)
                manual_raw = row.get("manual_fields")
                try:
                    manual_list = json.loads(manual_raw) if isinstance(manual_raw, str) else (manual_raw or [])
                except (TypeError, json.JSONDecodeError):
                    manual_list = []
                cfg["_manual_fields"] = set(manual_list)
                time_bucket = fields.pop("_time_day")
                fields = apply_config_fn(cfg, fields)
                fields["_time_day"] = time_bucket
            except Exception:
                logger.debug("应用用户配置失败 record_id=%s", row.get("id"), exc_info=True)
        # 注入「发送人」（放在公式应用之后，保证不被配置过滤/覆盖）
        _sender = (row.get("sender") or "").strip()
        if _sender:
            fields["发送人"] = sender_alias_map.get(_sender) or sender_name_map.get(_sender) or _sender
        else:
            fields["发送人"] = upload_name_map.get(row.get("user_id")) or ""
        records.append(fields)
    return records


def get_dashboard_data(db, enterprise_id: int, date_start: str, date_end: str,
                       user_config: dict = None, apply_config_fn=None) -> dict:
    """
    看板聚合主入口。返回 {cards, charts, ranking, targets}，每项附 explain 说明。
    - cards/charts/ranking 按页面时间范围 date_start~date_end 聚合
    - targets 按各目标自身周期聚合（滚动周期与页面筛选无关）
    """
    resolved = get_dashboard_config(db, enterprise_id)
    config = resolved["config"]
    time_field = config.get("time_field") or "签单日期"

    records = load_records(db, enterprise_id, date_start, date_end,
                           time_field, user_config, apply_config_fn)

    # ---- 指标卡 ----
    cards = []
    for c in config.get("cards", []):
        agg = c.get("agg") or "sum"
        field = c.get("field")
        filters = _sanitize_filters(c.get("filters"))
        value = _aggregate(records, agg, field, filters)
        cards.append({
            "id": c.get("id"), "title": c.get("title") or field or "未命名",
            "agg": agg, "field": field, "filters": filters,
            "value": value, "is_count": agg == "count",
            "explain": _build_explain(agg, field, filters, date_start, date_end,
                                      time_field, user_config),
        })

    # ---- 图表 ----
    charts = []
    for ch in config.get("charts", []):
        if ch.get("enabled") is False:
            continue  # 配置中停用的图表不聚合
        ctype = ch.get("type")
        if ctype == "trend":
            charts.append(_build_trend(ch, records, date_start, date_end, time_field, user_config))
        elif ctype in ("donut", "bar"):
            charts.append(_build_group_chart(ch, records, date_start, date_end, time_field, user_config))

    # ---- 目标（企业/条件） ----
    all_targets = list_targets(db, enterprise_id)
    today = date.today()
    goal_list = []
    for t in all_targets:
        if t["target_type"] == "employee" or not t.get("enabled"):
            continue
        ps, pe = _period_range(t["period_type"], t.get("period_start"), t.get("period_end"), today)
        if not ps:
            continue
        t_records = load_records(db, enterprise_id, ps, pe, time_field, user_config, apply_config_fn)
        actual = _aggregate(t_records, t["metric_agg"], t["metric_field"], t["filters"])
        rate = round(actual / t["target_value"] * 100, 1) if t["target_value"] else 0.0
        goal_list.append({
            "id": t["id"], "name": t["name"], "target_type": t["target_type"],
            "metric_agg": t["metric_agg"], "metric_field": t["metric_field"],
            "filters": t["filters"], "period_type": t["period_type"],
            "period_label": _PERIOD_LABEL.get(t["period_type"], t["period_type"]),
            "period_start": ps, "period_end": pe,
            "target_value": t["target_value"], "actual": actual, "rate": rate,
            "is_count": t["metric_agg"] == "count",
            "explain": _build_explain(t["metric_agg"], t["metric_field"], t["filters"],
                                      ps, pe, time_field, user_config)
                       + f"\n计算公式：完成率 = 实际值 ÷ 目标值 {t['target_value']}",
        })

    # ---- 业务员排行（含员工目标完成率列） ----
    ranking = _build_ranking(db, config, records, all_targets, enterprise_id,
                             date_start, date_end, time_field, user_config,
                             apply_config_fn, today)

    return {
        "time_field": time_field,
        "date_start": date_start, "date_end": date_end,
        "record_count": len(records),
        "config_source": resolved["source"],
        "enabled": resolved["enabled"],
        "cards": cards, "charts": charts, "targets": goal_list, "ranking": ranking,
    }


def _build_trend(ch: dict, records: list, date_start: str, date_end: str,
                 time_field: str, user_config: dict) -> dict:
    """趋势图：按时间粒度分桶，逐指标聚合。
    granularity=auto（默认）按时间范围自适应：≤31天按日、≤92天按周、更长按月"""
    granularity = ch.get("granularity") or "auto"
    if granularity == "auto":
        ds, de = _parse_date(date_start), _parse_date(date_end)
        span = (de - ds).days + 1 if ds and de else 31
        granularity = "day" if span <= 31 else ("week" if span <= 92 else "month")
    metrics = ch.get("metrics") or []

    def bucket_key(day_str):
        if granularity == "day":
            return day_str
        if granularity == "week":
            d = _parse_date(day_str)
            if not d:
                return day_str
            monday = d - timedelta(days=d.weekday())
            return monday.strftime("%Y-%m-%d") + "周"
        return day_str[:7]  # month → YYYY-MM

    buckets = {}
    for r in records:
        key = bucket_key(r.get("_time_day") or "")
        if not key:
            continue
        buckets.setdefault(key, []).append(r)
    keys = sorted(buckets.keys())

    # 补全时间轴：范围内没数据的桶补 0——否则只剩有单的月份/日期，两点连成误导性斜线
    ds, de = _parse_date(date_start), _parse_date(date_end)
    if ds and de and ds <= de:
        fill = []
        if granularity == "day":
            d = ds
            while d <= de:
                fill.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
        elif granularity == "week":
            d = ds - timedelta(days=ds.weekday())
            while d <= de:
                fill.append(d.strftime("%Y-%m-%d") + "周")
                d += timedelta(days=7)
        else:  # month
            y, m = ds.year, ds.month
            while (y, m) <= (de.year, de.month):
                fill.append(f"{y:04d}-{m:02d}")
                m += 1
                if m > 12:
                    y, m = y + 1, 1
        if len(fill) <= 400:  # 异常大范围兜底：仍退回仅有数据的桶
            keys = fill

    series = []
    for m in metrics:
        agg = m.get("agg") or "sum"
        field = m.get("field")
        filters = _sanitize_filters(m.get("filters"))
        series.append({
            "label": m.get("label") or field or "指标",
            "values": [_aggregate(buckets.get(k, []), agg, field, filters) for k in keys],
        })
    metric_desc = "、".join(f"「{m.get('field') or '保单量'}」{_AGG_LABEL.get(m.get('agg') or 'sum')}"
                           for m in metrics)
    gran_label = {"month": "月", "week": "周", "day": "日"}.get(granularity, granularity)
    return {
        "type": "trend", "title": ch.get("title") or "趋势",
        "granularity": granularity, "labels": keys, "series": series,
        "explain": (f"数据来源：按「{time_field}」逐{gran_label}汇总台账 {metric_desc}\n"
                    f"统计范围：{date_start} ~ {date_end}，已识别完成、未删除的保单"),
    }


# 分组展示归并：图表按维度分组时把左侧值并入右侧组（仅影响图表展示，不改台账字段/筛选条件）
_DIMENSION_MERGE = {
    "险种": {"驾意险": "非车险"},
}


def _build_group_chart(ch: dict, records: list, date_start: str, date_end: str,
                       time_field: str, user_config: dict) -> dict:
    """占比/分布图：按维度分组聚合"""
    dimension = ch.get("dimension") or "保险公司"
    metric = ch.get("metric") or {"agg": "sum", "field": "保费"}
    agg = metric.get("agg") or "sum"
    field = metric.get("field")
    top = int(ch.get("top") or 0)

    merge_map = _DIMENSION_MERGE.get(dimension, {})
    groups = {}
    for r in records:
        key = str(r.get(dimension) or "").strip() or "未知"
        key = merge_map.get(key, key)
        groups.setdefault(key, []).append(r)
    items = [{"name": k, "value": _aggregate(v, agg, field), "count": len(v)}
             for k, v in groups.items()]
    items.sort(key=lambda x: x["value"], reverse=True)
    if top and len(items) > top:
        head, tail = items[:top], items[top:]
        other_rows = [r for k, v in groups.items() for r in v
                      if k in {t["name"] for t in tail}]
        head.append({"name": "其他", "value": _aggregate(other_rows, agg, field),
                     "count": len(other_rows)})
        items = head
    total = round(sum(i["value"] for i in items), 2)
    metric_desc = "保单量计数" if agg == "count" else f"「{field}」{_AGG_LABEL.get(agg, agg)}"
    return {
        "type": ch.get("type"), "title": ch.get("title") or f"{dimension}分布",
        "dimension": dimension, "items": items, "total": total,
        "is_count": agg == "count",
        "explain": (f"数据来源：按台账「{dimension}」列分组，对{metric_desc}"
                    + (f"，取前 {top}" if top else "") + "\n"
                    f"统计范围：{date_start} ~ {date_end}（按{time_field}），已识别完成、未删除的保单"),
    }


def _build_ranking(db, config: dict, records: list, all_targets: list, enterprise_id: int,
                   date_start: str, date_end: str, time_field: str, user_config: dict,
                   apply_config_fn, today: date) -> dict:
    """业务员排行：页面时间范围内按维度分组聚合各列 + 员工目标项完成率列"""
    rk = config.get("ranking") or {}
    dimension = rk.get("dimension") or "发送人"
    columns = rk.get("columns") or []
    sort_by = rk.get("sort_by") or (columns[0].get("title") if columns else "")

    groups = {}
    for r in records:
        key = str(r.get(dimension) or "").strip() or "未知"
        groups.setdefault(key, []).append(r)

    # 员工目标项：name → {unified, persons: {员工名: value}, meta}
    target_columns = rk.get("target_columns") or []
    emp_items = {}
    for t in all_targets:
        if t["target_type"] != "employee" or not t.get("enabled"):
            continue
        item = emp_items.setdefault(t["name"], {"unified": None, "persons": {}, "meta": t})
        if t["employee_name"]:
            item["persons"][t["employee_name"]] = t["target_value"]
        else:
            item["unified"] = t["target_value"]
            item["meta"] = t
    # 仅展示配置勾选的目标项（未配置则全部展示）
    shown_names = [n for n in (target_columns or list(emp_items.keys())) if n in emp_items]

    # 每个目标项按自身周期加载记录并按人分组（周期与页面范围一致时复用已载记录）
    target_col_data = []
    for name in shown_names:
        item = emp_items[name]
        meta = item["meta"]
        ps, pe = _period_range(meta["period_type"], meta.get("period_start"),
                               meta.get("period_end"), today)
        if not ps:
            continue
        if (ps, pe) == (date_start, date_end):
            t_records = records
        else:
            t_records = load_records(db, enterprise_id, ps, pe, time_field,
                                     user_config, apply_config_fn)
        person_rows = {}
        for r in t_records:
            key = str(r.get(dimension) or "").strip() or "未知"
            person_rows.setdefault(key, []).append(r)
        target_col_data.append({
            "name": name, "item": item, "meta": meta,
            "period": (ps, pe), "person_rows": person_rows,
        })

    rows = []
    for person, person_records in groups.items():
        row = {"name": person, "values": {}}
        for col in columns:
            agg = col.get("agg") or "sum"
            field = col.get("field")
            filters = _sanitize_filters(col.get("filters"))
            row["values"][col.get("title") or field] = _aggregate(
                person_records, agg, field, filters)
        row["targets"] = {}
        for tc in target_col_data:
            item, meta = tc["item"], tc["meta"]
            tv = item["persons"].get(person, item["unified"])
            if tv is None:
                row["targets"][tc["name"]] = None  # 未设置目标
                continue
            actual = _aggregate(tc["person_rows"].get(person, []),
                                meta["metric_agg"], meta["metric_field"], meta["filters"])
            row["targets"][tc["name"]] = {
                "target": tv, "actual": actual,
                "rate": round(actual / tv * 100, 1) if tv else 0.0,
                "personal": person in item["persons"],
            }
        rows.append(row)
    rows.sort(key=lambda r: r["values"].get(sort_by) or 0, reverse=True)

    col_explains = {}
    for col in columns:
        title = col.get("title") or col.get("field")
        col_explains[title] = _build_explain(col.get("agg") or "sum", col.get("field"),
                                             _sanitize_filters(col.get("filters")),
                                             date_start, date_end, time_field, user_config)
    for tc in target_col_data:
        meta = tc["meta"]
        ps, pe = tc["period"]
        col_explains[tc["name"] + "·完成率"] = (
            _build_explain(meta["metric_agg"], meta["metric_field"], meta["filters"],
                           ps, pe, time_field, user_config)
            + "\n计算公式：完成率 = 该成员周期内实际值 ÷ 其目标值（单独设置优先于全员统一值）")

    return {
        "title": rk.get("title") or "发送人业绩排行",
        "dimension": dimension, "sort_by": sort_by,
        "columns": [c.get("title") or c.get("field") for c in columns],
        "target_columns": [tc["name"] for tc in target_col_data],
        "rows": rows, "col_explains": col_explains,
        "explain": f"数据来源：按台账「{dimension}」列分组统计；排序依据：{sort_by}",
    }


def get_salesmen(db, enterprise_id: int) -> list:
    """企业台账「发送人」去重名单（员工目标设置下拉用）：
    绑定别名 > 绑定用户真实姓名 > 微信原名；无 sender 的手动上传记录取上传人姓名"""
    try:
        from auth.db import get_sender_alias_map, get_sender_user_name_map
        alias_map = get_sender_alias_map(db, enterprise_id) or {}
        name_map = get_sender_user_name_map(db, enterprise_id) or {}
    except Exception:
        alias_map, name_map = {}, {}
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("""
            SELECT DISTINCT r.sender, r.user_id
            FROM insurance_records r
            WHERE r.enterprise_id = %s AND r.deleted_at IS NULL AND r.status = 'done'
        """, (enterprise_id,))
        rows = cursor.fetchall()
        uids = {r["user_id"] for r in rows
                if not (r.get("sender") or "").strip() and r.get("user_id")}
        uname = {}
        if uids:
            ph = ",".join(["%s"] * len(uids))
            cursor.execute(f"SELECT id, name FROM users WHERE id IN ({ph})", list(uids))
            for u in cursor.fetchall():
                if u.get("name"):
                    uname[u["id"]] = u["name"]
        out = set()
        for r in rows:
            s = (r.get("sender") or "").strip()
            if s:
                out.add(alias_map.get(s) or name_map.get(s) or s)
            elif r.get("user_id") and uname.get(r["user_id"]):
                out.add(uname[r["user_id"]])
        return sorted(out)
    finally:
        conn.close()

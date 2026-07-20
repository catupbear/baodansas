#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI质检模块 — 数据库层
负责 AI 质检相关的表结构、配置读写、调用流水与统计聚合。

表/配置：
- insurance_records 新增列：ai_check_status / ai_fields / ai_corrections / ai_checked_at
- ai_check_log：AI 调用流水（手动质检 + 自动质检共用，成本统计依据）
- insurance_config KV：ai_model_config（多模型配置）、ai_check_enterprises（企业质检开关/范围）
"""

import json
import logging
import uuid

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

AI_MODEL_CONFIG_KEY = "ai_model_config"
AI_CHECK_ENTERPRISES_KEY = "ai_check_enterprises"

# 默认提取 Prompt（{{FIELD_LIST}} 会被替换为 FIELD_ORDER 字段清单）
DEFAULT_PROMPT_TEMPLATE = (
    "你是保险保单信息提取专家。请仔细阅读保单图片，提取以下字段并输出 JSON 对象（不要输出任何其他内容）：\n"
    "{{FIELD_LIST}}\n"
    "要求：\n"
    "1. 严格按图片上的原文提取，禁止编造或推测；未识别到的字段填 null。\n"
    "2. 日期统一输出为 YYYY年MM月DD日 格式（含时间的保留时间，如 2026年07月20日0时）。\n"
    "3. 金额字段只输出数字（保留小数），不要带货币符号和千分位逗号。\n"
    "4. 「文档类型」从以下取值中选择：保单/投保单/批单/发票/清单/其他非保单。\n"
    "5. 「险种类型」判断规则：交强险/商业险/驾意险，无法判断填 保单。\n"
    "6. 车牌号无牌时填 暂未上牌；「保险公司简称」输出保司通用简称（如 人保/平安/太平洋/国寿财）。"
)

# 内置厂商预设（base_url 均为 OpenAI 兼容 Chat Completions 端点）
PROVIDER_PRESETS = [
    {"provider": "dashscope", "label": "阿里·百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model_hint": "qwen-vl-max"},
    {"provider": "zhipu", "label": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model_hint": "glm-4.5v"},
    {"provider": "volcengine", "label": "火山·方舟（豆包）", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model_hint": "doubao-seed-1-6-vision-250815"},
    {"provider": "moonshot", "label": "月之暗面（Kimi）", "base_url": "https://api.moonshot.cn/v1", "model_hint": "moonshot-v1-32k-vision-preview"},
    {"provider": "hunyuan", "label": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "model_hint": "hunyuan-vision"},
    {"provider": "custom", "label": "自定义（OpenAI兼容）", "base_url": "", "model_hint": ""},
]

DEFAULT_AI_MODEL_CONFIG = {
    "enabled": False,           # AI 能力总开关
    "allow_auto_fix": False,    # 全局「允许AI直接覆盖」总闸
    "models": [],               # [{id,name,provider,base_url,model,api_key,timeout,enabled,priority}]
    "active_model_id": "",      # 当前使用的模型 id
    "max_pages": 4,             # PDF 渲染最大页数
    "cost_alert_threshold": 0,  # 日成本提醒阈值（元），0=不提醒；仅提醒不拦截
    "notify_webhook": "",       # 钉钉通知机器人（留空回退系统告警机器人）
    "notify_secret": "",
    "prompt_template": DEFAULT_PROMPT_TEMPLATE,
}

# 企业质检配置默认值（scope 自定义条件为「或」关系，命中任一即质检）
DEFAULT_ENTERPRISE_CHECK = {
    "enabled": False,
    "auto_fix": False,          # 企业级「AI直接覆盖」，需全局 allow_auto_fix 同时开启才生效
    "scope": {
        "mode": "all",          # all=全部 / custom=自定义条件
        "statuses": [],         # 非保单 / 需人工补充 / 识别异常 / 识别失败 / 正常完成
        "companies": [],        # 保司简称多选
        "company_history_lt": 0,  # >0 时：保司历史总识别数少于该值的自动质检
    },
    "compare_fields": [],       # 空=使用默认核心字段集（见 ai_checker.DEFAULT_COMPARE_FIELDS）
}


def init_ai_tables(db):
    """建立 AI 质检所需的表结构（幂等，随 init_insurance_tables 调用）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # insurance_records 新增列（幂等 ADD COLUMN）
        for col_name, col_def in [
            ("ai_check_status", "VARCHAR(16) DEFAULT NULL COMMENT 'AI质检状态: pass/corrected/flagged/resolved/error'"),
            ("ai_fields", "LONGTEXT COMMENT '被AI覆盖过的字段名列表JSON(类比manual_fields)'"),
            ("ai_corrections", "LONGTEXT COMMENT 'AI修正历史JSON: {字段:{原值,AI值,时间,来源}}'"),
            ("ai_checked_at", "DATETIME DEFAULT NULL COMMENT 'AI质检时间'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE insurance_records ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass  # 字段已存在，忽略

        try:
            cursor.execute("CREATE INDEX idx_insurance_ai_check ON insurance_records(ai_check_status)")
        except Exception:
            pass

        # AI 调用流水表（手动+自动共用）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_check_log (
                id                INT PRIMARY KEY AUTO_INCREMENT,
                record_id         INT NOT NULL COMMENT '关联 insurance_records.id',
                enterprise_id     INT DEFAULT NULL COMMENT '所属企业ID',
                trigger_type      VARCHAR(16) DEFAULT 'manual' COMMENT 'manual=手动质检/auto=自动质检',
                operator_user_id  INT DEFAULT NULL COMMENT '操作人user_id(manual必填,auto为空)',
                operator_name     VARCHAR(64) DEFAULT NULL COMMENT '操作人姓名快照',
                model             VARCHAR(128) DEFAULT '' COMMENT '实际使用的模型',
                result            VARCHAR(16) DEFAULT '' COMMENT 'extracted/applied/pass/corrected/flagged/error',
                diff_json         LONGTEXT COMMENT '差异明细JSON',
                applied_fields    LONGTEXT COMMENT '实际应用覆盖的字段及原值备份JSON(未应用为空)',
                prompt_tokens     INT DEFAULT 0,
                completion_tokens INT DEFAULT 0,
                duration_ms       INT DEFAULT 0,
                error_message     TEXT,
                created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_acl_record (record_id),
                KEY idx_acl_ent_time (enterprise_id, created_at),
                KEY idx_acl_created (created_at),
                KEY idx_acl_result (result)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI质检调用流水'
        """)

        # ai_check_log 新增列（幂等 ADD COLUMN，兼容历史已建表）
        for col_name, col_def in [
            ("extracted_json", "LONGTEXT COMMENT '本次AI提取的完整字段JSON(全字段历史对比用)'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE ai_check_log ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass  # 字段已存在，忽略

        conn.commit()
        logger.info("AI质检数据库表初始化完成")
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 配置读写
# ------------------------------------------------------------------ #

def get_ai_model_config(db) -> dict:
    """读取 AI 模型配置，缺省项自动合并默认值。"""
    from .db import get_insurance_config
    raw = get_insurance_config(db, AI_MODEL_CONFIG_KEY, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    cfg = dict(DEFAULT_AI_MODEL_CONFIG)
    cfg.update(raw)
    if not isinstance(cfg.get("models"), list):
        cfg["models"] = []
    return cfg


def save_ai_model_config(db, cfg: dict):
    from .db import set_insurance_config
    set_insurance_config(db, AI_MODEL_CONFIG_KEY, cfg)


def mask_api_key(key: str) -> str:
    """API Key 打码回显：保留前4后4。"""
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * 8 + key[-4:]


def mask_model_config(cfg: dict) -> dict:
    """返回打码后的配置副本（api_key/notify_secret 打码），供前端回显。"""
    masked = json.loads(json.dumps(cfg, ensure_ascii=False))
    for m in masked.get("models", []):
        m["api_key"] = mask_api_key(m.get("api_key", ""))
    if masked.get("notify_secret"):
        masked["notify_secret"] = mask_api_key(masked["notify_secret"])
    return masked


def merge_masked_model_config(db, incoming: dict) -> dict:
    """
    前端保存时，打码的 api_key/notify_secret（含 * 号）表示未修改，
    需用数据库中已有明文回填，避免打码串覆盖真实密钥。
    """
    current = get_ai_model_config(db)
    cur_models = {m.get("id"): m for m in current.get("models", [])}
    for m in incoming.get("models", []):
        key = m.get("api_key", "") or ""
        if "*" in key:
            old = cur_models.get(m.get("id"))
            m["api_key"] = old.get("api_key", "") if old else ""
        if not m.get("id"):
            m["id"] = uuid.uuid4().hex[:8]
    secret = incoming.get("notify_secret", "") or ""
    if "*" in secret:
        incoming["notify_secret"] = current.get("notify_secret", "")
    return incoming


def get_active_model(cfg: dict) -> dict | None:
    """返回当前使用的模型配置；未设置时取优先级最高的启用模型。"""
    models = [m for m in cfg.get("models", []) if m.get("enabled", True)]
    if not models:
        return None
    active_id = cfg.get("active_model_id")
    for m in models:
        if m.get("id") == active_id:
            return m
    return sorted(models, key=lambda x: x.get("priority", 99))[0]


def get_fallback_models(cfg: dict, exclude_id: str = "") -> list:
    """按优先级返回除指定模型外的启用模型列表（自动降级顺序）。"""
    models = [
        m for m in cfg.get("models", [])
        if m.get("enabled", True) and m.get("id") != exclude_id
    ]
    return sorted(models, key=lambda x: x.get("priority", 99))


def get_ai_check_enterprises(db) -> dict:
    """读取企业质检配置 {enterprise_id(str): {...}}。"""
    from .db import get_insurance_config
    raw = get_insurance_config(db, AI_CHECK_ENTERPRISES_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def save_ai_check_enterprises(db, cfg: dict):
    from .db import set_insurance_config
    set_insurance_config(db, AI_CHECK_ENTERPRISES_KEY, cfg)


def get_enterprise_check_config(db, enterprise_id) -> dict:
    """获取指定企业的质检配置（合并默认值）。"""
    all_cfg = get_ai_check_enterprises(db)
    raw = all_cfg.get(str(enterprise_id), {}) if enterprise_id is not None else {}
    cfg = json.loads(json.dumps(DEFAULT_ENTERPRISE_CHECK, ensure_ascii=False))
    if isinstance(raw, dict):
        scope = raw.get("scope")
        cfg.update(raw)
        if isinstance(scope, dict):
            merged_scope = dict(DEFAULT_ENTERPRISE_CHECK["scope"])
            merged_scope.update(scope)
            cfg["scope"] = merged_scope
        else:
            cfg["scope"] = dict(DEFAULT_ENTERPRISE_CHECK["scope"])
    return cfg


# ------------------------------------------------------------------ #
# 调用流水
# ------------------------------------------------------------------ #

def insert_ai_check_log(db, log: dict) -> int:
    """写入一条 AI 调用流水，返回 log_id。dict/list 值自动序列化。"""
    data = {}
    for k, v in log.items():
        data[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["%s"] * len(data))
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"INSERT INTO ai_check_log ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )
        log_id = cursor.lastrowid
        conn.commit()
        return log_id
    finally:
        conn.close()


def update_ai_check_log(db, log_id: int, updates: dict):
    data = {}
    for k, v in updates.items():
        data[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            f"UPDATE ai_check_log SET {set_clause} WHERE id = %s",
            list(data.values()) + [log_id],
        )
        conn.commit()
    finally:
        conn.close()


def get_ai_check_log(db, log_id: int) -> dict | None:
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM ai_check_log WHERE id = %s", (log_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def query_ai_check_logs(db, filters: dict = None, page: int = 1, page_size: int = 20) -> dict:
    """流水列表查询：筛选企业/触发方式/操作人/结果/日期。"""
    filters = filters or {}
    where, params = ["1=1"], []
    if filters.get("enterprise_id"):
        where.append("l.enterprise_id = %s")
        params.append(filters["enterprise_id"])
    if filters.get("trigger_type"):
        where.append("l.trigger_type = %s")
        params.append(filters["trigger_type"])
    if filters.get("operator_user_id"):
        where.append("l.operator_user_id = %s")
        params.append(filters["operator_user_id"])
    if filters.get("result"):
        where.append("l.result = %s")
        params.append(filters["result"])
    if filters.get("date_start"):
        where.append("l.created_at >= %s")
        params.append(filters["date_start"] + " 00:00:00")
    if filters.get("date_end"):
        where.append("l.created_at <= %s")
        params.append(filters["date_end"] + " 23:59:59")
    where_sql = " AND ".join(where)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM ai_check_log l WHERE {where_sql}", params)
        total = (cursor.fetchone() or {}).get("cnt", 0)
        cursor.execute(
            f"SELECT l.*, r.filename, r.room_name FROM ai_check_log l "
            f"LEFT JOIN insurance_records r ON r.id = l.record_id "
            f"WHERE {where_sql} ORDER BY l.id DESC LIMIT %s OFFSET %s",
            params + [page_size, (page - 1) * page_size],
        )
        rows = [dict(r) for r in cursor.fetchall()]
        # token 成本汇总（同筛选条件全量）
        cursor.execute(
            f"SELECT COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct "
            f"FROM ai_check_log l WHERE {where_sql}",
            params,
        )
        tokens = cursor.fetchone() or {}
        return {
            "total": total,
            "list": rows,
            "token_summary": {"prompt_tokens": int(tokens.get("pt", 0)), "completion_tokens": int(tokens.get("ct", 0))},
        }
    finally:
        conn.close()


def get_today_usage(db) -> dict:
    """今日 AI 调用量与 token 消耗（成本提醒用）。"""
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(prompt_tokens),0) AS pt, "
            "COALESCE(SUM(completion_tokens),0) AS ct "
            "FROM ai_check_log WHERE created_at >= CURDATE()"
        )
        row = cursor.fetchone() or {}
        return {
            "calls": int(row.get("cnt", 0)),
            "prompt_tokens": int(row.get("pt", 0)),
            "completion_tokens": int(row.get("ct", 0)),
        }
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 问题列表 / 统计
# ------------------------------------------------------------------ #

def query_ai_check_issues(db, filters: dict = None, page: int = 1, page_size: int = 20) -> dict:
    """
    AI质检问题列表：ai_check_status 为 corrected/flagged/resolved 的记录。
    默认只看待处理（flagged）；差异明细取该记录最近一条 auto/manual 流水的 diff_json。
    """
    filters = filters or {}
    where, params = ["r.deleted_at IS NULL"], []
    status = filters.get("status")
    if status:
        where.append("r.ai_check_status = %s")
        params.append(status)
    else:
        where.append("r.ai_check_status IN ('corrected','flagged','resolved')")
    if filters.get("enterprise_id"):
        where.append("r.enterprise_id = %s")
        params.append(filters["enterprise_id"])
    if filters.get("date_start"):
        where.append("r.ai_checked_at >= %s")
        params.append(filters["date_start"] + " 00:00:00")
    if filters.get("date_end"):
        where.append("r.ai_checked_at <= %s")
        params.append(filters["date_end"] + " 23:59:59")
    where_sql = " AND ".join(where)

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM insurance_records r WHERE {where_sql}", params)
        total = (cursor.fetchone() or {}).get("cnt", 0)
        cursor.execute(
            "SELECT r.id, r.filename, r.room_name, r.sender_name, r.enterprise_id, r.company_short, "
            "r.ai_check_status, r.ai_checked_at, r.ai_corrections, r.manual_fields, r.cos_url, "
            "(SELECT l.diff_json FROM ai_check_log l WHERE l.record_id = r.id AND l.diff_json IS NOT NULL "
            " ORDER BY l.id DESC LIMIT 1) AS diff_json "
            f"FROM insurance_records r WHERE {where_sql} "
            "ORDER BY r.ai_checked_at DESC LIMIT %s OFFSET %s",
            params + [page_size, (page - 1) * page_size],
        )
        rows = [dict(r) for r in cursor.fetchall()]
        # 待处理计数（前端 Tab 红点，全局口径）
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM insurance_records r "
            "WHERE r.deleted_at IS NULL AND r.ai_check_status = 'flagged'"
        )
        pending = (cursor.fetchone() or {}).get("cnt", 0)
        return {"total": total, "list": rows, "pending": pending}
    finally:
        conn.close()


def get_ai_check_stats(db, date_start: str = "", date_end: str = "") -> dict:
    """
    企业质检统计：按企业聚合 ai_check_log（仅 auto 流水），
    返回 质检数/准确数/修正数/待确认数/真实准确率/token 消耗/高频出错字段 + 全局合计。
    """
    where, params = ["l.trigger_type = 'auto'"], []
    if date_start:
        where.append("l.created_at >= %s")
        params.append(date_start + " 00:00:00")
    if date_end:
        where.append("l.created_at <= %s")
        params.append(date_end + " 23:59:59")
    where_sql = " AND ".join(where)

    rec_where, rec_params = ["deleted_at IS NULL"], []
    if date_start:
        rec_where.append("created_at >= %s")
        rec_params.append(date_start + " 00:00:00")
    if date_end:
        rec_where.append("created_at <= %s")
        rec_params.append(date_end + " 23:59:59")

    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT l.enterprise_id, COUNT(*) AS checked, "
            "SUM(l.result='pass') AS passed, SUM(l.result='corrected') AS corrected, "
            "SUM(l.result='flagged') AS flagged, SUM(l.result='error') AS errors, "
            "COALESCE(SUM(l.prompt_tokens),0) AS pt, COALESCE(SUM(l.completion_tokens),0) AS ct "
            f"FROM ai_check_log l WHERE {where_sql} GROUP BY l.enterprise_id",
            params,
        )
        stat_rows = {r["enterprise_id"]: dict(r) for r in cursor.fetchall()}

        # 各企业识别总数（同时间范围）
        cursor.execute(
            "SELECT enterprise_id, COUNT(*) AS total_records FROM insurance_records "
            f"WHERE {' AND '.join(rec_where)} GROUP BY enterprise_id",
            rec_params,
        )
        rec_counts = {r["enterprise_id"]: int(r["total_records"]) for r in cursor.fetchall()}

        # 企业名称
        ent_names = {}
        try:
            cursor.execute("SELECT id, name FROM enterprises")
            ent_names = {r["id"]: r["name"] for r in cursor.fetchall()}
        except Exception:
            pass

        # 高频出错字段：聚合 diff_json（限最近 2000 条，控制内存）
        cursor.execute(
            f"SELECT l.enterprise_id, l.diff_json FROM ai_check_log l "
            f"WHERE {where_sql} AND l.result IN ('corrected','flagged') "
            f"ORDER BY l.id DESC LIMIT 2000",
            params,
        )
        field_freq = {}  # {enterprise_id: {字段: 次数}}
        for r in cursor.fetchall():
            try:
                diffs = json.loads(r["diff_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            eid = r["enterprise_id"]
            bucket = field_freq.setdefault(eid, {})
            for d in diffs if isinstance(diffs, list) else []:
                fname = d.get("field") if isinstance(d, dict) else None
                if fname:
                    bucket[fname] = bucket.get(fname, 0) + 1

        result, total = [], {"total_records": 0, "checked": 0, "passed": 0, "corrected": 0,
                             "flagged": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0}
        for eid, s in stat_rows.items():
            checked = int(s.get("checked") or 0)
            passed = int(s.get("passed") or 0)
            freq = sorted(field_freq.get(eid, {}).items(), key=lambda x: -x[1])[:3]
            row = {
                "enterprise_id": eid,
                "enterprise_name": ent_names.get(eid, f"企业#{eid}" if eid else "未归属"),
                "total_records": rec_counts.get(eid, 0),
                "checked": checked,
                "passed": passed,
                "corrected": int(s.get("corrected") or 0),
                "flagged": int(s.get("flagged") or 0),
                "errors": int(s.get("errors") or 0),
                "accuracy": round(passed / checked * 100, 1) if checked else None,
                "top_error_fields": [{"field": f, "count": c} for f, c in freq],
                "prompt_tokens": int(s.get("pt") or 0),
                "completion_tokens": int(s.get("ct") or 0),
            }
            result.append(row)
            for k in ("total_records", "checked", "passed", "corrected", "flagged", "errors"):
                total[k] += row[k]
            total["prompt_tokens"] += row["prompt_tokens"]
            total["completion_tokens"] += row["completion_tokens"]

        total["accuracy"] = round(total["passed"] / total["checked"] * 100, 1) if total["checked"] else None
        result.sort(key=lambda x: -x["checked"])
        return {"list": result, "total": total}
    finally:
        conn.close()


def get_company_history_count(db, company_short: str) -> int:
    """某保司历史总识别数（质检范围「保司成熟度」条件用）。"""
    if not company_short:
        return 0
    conn = db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM insurance_records "
            "WHERE company_short = %s AND deleted_at IS NULL",
            (company_short,),
        )
        return int((cursor.fetchone() or {}).get("cnt", 0))
    finally:
        conn.close()

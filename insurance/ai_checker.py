#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI质检模块 — 自动质检器
识别完成后按企业配置自动用大模型复核提取结果：
  范围条件匹配 → 入队（独立串行线程）→ AI 提取 → 逐字段归一化比对
  → 直接覆盖(双层开关) / 仅标记 → 写流水 → 钉钉通知

设计约束（与方案一致）：
- 人工修改过的字段(manual_fields)永不自动覆盖，只提示差异；
- 自动覆盖仅限「列表值为空AI有值(补充)」或「AI值可在原文交叉验证」的场景；
- 所有修正备份进 ai_corrections，可还原；
- 不自动同步钉钉多维表；
- 同一记录只自动质检一次（ai_check_status 非空则跳过，重识别后由 API 重置）。
"""

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# 默认核心比对字段集（企业未配置 compare_fields 时使用）
DEFAULT_COMPARE_FIELDS = [
    "保单号", "保险公司简称", "险种类型",
    "被保险人", "投保人", "车牌号", "车架号VIN",
    "保费合计", "保险起期", "保险止期",
]

# 范围条件里的状态选项（前端多选值）
SCOPE_STATUS_OPTIONS = ["非保单", "需人工补充", "识别异常", "识别失败", "正常完成"]

# 成本估算（元/1k token，粗略值，仅用于日成本提醒，不用于计费）
_PRICE_PROMPT_PER_1K = 0.003
_PRICE_COMPLETION_PER_1K = 0.009

_QUEUE_MAXSIZE = 2000


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """粗略估算调用成本（元）。"""
    return prompt_tokens / 1000 * _PRICE_PROMPT_PER_1K + completion_tokens / 1000 * _PRICE_COMPLETION_PER_1K


# ------------------------------------------------------------------ #
# 字段归一化与比对
# ------------------------------------------------------------------ #

_AMOUNT_FIELDS = {"保费合计", "不含税保费", "增值税额", "车船税"}
_DATE_FIELDS = {"保险起期", "保险止期", "签单日期", "初次登记日期", "收费确认时间"}


def _norm_amount(v: str) -> str:
    s = re.sub(r"[¥￥,，\s元]", "", v or "")
    try:
        f = float(s)
        return f"{f:.2f}"
    except ValueError:
        return s


def _norm_date(v: str) -> str:
    """日期归一化为纯数字串比较：2026年07月20日0时 / 2026-07-20 00:00 → 202607200"""
    digits = re.findall(r"\d+", v or "")
    # 月/日补零对齐（2026,7,20 与 2026,07,20 一致）
    out = []
    for i, d in enumerate(digits):
        if i in (1, 2) and len(d) == 1:
            d = "0" + d
        out.append(d)
    return "".join(out).rstrip("0") or "0"


def _norm_plate(v: str) -> str:
    return re.sub(r"[-—\s·]", "", v or "").upper()


def normalize_value(field: str, value) -> str:
    """按字段类型归一化后用于比较（不改变存储值）。"""
    s = str(value or "").strip()
    if not s:
        return ""
    if field in _AMOUNT_FIELDS:
        return _norm_amount(s)
    if field in _DATE_FIELDS:
        return _norm_date(s)
    if field == "车牌号":
        return _norm_plate(s)
    if field == "车架号VIN":
        return re.sub(r"\s", "", s).upper()
    return re.sub(r"\s", "", s)


def compute_diff(parsed_fields: dict, ai_fields: dict, compare_fields: list, manual_fields: list) -> list:
    """
    逐字段比对，返回差异列表：
    [{field, current, ai, type: diff/supplement/ai_missing, manual: bool}]
    - diff:       两边都有值且归一化后不同
    - supplement: 列表值为空、AI 有值（可补充）
    - ai_missing: 列表有值、AI 未提取（AI拿不准，不修正只展示）
    """
    manual_set = set(manual_fields or [])
    diffs = []
    for field in compare_fields:
        cur = str((parsed_fields or {}).get(field) or "").strip()
        ai = str((ai_fields or {}).get(field) or "").strip()
        if not cur and not ai:
            continue
        if cur and not ai:
            continue  # AI 未提取到，无从验证，不算差异（避免噪音）
        if not cur and ai:
            diffs.append({"field": field, "current": cur, "ai": ai,
                          "type": "supplement", "manual": field in manual_set})
            continue
        if normalize_value(field, cur) != normalize_value(field, ai):
            diffs.append({"field": field, "current": cur, "ai": ai,
                          "type": "diff", "manual": field in manual_set})
    return diffs


def _value_in_text(field: str, value: str, text: str) -> bool:
    """AI 值能否在原文中交叉验证（归一化后子串匹配）。"""
    if not text:
        return False
    norm_v = normalize_value(field, value)
    if not norm_v:
        return False
    norm_text = re.sub(r"[\s,，¥￥]", "", text)
    if field in _DATE_FIELDS:
        # 日期在原文中格式多变，仅比对数字序列
        return norm_v in re.sub(r"\D", "", norm_text)
    return norm_v in norm_text.upper() if field in ("车牌号", "车架号VIN") else norm_v in norm_text


def split_fixable(diffs: list, record: dict) -> tuple:
    """
    将差异拆为「可自动修正」与「仅标记」两组。
    可自动修正 = 非手动字段 且（补充型 或 AI值可在原文交叉验证）。
    """
    text = (record.get("raw_text") or "") + "\n" + (record.get("ocr_text") or "")
    fixable, flag_only = [], []
    for d in diffs:
        if d.get("manual"):
            flag_only.append(d)
        elif d["type"] == "supplement":
            fixable.append(d)
        elif d["type"] == "diff" and _value_in_text(d["field"], d["ai"], text):
            fixable.append(d)
        else:
            flag_only.append(d)
    return fixable, flag_only


# ------------------------------------------------------------------ #
# 质检范围条件匹配
# ------------------------------------------------------------------ #

def match_check_scope(db, record: dict, ent_cfg: dict) -> bool:
    """判断记录是否命中企业的质检范围条件（自定义条件为「或」关系）。"""
    scope = ent_cfg.get("scope") or {}
    if scope.get("mode", "all") == "all":
        return True

    status = record.get("status") or ""
    doc_category = record.get("doc_category") or ""
    is_abnormal = int(record.get("is_abnormal") or 0)
    statuses = scope.get("statuses") or []

    if "非保单" in statuses and doc_category and doc_category != "保单":
        return True
    if "需人工补充" in statuses and is_abnormal == 1:
        return True
    if "识别异常" in statuses and is_abnormal == 1:
        return True
    if "识别失败" in statuses and status == "failed":
        return True
    if "正常完成" in statuses and status in ("done", "success") and is_abnormal == 0 \
            and (not doc_category or doc_category == "保单"):
        return True

    company_short = record.get("company_short") or ""
    companies = scope.get("companies") or []
    if companies and company_short in companies:
        return True

    history_lt = int(scope.get("company_history_lt") or 0)
    if history_lt > 0 and company_short:
        from .ai_db import get_company_history_count
        if get_company_history_count(db, company_short) < history_lt:
            return True

    return False


# ------------------------------------------------------------------ #
# 钉钉通知
# ------------------------------------------------------------------ #

def _notify_check_result(db, record: dict, fixed: list, flagged: list):
    """质检发现差异后发钉钉通知（处理入口在系统「AI质检问题列表」）。"""
    try:
        from .ai_db import get_ai_model_config
        from core.notify import notify_ai_check

        ent_name = ""
        eid = record.get("enterprise_id")
        if eid:
            try:
                conn = db.pool.connection()
                try:
                    import pymysql.cursors as _cursors
                    cur = conn.cursor(_cursors.DictCursor)
                    cur.execute("SELECT name FROM enterprises WHERE id = %s", (eid,))
                    row = cur.fetchone()
                    ent_name = (row or {}).get("name") or ""
                finally:
                    conn.close()
            except Exception:
                pass

        cfg = get_ai_model_config(db)
        title = "🤖 AI质检-发现差异并已自动修正" if fixed else "⚠️ AI质检-发现差异待人工确认"
        lines = [
            f"### {title}",
            f"- **企业**: {ent_name or '未归属'}",
            f"- **群**: {record.get('room_name') or '-'}",
            f"- **文件**: {record.get('filename') or '-'}（记录 #{record.get('id')}）",
        ]
        if fixed:
            lines.append(f"- **自动修正 {len(fixed)} 项**:")
            for d in fixed[:8]:
                lines.append(f"  - {d['field']}: {d['current'] or '（空）'} → {d['ai']}")
        if flagged:
            lines.append(f"- **待人工确认 {len(flagged)} 项**:")
            for d in flagged[:8]:
                mark = "（人工改过）" if d.get("manual") else ""
                lines.append(f"  - {d['field']}{mark}: 列表「{d['current'] or '空'}」 vs AI「{d['ai']}」")
        lines.append("")
        lines.append("> 请前往系统「保单识别 → AI质检」问题列表处理。")

        notify_ai_check(
            f"{title} #{record.get('id')}",
            "\n".join(lines),
            webhook=cfg.get("notify_webhook") or "",
            secret=cfg.get("notify_secret") or "",
        )
    except Exception as e:
        logger.warning("AI质检钉钉通知失败: %s", e)


def _maybe_notify_cost(db):
    """日成本超过提醒阈值时发一次钉钉提醒（同日去重，仅提醒不拦截）。"""
    try:
        from .ai_db import get_ai_model_config, get_today_usage
        from core.notify import notify_ai_check

        cfg = get_ai_model_config(db)
        threshold = float(cfg.get("cost_alert_threshold") or 0)
        if threshold <= 0:
            return
        usage = get_today_usage(db)
        cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"])
        if cost < threshold:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        notify_ai_check(
            f"💰 AI质检日成本提醒 {today}",  # title 含日期，钉钉通知模块同标题去重
            f"### 💰 AI质检日成本提醒\n- **日期**: {today}\n- **今日调用**: {usage['calls']} 次\n"
            f"- **估算成本**: ¥{cost:.2f}（阈值 ¥{threshold:.2f}）\n\n> 仅提醒，不拦截调用；可在系统设置调整阈值。",
            webhook=cfg.get("notify_webhook") or "",
            secret=cfg.get("notify_secret") or "",
        )
    except Exception as e:
        logger.debug("AI成本提醒失败: %s", e)


# ------------------------------------------------------------------ #
# 核心质检逻辑
# ------------------------------------------------------------------ #

def run_ai_check(db, record_id: int) -> dict:
    """
    对单条记录执行自动质检（在质检线程中调用）。
    返回 {"result": pass/corrected/flagged/error, ...}
    """
    from .db import get_insurance_record, update_insurance_record
    from .ai_db import (
        get_ai_model_config, get_enterprise_check_config,
        insert_ai_check_log,
    )
    from .ai_extractor import extract_fields_from_pdf, download_pdf_from_cos, AIExtractError

    record = get_insurance_record(db, record_id)
    if not record:
        return {"result": "error", "error": "记录不存在"}
    if record.get("ai_check_status"):
        return {"result": "skip", "error": "已质检过"}

    ent_cfg = get_enterprise_check_config(db, record.get("enterprise_id"))
    model_cfg = get_ai_model_config(db)

    log = {
        "record_id": record_id,
        "enterprise_id": record.get("enterprise_id"),
        "trigger_type": "auto",
    }
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        pdf_bytes = download_pdf_from_cos(record)
        extract = extract_fields_from_pdf(db, pdf_bytes)
        log.update({
            "model": extract["model"],
            "prompt_tokens": extract["prompt_tokens"],
            "completion_tokens": extract["completion_tokens"],
            "duration_ms": extract["duration_ms"],
        })

        try:
            parsed_fields = json.loads(record.get("parsed_fields") or "{}")
        except (TypeError, json.JSONDecodeError):
            parsed_fields = {}
        try:
            manual_fields = json.loads(record.get("manual_fields") or "[]")
        except (TypeError, json.JSONDecodeError):
            manual_fields = []

        compare_fields = ent_cfg.get("compare_fields") or DEFAULT_COMPARE_FIELDS
        diffs = compute_diff(parsed_fields, extract["fields"], compare_fields, manual_fields)

        if not diffs:
            update_insurance_record(db, record_id, {
                "ai_check_status": "pass", "ai_checked_at": now_str,
            })
            log["result"] = "pass"
            log["diff_json"] = []
            insert_ai_check_log(db, log)
            return {"result": "pass"}

        # 双层开关：全局 allow_auto_fix 且 企业 auto_fix 同时开启才直接覆盖
        allow_fix = bool(model_cfg.get("allow_auto_fix")) and bool(ent_cfg.get("auto_fix"))
        fixable, flag_only = split_fixable(diffs, record) if allow_fix else ([], diffs)

        if fixable:
            # 直接覆盖：写 parsed_fields + 备份 ai_corrections + 标记 ai_fields
            try:
                ai_corrections = json.loads(record.get("ai_corrections") or "{}")
            except (TypeError, json.JSONDecodeError):
                ai_corrections = {}
            try:
                ai_field_list = json.loads(record.get("ai_fields") or "[]")
            except (TypeError, json.JSONDecodeError):
                ai_field_list = []

            new_parsed = dict(parsed_fields)
            for d in fixable:
                new_parsed[d["field"]] = d["ai"]
                ai_corrections[d["field"]] = {
                    "old": d["current"], "new": d["ai"],
                    "time": now_str, "source": "auto",
                }
                if d["field"] not in ai_field_list:
                    ai_field_list.append(d["field"])

            status_val = "corrected" if not flag_only else "flagged"
            update_insurance_record(db, record_id, {
                "parsed_fields": new_parsed,
                "ai_corrections": ai_corrections,
                "ai_fields": ai_field_list,
                "ai_check_status": status_val,
                "ai_checked_at": now_str,
            })
            log["result"] = "corrected"
        else:
            update_insurance_record(db, record_id, {
                "ai_check_status": "flagged", "ai_checked_at": now_str,
            })
            log["result"] = "flagged"

        log["diff_json"] = diffs
        if fixable:
            log["applied_fields"] = {d["field"]: {"old": d["current"], "new": d["ai"]} for d in fixable}
        insert_ai_check_log(db, log)

        _notify_check_result(db, {**record, "id": record_id}, fixable, flag_only)
        _maybe_notify_cost(db)
        return {"result": log["result"], "diffs": diffs, "fixed": len(fixable)}

    except AIExtractError as e:
        logger.warning("AI质检提取失败 record_id=%d: %s", record_id, e)
        update_insurance_record(db, record_id, {
            "ai_check_status": "error", "ai_checked_at": now_str,
        })
        log.update({"result": "error", "error_message": str(e)[:500]})
        insert_ai_check_log(db, log)
        return {"result": "error", "error": str(e)}
    except Exception as e:
        logger.error("AI质检异常 record_id=%d: %s", record_id, e, exc_info=True)
        try:
            update_insurance_record(db, record_id, {
                "ai_check_status": "error", "ai_checked_at": now_str,
            })
            log.update({"result": "error", "error_message": str(e)[:500]})
            insert_ai_check_log(db, log)
        except Exception:
            pass
        return {"result": "error", "error": str(e)}


# ------------------------------------------------------------------ #
# 质检队列（独立于识别队列，串行消费不阻塞主流程）
# ------------------------------------------------------------------ #

class AICheckQueue:
    """AI质检队列：Queue + 守护线程串行消费（复用 handler 的守护线程模式）。"""

    def __init__(self, db):
        self.db = db
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker = threading.Thread(target=self._consume, daemon=True)
        self._worker.start()
        logger.info("AI质检队列已启动")

    def enqueue(self, record_id: int) -> bool:
        try:
            self._queue.put_nowait(record_id)
            return True
        except queue.Full:
            logger.warning("AI质检队列已满，丢弃 record_id=%d", record_id)
            return False

    def _consume(self):
        while True:
            try:
                record_id = self._queue.get(block=True, timeout=1)
            except queue.Empty:
                continue
            try:
                run_ai_check(self.db, record_id)
            except Exception as e:
                logger.error("AI质检消费异常 record_id=%s: %s", record_id, e, exc_info=True)
            finally:
                self._queue.task_done()


_check_queue: AICheckQueue | None = None


def init_ai_checker(db):
    """初始化全局质检队列（main.py 或首次使用时调用）。"""
    global _check_queue
    if _check_queue is None:
        _check_queue = AICheckQueue(db)
    return _check_queue


def maybe_enqueue_ai_check(db, record_id: int):
    """
    识别流水线完成后调用：命中企业质检配置则入队（异常绝不影响主流程）。
    条件：AI总开关开 + 企业质检开 + 命中范围条件 + 未质检过 + 有COS文件。
    """
    try:
        from .db import get_insurance_record
        from .ai_db import get_ai_model_config, get_enterprise_check_config

        model_cfg = get_ai_model_config(db)
        if not model_cfg.get("enabled"):
            return
        record = get_insurance_record(db, record_id)
        if not record or record.get("ai_check_status") or not record.get("cos_url"):
            return
        eid = record.get("enterprise_id")
        if eid is None:
            return
        ent_cfg = get_enterprise_check_config(db, eid)
        if not ent_cfg.get("enabled"):
            return
        if not match_check_scope(db, record, ent_cfg):
            return
        q = init_ai_checker(db)
        if q.enqueue(record_id):
            logger.info("记录已入AI质检队列 record_id=%d", record_id)
    except Exception as e:
        logger.warning("AI质检入队失败（不影响主流程） record_id=%s: %s", record_id, e)

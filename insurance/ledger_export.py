# -*- coding: utf-8 -*-
"""
群消息触发的保单台账导出。

监控群内已绑定账号的人发送"发送台账+今日/本周/本月"文本时，
通过内部环回 HTTP 调用复用 /api/insurance/records + /api/insurance/export/excel
的现成导出逻辑（列配置、公式列、费率格式化、交强到期时间注入等），
避免脱离 Flask 请求上下文重复实现这套复杂逻辑。
"""
import logging
import re
import urllib.parse
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

_INTERNAL_BASE = "http://127.0.0.1:8090"

LEDGER_TRIGGER_RE = re.compile(r"发送台账\s*(今日|本周|本月)")


def match_ledger_trigger(text: str):
    """匹配"发送台账XX"命令，命中返回周期关键词（今日/本周/本月），否则 None。

    注：不支持"全部"——数据量大的企业全量导出会超过 gunicorn 30 秒请求超时导致 500。
    """
    if not text:
        return None
    m = LEDGER_TRIGGER_RE.search(text.strip())
    return m.group(1) if m else None


def _resolve_period_range(period: str):
    """今日/本周/本月 -> (date_start, date_end)，均为 'YYYY-MM-DD HH:MM:SS' 字符串。"""
    now = datetime.now()
    today = datetime(now.year, now.month, now.day)
    if period == "本周":
        monday = today - timedelta(days=today.weekday())
        start, end = monday, monday + timedelta(days=7)
    elif period == "本月":
        first = today.replace(day=1)
        next_first = (first.replace(year=first.year + 1, month=1)
                      if first.month == 12 else first.replace(month=first.month + 1))
        start, end = first, next_first
    else:  # "今日"（LEDGER_TRIGGER_RE 只产出这三种取值之一，此处兜底为今日）
        start, end = today, today + timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def export_ledger_via_binding(db, user_id: int, enterprise_id, period: str):
    """
    以 user_id 的身份（沿用其导出列配置/角色权限），环回调用现成的
    /api/insurance/records + /api/insurance/export/excel 生成台账 Excel。

    Returns:
        (excel_bytes, filename, record_count)；
        查不到用户 / 请求失败 / 该周期无数据 均返回 (None, "", 0)。
    """
    from auth.db import get_user_by_id
    from auth.jwt_utils import generate_token

    user = get_user_by_id(db, user_id)
    if not user:
        logger.warning("台账导出：user_id=%s 不存在", user_id)
        return None, "", 0

    token = generate_token(user["id"], user["phone"], user["role"])
    headers = {"Authorization": f"Bearer {token}"}

    date_start, date_end = _resolve_period_range(period)

    list_params = {
        "status": "done", "is_abnormal": "0", "doc_category": "保单",
        "dedup": "1", "page": 1, "page_size": 10000,
    }
    if date_start:
        list_params["date_start"] = date_start
        list_params["date_end"] = date_end
    if enterprise_id:
        list_params["enterprise_id"] = enterprise_id

    try:
        resp = requests.get(f"{_INTERNAL_BASE}/api/insurance/records", params=list_params,
                             headers=headers, timeout=30)
        if not resp.ok:
            logger.error("台账导出：查询记录失败 status=%s body=%s", resp.status_code, resp.text[:300])
            return None, "", 0
        records = (resp.json().get("data") or {}).get("records", [])
    except Exception as e:
        logger.error("台账导出：查询记录异常: %s", e, exc_info=True)
        return None, "", 0

    if not records:
        return None, "", 0

    def _extra(r):
        df = r.get("display_fields") or {}
        return {
            "文件名": r.get("filename", ""),
            "文档类型": r.get("doc_category", ""),
            "状态": "已完成",
            "创建时间": df.get("创建时间") or r.get("created_at", ""),
            "识别时间": df.get("识别时间") or r.get("updated_at", ""),
            "群名": r.get("room_name") or r.get("roomid", ""),
            "发送人": r.get("sender_name") or r.get("sender", ""),
        }

    invoices = []
    for r in records:
        fields = dict(r.get("display_fields") or {})
        fields.update(_extra(r))
        invoices.append({"fields": fields, "filename": r.get("filename", ""),
                          "manual_fields": r.get("manual_fields") or []})

    export_body = {
        "invoices": invoices,
        "export_type": "success",
        "merge_by_plate": False,
        "config_applied": True,
        "export_label": f"保单台账_{period}",
    }
    if date_start:
        export_body["date_start"] = date_start[:10]
        export_body["date_end"] = date_end[:10]
    if enterprise_id:
        export_body["enterprise_id"] = enterprise_id

    try:
        resp2 = requests.post(f"{_INTERNAL_BASE}/api/insurance/export/excel", json=export_body,
                              headers=headers, timeout=60)
        if not resp2.ok:
            logger.error("台账导出：生成Excel失败 status=%s body=%s", resp2.status_code, resp2.text[:300])
            return None, "", 0
    except Exception as e:
        logger.error("台账导出：生成Excel异常: %s", e, exc_info=True)
        return None, "", 0

    cd = resp2.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    filename = urllib.parse.unquote(m.group(1)) if m else f"保单台账_{period}.xlsx"

    return resp2.content, filename, len(invoices)

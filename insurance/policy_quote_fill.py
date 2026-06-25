# insurance/policy_quote_fill.py
"""
群内"政策报价"文本回填（企业定制）。

群成员会发这样一段文字：
    粤BW44C6 何晓燕 13480707210 商业20 交强3 驾意20

本模块按其中的【车牌号】找到系统里同车牌的已识别保单记录，把
    商业X  -> 政策商业险
    交强X / 强险X -> 政策强险
    驾意X / 非车X -> 政策非车险
    11位手机号 -> 电话
回填到该车牌的【所有】同车牌记录（自定义字段，透传到导出/页面列）。

姓名（如"何晓燕"）忽略不处理。

只有同时出现【车牌】+【至少一个政策项】才认为是政策报价文本，
普通聊天文本返回 None、不做任何处理。
"""

import re
import logging

logger = logging.getLogger(__name__)

# 车牌：省份简称 + 字母 + 4~6 位本体（兼容新能源 6 位）
_PROVINCE = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
_PLATE_RE = re.compile(rf"[{_PROVINCE}][-\s]?[A-Z][-\s]?[A-Z0-9]{{4,6}}")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")

# 字段关键词 -> 目标自定义字段
# 顺序 + 负向前瞻很关键：政策三项(商业/交强 后直接跟数字、驾意 后跟数字)不能误吃"前端/后端/险..."
_FIELD_PATTERNS = [
    ("政策商业险", re.compile(r"商\s*业\s*(?![前后])[:：]?\s*(\d+(?:\.\d+)?)")),
    ("政策强险", re.compile(r"(?:交\s*强|强\s*险)\s*(?![前后])[:：]?\s*(\d+(?:\.\d+)?)")),
    ("政策非车险", re.compile(r"驾\s*意\s*(?!险)[:：]?\s*(\d+(?:\.\d+)?)")),
    # 前后端佣金/费率（可能小数）
    ("商业前端", re.compile(r"商\s*业\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("商业后端", re.compile(r"商\s*业\s*后\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("交强前端", re.compile(r"交\s*强\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("交强后端", re.compile(r"交\s*强\s*后\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("驾意前端", re.compile(r"驾\s*意\s*险?\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("驾意后端", re.compile(r"驾\s*意\s*险?\s*后\s*端?\s*[:：]?\s*(\d+(?:\.\d+)?)")),
]


def parse_policy_quote_text(text: str):
    """
    解析政策报价文本。
    返回 None（非政策报价文本）或 dict：
      {"plate": "粤BW44C6", "政策商业险": "20", "政策强险": "3",
       "政策非车险": "20", "电话": "13480707210"}（仅含解析到的项）。
    """
    if not text or not text.strip():
        return None
    t = text.replace("　", " ")

    pm = _PLATE_RE.search(t)
    if not pm:
        return None
    # 车牌归一化：去掉连字符/空格，统一大写（与库中"车牌号"对齐）
    result = {"plate": re.sub(r"[-\s　]", "", pm.group(0)).upper()}

    for field, pat in _FIELD_PATTERNS:
        m = pat.search(t)
        if m:
            result[field] = m.group(1)

    # 必须至少有一个政策项，否则视为普通聊天
    if not any(k in result for k in ("政策商业险", "政策强险", "政策非车险")):
        return None

    pm2 = _PHONE_RE.search(t)
    if pm2:
        result["电话"] = pm2.group(0)
        # 业务渠道：电话后紧跟的中文名（如"小张"）；排除政策关键词，避免误吃"商业…"
        mc = re.search(r"1[3-9]\d{9}\s+([一-鿿]{2,4})", t)
        if mc and not re.match(r"^(商业|交强|驾意|强险|非车|驾乘)", mc.group(1)):
            result["业务渠道"] = mc.group(1)

    return result


def apply_policy_quote(db, text: str):
    """
    解析文本并按车牌回填到所有同车牌记录。
    返回 (matched_count, parsed_dict)：
      - 非政策报价文本：(0, None)
      - 是政策报价但无同车牌记录：(0, parsed)
      - 成功：(命中记录数, parsed)
    """
    from insurance.db import find_records_by_plate, update_insurance_record

    parsed = parse_policy_quote_text(text)
    if not parsed:
        return 0, None

    plate = parsed["plate"]
    fill = {k: v for k, v in parsed.items() if k != "plate"}

    records = find_records_by_plate(db, plate)
    if not records:
        logger.info("政策回填：车牌 %s 暂无识别记录，待补 %s", plate, fill)
        return 0, parsed

    cnt = 0
    for rec in records:
        pf = rec.get("parsed_fields") or {}
        if not isinstance(pf, dict):
            continue
        pf.update(fill)
        update_insurance_record(db, rec["id"], {"parsed_fields": pf})
        cnt += 1

    logger.info("政策回填成功：车牌=%s 命中 %d 条，值=%s", plate, cnt, fill)
    return cnt, parsed

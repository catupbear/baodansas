# insurance/policy_quote_fill.py
"""
群内"政策报价"文本回填（企业定制）。

群成员会发这样一段文字：
    粤BW44C6 何晓燕 13480707210 商业20 交强3 驾意20 业务员:小张 出单处:天河店

本模块按其中的【车牌号】找到系统里同车牌的已识别保单记录，把
    商业X  -> 政策商业险
    交强X / 强险X -> 政策强险
    驾意X / 非车X -> 政策非车险
    11位手机号 -> 电话
    业务员XXX -> 业务员#2（紫金宝安定制：故意不用"业务员"这个key，避免覆盖保单OCR提取的系统字段）
    出单处XXX -> 出单处
    出单渠道XXX -> 出单渠道#2（同理，"出单渠道"是OCR"销售渠道"映射来的系统字段，
                            群里补充的存到独立的自定义字段，不覆盖OCR识别结果）
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
    ("政策非车险", re.compile(r"(?:驾\s*意|非\s*车)\s*(?!险)[:：]?\s*(\d+(?:\.\d+)?)")),
    # 前后端佣金/费率（可能小数）
    ("商业前端", re.compile(r"商\s*业\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("商业后端", re.compile(r"商\s*业\s*后\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("交强前端", re.compile(r"交\s*强\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("交强后端", re.compile(r"交\s*强\s*后\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("驾意前端", re.compile(r"驾\s*意\s*险?\s*前\s*端\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    ("驾意后端", re.compile(r"驾\s*意\s*险?\s*后\s*端?\s*[:：]?\s*(\d+(?:\.\d+)?)")),
    # 业务员/出单处：紫金宝安定制自定义字段（key 对应他们后台已配置的列，业务员刻意用
    # "业务员#2"而非"业务员"，避免覆盖保单OCR提取出的系统字段"业务员"）
    ("业务员#2", re.compile(r"业\s*务\s*员\s*[:：]?\s*([一-鿿A-Za-z]{1,6})")),
    ("出单处", re.compile(r"出\s*单\s*处\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    # 2026-07-17 新增自定义字段：短文本类统一用"非空白/非常见标点"截止，
    # 避免贪婪吃掉后面紧跟着的其他字段（如"备注信息:xx 出单处:天河店"）
    # 出单渠道#2：跟业务员#2同理，故意不用"出单渠道"这个key——它是保单OCR"销售渠道"
    # 映射过来的系统字段，群里补充的是另一个自定义字段，不覆盖OCR识别结果
    ("出单渠道#2", re.compile(r"出\s*单\s*渠\s*道\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    ("保险公司经办人", re.compile(r"保\s*险\s*公\s*司\s*经\s*办\s*人\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    ("业务经理", re.compile(r"业\s*务\s*经\s*理\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    ("业务专员", re.compile(r"业\s*务\s*专\s*员\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    ("应付费率", re.compile(r"应\s*付\s*费\s*率\s*[:：]?\s*(\d+(?:\.\d+)?%?)")),
    ("备注信息", re.compile(r"备\s*注\s*信\s*息\s*[:：]?\s*([^\s，,。、；;]{1,20})")),
    ("其他备注", re.compile(r"其\s*他\s*备\s*注\s*[:：]?\s*([^\s，,。、；;]{1,20})")),
    ("联系电话", re.compile(r"联\s*系\s*电\s*话\s*[:：]?\s*(1[3-9]\d{9})")),
    ("过户后车牌地", re.compile(r"过\s*户\s*后\s*车\s*牌\s*地?\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
    ("录单网点", re.compile(r"录\s*单\s*网\s*点\s*[:：]?\s*([^\s，,。、；;]{1,10})")),
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
    # 紫金宝安实际用法里常常只发"车牌+业务员+出单处"、不带商业/交强/驾意数字，
    # 所以业务员#2/出单处命中也算数，不能只看三个保费数字项
    _GATE_FIELDS = (
        "政策商业险", "政策强险", "政策非车险", "业务员#2", "出单处",
        "出单渠道#2", "保险公司经办人", "业务经理", "业务专员", "应付费率",
        "备注信息", "其他备注", "联系电话", "过户后车牌地", "录单网点",
    )
    if not any(k in result for k in _GATE_FIELDS):
        return None

    pm2 = _PHONE_RE.search(t)
    if pm2:
        result["电话"] = pm2.group(0)
        # 业务渠道：电话后紧跟的中文名（如"小张"）；排除政策关键词，避免误吃"商业…"
        mc = re.search(r"1[3-9]\d{9}\s+([一-鿿]{2,4})", t)
        if mc and not re.match(r"^(商业|交强|驾意|强险|非车|驾乘)", mc.group(1)):
            result["业务渠道"] = mc.group(1)

    return result


# ------------------------------------------------------------------ #
# 可配置关键词规则引擎（企业级，双轨制）
#
# 企业在「字段配置 → 群内补充」保存过规则时走这里；从未保存过的企业
# 仍走上面的 parse_policy_quote_text 原写死逻辑，行为与线上完全一致。
# ------------------------------------------------------------------ #

# 值类型 -> 捕获正则片段（文本类另在编译时注入"其他关键词"截止边界）
_VALUE_PATTERNS = {
    "number": r"(\d+(?:\.\d+)?)",
    "phone": r"(1[3-9]\d{9})",
    "percent": r"(\d+(?:\.\d+)?%?)",
}

# 企业规则缓存：{enterprise_id: (compiled_or_None, 过期时间戳)}
_rules_cache: dict = {}
_RULES_TTL = 60


def _keyword_regex(keyword: str) -> str:
    """关键词逐字 re.escape 并允许字间空格（兼容"业 务 员"写法）。"""
    return r"[ \t]*".join(re.escape(ch) for ch in keyword.strip())


def compile_fill_rules(rules: list):
    """
    把配置规则编译为 [(target, compiled_regex), ...]。

    分隔符兼容：全/半角冒号可选、行内空格可选（\\s 不跨行，"业务员："
    后换行不会误吃下一行内容）。文本类值以"其他已配置关键词"为截止
    边界，防止"业务员：出单渠道：演示"同行粘连时把下个关键词误当值。
    非法规则（无 target/keywords）跳过；全部非法返回空列表。
    """
    valid = [r for r in (rules or [])
             if r.get("enabled", True) and r.get("target")
             and [k for k in (r.get("keywords") or []) if k and str(k).strip()]]

    all_keywords = [str(k).strip() for r in valid for k in r["keywords"] if str(k).strip()]
    kw_alt = "|".join(_keyword_regex(k) for k in sorted(all_keywords, key=len, reverse=True))
    # 文本值：非空白/常见标点，且任意位置不吸入其他关键词（含冒号形式）
    text_char = r"[^\s，,。、；;:：]"
    if kw_alt:
        text_value = rf"((?:(?!(?:{kw_alt}))(?:{text_char})){{1,20}})"
    else:
        text_value = rf"({text_char}{{1,20}})"

    compiled = []
    for r in valid:
        vt = (r.get("value_type") or "text").lower()
        value_pat = _VALUE_PATTERNS.get(vt, text_value)
        for kw in r["keywords"]:
            kw = str(kw).strip()
            if not kw:
                continue
            pat = re.compile(rf"{_keyword_regex(kw)}[ \t]*[:：]?[ \t]*{value_pat}")
            compiled.append((r["target"], pat))
    return compiled


def get_compiled_rules(db, enterprise_id):
    """读取并编译企业规则，60s TTL 缓存。企业未配置返回 None。"""
    import time
    if enterprise_id is None:
        return None
    entry = _rules_cache.get(enterprise_id)
    if entry and time.time() < entry[1]:
        return entry[0]
    from insurance.field_config_db import get_group_fill_rules
    try:
        rules = get_group_fill_rules(db, enterprise_id)
    except Exception:
        logger.exception("读取群内补充规则失败: eid=%s", enterprise_id)
        rules = None
    compiled = compile_fill_rules(rules) if rules is not None else None
    _rules_cache[enterprise_id] = (compiled, time.time() + _RULES_TTL)
    return compiled


def parse_with_rules(text: str, compiled: list):
    """
    用企业配置规则解析文本。
    判定门槛与写死逻辑一致：车牌 + 至少一条规则命中且有有效值。
    带了关键词没填值（正则天然不命中）、没带的关键词 → 对应字段不处理。
    返回 None 或 {"plate": ..., 字段: 值}。
    """
    if not text or not text.strip() or not compiled:
        return None
    t = text.replace("　", " ")

    pm = _PLATE_RE.search(t)
    if not pm:
        return None
    result = {"plate": re.sub(r"[-\s　]", "", pm.group(0)).upper()}

    for target, pat in compiled:
        if target in result:
            continue  # 同一目标多个关键词，取先命中的
        m = pat.search(t)
        if m and m.group(1) and m.group(1).strip():
            result[target] = m.group(1).strip()

    if len(result) <= 1:  # 只有车牌、无任何有值项 → 普通聊天
        return None
    return result


def apply_policy_quote(db, text: str, roomid: str = None, enterprise_id: int = None):
    """
    解析文本并按车牌回填到所有同车牌记录。
    返回 (matched_count, parsed_dict)：
      - 非政策报价文本：(0, None)
      - 是政策报价但无同车牌记录：(0, parsed)
      - 成功：(命中记录数, parsed)

    roomid: 触发这条文本的群，只回填同一个群里的同车牌记录，
    避免不同群（不同企业）凑巧同车牌号时数据串到别的企业。
    enterprise_id: 该群所属企业；企业配置过关键词规则时走新引擎，
    否则走 parse_policy_quote_text 原写死逻辑（双轨制）。
    """
    from insurance.db import find_records_by_plate, update_insurance_record

    compiled = get_compiled_rules(db, enterprise_id)
    if compiled is not None:
        parsed = parse_with_rules(text, compiled)
    else:
        parsed = parse_policy_quote_text(text)
    if not parsed:
        return 0, None

    plate = parsed["plate"]
    # 空串/空白值一律不写库（带了关键词没填值的项不处理、不覆盖原值）
    fill = {k: v for k, v in parsed.items() if k != "plate" and v and str(v).strip()}
    if not fill:
        return 0, None

    records = find_records_by_plate(db, plate, roomid=roomid)
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

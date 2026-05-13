#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保单字段解析器
从PDF提取的文本中，智能提取交强险、商业险等保单关键字段
架构：先识别保司和险种类型 → 再按保司分发对应的正则规则
"""

import re
from typing import Dict, Any, List, Optional

import jieba.posseg as pseg
# 预加载 jieba 词典，避免首次调用延迟
pseg.cut("预加载")


# ============================================================
# 保司配置：名称、简称、保单号前缀、特征关键词
# ============================================================

# 公司基础名称列表（匹配后会自动抓取分公司等后缀）
COMPANY_BASES = [
    "中国人民财产保险股份有限公司",
    "中国平安财产保险股份有限公司",
    "中国太平洋财产保险股份有限公司",
    "大家财产保险有限责任公司",
    "阳光财产保险股份有限公司",
    "太平财产保险有限公司",
    "国寿财产保险股份有限公司",
    "中国人寿财产保险股份有限公司",
    "紫金财产保险股份有限公司",
    "申能财产保险股份有限公司",
    "浙商财产保险股份有限公司",
    "中华联合财产保险股份有限公司",
    "天安财产保险股份有限公司",
    "华安财产保险股份有限公司",
    "永安财产保险股份有限公司",
    "华泰财产保险有限公司",
    "众安在线财产保险股份有限公司",
    "亚太财产保险有限公司",
    "都邦财产保险股份有限公司",
    "渤海财产保险股份有限公司",
    "锦泰财产保险股份有限公司",
    # 新增保司
    "永诚财产保险股份有限公司",
    "新疆前海联合财产保险股份有限公司",
    "前海联合财产保险股份有限公司",
    "京东安联财产保险有限公司",
    "安盛天平财产保险有限公司",
    "华农财产保险股份有限公司",
    "鼎和财产保险股份有限公司",
    "众诚汽车保险股份有限公司",
    "安诚财产保险股份有限公司",
    "国任财产保险股份有限公司",
    "泰康在线财产保险股份有限公司",
]

# 保司简称映射
COMPANY_SHORT_MAP = {
    "人民财产": "人保PICC",
    "平安财产": "平安",
    "太平洋财产": "太平洋",
    "大家财产": "大家财险",
    "阳光财产": "阳光",
    "太平财产": "太平",
    "国寿财产": "国寿财产",
    "人寿财产": "国寿财产",
    "紫金财产": "紫金",
    "申能财产": "申能",
    "浙商财产": "浙商",
    "中华联合": "中华联合",
    "天安财产": "天安",
    "华安财产": "华安",
    "永安财产": "永安",
    "华泰财产": "华泰",
    "众安在线": "众安",
    "亚太财产": "亚太",
    "都邦财产": "都邦",
    "渤海财产": "渤海",
    "锦泰财产": "锦泰",
    "永诚财产": "永诚",
    "前海联合": "前海联合",
    "京东安联": "京东安联",
    "安盛天平": "安盛天平",
    "华农财产": "华农",
    "鼎和财产": "鼎和",
    "众诚汽车": "众诚",
    "安诚财产": "安诚",
    "国任财产": "国任",
    "泰康在线": "泰康在线",
}

# 通过特征关键词辅助识别保司（当公司名未直接出现时的兜底）
COMPANY_FALLBACK_SIGNALS = [
    # (特征关键词/正则, 公司全称)
    (r"95552|alltrust\.com|yongcheng\.com|永诚", "永诚财产保险股份有限公司"),
    (r"4008110110|qhins\.com|前海联合", "新疆前海联合财产保险股份有限公司"),
    (r"950610|JDallianz|京东安联", "京东安联财产保险有限公司"),
    (r"95550|axa\.cn|安盛天平|AXAINS", "安盛天平财产保险有限公司"),
    (r"95519|中国人寿财产|国寿财产", "中国人寿财产保险股份有限公司"),
    (r"95585|cic\.cn|中华保|中华联合", "中华联合财产保险股份有限公司"),
    (r"95518|epicc\.com|人保财险|PICC", "中国人民财产保险股份有限公司"),
    (r"95511|pingan\.com|平安好车主|平安好生活|平安产险", "中国平安财产保险股份有限公司"),
    (r"95500|cpic\.com|太平洋财产", "中国太平洋财产保险股份有限公司"),
    (r"95589|大家财险|大家财产", "大家财产保险有限责任公司"),
    (r"95510|阳光财产", "阳光财产保险股份有限公司"),
    (r"95589.*太平|太平财产|太平保险", "太平财产保险有限公司"),
    (r"紫金财产", "紫金财产保险股份有限公司"),
    (r"申能财产", "申能财产保险股份有限公司"),
    (r"95154|浙商财产", "浙商财产保险股份有限公司"),
    (r"4006095509|ehuatai\.com|华泰财险", "华泰财产保险有限公司"),
    (r"华农财产|华农保险", "华农财产保险股份有限公司"),
    (r"鼎和财产|鼎和保险", "鼎和财产保险股份有限公司"),
    (r"4008600600|众诚汽车|众诚保险|urtrust\.com", "众诚汽车保险股份有限公司"),
    (r"95544|安诚财产|安诚保险", "安诚财产保险股份有限公司"),
    (r"956030|guorenpcic|国任财产|国任保险", "国任财产保险股份有限公司"),
    (r"95522|tkol\.com|泰康在线|泰康财产", "泰康在线财产保险股份有限公司"),
]


# ============================================================
# 被保险人/投保人提取时需过滤的无效值
# ============================================================

PERSON_BLACKLIST = {
    "地址", "手机号", "信息", "名称", "保单验真码", "保单验真码:",
    "行驶证车主", "行驶证", "为", "身份证", "证件",
    "姓名", "证件类型", "证件号码", "申请", "投保申请", "个人信息",
    "手机号码", "联系人", "通讯地址", "电子邮箱",
    "法定", "法定受益人", "受益人", "主被保人",
    # 新增：各保司OCR常见误提取
    "出生日期", "手机电话", "联系电话", "机关",
    "公章并由投", "声明", "社会统一信用",
    "包括主", "Policy",
    # 投保单中的条款文字
    "须如实告知",
    # 单独的通用词（非具体名称）
    "公司",
}


def _is_valid_person(val: str) -> bool:
    """
    判断提取的值是否为有效的人名或公司名。
    采用白名单策略：只有明确符合人名/公司名特征的才通过。
    宁可漏提取，不可错提取。
    """
    if not val or len(val) < 2:
        return False

    # 去掉首尾空白
    val = val.strip()

    # 黑名单精确匹配
    if val in PERSON_BLACKLIST:
        return False

    # ====== 公司名判断（优先，允许较长） ======
    # 去掉括号后缀再判断（如"飘逸共雅饮品店（个体工商户）"→ 以"店"结尾）
    val_no_paren = re.sub(r'[（(][^）)]*[）)]$', '', val)
    # 含"(地名)"的企业名（如"中设(深圳)设备检验检测"）也视为公司名
    has_location_paren = bool(re.search(r'[\u4e00-\u9fff][（(][\u4e00-\u9fff]{2,4}[）)][\u4e00-\u9fff]', val))
    if re.search(r'公司|集团|合伙|工厂|商行|商贸|车行(?!驶)|车队|事务所|经营部|经营店|经销商|专营店|服务部|加工厂|养殖场|合作社|研究院|研究所|设计院|分院|医院|学院|中心|个体工商户', val) or val_no_paren.endswith('店') or has_location_paren:
        # 公司名允许长一些，但不能超过 30 字
        # 排除含动词/条款用语/保险术语的句子片段（如"向本公司提出的申请"）
        if re.search(r'提出|提供|负责|承担|向.*公司|本公司.*的|申请|告知|声明|附加.*险|附加.*费|损失费|保险费', val):
            return False
        return len(val) <= 30

    # ====== 以下是人名判断（严格白名单） ======

    # 人名长度：2-6 个中文字符（含少数民族 4-6 字名）
    # 允许中间有 · 分隔符（少数民族名如"阿卜杜拉·艾买提"）
    if not re.match(r'^[\u4e00-\u9fff]{2,6}$', val) and \
       not re.match(r'^[\u4e00-\u9fff]{1,4}[·・][\u4e00-\u9fff]{1,6}$', val):
        return False

    # 含任何非人名词汇的直接拒绝
    if re.search(
        # 保险术语
        r'保险|保单|保费|条款|申请|投保|理赔|赔偿|责任|免责|免赔'
        r'|绑单|验真|承保|退保|批改|核保|出险|报案'
        # 法律/合同用语
        r'|死亡|伤害|伤残|遭受|宣告|期间|约定|告知|声明|同意|签署'
        r'|解除|终止|变更|转让|撤销|无效|违约|纠纷|诉讼|仲裁'
        r'|民事|刑事|行为|能力|限制|法定|监护'
        r'|主险|附加|合同|条件|生效|届满|届时'
        # 车辆/证件相关
        r'|交通|机动车|车辆|证件|身份证|手机|电话|姓名|号码|地址'
        r'|发动机|车架号|车牌|行驶证|户籍|证明|或者'
        # 文档结构词
        r'|信息|个人信|序号|编号|日期|时间|金额|费用|合计|总计'
        r'|本公司|该公司|受益人|被保人|投保人'
        # 动词/介词/虚词
        r'|提出|提供|负责|承担|享有|具有|应当|可以|不得'
        r'|下列|上述|以下|以上|其中|根据|按照|依据'
        # 条款常见误提取
        r'|本人|雇佣',
        val
    ):
        return False

    return True


def _clean_person_name(val: str) -> str:
    """清理人名中的常见干扰字符"""
    val = val.strip()
    # 清理中文字符间的OCR空格（如"沈 心诚" → "沈心诚"、"大 家财产" → "大家财产"）
    val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', val)
    val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', val)  # 二次清理
    # 保留名称中间的括号内容（如"中设(深圳)设备检验检测"），只清理末尾或无关的括号
    if re.search(r'[（(][^）)]*[）)][\u4e00-\u9fff]', val):
        # 括号后面还有中文，属于名称的一部分（如"中设(深圳)设备"），只清理冒号
        val = re.sub(r'[：:].*', '', val)
    else:
        val = re.sub(r'[：:（(].*', '', val)
    val = re.sub(r'车主.*', '', val)
    val = re.sub(r'名称.*', '', val)
    val = re.sub(r'证件.*', '', val)
    # 公司名后粘连保险术语（如"XX有限公司附加出行不便损失费"→"XX有限公司"）
    val = re.sub(r'((?:股份有限|有限责任|有限)公司)(?:附加|免责|条款|保险费|损失费|出行).*', r'\1', val)
    # 永诚OCR干扰：人名后面附带"有敬异的"
    val = re.sub(r'有敬异的.*', '', val)
    val = re.sub(r'果尊.*', '', val)
    # 清理人名后面多余的职业/证件后缀（如"马玲玲执业证" → "马玲玲"）
    val = re.sub(r'(执业|资格|从业|代理|经纪)(证|号|资格).*', '', val)
    return val.strip()


# ============================================================
# 第一步：识别保司
# ============================================================

def _identify_company(text: str) -> Dict[str, str]:
    """
    识别保司，返回 {"保险公司": "全称", "保险公司简称": "简称"}
    策略：
    1. 先从文本中直接匹配公司全称
    2. 匹配失败时，通过特征关键词（电话、网址等）兜底识别
    """
    result = {}

    # 先处理公司名中可能存在的字间空格
    company_text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2',
                          re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text))

    # 方式1："公司名称："或"承保公司为："格式
    m = re.search(r"(?:公司名称|承保公司为?)[：:\s]*\n?\s*(.+?)(?:\s+公司地址|$)", company_text)
    if m:
        val = m.group(1).strip()
        if len(val) > 6:
            # 清理尾部多余内容
            val = re.sub(r'(公司|营业部|服务部|业务部|支公司).*', r'\1', val)
            result["保险公司"] = val

    # 方式2：遍历公司基础名称列表匹配
    if "保险公司" not in result:
        for base in COMPANY_BASES:
            p = re.escape(base) + r'[\S]*'
            m = re.search(p, company_text)
            if m:
                val = m.group(0).strip()
                val = re.sub(r'\s+公司地址.*', '', val)
                val = re.sub(r'(公司|营业部|服务部|业务部|支公司).*', r'\1', val)
                if len(val) > 6:
                    result["保险公司"] = val
                    break

    # 方式3：特征关键词兜底识别
    if "保险公司" not in result:
        for pattern, company_name in COMPANY_FALLBACK_SIGNALS:
            if re.search(pattern, text, re.IGNORECASE):
                result["保险公司"] = company_name
                break

    # 方式4：通过保单号前缀推断保司（极简标志文件等场景）
    if "保险公司" not in result:
        # 从文本中提取可能的保单号
        m = re.search(r'(\d{15,30})', re.sub(r'\s+', '', text))
        if m:
            pno = m.group(1)
            prefix_map = [
                (r'^1[01][\d]{2,3}003', "中国平安财产保险股份有限公司"),  # 平安车险
                (r'^1[01][\d]{2,3}005', "中国平安财产保险股份有限公司"),  # 平安交强
                (r'^1[01][\d]{2,3}006', "中国平安财产保险股份有限公司"),  # 平安意外险
                (r'^1355', "中国平安财产保险股份有限公司"),            # 平安（1355前缀）
                (r'^1045', "中国平安财产保险股份有限公司"),            # 平安（1045前缀）
                (r'^606EA', "华泰财产保险有限公司"),                  # 华泰
                (r'^112B', "永诚财产保险股份有限公司"),                # 永诚
                (r'^183', "新疆前海联合财产保险股份有限公司"),          # 前海联合
                (r'^2104', "安盛天平财产保险有限公司"),                # 安盛天平
                (r'^29104', "安盛天平财产保险有限公司"),               # 安盛天平（29104前缀）
                (r'^P26', "中华联合财产保险股份有限公司"),             # 中华联合
                (r'^29104', "紫金财产保险股份有限公司"),               # 紫金
                (r'^2136', "大家财产保险有限责任公司"),                # 大家
                (r'^6605', "阳光财产保险股份有限公司"),                # 阳光
                (r'^6627', "中国人寿财产保险股份有限公司"),            # 国寿
                (r'^P8100', "京东安联财产保险有限公司"),               # 京东安联驾乘
                (r'^ASHZ', "中国太平洋财产保险股份有限公司"),          # 太平洋
                (r'^BSHZ', "中国太平洋财产保险股份有限公司"),          # 太平洋批单
                (r'^P432', "太平财产保险有限公司"),                   # 太平
                (r'^6203', "申能财产保险股份有限公司"),                # 申能
                (r'^6260', "国任财产保险股份有限公司"),                # 国任
            ]
            for prefix_pat, company_name in prefix_map:
                if re.match(prefix_pat, pno):
                    result["保险公司"] = company_name
                    break

    # 映射简称
    for keyword, short in COMPANY_SHORT_MAP.items():
        if keyword in result.get("保险公司", ""):
            result["保险公司简称"] = short
            break

    return result


# ============================================================
# 第二步：识别险种类型
# ============================================================

def _identify_policy_type(text: str) -> Optional[str]:
    """识别险种类型"""
    for p in [
        r"(新能源汽车商业保险)",
        r"(机动车商业保险)",
        r"(机动车辆商业保险)",             # 大家财险格式
        r"(特种车商业保险)",               # 太平/京东安联特种车格式
        r"(新能源汽车交通事故责任强制保险)",
        r"(机动车交通事故责任强制保险)",
        r"(非?家庭自用车?驾乘人员人身意外伤害保险)",
        r"(驾乘综合保险)",
        r"(驾乘人员人身意外伤害保险)",
        r"(驾乘人员意外伤害保险)",          # 大家财险格式（无"人身"）
        r"(驾乘人员意外险\S*?(?:[（(]\S+?[)）])?)",  # 中华联合格式（如"驾乘人员意外险A款（团体）"）
        r"(驾乘无忧\S+)",
        r"(机动车驾乘人员意外伤害保险(?:[（(]\S+?[)）])?)",
        r"(机动车辆驾乘人员意外伤害保险)",  # 大家财险格式
        r"(平安车主尊享保障)",
        r"(E车无忧\S*保险?)",              # 阳光E车无忧守护版
        r"(交通出行人身意外伤害保险)",
        r"(交通出行意外伤害保险)",
        r"(交通工具意外伤害保险)",          # 安盛天平/平安格式
        r"(锦程交通意外伤害保险)",          # 申能格式
        r"(个人意外伤害保险)",
        r"(\u201c如意行\u201d驾乘综合保险)",
        r"(机动车辆保险)",
        r"(机动车辆综合险)",               # 安盛天平格式
        r"(平安车主出行险)",
        r"(守护出行无忧[\d.]*(?:[（(]\S+?[)）])?)",  # 太平守护出行无忧3.0
        r"(安心行\S*驾乘意外险)",          # 京东安联安心行驾乘意外险
        r"(卓越全意保\S*保险)",            # 安盛天平卓越全意保
        r"(个人账户资金安全险)",            # 国任非车险
        r"(信用卡盗用保险)",               # 国任非车险
        r"(家财守护(?:[（(]\S+?[)）])?)",  # 家财守护（单交经济版）等家财险产品
    ]:
        m = re.search(p, text)
        if m:
            return m.group(1)

    # 太平洋"神行车保"系列：标题不含标准险种关键词，需从标题推断
    # "神行车保机动车保险单" / "神行车保新能源汽车保险单" / "神行车保系列产品批单"
    m = re.search(r"神行车保\S*?(新能源汽车|机动车)\S*?保险单", text)
    if m:
        if "新能源" in m.group(0):
            return "新能源汽车商业保险"
        return "机动车商业保险"

    # 京东安联 POLICY SCHEDULE 格式：从"Total Premium"等判断
    if re.search(r"POLICY\s*SCHEDULE|保\s*险\s*单", text[:300]):
        if re.search(r"Total\s*Premium|总保费", text):
            # 检查是否为驾乘意外险
            if re.search(r"Accidental|意外.*身故|意外.*伤残|驾乘", text):
                return "驾乘人员意外伤害保险"

    # 合同条款格式：投保人已向本保险人投保"网约车道路客运承运人责任险"
    m = re.search(r'投保["\u201c]([^"\u201d]+(?:险|保险))["\u201d]', text)
    if m:
        return m.group(1)

    # 兜底：从标题提取，如"道路危险货物承运人责任险保险单"
    m = re.search(r"([\u4e00-\u9fff]+(?:责任险|保险|意外险|综合险))保险单", text[:500])
    if m:
        return m.group(1)

    # 众诚等格式："非车险保险单(电子保单)"
    m = re.search(r"([\u4e00-\u9fff]+险)保险单", text[:500])
    if m:
        return m.group(1)

    # 兜底：无标题但包含交强险特征字段（赔偿限额），如华农交强险保单
    compulsory_indicators = [
        r"死亡伤残赔偿限额",
        r"医疗费用赔偿限额",
        r"财产损失赔偿限额",
    ]
    if sum(1 for p in compulsory_indicators if re.search(p, text)) >= 2:
        return "机动车交通事故责任强制保险"

    return None


def _get_policy_type_code(policy_type: str) -> tuple:
    """根据险种类型返回 (type_code, type_name)"""
    if not policy_type:
        return "unknown", "未知"
    if "交通事故责任强制" in policy_type:
        return "compulsory", "交强险"
    if "商业保险" in policy_type or "机动车辆保险" in policy_type or "机动车辆综合险" in policy_type:
        return "commercial", "商业险"
    if "驾乘" in policy_type or "意外" in policy_type or "出行险" in policy_type or "出行无忧" in policy_type or "安心行" in policy_type or "E车无忧" in policy_type or "卓越全意保" in policy_type:
        return "accident", "驾乘/意外险"
    if "账户" in policy_type or "资金安全" in policy_type or "信用卡" in policy_type or "盗用" in policy_type:
        return "non_vehicle", "非车险"
    if "承运人" in policy_type or "货运" in policy_type or "货物" in policy_type or "责任险" in policy_type:
        return "non_vehicle", "非车险"
    if "家财" in policy_type:
        return "non_vehicle", "非车险"
    return "unknown", policy_type


# ============================================================
# 第三步：通用字段提取（各保司共用）
# ============================================================

def _extract_common_fields(text: str, company_short: str, policy_type: str = "") -> Dict[str, Any]:
    """提取各保司通用的字段"""
    fields = {}
    text_merged = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2',
                         re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text))

    # ===== 保单号 =====
    _extract_policy_no(text, fields, company_short, policy_type)

    # ===== 投保确认码 =====
    m = re.search(r"(?:投保)?确认码[：:\s]*(\S+)", text)
    if m:
        fields["投保确认码"] = m.group(1)

    # ===== 人员信息 =====
    _extract_insured(text, text_merged, fields, company_short)
    _extract_proposer(text, text_merged, fields, company_short)

    # ===== 车辆信息 =====
    _extract_vehicle_info(text, fields, company_short)

    # ===== 费用信息 =====
    _extract_premium(text, fields, company_short)
    _extract_tax(text, fields)

    # ===== 时间信息 =====
    _extract_period(text, text_merged, fields, company_short)
    _extract_sign_date(text, fields, company_short)

    # ===== 收费确认时间 =====
    m = re.search(r"收费确认时间[：:\s]*([\d\-/:年月日时分秒\s]+?)(?:\s{2,}|$|投保|有效|打印)", text)
    if m:
        fields["收费确认时间"] = m.group(1).strip()

    # ===== 销售渠道 =====
    m = re.search(r"销售渠道[：:].*?√(\S+)", text)
    if m:
        fields["销售渠道"] = m.group(1)
    elif not fields.get("销售渠道"):
        m = re.search(r"销售渠道\s+(\S+)", text)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and len(val) <= 20:
                fields["销售渠道"] = val

    for p in [
        r"中介机构名称[：:]\s*(.+?)(?:\(|（|\s*$)",
        r"经纪公司名称[：:]\s*(.+?)(?:\s+联系电话|$)",
        r"保险中介名称[：:]\s*(.+?)(?:\s|$)",
    ]:
        m = re.search(p, text)
        if m:
            fields["中介机构"] = m.group(1).strip()
            break

    # ===== 其他 =====
    m = re.search(r"争议解决方式[：:\s]*(\S+)", text)
    if m:
        fields["争议解决方式"] = m.group(1)

    # 匹配"制单："或"制单人："，排除"制单时间"
    m = re.search(r"制单(?:人)?(?!时间)[：:\s]*(\S+?)(?:\s|$)", text)
    if m:
        fields["制单人"] = m.group(1).lstrip("：:")

    m = re.search(r"经办(?:人员?)?[：:]\s*([\u4e00-\u9fff](?:[^\S\n]?[\u4e00-\u9fff]){0,5})", text)
    if m:
        val = re.sub(r'\s+', '', m.group(1))  # 去除名字中的OCR空格
        if val:
            fields["经办人"] = val

    # 紫金等表格格式：值在标签行上方
    # "999999999 赖晨 王丽霞\n核保： 制单： 经办："
    # 经办人为上一行最后一个中文人名
    if "经办人" not in fields:
        lines_all = text.split('\n')
        for i, line in enumerate(lines_all):
            if re.search(r'核保[：:]\s*制单[：:]\s*经办[：:]', line) and i > 0:
                prev = lines_all[i - 1].strip()
                # 提取上一行中所有中文人名（按空格分割）
                names = [p for p in re.split(r'\s+', prev) if re.match(r'^[\u4e00-\u9fff]{2,6}$', p)]
                if names:
                    val = _clean_person_name(names[-1])  # 最后一个对应"经办"
                    if val and len(val) >= 2:
                        fields["经办人"] = val
                break

    # ===== 业务员 / 代理人 =====
    # 注意："代理人名称"是中介机构公司名，不是业务员
    for p in [
        r"业务员[姓名称]*[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff]{1,5})",
        r"销售人员[名称]*[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff]{1,5})",
        r"代理人[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff]{1,5})",
    ]:
        m = re.search(p, text)
        if m:
            val = _clean_person_name(m.group(1))
            if val and len(val) >= 2 and not re.search(r'公司|集团|保险|有限|机构|人寿|平安|太平|阳光|人保|车险|非车|财险|寿险|健康险|意外险|团险|渠道', val):
                fields["业务员"] = val
                break
    # 中华联合：销售渠道名称兜底为业务员（值可能是代理公司名）
    if "业务员" not in fields and company_short == "中华联合":
        m = re.search(r"销售渠道名称[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,20})", text)
        if m:
            val = m.group(1).strip()
            if val and len(val) >= 2:
                fields["业务员"] = val
    # 华农：销售机构兜底为业务员
    if "业务员" not in fields and company_short == "华农":
        m = re.search(r"销售机构[：:]\s*(\S+)", text)
        if m:
            val = m.group(1).strip()
            if val and len(val) >= 2:
                fields["业务员"] = val
    # 太平洋：业务员为"渠道"或未提取到时，取制单人（如"渠道QXL02047"）
    if company_short == "太平洋" and (fields.get("业务员") == "渠道" or "业务员" not in fields):
        if "制单人" in fields and fields["制单人"].startswith("渠道"):
            fields["业务员"] = fields["制单人"]
        elif "业务员" in fields:
            del fields["业务员"]

    # 安盛天平：出单代理 Agency Name 作为业务员（值通常是代理公司名）
    if "业务员" not in fields and company_short == "安盛天平":
        m = re.search(r"出单代理\s*(?:Agency\s*Name)?[：:]\s*(.+?)(?:\s{2,}|\n|$)", text, re.IGNORECASE)
        if m:
            val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', m.group(1).strip())
            if val and len(val) >= 2:
                fields["业务员"] = val

    # 平安：经办为代理公司名时作为业务员（如"经办:维客保险代理有限公司"）
    if "业务员" not in fields and company_short == "平安":
        m = re.search(r"经办[：:]\s*(.+?)(?:\s{2,}|$)", text, re.MULTILINE)
        if m:
            # 合并OCR空格后取值
            val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', m.group(1).strip())
            # 截掉尾部紧跟的日期（如"杨晓棠2026年4月27日..."）
            val = re.sub(r'\d{4}年.*$', '', val).strip()
            # 过滤承保公司名的误匹配（如"中国平安人寿"），但保留代理公司名
            if val and len(val) >= 2 and not re.search(r'中国平安|平安财产|平安人寿|人寿保险', val):
                fields["业务员"] = val

    # 经办人兜底为业务员；经办人无效时用制单人兜底
    if "业务员" not in fields:
        for fallback_field in ["经办人", "制单人"]:
            if fallback_field in fields:
                val = _clean_person_name(fields[fallback_field])
                if val and len(val) >= 2 and re.match(r'^[\u4e00-\u9fff]+$', val) \
                        and not re.search(r'公司|集团|保险|有限|机构|自动|车险|非车|财险|寿险|健康险|意外险|团险|渠道', val):
                    fields["业务员"] = val
                    break

    return fields


# ============================================================
# 保单号提取
# ============================================================

def _extract_policy_no(text: str, fields: dict, company_short: str, policy_type: str = ""):
    """提取保单号"""

    # 浙商等格式：文本中同时包含"交强险投保单号"和"商业险投保单号"，根据险种类型选取
    m_commercial = re.search(r"商业险投\s*保单号[：:\s]*(\d{15,30})", text)
    m_compulsory = re.search(r"交强险投\s*保单号[：:\s]*(\d{15,30})", text)
    if m_commercial and m_compulsory:
        if "商业" in policy_type or "机动车辆保险" in policy_type or "机动车辆综合险" in policy_type:
            fields["保单号"] = m_commercial.group(1)
        else:
            # 交强险或其他情况默认取交强险
            fields["保单号"] = m_compulsory.group(1)
        return
    elif m_commercial:
        fields["保单号"] = m_commercial.group(1)
        return
    elif m_compulsory:
        fields["保单号"] = m_compulsory.group(1)
        return

    # 紫金格式：保单号在"投保确认时间"行，与日期粘连或空格分隔
    # 格式1："投保确认时间：\n20590A44030226000BW12026-04-2716:33:54"（粘连）
    # 格式2："投保确认时间：\n20590A44030226000BWY 2026-04-2718:38:01"（空格分隔）
    if company_short == "紫金":
        m = re.search(r"投保确认时间[：:\s]*\n?\s*([A-Za-z0-9]+?)\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            val = m.group(1)
            if len(val) > 5:
                fields["保单号"] = val
                # 顺便存投保确认时间
                fields["收费确认时间"] = m.group(2)
                return

    # 华农等格式："保单号\n保单号：XXX\n流水号 流水号：YYY"，优先取保单号而非流水号
    m = re.search(r"保单号[：:]\s*(\d{15,30})", text)
    if m:
        fields["保单号"] = m.group(1)
        return

    # 华泰/京东安联等格式："保险单号Policy No.：XXX" 或 "保单号码Policy No.:XXX"
    m = re.search(r"保[险]?单号[码]?\s*Policy\s*No\.?[：:\s]+(\S+)", text)
    if m:
        val = m.group(1).strip().lstrip("：:")
        if val and len(val) > 3:
            fields["保单号"] = val
            return

    for p in [
        r"保险单号[码]?[：:\s]\s*(\S+)",
        r"保单号[码]?[：:\s]\s*(\S+)",
        r"保险单\s*号[码]?[：:\s]\s*(\S+)",
        r"保险凭证号[：:\s]\s*(\S+)",
        r"保险号[码]?[：:\s]\s*(\S+)",       # 国任等格式
    ]:
        m = re.search(p, text)
        if m:
            val = m.group(1).strip().lstrip("：:")
            # 过滤明显不是保单号的内容（排除英文单词如"Policy"）
            if val and not re.match(r'^[若如]', val) and not re.match(r'^[A-Za-z]+$', val) and len(val) > 3:
                # 如果值以"Policy"等英文开头，跳过（华泰混合格式由上方专用规则处理）
                if re.match(r'^Policy', val, re.IGNORECASE):
                    continue
                fields["保单号"] = val
                break

    # 永诚等保司：保单号中有空格 "1 12B5032620260001099" → "112B5032620260001099"
    if "保单号" not in fields:
        m = re.search(r"保险单号[码]?[：:\s]\s*(\d{1,3})\s+(\S+)", text)
        if m:
            val = m.group(1) + m.group(2)
            if len(val) > 6:
                fields["保单号"] = val

    # 华泰格式："保险单号 ： 606EA..." （冒号前后都有空格）
    if "保单号" not in fields:
        m = re.search(r"保险单号\s*[：:]\s*(\S+)", text)
        if m:
            val = m.group(1).strip()
            if val and len(val) > 3:
                fields["保单号"] = val

    # 永诚等：保单号每个字符之间有空格 "1 1 2 B 5 0 3 6 42..."
    # 通过清理空格后重新匹配
    if "保单号" not in fields:
        # 找到"保险单号："后面的内容，去除空格
        m = re.search(r"保险单号[码]?[：:\s]\s*(.+?)(?:\s*投\s*保|$)", text)
        if m:
            val = re.sub(r'\s+', '', m.group(1))
            if val and len(val) > 6 and re.match(r'^[A-Z0-9]', val):
                fields["保单号"] = val

    # 极简标志文件：保单号独立一行，无标签
    if "保单号" not in fields:
        lines = text.strip().split('\n')
        for line in lines[:3]:  # 只检查前3行
            stripped = line.strip()
            if re.match(r'^[A-Z0-9]{15,30}$', stripped):
                fields["保单号"] = stripped
                break

    # 统一清理：保单号只保留字母、数字、横线，截断中文/标点及之后的内容
    if "保单号" in fields:
        val = fields["保单号"]
        # 众诚：OCR可能将服务电话4008600600粘到保单号末尾
        if company_short == "众诚" and val.endswith("4008600600"):
            val = val[:-10]
            fields["保单号"] = val
        # 从第一个中文字符或中文标点处截断
        m = re.match(r'^[A-Za-z0-9\-_/.]+', val)
        if m:
            fields["保单号"] = m.group(0).rstrip(".-_/")
        else:
            # 全是中文等异常情况，删除
            del fields["保单号"]


# ============================================================
# 被保险人提取（按保司分策略）
# ============================================================

def _extract_insured(text: str, text_merged: str, fields: dict, company_short: str):
    """提取被保险人，按保司分发不同策略"""

    # ===== 平安个意险表格格式 =====
    # "序号 被保险人姓名 证件类型 ..."  → 下一行 "1 黄开支 身份证 ..."
    if company_short == "平安":
        _extract_insured_pingan(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 华泰特殊格式：无冒号，空格分隔 =====
    # "被保险人 陈灿强 证件号码"
    # "被 保 险 人 陈丽莲"
    if company_short == "华泰":
        _extract_insured_huatai(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 永诚特殊格式：OCR干扰 =====
    # "被 保 险 人 张爱良\n有 敬\n异 的"
    if company_short == "永诚":
        _extract_insured_yongcheng(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 太平特殊处理 =====
    if company_short == "太平":
        _extract_insured_taiping(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 京东安联英文格式 =====
    if company_short == "京东安联":
        # "被保险人信息" 或 "Num of Insured" 格式
        # 京东安联驾乘险被保险人为"驾驶或乘坐以下指定车辆的人员"
        m = re.search(r"被保险人.*?驾驶或乘坐\S*?指定车辆的人员", text)
        if m:
            # 被保险人为车上人员，使用投保人作为被保人
            if "投保人" in fields:
                fields["被保险人"] = fields["投保人"]
                return
        # 被保险人名称/姓名
        m = re.search(r"被保险人\s*(?:姓名|名称)?[：:\s]+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # ===== 前海联合特殊格式 =====
    if company_short == "前海联合":
        _extract_insured_qianhai(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 国寿驾乘险特殊格式 =====
    if company_short == "国寿财产":
        _extract_insured_guoshou(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 中华联合特殊格式 =====
    if company_short == "中华联合":
        _extract_insured_zhonghua(text, text_merged, fields)
        if "被保险人" in fields:
            return

    # ===== 人保驾乘险："被保险人为以下车辆的驾驶人员及乘客" =====
    if company_short == "人民财产":
        if re.search(r"被保险人为以下车辆的驾驶人员及乘客", text):
            if "投保人" in fields:
                fields["被保险人"] = fields["投保人"]
                return

    # ===== 紫金被保险人清单格式 =====
    # "被保险人清单" → 表头含"被保险人姓名" → 数据行"序号 姓名 ..."，可能多人
    if company_short == "紫金":
        _extract_insured_zijin(text, fields)
        if "被保险人" in fields:
            return

    # ===== 通用提取逻辑 =====
    _extract_insured_common(text, text_merged, fields)


def _extract_insured_pingan(text: str, text_merged: str, fields: dict):
    """平安保单被保险人提取"""
    lines = text.split('\n')

    # 平安个意险表格格式："序号 被保险人姓名 证件类型 证件号码"
    # 下一行有效数据行开头为数字序号："1 黄开支 身份证 441621..."
    for i, line in enumerate(lines):
        if re.search(r'被保险人姓名\s+证件类型', line):
            for j in range(i + 1, min(i + 6, len(lines))):
                line_j = lines[j].strip()
                # 纯车辆信息行跳过（行首就是车辆关键词）
                if re.match(r'(?:发动机|车架号|车牌|VIN)', line_j):
                    continue
                # 匹配"序号 姓名 身份证类型" 格式
                m = re.match(r'\d+\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)\s+(?:身份证|证件|统一社会)', line_j)
                if m and _is_valid_person(m.group(1)):
                    fields["被保险人"] = m.group(1)
                    return
                # 匹配"姓名 身份证 ..."格式（行尾可能粘连车辆信息如"发动机号:XXX"）
                m = re.match(r'([\u4e00-\u9fff]{2,6})\s+(?:身份证|证件|统一社会)', line_j)
                if m and _is_valid_person(m.group(1)):
                    fields["被保险人"] = m.group(1)
                    return
                # 匹配纯"姓名 身份证类型"格式
                parts = re.split(r'\s+', line_j)
                if len(parts) >= 2 and _is_valid_person(parts[0]):
                    fields["被保险人"] = parts[0]
                    return
            break

    # 平安商业险跨行格式：OCR 将公司名截断到下一行
    # "被保 正式名称: 深圳市义奇和乐科技有限公 证件类型: ...\n险人 司\n信息 ..."
    # 需要从"正式名称:"到"证件类型"之间提取公司名，并拼接下一行的残余部分
    for i, line in enumerate(lines):
        m = re.search(r"被保\s*正式名称[：:]\s*(.+?)(?:\s+证件类型)", line)
        if m:
            val = m.group(1).strip()
            # 如果公司名不完整（不以"公司/集团/商行/店"等结尾），尝试从下一行拼接
            if val and not re.search(r'(?:公司|集团|商行|商贸|店|工厂|事务所)$', val):
                for j in range(i + 1, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    # 下一行开头可能有"险人"等干扰，提取第一个有意义的中文片段
                    parts = re.split(r'\s+', next_line)
                    for part in parts:
                        # 尝试拼接每个片段，看是否能组成完整公司名
                        candidate = val + part
                        if re.search(r'(?:公司|集团|商行|商贸|店|工厂|事务所)$', candidate):
                            val = candidate
                            break
                    if re.search(r'(?:公司|集团|商行|商贸|店|工厂|事务所)$', val):
                        break
            val = _clean_person_name(val)
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # 平安标准格式：被保姓名 / 被保 正式名称
    for p in [
        r"被保姓名[：:]\s*(\S+)",
        r"被保\s*(?:险人)?\s*姓?\s*名[：:]\s*(\S+)",
        r"被保\s*正式名称[：:]\s*(\S+)",
        r"被保险人[：:]\s*(\S+)",
    ]:
        m = re.search(p, text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # 投保人姓名表格格式
    for i, line in enumerate(lines):
        if re.search(r'投保人姓名\s+证件类型', line):
            for j in range(i + 1, min(i + 4, len(lines))):
                parts = re.split(r'\s+', lines[j].strip())
                if parts and _is_valid_person(parts[0]):
                    fields["被保险人"] = parts[0]
                    return
            break


def _extract_insured_huatai(text: str, text_merged: str, fields: dict):
    """华泰保单被保险人提取"""
    # 华泰格式1："被保险人 陈灿强 证件号码"
    m = re.search(r"被\s*保\s*险\s*人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s+证件|\s+被保险人|\s*$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 华泰格式2：合并空格后 "被保险人陈丽莲"
    m = re.search(r"被保险人([\u4e00-\u9fff]{2,10})(?:被保险人|身份证|证件|地址)", text_merged)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 兜底：姓名：后面的值（排除"联系人姓名"等非被保险人标签）
    m = re.search(r"(?<!联系人)(?<!联系)(?<!经办人)姓名[：:\s]*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|；|;|$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


def _extract_insured_yongcheng(text: str, text_merged: str, fields: dict):
    """永诚保单被保险人提取（处理OCR侧边栏干扰）"""
    # 永诚的OCR文本中，侧边栏文字"如果尊敬的客户有异议的"会混入主文本
    # 典型格式："被 保 险 人 张爱良\n有 敬\n异 的"
    # 合并空格后变成 "被保险人张爱良有敬异的"

    # 先用合并文本匹配，然后清理干扰
    m = re.search(r"被保险人\s*([\u4e00-\u9fff]{2,10})", text_merged)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 标准冒号格式
    m = re.search(r"被\s*保\s*险\s*人[：:\s]+([\u4e00-\u9fff][\u4e00-\u9fff\w]+)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


def _extract_insured_taiping(text: str, text_merged: str, fields: dict):
    """太平保单被保险人提取"""
    # 太平格式特殊：先找"被保险人名称"或特别约定中的人名
    for p in [
        r"被保险人名称[：:\s]*(\S+)",
        r"被保险人[：:\s]*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)",
    ]:
        m = re.search(p, text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # 太平特别约定中"被保险人为XXX"
    m = re.search(r"被保险人为([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)", text)
    if m and _is_valid_person(m.group(1)):
        fields["被保险人"] = m.group(1)
        return

    # 太平驾乘险可能在表格中
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if re.search(r'被保险人姓名\s+证件类型', line):
            for j in range(i + 1, min(i + 6, len(lines))):
                parts = re.split(r'\s+', lines[j].strip())
                if parts and _is_valid_person(parts[0]):
                    fields["被保险人"] = parts[0]
                    return
            break


def _extract_insured_zijin(text: str, fields: dict):
    """紫金被保险人清单提取（支持多人）
    格式：
      被保险人清单
      序号 被保险人姓名 方案号 证件类型 证件号码 ...
      1    钟赛敬       2     身份证   441424...
      2    张三         1     身份证   ...
    """
    lines = text.split('\n')
    header_idx = -1
    name_col = -1

    for i, line in enumerate(lines):
        # 查找含"被保险人姓名"的表头行
        if re.search(r'被保险人姓名', line):
            header_idx = i
            # 确定"被保险人姓名"在表头中的列位置
            cols = re.split(r'\s+', line.strip())
            for ci, col in enumerate(cols):
                if '被保险人姓名' in col:
                    name_col = ci
                    break
            break

    if header_idx < 0:
        return

    # 从表头下一行开始，提取以数字序号开头的数据行中的姓名
    names = []
    for j in range(header_idx + 1, min(header_idx + 50, len(lines))):
        row = lines[j].strip()
        if not row:
            continue
        parts = re.split(r'\s+', row)
        # 数据行以数字序号开头
        if not parts or not re.match(r'^\d+$', parts[0]):
            break
        # 姓名在序号后面（第2列，即 index 1）
        if len(parts) > 1 and _is_valid_person(parts[1]):
            names.append(parts[1])

    if names:
        fields["被保险人"] = "、".join(names)


def _extract_insured_qianhai(text: str, text_merged: str, fields: dict):
    """前海联合保单被保险人提取"""
    # 格式："被 保 险 人 倪文珊"
    m = re.search(r"被\s*保\s*险\s*人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 合并文本
    m = re.search(r"被保险人([\u4e00-\u9fff]{2,10})(?:被保险人|证件|地址)", text_merged)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


def _extract_insured_guoshou(text: str, text_merged: str, fields: dict):
    """国寿被保险人提取"""
    # 国寿驾乘险格式："名称/姓名 梁书敏 441322..."
    m = re.search(r"名称/姓名\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s+\d|\s*$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 国寿商业险格式："姓名/名称 刘玖英 证件号码 ..." 在"被保险人"上方一行
    m = re.search(r"姓名/名称\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s+证件|\s+\d|\s*$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 国寿交强险格式："被保险人 刘玖英"（同一行，后面跟身份证号等）
    m = re.search(r"被保险人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


def _extract_insured_zhonghua(text: str, text_merged: str, fields: dict):
    """中华联合被保险人提取"""
    # 中华联合格式：可能有"1"插入 "被1保1险1人 深圳市科瑞达科技有限公司"
    cleaned = re.sub(r'([\u4e00-\u9fff])1([\u4e00-\u9fff])', r'\1\2',
                     re.sub(r'([\u4e00-\u9fff])1([\u4e00-\u9fff])', r'\1\2', text))

    m = re.search(r"被保险人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", cleaned)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 标准格式
    m = re.search(r"被保险人[：:]\s*(\S+)", cleaned)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


def _extract_insured_common(text: str, text_merged: str, fields: dict):
    """通用被保险人提取"""
    # OCR带空格格式："被 保 险 人 深圳市中立咨询有限公司"（原始text匹配，优先于冒号格式）
    m = re.search(r"被\s+保\s+险\s+人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w（()）)]{1,29})", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 标准冒号格式
    for p in [
        r"被保险人名称[：:]\s*(\S+(?:[（(][^）)]+[）)])?)",
        r"被保险人[：:]\s*(\S+(?:[（(][^）)]+[）)])?)",
        r"被保姓名[：:]\s*(\S+)",
        r"(?<!被)被保险人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,29}(?:[（(][^）)]+[）)])?)(?:\s|$)",
    ]:
        m = re.search(p, text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # 阳光/众诚等格式："被保险人"单独一行或"被保险人 证件号码..."开头
    # 向附近行查找"名称 XXX 手机号"格式（优先于 merged text，避免匹配到条款文本）
    lines = text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '被保险人' or re.match(r'^被保险人\s*(?:证件|信息|承保)', stripped):
            # 先查附近行的"名称/姓名 XXX"或"客户名称：XXX"格式（华农/亚太等）
            for j in range(max(0, i - 3), min(len(lines), i + 3)):
                if j == i:
                    continue
                m = re.match(r'(?:客户)?(?:姓名|名称)[：:\s]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+(?:[（(][^）)]+[）)])?)', lines[j].strip())
                if m and _is_valid_person(m.group(1)):
                    fields["被保险人"] = m.group(1)
                    return
            # 紫金等表格格式：被保人名字在"被保险人"标签的上一行
            # 如："刘础行\n被保险人  承保公司"
            if i > 0:
                prev = lines[i - 1].strip()
                # 上一行去掉可能的空格后作为候选人名
                prev_cleaned = _clean_person_name(re.sub(r'\s+', '', prev))
                if prev_cleaned and _is_valid_person(prev_cleaned):
                    fields["被保险人"] = prev_cleaned
                    return
            # 众安等格式：区域标题后下一行 "被保险人 XXX 证件类型 其他"
            for j in range(i + 1, min(len(lines), i + 3)):
                m_next = re.match(r'被保险人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)', lines[j].strip())
                if m_next:
                    val = _clean_person_name(m_next.group(1))
                    if _is_valid_person(val):
                        fields["被保险人"] = val
                        return
                    # 驾乘险被保险人为"车辆随车人员"等，保留原始值
                    if re.search(r'车辆随车人员|车上人员|随车人员', m_next.group(1) + lines[j]):
                        # 提取车牌+描述作为被保险人（如"粤AB00R2车辆随车人员"）
                        fields["被保险人"] = m_next.group(1)
                        return
                    break
            break

    # 合并文本匹配无分隔符格式（允许终止词前有换行/空白）
    for p in [
        r"被保险人([\u4e00-\u9fff]{2,20}?)\s*(?:被保险人|身份证|证件|地址|联系)",
        r"被保姓名[：:]\s*(\S+)",
    ]:
        m = re.search(p, text_merged)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["被保险人"] = val
                return

    # 从特别约定中找
    m = re.search(r"被保险人为([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)", text)
    if m and _is_valid_person(m.group(1)):
        fields["被保险人"] = m.group(1)
        return

    # 表格格式：表头含"被保险人姓名"，数据行以序号开头
    for i, line in enumerate(lines):
        if re.search(r'被保险人姓名\s+(?:证件类型|方案号|方案|保额)', line):
            for j in range(i + 1, min(i + 6, len(lines))):
                parts = re.split(r'\s+', lines[j].strip())
                # 序号开头的数据行，姓名在第2列
                if len(parts) > 1 and re.match(r'^\d+$', parts[0]) and _is_valid_person(parts[1]):
                    fields["被保险人"] = parts[1]
                    return
                # 兼容无序号的格式，姓名在第1列
                if parts and _is_valid_person(parts[0]):
                    fields["被保险人"] = parts[0]
                    return
            break

    # "被保 姓 名:" 格式（平安商业险）
    m = re.search(r'被保\s*(?:险人)?\s*姓?\s*名[：:]\s*(\S+)', text)
    if m:
        val = re.sub(r'证件.*', '', m.group(1)).strip()
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 安盛天平格式："姓名：何洪坤；身份证号"
    # 排除"联系人姓名"等非被保险人标签
    m = re.search(r"(?<!联系人)(?<!联系)(?<!经办人)姓名[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[；;]|身份证|\s)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val
            return

    # 缴款通知格式："投保人" 列中的公司/人名（在"该笔保费用于"之后）
    m = re.search(r"该笔保费.*?投保人[\s\S]*?([\u4e00-\u9fff][\u4e00-\u9fff\w]{2,20}(?:有限公司|有限责任公司))", text)
    if m:
        fields["被保险人"] = m.group(1)
        return
    # 发票格式："购买方 名称：钱昊"
    m = re.search(r"购\s*买?\s*方?\s*名称[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["被保险人"] = val


# ============================================================
# 投保人提取
# ============================================================

def _extract_proposer(text: str, text_merged: str, fields: dict, company_short: str):
    """提取投保人"""

    # 永诚特殊处理：清理OCR干扰
    if company_short == "永诚":
        # 永诚的投保人也会被OCR干扰
        pass  # 使用通用逻辑，但_clean_person_name会处理"有敬异的"

    # 京东安联英文格式："投保人信息 Policy Holder: 龙荣熙" 或 "Policy Holder 龙荣熙"
    if company_short == "京东安联":
        m = re.search(r"(?:投保人信息|投保人)\s*(?:Policy\s*Holder)?[：:\s]+(\S+)", text)
        if not m:
            m = re.search(r"Policy\s*Holder[：:\s]+(\S+)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val
                return
        # 表格格式："投保人姓名 XXX 性别 男"
        m = re.search(r"投保人姓名\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)\s+(?:性别|证件)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val
                return

    # "单位/团体名称：" 后面的值（太平驾乘险等，可能跨行）
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if re.search(r'单位[/／]团体名称[：:]', line):
            # 同行取值
            m = re.search(r'单位[/／]团体名称[：:]\s*(\S+)', line)
            if m and _is_valid_person(m.group(1)):
                fields["投保人"] = m.group(1)
                return
            # 下一行取值
            if i + 1 < len(lines):
                val = lines[i + 1].strip()
                if val and _is_valid_person(val):
                    fields["投保人"] = val
                    return
            break

    # 华泰/国任/亚太等表格格式："姓名\n付琴\n联系电话\n...\n投保人\n证件类型"
    # 或亚太格式："姓名 王忠武 联系电话...\n投保人 证件类型 身份证..."
    # "投保人"是区域标题，投保人姓名在前面"姓名"字段的下一行或同一行
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped == '投保人' or re.match(r'^投保人\s+证件', stripped)) and i >= 1:
            # 向前找"姓名"行（不含/名称，区别于浙商格式）
            for j in range(max(0, i - 6), i):
                if lines[j].strip() == '姓名' and j + 1 < i:
                    val = lines[j + 1].strip()
                    if val and _is_valid_person(val):
                        fields["投保人"] = val
                        return
                # 国任等格式："姓名 倪于华 联系方式 15919735986"（姓名和值在同一行）
                m_name = re.match(r'姓名\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)', lines[j].strip())
                if m_name:
                    val = _clean_person_name(m_name.group(1))
                    if _is_valid_person(val):
                        fields["投保人"] = val
                        return
            # 众诚等格式：向前找"名称 XXX 手机号"行
            for j in range(max(0, i - 4), i):
                m = re.match(r'名称\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+(?:[（(][^）)]+[）)])?)', lines[j].strip())
                if m and _is_valid_person(m.group(1)):
                    fields["投保人"] = m.group(1)
                    return
            # 众安等格式：区域标题后下一行 "投保人 黄宝兰 证件类型 身份证"
            for j in range(i + 1, min(len(lines), i + 3)):
                m_next = re.match(r'投保人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)', lines[j].strip())
                if m_next:
                    val = _clean_person_name(m_next.group(1))
                    if _is_valid_person(val):
                        fields["投保人"] = val
                        return
            break

    # 永诚/中华联合等格式："投保人信息\n姓名：李飞" 或 "投保人信息\n姓名/名称：深圳XXX公司"
    # 华农格式："投保人信息\n名称：邹柳青" 或 "投保人信息\n客户名称：深圳市XXX公司"
    m = re.search(r"投保人信息[\s\n]*(?:客户)?(?:姓名(?:[/／]名称)?|名称)[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["投保人"] = val
            return

    # 人保非车险等格式："投保人信息\n中设(深圳)设备检验检测\n姓名/名称:\n...\n技术有限公司"
    # OCR 可能将公司名拆成多行，需在"投保人信息"和"被保险人信息"之间拼接
    for i, line in enumerate(lines):
        if line.strip() == '投保人信息' and i + 1 < len(lines):
            next_val = lines[i + 1].strip()
            # 排除下一行是"姓名"/"名称"等标签行的情况（由上面的正则处理）
            if next_val and not re.match(r'^(?:客户)?(?:姓名|名称|证件|联系)', next_val):
                # 在后续行中查找公司名后缀（如"技术有限公司"），拼接完整公司名
                for j in range(i + 2, min(len(lines), i + 12)):
                    if re.match(r'^被保险人', lines[j].strip()):
                        break
                    suffix = lines[j].strip()
                    if suffix and re.search(r'(?:有限公司|有限责任公司|集团公司|股份公司)$', suffix) and len(suffix) <= 15:
                        next_val = next_val + suffix
                        break
                next_val = _clean_person_name(next_val)
                if _is_valid_person(next_val):
                    fields["投保人"] = next_val
                    return
            break

    # 按行匹配"投保人为："（处理名字中有OCR空格的情况，如"沈 心诚"）
    for line in lines:
        m = re.search(r'投保人为[：:]\s*(.+)', line.strip())
        if m:
            # 取冒号后的内容，清理空格
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val
                return

    # 通用提取
    for p in [
        r"投保人\s*(?:姓名|名称)?[：:]\s*(\S+)",
        r"本保单\s*[的]?\s*投保人为[：:]?\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s\n]|$)",
        r"本保单\s*投保人为[：:]?\s*(\S+)",
        r"(?<![/／\d])投保人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)",
    ]:
        m = re.search(p, text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val
                return

    # 前海等格式：冒号后人名带OCR空格（如"投保人 ： 叶 文 燕"）
    # 按行匹配，取冒号后整段内容合并空格
    for line in lines:
        m = re.match(r'\s*投保人\s*[：:]\s*(.+)', line)
        if m:
            raw = m.group(1).strip()
            # 合并中文字符间的空格（如"叶 文 燕" → "叶文燕"）
            val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', raw)
            val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', val)
            val = _clean_person_name(val)
            if _is_valid_person(val):
                fields["投保人"] = val
                return

    # 兜底：用合并文本（消除OCR字间空格干扰）
    for p in [
        r"本保单[的]?投保人为[：:]?\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]*?(?:有��公司|有限责任公司|集团|分院|���究院|研究所|设计院|中心))",
        r"投保人为[：:]?\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)",
        r"投保人([\u4e00-\u9fff]{2,20}?)(?:，|,|。|证件|保单|$)",
    ]:
        m = re.search(p, text_merged)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val
                return

    # 平安格式：投保人名字在"投保人:"行之前单独一行
    # 例1："深圳市农丰达食品有限公司 保单验真码: xxx\n投保人:"（投保人行为空）
    # 例2："姚波\n投保人: 保单验真码: xxx"（投保人行后跟验真码）
    lines = text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^投保人[:：]', stripped) and i > 0:
            # 检查"投保人:"后面是否为空或仅为非名字内容（如验真码）
            after_colon = re.sub(r'^投保人[:：]\s*', '', stripped)
            if not after_colon or re.match(r'保单验真码|企业宝|验真码', after_colon):
                prev = lines[i - 1].strip()
                # 清除 "保单验真码" 等后缀
                prev = re.sub(r'\s*保单验真码.*', '', prev).strip()
                prev = re.sub(r'\s*企业宝.*', '', prev).strip()
                prev = _clean_person_name(prev)
                if _is_valid_person(prev):
                    fields["投保人"] = prev
                    return

    # 表格格式
    for i, line in enumerate(lines):
        if re.search(r'投保人姓名\s+证件类型', line):
            for j in range(i + 1, min(i + 4, len(lines))):
                parts = re.split(r'\s+', lines[j].strip())
                if parts:
                    cleaned = _clean_person_name(parts[0])
                    if _is_valid_person(cleaned):
                        fields["投保人"] = cleaned
                    return
            break

    # "投保人名称：XXX"格式
    m = re.search(r"投保人名称[：:]\s*(\S+)", text)
    if m:
        val = _clean_person_name(m.group(1))
        if _is_valid_person(val):
            fields["投保人"] = val
            return

    # 浙商等格式
    for i, line in enumerate(lines):
        if line.strip() == '投保人' and i >= 2:
            for j in range(max(0, i - 5), i):
                if '姓名/名称' in lines[j] or '姓名／名称' in lines[j]:
                    if j > 0:
                        val = lines[j - 1].strip()
                        for k in range(j + 1, i):
                            frag = lines[k].strip()
                            if frag and len(frag) <= 3 and not re.match(r'^(投保|证件|邮编)', frag):
                                val += frag
                        val = _clean_person_name(val)
                        if len(val) >= 2:
                            fields["投保人"] = val
                    break
            break

    # "姓名/名称：XXX" 格式（人保驾乘、安盛天平等）
    if "投保人" not in fields:
        m = re.search(r"姓名/名称[：:\s]*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val

    # "投保人姓名 XXX 性别" 格式（阳光驾乘无忧、E车无忧等）
    if "投保人" not in fields:
        m = re.search(r"投保人姓名\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)\s+(?:性别|证件)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val

    # 安盛天平/京东安联："姓名：XXX；" 格式（如果被保险人已有，投保人可能一样）
    if "投保人" not in fields and company_short in ("安盛天平", "京东安联"):
        m = re.search(r"姓名[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[；;]|身份证|\s)", text)
        if m:
            val = _clean_person_name(m.group(1))
            if _is_valid_person(val):
                fields["投保人"] = val


# ============================================================
# 车辆信息提取
# ============================================================

def _extract_vehicle_info(text: str, fields: dict, company_short: str):
    """提取车牌号、车架号、发动机号等"""

    # 车牌号
    PROVINCE_CHARS = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁"
    # 处理跨行车牌：省份简称后换行跟字母数字（如"鄂\nABC123"）
    plate_text = re.sub(rf'([{PROVINCE_CHARS}])\s*\n\s*([A-Z])', r'\1\2', text)
    plate_text = re.sub(r'([A-Z]-?)\s*\n\s*([A-Z0-9])', r'\1\2', plate_text)
    # 中华联合格式："号1牌1号1码" 需要合并
    plate_text_merged = re.sub(r'([\u4e00-\u9fff])1([\u4e00-\u9fff])', r'\1\2',
                                re.sub(r'([\u4e00-\u9fff])1([\u4e00-\u9fff])', r'\1\2', plate_text))

    for p in [
        rf"号\s*牌\s*号\s*码[：:\s]*([{PROVINCE_CHARS}]\S+)",
        rf"号牌号码[：:\s]*([{PROVINCE_CHARS}]\S+)",
        rf"车牌号码?[：:\s]*([{PROVINCE_CHARS}]\S+)",
        rf"车牌号?[：:\s]*([{PROVINCE_CHARS}]\S+)",
        rf"号牌号码\s+([{PROVINCE_CHARS}]\S+)",
        rf"([{PROVINCE_CHARS}][A-Z][-]?[A-Z0-9]{{4,6}})",
    ]:
        # 先尝试合并文本
        m = re.search(p, plate_text_merged)
        if not m:
            m = re.search(p, plate_text)
        if m:
            plate = m.group(1).replace("-", "").rstrip(";；,，")
            # 清理车牌后面可能粘连的车架号等信息
            plate = re.split(r'[；;，,\s]', plate)[0]
            if len(plate) >= 6:
                fields["车牌号"] = plate
                break

    # 平安个意险：车牌嵌入在"其他信息"字段中，如 "车牌号码:粤B-CK9833" 或 "车牌号：粤BCK9833"
    if "车牌号" not in fields:
        m = re.search(rf"车牌号[码]?[：:]\s*([{PROVINCE_CHARS}][A-Z][-]?[A-Z0-9]{{4,6}})", text)
        if m:
            fields["车牌号"] = m.group(1).replace("-", "")

    # 新车车牌特殊处理："粤B-*新"、"*-*" 等
    if "车牌号" not in fields:
        PROVINCE_CHARS = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁"
        # "粤B-*新" / "粤B*新" 格式
        for p in [
            rf"(?:号\s*牌\s*号\s*码|号牌号码|车牌号码?)[：:\s]*([{PROVINCE_CHARS}][A-Z][-\s]*\*?\s*新)",
            rf"(?:号\s*牌\s*号\s*码|号牌号码|车牌号码?)[：:\s]*([{PROVINCE_CHARS}][A-Z][-\s]*\*)",
        ]:
            m = re.search(p, plate_text)
            if not m:
                m = re.search(p, plate_text_merged)
            if m:
                fields["车牌号"] = m.group(1).replace(" ", "").replace("-", "")
                break
        # "*-*" 格式（完全无车牌信息的新车）
        if "车牌号" not in fields:
            if re.search(r"(?:号[1]?牌[1]?号[1]?码|车牌号码?)[：:\s]*\*[-\s]*\*", text):
                fields["车牌号"] = "新车"

    # 新车用车架号后几位作为车牌（如"车牌号:LSB08602"，非省份简称开头）
    if "车牌号" not in fields:
        m = re.search(r"(?:号牌号码|车牌号码?)[：:\s]*([A-Z0-9]{6,17})", text)
        if m:
            fields["车牌号"] = m.group(1)

    # 平安非车险保单：车牌号码后只有省份简称（如"车牌号码:鄂"），OCR将剩余部分拆到附近行
    if "车牌号" not in fields and company_short == "平安":
        lines = text.split('\n')
        for i, line in enumerate(lines):
            m_prov = re.search(rf"车牌号码?[：:]\s*([{PROVINCE_CHARS}])\s*$", line)
            if m_prov:
                prov = m_prov.group(1)
                for j in range(i + 1, min(len(lines), i + 5)):
                    m_rest = re.search(r'(?<!\w)([A-Z][-]?\*)', lines[j])
                    if m_rest:
                        rest = m_rest.group(1).replace("-", "")
                        fields["车牌号"] = prov + rest
                        break
                break

    # 车架号/VIN
    for p in [
        r"VIN码?/?车架号[：:\s]*([A-Z0-9]{17})",
        r"车架号[：:\s]*([A-Z0-9]{17})",
        r"VIN[：:\s]*([A-Z0-9]{17})",
        r"车辆识别代[号码][：:\s]*([A-Z0-9]{17})",
        r"识别代码\s*(?:\(车架号\)|（车架号）)\s*([A-Z0-9]{17})",
        r"识别代码[：:\s]*([A-Z0-9]{17})",
    ]:
        m = re.search(p, text)
        if m:
            fields["车架号VIN"] = m.group(1)
            break

    # 发动机号
    for p in [
        r"发\s*动\s*机\s*号[码：:\s]*([A-Za-z0-9]+)",
        r"发动机号码[：:\s]*(\S+)",
    ]:
        m = re.search(p, text)
        if m:
            fields["发动机号"] = m.group(1)
            break

    # 厂牌型号
    for p in [
        r"厂\s*牌\s*型\s*号[：:\s]*(\S+)",
        r"厂牌型号[：:\s]*(\S+)",
        r"品牌型号[：:\s]*(\S+)",
    ]:
        m = re.search(p, text)
        if m:
            val = m.group(1)
            if len(val) >= 2 and val not in ("核", "核定", "营业性质", "营业"):
                fields["厂牌型号"] = val
                break

    # 核定载客
    m = re.search(r"核\s*定\s*载\s*客[：:\s]*(\d+)\s*人", text)
    if m:
        fields["核定载客"] = m.group(1) + "人"

    # 核定载质量
    m = re.search(r"核定载质量[：:\s]*([\d.]+)\s*千克", text)
    if m:
        fields["核定载质量"] = m.group(1) + "千克"

    # 使用性质
    for p in [
        r"使用性质[：:\s]*(\S+?)(?:\s|$|核定|年平均|机动车种类)",
        r"使用性质\s+(\S+)",
    ]:
        m = re.search(p, text)
        if m:
            val = m.group(1)
            # 中华联合的"使1用1性1质"清理
            val = re.sub(r'1', '', val)
            if len(val) <= 10:
                fields["使用性质"] = val
                break

    # 机动车种类
    m = re.search(r"机动车种类[：:\s]*(\S+?)(?:\s|$)", text)
    if m:
        val = m.group(1)
        val = re.sub(r'1', '', val)
        fields["机动车种类"] = val

    # 初次登记日期
    m = re.search(r"初[次登]*登[记日]*[期：:\s]*([\d\-/年月日]+)", text)
    if m:
        fields["初次登记日期"] = m.group(1)

    # 证件号码
    m = re.search(r"证件号码[：:\s]*([A-Z0-9]+)", text)
    if m:
        fields["证件号码"] = m.group(1)
    else:
        m = re.search(r"证件号[：:\s]*([A-Z0-9]+)", text)
        if m:
            fields["证件号码"] = m.group(1)

    # 车主
    for p in [
        r"(?:行驶证)?车主(?:\s*名称)?[：:\s]+(\S+?)(?:\s|$|投保人)",
        r"车主\s+(\S+?)(?:\s|$)",
    ]:
        m = re.search(p, text)
        if m:
            val = m.group(1)
            if _is_valid_person(val):
                fields["车主"] = val
                break


# ============================================================
# 保费提取（按保司分策略）
# ============================================================

# 大写中文金额 → 数字（如"壹佰捌拾玖元整" → "189.00"）
_CN_DIGIT = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4,
             "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_UNIT = {"拾": 10, "佰": 100, "仟": 1000, "万": 10000, "亿": 100000000}


def _parse_chinese_amount(text: str) -> Optional[str]:
    """将大写中文金额转为数字字符串，如 '壹佰捌拾玖元整' → '189.00'"""
    m = re.search(r'人民币([\u4e00-\u9fff]+元(?:整|[\u4e00-\u9fff]*[分角整])?)', text)
    if not m:
        return None
    s = m.group(1)
    # 拆分元/角/分
    yuan_part, jiao_val, fen_val = "", 0, 0
    if "元" in s:
        yuan_part, rest = s.split("元", 1)
    else:
        return None
    # 解析元部分
    total = 0
    cur = 0
    wan_total = 0  # 万以上部分
    for ch in yuan_part:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10000:
                total = (total + cur) * unit
                wan_total = total
                total = 0
                cur = 0
            elif unit == 100000000:
                total = (total + cur) * unit
                wan_total = total
                total = 0
                cur = 0
            else:
                total += cur * unit
                cur = 0
    total += cur + wan_total
    # 解析角/分
    rest = rest.replace("整", "")
    for ch_i, ch in enumerate(rest):
        if ch in _CN_DIGIT:
            d = _CN_DIGIT[ch]
            # 下一个字符决定是角还是分
            next_ch = rest[ch_i + 1] if ch_i + 1 < len(rest) else ""
            if next_ch == "角":
                jiao_val = d
            elif next_ch == "分":
                fen_val = d
    amount = total + jiao_val * 0.1 + fen_val * 0.01
    if amount <= 0:
        return None
    return f"{amount:.2f}"


def _extract_premium(text: str, fields: dict, company_short: str):
    """提取保费合计"""

    # 各保司通用模式列表
    patterns = [
        r"保险?费合计.*?[￥¥][：:\s]*([\d,]+\.\d{1,2})",
        r"保险?费合计.*?([\d,]+\.\d{1,2})\s*元",
        r"保险费[：:]\s*人民币\s*\S+[（(]RMB[:：]?\s*([\d,]+\.\d{2})(?:元)?[)）]",
        r"RMB[:：]?\s*([\d,]+\.\d{2})(?:元)?[)）]",
        r"[（(]小写[）)]\s*RMB\s*([\d,]+\.\d{2})",
        r"RMB([\d,]+\.\d{2})元",
        r"RMB([\d,]+\.\d{2})",
        r"总保险费.*?[￥¥]([\d,]+\.\d{2})",
        r"实收保费[：:\s]*([\d,]+\.\d{2})",
        r"总?保险?费[：:\s]*([\d,]+\.\d{2})",
        # 众安等格式：无小数点 "（小写）RMB 499元"
        r"总保险费.*?RMB\s*([\d,]+)元",
    ]

    # 太平洋格式："保险费及车船税合计金额 ￥：19544.56 元"（优先取含税合计）
    if company_short == "太平洋":
        patterns.insert(0, r"保险费及车船税合计金额\s*[￥¥][：:\s]*([\d,]+\.\d{2})")

    # 人保等合并投保单："保险费+车船税合计（人民币大写）：... （￥ 5017.32 元）"
    m = re.search(r"保险费[+＋]车船税合计.*?[（(\s][￥¥]\s*([\d,]+\.\d{2})\s*(?:元)?[)）]", text)
    if m:
        fields["保费合计"] = m.group(1).replace(",", "")
        return

    # 人保非车险格式（跨行）："保险费合计:\n人民币(大写)贰佰捌拾捌元整¥288.00元"
    if company_short == "人民财产":
        patterns.insert(0, r"保险费合计[\s\S]{0,80}?[￥¥]([\d,]+\.\d{2})\s*元")
        # 兜底：不依赖¥符号，直接匹配"保险费合计"后跨行的第一个金额
        patterns.insert(1, r"保险费合计[\s\S]{0,80}?([\d,]+\.\d{2})\s*元")

    # 亚太等格式："保险费 大写：人民币陆拾捌元整 小写：CNY 68.00"
    # 需排除"总保险金额"行的干扰，优先精确匹配"保险费"行
    if company_short == "亚太":
        patterns.insert(0, r"保险费\s+大写[：:].*?小写[：:\s]*CNY\s*([\d,]+\.\d{2})")

    # 华农格式：保费可能无小数点，优先匹配避免误取不含税保费
    # "总保费（人民币：元）:240" 或 "保险费合计（元）：1200"
    if company_short == "华农":
        patterns.insert(0, r"保险?费合计[^￥¥\d\n]*[：:]\s*([\d,]+\.?\d*)(?:\s|$)")
        patterns.insert(1, r"总保费[^:\n]*[：:]\s*([\d,]+\.?\d*)(?:\s|$)")

    # 安诚格式（跨行）："保险费合计(人民币大写):壹仟...分\n(¥:1617.85元)"
    if company_short == "安诚":
        patterns.insert(0, r"保险费合计[\s\S]{0,80}?[（(][￥¥][：:\s]*([\d,]+\.?\d{0,2})\s*(?:元)?[)）]")

    # 华泰/京东安联/安盛天平特殊格式："（¥： 1,850.00 元）"
    if company_short in ("华泰", "京东安联", "安盛天平", "永诚", "前海联合"):
        patterns.insert(0, r"保险费合计.*?[（(][￥¥][：:\s]*([\d,]+\.?\d{0,2})\s*(?:元)?[)）]")
        patterns.insert(1, r"保险费合计.*?¥[：:\s]*([\d,]+\.?\d{0,2})(?:\s*元)?")
        # "含税保险费合计" 格式
        patterns.insert(2, r"含税保险费合计.*?[（(][￥¥][：:\s]*([\d,]+\.?\d{0,2})\s*(?:元)?[)）]")

    # 安盛天平驾乘格式："总保险费(含税价): RMB 保费：￥180.32 税费：￥9.68 价税合计：￥190.00"
    # 优先取"价税合计"金额
    if company_short == "安盛天平":
        patterns.insert(0, r"价税合计[：:\s]*[￥¥]([\d,]+\.\d{2})")
        patterns.insert(1, r"总保险费.*?价税合计[：:\s]*[￥¥]([\d,]+\.\d{2})")

    # 国寿驾乘险："保险费合计 人民币（大写） XXX ￥CNY299.00元"
    if company_short == "国寿财产":
        patterns.insert(0, r"保险费合计.*?[￥¥]?CNY([\d,]+\.\d{2})(?:元)?")

    # 京东安联驾乘："总保费 Total Premium: RMB 198.00"
    if company_short == "京东安联":
        patterns.insert(0, r"[总T]otal\s*[保P]remium[：:\s]*RMB\s*([\d,]+\.\d{2})")
        patterns.insert(1, r"总保费.*?RMB\s*([\d,]+\.\d{2})")

    # 前海联合驾乘："保险费：(大写) 玖拾元整 RMB 90.0元"
    if company_short == "前海联合":
        patterns.insert(0, r"保险费[：:].*?RMB\s*([\d,]+\.?\d*)(?:元)?")

    # 大家交强："本保单含税保费1080.0元"
    if company_short == "大家财险":
        patterns.insert(0, r"含税保费([\d,]+\.?\d*)(?:元)?")

    # 申能格式："保险费总计： （大写） 叁佰元 小写 300.00元"（优先取总计，而非不含税保费）
    if company_short == "申能":
        patterns.insert(0, r"保险费总计.*?小写\s*([\d,]+\.?\d{0,2})\s*元")
        patterns.insert(1, r"保险费总计.*?([\d,]+\.\d{2})\s*元")

    # 太平驾乘格式（跨行）："总保费(含税)：\n小写： CNY 599.00（\n不含税保费：587.23元，税额：11.77元）"
    # 优先取"总保费"后"小写"行的 CNY 金额
    if company_short == "太平":
        patterns.insert(0, r"总保费[\s\S]{0,30}?小写[：:\s]*CNY\s*([\d,]+\.\d{2})")
        # "人民币大写：...，小写：RMB855.00元（不含税保费...）其中 保险费合计"
        patterns.insert(1, r"小写[：:\s]*RMB\s*([\d,]+\.\d{2})(?:元)?[\s\S]{0,80}?保险费合计")
        # "保 险 费 合 计 人民币大写：...，小写：RMB4636.16元（其中 不含税保费...）"
        # OCR 可能将"保险费合计"拆成带空格的形式
        patterns.insert(2, r"保\s*险\s*费\s*合\s*计.*?小写[：:\s]*RMB\s*([\d,]+\.\d{2})")

    # 阳光驾乘无忧："总保险费 人民币（大写）:壹佰捌拾捌元    ￥188.00"
    if company_short == "阳光":
        patterns.insert(0, r"总保险费\s*人民币.*?[￥¥]\s*([\d,]+\.\d{2})")
        patterns.insert(1, r"保险费合计\s*人民币.*?[￥¥]\s*([\d,]+\.\d{2})")

    # 永诚/太平驾意险："(小写)：200.00" 或 "小写： CNY 430.00" 或 "（小写）￥ 280.00"
    patterns.append(r"[（(]?小写[）)]?[：:\s]*(?:CNY\s*|[￥¥]\s*)?([\d,]+\.\d{2})")
    # 缴款通知书："签单保费合计为17029.02元"
    patterns.append(r"签单保费合计[为：:\s]*([\d,]+\.\d{2})")
    # 发票："价税合计...（小写）¥15691.44"
    patterns.append(r"价税合计.*?[（(]小写[）)][￥¥]([\d,]+\.\d{2})")

    for p in patterns:
        m = re.search(p, text)
        if m:
            val = m.group(1).replace(",", "")
            if val and float(val) > 0:
                fields["保费合计"] = val
                break

    # 鼎和等格式：跨行"含税总保险费 人民币：贰佰陆拾\nCNY: 268.00 元"
    if "保费合计" not in fields:
        m = re.search(r"总保险费[\s\S]{0,50}?CNY[：:\s]*([\d,]+\.\d{2})", text)
        if m:
            val = m.group(1).replace(",", "")
            if float(val) > 0:
                fields["保费合计"] = val

    # 紫金等格式1：同行"小 写 ： C NY 30 .00"（OCR空格拆散）
    # 安诚等格式：金额在下一行 "(¥:1617.85元)"
    if "保费合计" not in fields:
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if re.search(r'保险?费合计', line):
                cleaned = re.sub(r'\s+', '', line)
                m = re.search(r'(?:小写|CNY|[￥¥])[：:]*(\d{1,10}\.\d{2})', cleaned)
                if m:
                    val = m.group(1)
                    if float(val) > 0:
                        fields["保费合计"] = val
                        break
                # 同行没找到，检查下一行（安诚格式："(¥:1617.85元)"在下一行）
                if i + 1 < len(lines):
                    next_cleaned = re.sub(r'\s+', '', lines[i + 1])
                    m = re.search(r'[（(][￥¥][：:]\s*([\d,]+\.?\d{0,2})\s*(?:元)?[)）]', next_cleaned)
                    if m:
                        val = m.group(1).replace(",", "")
                        if float(val) > 0:
                            fields["保费合计"] = val
                break

    # 紫金等格式2：金额在"保险费合计"标签的上一行，与其他数字粘连
    # "陆佰陆拾伍元整 665.000.00.00\n保险费合计（人民币大写）： （¥： 元）"
    if "保费合计" not in fields:
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if re.search(r'保险?费合计', line) and i > 0:
                prev = lines[i - 1].strip()
                m = re.search(r'(\d{1,10}\.\d{2})', prev)
                if m:
                    val = m.group(1).replace(",", "")
                    if float(val) > 0:
                        fields["保费合计"] = val
                break

    # 兜底：只有大写金额没有小写数字（安诚等格式）
    # "保费合计\n(大写)人民币壹佰捌拾玖元整"
    if "保费合计" not in fields:
        m = re.search(r"保险?费合计[\s\S]{0,60}?人民币([\u4e00-\u9fff]+元(?:整|[\u4e00-\u9fff]*[分角整])?)", text)
        if m:
            val = _parse_chinese_amount("人民币" + m.group(1))
            if val:
                fields["保费合计"] = val

    # 不含税保费：兼容"不含税保费：1132.08"和"不含税保费（元）：1132.08"等
    m = re.search(r"不含税保[险]?费[^：:\d]*[：:\s]*([\d,]+\.\d{2})", text)
    if m:
        fields["不含税保费"] = m.group(1)

    # 增值税额：兼容"税额：67.92"、"增值税10.88元"、"增值税（元）：67.92"等
    m = re.search(r"(?:增值)?税额[^：:\d]*[：:\s]*([\d,]+\.\d{2})", text)
    if not m:
        m = re.search(r"增值税[^：:\d]*[：:\s]*([\d,]+\.\d{2})", text)
    if not m:
        m = re.search(r"增值税([\d,]+\.\d{2})\s*元", text)
    if m:
        fields["增值税额"] = m.group(1)


# ============================================================
# 车船税提取
# ============================================================

def _extract_tax(text: str, fields: dict):
    """提取车船税，优先取合计金额"""
    # 合并车船税区域的换行（处理"￥\n：\n2469.6"等跨行格式）
    tax_text = text
    # 处理OCR将"车船税"拆到多行的情况（如"车\n船 合计...元）\n税"）
    # 提取"车"开头到"税"结束的区域，合并为单行后追加到 tax_text 供正则匹配
    m_tax_block = re.search(r"车\n.*?船\s*合计.*?[（(]\s*[￥¥][：:\s]*([\d,.]+)\s*(?:元)?[)）].*?\n\s*税", tax_text, re.DOTALL)
    if m_tax_block:
        val = m_tax_block.group(1).strip().replace(",", "")
        try:
            amt = float(val)
            if amt >= 0:
                fields["车船税"] = val
                return
        except (ValueError, AttributeError):
            pass
    # 先排除"保险费+车船税合计"/"保险费及车船税合计"行，避免误提取
    tax_text_clean = re.sub(r'保险费[+＋及]车船税合计.*', '', tax_text)
    for p in [
        # 车船税...合计...（￥：XXX 元）
        r"车\s*船\s*税[\s\S]*?合计.*?[（(]\s*[￥¥][：:\s]*([\d,.]+)\s*(?:元)?[)）]",
        r"车\s*船\s*税[\s\S]*?合计.*?([\d,.]+)\s*元",
        # 平安格式：合计(人民币大写)：...( ￥ XXX 元)（排除"保险费合计"）
        r"(?<!保险费)合计\s*[（(]人民币大写[)）][：:\s]*.*?[（(\s]+[￥¥]\s*([\d,.]+)\s*元",
        # 车船税...合计...￥（可能跨行）：金额
        r"车\s*船\s*税[\s\S]*?合计[^￥¥]*?[￥¥]\s*[：:\s]*\n?\s*([\d,.]+)",
        # 兜底：当年应缴
        r"当年应缴\s*[（(\s]*[￥¥][：:\s]*([\d,.]+)\s*元",
    ]:
        m = re.search(p, tax_text_clean)
        if m:
            val = m.group(1).strip().replace(",", "")
            try:
                amt = float(val)
                if amt >= 0:
                    fields["车船税"] = val
                    return
            except ValueError:
                continue


# ============================================================
# 保险期间提取（按保司分策略）
# ============================================================

def _extract_period(text: str, text_merged: str, fields: dict, company_short: str):
    """提取保险期间"""
    # 紫金等格式：同一行有两个"YYYY年M月D日HH时M分"日期，标签在其他行
    # 直接在全文中匹配，不依赖标签位置
    if company_short == "紫金":
        m = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}时\d{1,2}分\s+(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}时\d{1,2}分', text)
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return

    period_text = text_merged
    # 清理下划线（人保投保单OCR格式："_2_0_2_6_____年_05____月_0_7___日"）
    period_text = period_text.replace('_', ' ')
    # 清理数字之间的空格："20 2 6" → "2026"（需多次执行）
    for _ in range(5):
        period_text = re.sub(r'(\d)\s+(\d)', r'\1\2', period_text)
    # 清理数字与年/月/日之间的空格："2026 年 4 月 20 日" → "2026年4月20日"
    period_text = re.sub(r'(\d)\s+(年|月|日)', r'\1\2', period_text)
    period_text = re.sub(r'(年|月)\s+(\d)', r'\1\2', period_text)
    # 清理时/分之间的空格
    period_text = re.sub(r'(\d)\s+(时|分|秒)', r'\1\2', period_text)
    period_text = re.sub(r'(时|分)\s+(\d)', r'\1\2', period_text)
    # 清理冒号周围的空格（人保投保单OCR："0 : 00" → "0:00"）
    period_text = re.sub(r'(\d)\s*:\s*(\d)', r'\1:\2', period_text)

    # 通用模式（使用re.DOTALL让.匹配换行，支持"保险期间"与日期跨行的情况）
    for p in [
        # "保险期间...起始日期...至...结束日期"（兼容"由...至..."格式，日和至之间可能无空格）
        r"保险期间.{0,30}?(\d{4}年\d{1,2}月\d{1,2}日?)[\s\d时分秒:]*?(?:起|时起)?[，,\s]*[至到]\s*(\d{4}年\d{1,2}月\d{1,2}日?)",
        r"保险期间[：:\s]*(\d{4}年?\d{1,2}月?\d{1,2}日?)\s*(?:\d{2}:\d{2}(?::\d{2})?)?\s*[至到]\s*(\d{4}年?\d{1,2}月?\d{1,2}日?)",
    ]:
        m = re.search(p, period_text, re.DOTALL)
        if m:
            start = re.sub(r'\s+', '', m.group(1))
            end = re.sub(r'\s+', '', m.group(2))
            fields["保险起期"] = start
            fields["保险止期"] = end
            fields["保险期间"] = f"{start} 至 {end}"
            return

    # "自 (From) ... 至 (To) ..." 格式
    m = re.search(r"自\s*[（(]?From[)）]?\s*(\d{4}年\d{1,2}月\d{1,2}日?).*?[至到]\s*[（(]?To[)）]?\s*(\d{4}年\d{1,2}月\d{1,2}日?)", period_text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 安盛天平/京东安联格式："保险期间 2026 年 04 月 25 日 00 时 00 分 00 秒起至 2027 年 04 月 24 日 23 时 59 分 59 秒止"
    # 用原始text匹配（空格在每个数字之间）
    m = re.search(r"保险期间\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日.*?起\s*至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        start = f"{m.group(1)}年{m.group(2)}月{m.group(3)}日"
        end = f"{m.group(4)}年{m.group(5)}月{m.group(6)}日"
        fields["保险起期"] = start
        fields["保险止期"] = end
        fields["保险期间"] = f"{start} 至 {end}"
        return

    # "保险期间自 2026年4月19日0时0分 起至 2027年4月19日0时0分 止" 格式
    # 也处理 "零时起" "二十四时止" 等中文时间
    m = re.search(r"保险期间\s*自?\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*(?:\d+[时:]\d+[分:]?\d*[秒]?|零时|[零〇○]时)\s*起\s*[至到]\s*(\d{4}年\d{1,2}月\d{1,2}日)", period_text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # "保险期间：自2026年04月14日18:03:12起至2027年04月14日18:03:12止" 格式（中华联合）
    m = re.search(r"保险期间[：:\s]*自?(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}:\d{2}(?::\d{2})?起至(\d{4}年\d{1,2}月\d{1,2}日)", period_text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 跨行格式
    lines = text.split('\n')
    for i, line in enumerate(lines):
        m_start = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)\d{2}(?:时|:)\d{2}(?:分|:)\d{2}(?:秒)?\s*起\s*[至到]', line)
        if m_start:
            for j in range(i, min(i + 3, len(lines))):
                m_end = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)\d{2}(?:时|:)\d{2}(?:分|:)\d{2}(?:秒)?\s*止', lines[j])
                if m_end:
                    fields["保险起期"] = m_start.group(1)
                    fields["保险止期"] = m_end.group(1)
                    fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
                    return
            break

    # 紫金等格式：日期与"保险期间自 起至 止"标签分离
    # OCR输出示例：
    # "2026年5月6日18时0分 2027年5月6日18时0分\n保险期间自 起至 止"
    # 或中间隔多行："...日期\n—\n终保日期\n—\n保险期间自 起至 止"
    period_lines = period_text.split('\n')
    two_date_pattern = r'(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}时\d{1,2}分\s+(\d{4}年\d{1,2}月\d{1,2}日)'
    for i, line in enumerate(period_lines):
        # 匹配"保险期间自起至止"或"保险期间自 起至 止"（合并/未合并空格均可）
        if re.search(r'保险期间\s*自\s*起\s*[至到]\s*止', line):
            for j in range(max(0, i - 8), i):
                m = re.search(two_date_pattern, period_lines[j].strip())
                if m:
                    fields["保险起期"] = m.group(1)
                    fields["保险止期"] = m.group(2)
                    fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
                    return
            break

    # 兜底：全文搜索同一行内两个"YYYY年M月D日HH时M分"格式的日期（紫金等无标签关联的情况）
    if "保险期间" not in fields:
        for line in period_lines:
            m = re.search(two_date_pattern, line.strip())
            if m:
                # 确认附近有保险期间相关关键词，避免误匹配
                line_idx = period_lines.index(line)
                context = '\n'.join(period_lines[max(0, line_idx - 2):min(len(period_lines), line_idx + 5)])
                if re.search(r'保险期间|起保日期|终保日期', context):
                    fields["保险起期"] = m.group(1)
                    fields["保险止期"] = m.group(2)
                    fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
                    return

    # 兜底：无"保险期间"标签，直接匹配"YYYY年MM月DD日...时起至...YYYY年MM月DD日...时止"
    # 人保投保单OCR格式清理后：" 2026年05月07日 0:00时起至 2027年05月06日 24:00时止"
    if "保险期间" not in fields:
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{1,2}:\d{2}\s*时?\s*起\s*至\s*(\d{4}年\d{1,2}月\d{1,2}日)", period_text)
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return

    # "保险期限： 2026-05-17 00:00:00 至 2027-05-16 23:59:59" 格式（太平驾意险等）
    # 也支持 "保险期间： 自 2026-04-22 00:00:00 起，至 2027-04-21 23:59:59 止。"
    # 也支持 "-" 分隔（安盛天平标志）："2026-04-24 00:00:00 - 2027-04-23 23:59:59"
    m = re.search(r"保险期[限间][：:\s]*(?:自\s*)?(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:\d{2}:\d{2}(?::\d{2})?)?\s*(?:起[，,]?\s*)?[至到\-]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
    if not m:
        # 京东安联驾乘格式："保单生效日 Effective Date: 2026-04-19" + "保单到期日 Expiry Date: 2027-04-19"
        # 安盛天平驾乘格式："保险合同生效日: 2026-04-25" + "保险合同满期日: 2027-04-24"
        m_start = re.search(r"(?:保[单险](?:合同)?生效日|Effective\s*Date)[：:\s]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        m_end = re.search(r"(?:保[单险](?:合同)?(?:到期|满期)日|Expiry\s*Date)[：:\s]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if m_start and m_end:
            fields["保险起期"] = m_start.group(1)
            fields["保险止期"] = m_end.group(1)
            fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
            return
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 国寿驾乘险格式："自2026年4月29日零时起，至2027年4月28日二十四时止。"
    m = re.search(r"自(\d{4}年\d{1,2}月\d{1,2}日).*?[至到](\d{4}年\d{1,2}月\d{1,2}日)", period_text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 阳光格式："保险期限 ... 2026年04月12日00时00分00秒 起 至 ... 2027年04月11日24时00分00秒 止"（跨行）
    for i, line in enumerate(lines):
        m_start = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}[时:]\d{2}[分:]\d{2}[秒]?\s*起\s*[至到]?', line)
        if m_start and '保险期' in ''.join(lines[max(0,i-3):i+1]):
            for j in range(i, min(i + 4, len(lines))):
                m_end = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}[时:]\d{2}[分:]\d{2}[秒]?\s*止', lines[j])
                if m_end:
                    fields["保险起期"] = m_start.group(1)
                    fields["保险止期"] = m_end.group(1)
                    fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
                    return
            break

    # 国任等格式："2026/05/22 00:00:00 - 2027/05/21 23:59:59"（用 - 分隔，HH:MM:SS格式）
    m = re.search(r"(\d{4}/\d{2}/\d{2})\s*\d{2}:\d{2}:\d{2}\s*-\s*(\d{4}/\d{2}/\d{2})", text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 国任等格式："2026年05月22日00:00:00 - 2027年05月21日23:59:59"（中文年月日 + HH:MM:SS + - 分隔）
    m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}:\d{2}:\d{2}\s*-\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}:\d{2}:\d{2}", text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 缴款通知表格格式："2026/05/01 00时至2027/05/01 00时" 可能跨行
    m = re.search(r"(\d{4}/\d{2}/\d{2})\s*\d{2}时[\s\S]{0,50}?至\s*(\d{4}/\d{2}/\d{2})", text)
    if m:
        fields["保险起期"] = m.group(1)
        fields["保险止期"] = m.group(2)
        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
        return

    # 极简格式（交强险标志等）："2026年5月11日至2027年5月10日" 独立一行
    for line in lines:
        m = re.match(r'\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*[至到]\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*$', line)
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return


# ============================================================
# 签单日期提取
# ============================================================

def _extract_sign_date(text: str, fields: dict, company_short: str):
    """提取签单日期"""
    # 预处理：清理OCR空格（汉字间空格如"签 单 日 期"，日期中空格如"2026 - 04-27"）
    sign_text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2',
                        re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text))
    sign_text = re.sub(r'(\d)\s+([-/])\s*(\d)', r'\1\2\3', sign_text)
    sign_text = re.sub(r'(\d)\s*([-/])\s+(\d)', r'\1\2\3', sign_text)
    for p in [
        r"签单日期[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        r"签单日期[：:\s]*([\d\-/年月日]+)",
        r"签发时间[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        r"签发日期[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        # 平安个意险格式；安盛天平格式："出单日期 Issue Date(年/月/日 Y/M/D):2026-04-28"
        r"出单日期[^：:\d]*[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        # 华泰格式
        r"签单时间[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        # 京东安联格式："收费日期 Date of Premium Receipt: 2026-05-08"
        r"收费日期[^：:\d]*[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
    ]:
        m = re.search(p, sign_text)
        if m:
            val = m.group(1).strip()
            if len(val) >= 8:
                fields["签单日期"] = val
                return

    # 安盛天平/华泰格式："签单日期：2026-04-23 17:48:56" (带时间)
    m = re.search(r"签单日期[：:\s]*([\d]{4}[-/]\d{1,2}[-/]\d{1,2})\s*\d{2}:\d{2}", sign_text)
    if m:
        fields["签单日期"] = m.group(1)
        return

    # 太平驾意险：无签单日期，用"制单时间"替代
    if "签单日期" not in fields:
        m = re.search(r"制单时间[：:\s]*([\d]{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if m:
            fields["签单日期"] = m.group(1)
            return

    # 兜底：用"保单生成时间"替代（支持多种日期格式）
    if "签单日期" not in fields:
        m = re.search(r"保单生成时间[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
        if m:
            fields["签单日期"] = m.group(1)
            return

    # 兜底：保险人（签章）后紧跟日期，如"保险人（签章）\n2026年04月27日11时57分40秒"
    if "签单日期" not in fields:
        m = re.search(r"保险人[（(]签章[）)]\s*\n?\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
        if m:
            fields["签单日期"] = m.group(1)

    # 兜底：浙商格式 — 投保人签名/签章后紧跟日期，如"投保人签名/签章：\n2026年03月31日"
    if "签单日期" not in fields and company_short == "浙商":
        m = re.search(r"投保人签名[/／]?签章[：:\s]*\n?\s*(\d{4}年\d{1,2}月\d{1,2}日)", text)
        if m:
            fields["签单日期"] = m.group(1)

    # 兜底：平安等格式 — 无签单日期，用"收费确认时间"或"投保确认时间"替代
    # "收费确认时间： 2026年5月6日14:45时" / "投保确认时间： 2026年5月6日14:45时"
    if "签单日期" not in fields:
        m = re.search(r"(?:收费确认时间|投保确认时间)[：:\s]*(\d{4}年\d{1,2}月\d{1,2}日)", sign_text)
        if m:
            fields["签单日期"] = m.group(1)
            return

    # 兜底：华泰驾乘险等 — "制单:XXX"或"复核:XXX"上方紧邻的日期行
    # 格式："2026年05月08日\n绿单专用章\n复核:自动核保\n制单:贾子河"
    # 注意：用原始 text 匹配（sign_text 预处理会把换行符两侧的汉字合并）
    if "签单日期" not in fields:
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)\s*\n[^\n]*(?:制单|复核|专用章)", text)
        if m:
            fields["签单日期"] = m.group(1)


# ============================================================
# 日期格式标准化
# ============================================================

def _normalize_date(val: str) -> str:
    """
    将各种日期格式统一为"YYYY年M月D日"格式（无前导零）
    支持格式：
      - 2026/04/21 → 2026年4月21日
      - 2026-04-21 → 2026年4月21日
      - 2026年04月21日 → 2026年4月21日
      - 2026年4月21日 → 2026年4月21日（不变）
    """
    if not val:
        return val
    # 尝试匹配 YYYY/MM/DD 或 YYYY-MM-DD
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', val)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    # 尝试匹配 YYYY年MM月DD日（去掉前导零）
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日?', val)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    return val


def _normalize_date_fields(fields: dict):
    """统一所有日期字段的格式"""
    for key in ["保险起期", "保险止期", "签单日期"]:
        if key in fields:
            fields[key] = _normalize_date(fields[key])
    # 保险期间也同步更新
    if "保险起期" in fields and "保险止期" in fields:
        fields["保险期间"] = f"{fields['保险起期']} 至 {fields['保险止期']}"


# ============================================================
# 文档类型识别
# ============================================================

def _identify_doc_category(text: str, fields: dict) -> str:
    """
    识别文档类型，返回类别标识：
    - "保单"：正式保单文件
    - "批单"：保险批改单（加保/退保/变更）
    - "电子标志"：交强险电子标志/贴纸，仅含保单号车牌期间，无保单详细信息
    - "条款"：保险条款文件
    - "缴款通知"：缴费通知书
    - "发票"：保险发票
    - "其他"：无法识别的文件
    """
    text_head = text[:500]
    text_len = len(text)

    # 判断前500字中是否包含保单关键字段（用于区分正式保单 vs 附属文件）
    # 注意：条款文件中也可能出现"被保险人义务"、"投保人声明"等，需排除
    _has_policy_markers = any(x in text_head for x in [
        '保险单号', '保单号', '号牌号码',
        '电子保单', '投保确认',
    ])
    # "被保险人"出现在保单中，但需排除条款语境（如"被保险人义务条款"）
    if not _has_policy_markers and '被保险人' in text_head:
        if not re.search(r'被保险人(?:义务|的权利|应当|须)', text_head):
            _has_policy_markers = True
    # "投保人"出现在保单中但需排除"投保人声明"这种条款标题
    if not _has_policy_markers and '投保人' in text_head:
        if '投保人声明' not in text_head:
            _has_policy_markers = True

    # CID编码（无法读取文字），优先判断
    if '(cid:' in text.lower()[:200]:
        return "其他"

    # 电子标志：文本极短（<500字）+ 包含"保险人签章"/"注释" + 无被保人/保费信息
    if text_len < 500 and '保险人签章' in text:
        return "电子标志"

    # 交强险标志：包含"此标志粘贴在机动车前窗"等特征
    # 注意：多页交强险保单可能在附页包含电子标志，需排除含保单关键字段的情况
    if '此标志粘贴在机动车前窗' in text or '此标志正面的年份' in text:
        if not _has_policy_markers:
            return "电子标志"
    # 电子标志：文本极短 + 含"单证查验" + 无被保人/保费信息（安盛天平等格式）
    if text_len < 500 and '单证查验' in text:
        if '被保险人' not in text and '保险费' not in text:
            return "电子标志"

    # 发票
    if '电子发票' in text_head or '发票号码' in text_head:
        return "发票"

    # 缴款通知
    if '缴款通知书' in text_head:
        return "缴款通知"

    # 投保人声明 / 免责事项说明书 / 授权委托书
    # 这些文档本身含"投保人"等关键词，会触发 _has_policy_markers，需优先独立判断
    if '投保人声明' in text_head or '免责事项说明书' in text_head:
        return "条款"
    if '授权委托书' in text_head:
        return "其他"

    # 条款/免责说明
    # 注意：正式保单中常出现"适用条款"/"产品名称"引用了"保险示范条款"字样，
    # 必须排除含保单关键字段的文件，避免将正式保单误判为条款
    if not _has_policy_markers:
        if '保险示范条款' in text_head:
            return "条款"
        if re.search(r'示范条款[（(]\d{4}版[)）]', text_head):
            return "条款"
        # "XX保险条款（条款编码:BXXX）"格式
        if re.search(r'保险条款[（(](?:\d{4}版|条款编码)', text_head):
            return "条款"
        # 条款文件特征：有"总则"+"第一条"
        if '总则' in text_head and '第一条' in text_head:
            return "条款"

    # 批单：包含"批单"/"批改"关键词（在保单判断之前，避免被误判为保单）
    if re.search(r'(?:电子批单|保险批单|批\s*单\s*号|批改日期|批改确认码)', text_head):
        return "批单"

    # 有保单关键字段才认定为保单，否则标记为未知
    if _has_policy_markers:
        return "保单"
    return "未知"


# ============================================================
# 文本预处理：PDF双层文字去重
# ============================================================

def _is_doubled_token(s: str) -> bool:
    """检查字符串是否为每字符重复格式（如'黄黄宝宝兰兰'、'449999'）"""
    if len(s) < 4 or len(s) % 2 != 0:
        return False
    if not all(s[i] == s[i + 1] for i in range(0, len(s), 2)):
        return False
    # 去重后至少有2个不同字符，排除"0000"、"...."等误判
    deduped = s[::2]
    return len(set(deduped)) >= 2


def _dedup_doubled_text(text: str) -> str:
    """检测并修复PDF双层文字（表单值每个字符重复一次）
    众安等PDF会出现双层渲染，导致pdfplumber提取的文本中
    表单值字符逐个重复，如'黄黄宝宝兰兰'应为'黄宝兰'
    按行检测：一行中超过一半的>=4字符token是doubled则去重该行
    """
    lines = text.split('\n')
    result = []
    for line in lines:
        if any(_is_doubled_token(t) for t in re.findall(r'\S{4,}', line)):
            line = re.sub(r'\S+', lambda m: m.group(0)[::2] if _is_doubled_token(m.group(0)) else m.group(0), line)
        result.append(line)
    return '\n'.join(result)


# ============================================================
# 主入口：parse_policy_text
# ============================================================

def parse_policy_text(text: str) -> Dict[str, Any]:
    """
    从保单文本中提取所有关键字段

    架构：先识别保司和险种 → 按保司分发对应的正则规则

    Args:
        text: 从PDF提取的完整文本

    Returns:
        包含字段和险种明细的结果字典
    """
    # 预处理：修复PDF双层文字（众安等）
    text = _dedup_doubled_text(text)

    fields = {}
    insurance_items = []

    # ===== 第一步：识别保司 =====
    company_info = _identify_company(text)
    fields.update(company_info)
    company_short = fields.get("保险公司简称", "")

    # ===== 第二步：识别险种类型 =====
    policy_type = _identify_policy_type(text)
    if policy_type:
        fields["险种类型"] = policy_type

    # ===== 第三步：按保司提取各字段 =====
    extracted = _extract_common_fields(text, company_short, policy_type or "")
    fields.update(extracted)

    # ===== 第四步：险种明细 =====
    lines = text.split("\n")
    for line in lines:
        if re.search(
            r"(损失保险|第三者责任|车上人员|座位险|司机|乘客|医疗费用|医保外"
            r"|精神损害|增值服务|救援服务|盗抢|自燃|涉水|玻璃|划痕|不计免赔"
            r"|代为送检|代为驾驶|车辆安全检测|绝对免赔额)",
            line
        ):
            amounts = re.findall(r"[\d,]+\.\d{2}", line)
            name_match = re.match(r"(.+?)\s+(?:[\d/共]|—)", line)
            name = name_match.group(1).strip() if name_match else line.strip()[:40]

            if amounts:
                item = {"险种名称": name, "保费": amounts[-1]}
                amt_match = re.search(r"([\d,]+\.?\d*(?:元|万元)?(?:/座\*\d+座)?)", line)
                if amt_match and amt_match.group(1) != amounts[-1]:
                    item["保额/限额"] = amt_match.group(1)
                insurance_items.append(item)
            elif "0元/次" in line or "次" in line:
                insurance_items.append({"险种名称": name, "保费": "0.00"})

    # ===== 日期格式统一化：统一为"YYYY年M月D日"格式 =====
    _normalize_date_fields(fields)

    # ===== 识别文档类型 =====
    doc_category = _identify_doc_category(text, fields)
    fields["文档类型"] = doc_category

    # ===== 计算置信度 =====
    key_fields = ["保单号", "被保险人", "车牌号", "保险起期", "保费合计"]
    hit_count = sum(1 for f in key_fields if f in fields)
    confidence = round(hit_count / len(key_fields), 2)

    # ===== 确定保单类型代码 =====
    type_code, type_name = _get_policy_type_code(fields.get("险种类型", ""))

    return {
        "type": type_name,
        "type_code": type_code,
        "confidence": confidence,
        "doc_category": doc_category,
        "fields": fields,
        "insurance_items": insurance_items,
        "raw_text": text,
    }


# ============================================================
# 多保单拆分：一个 PDF 包含多份保单（如交强险+商业险+非车险）
# ============================================================

# 用于检测保单标题行的模式：险种关键词 + "保险单"/"电子保单" 出现在同一行
# 这样可以过滤掉正文/条款中的关键词，只匹配真正的保单标题
# 注意：险种名与"保险单"之间可能有版本号、空格等，用 .{0,30}? 宽松匹配
_POLICY_HEADER_PATTERNS = [
    # 交强险标题行："机动车交通事故责任强制保险单" 或 "...强制保险 保险单（电子保单）"
    (r"交通事故责任强制保险[^，。,;；\n]{0,20}?(?:电子保险单|电子保单|保\s*单|单)", "compulsory"),
    # 商业险标题行："机动车商业保险保险单（电子保单）"
    (r"(?:新能源汽车|机动车|特种车)(?:辆)?(?:商业保险|综合险)[^，。,;；\n]{0,20}?(?:电子保险单|电子保单|保险单|保\s*单)", "commercial"),
    # 驾乘/意外险标题行："机动车驾乘人员人身意外伤害保险（2022 版）电子保险单"
    (r"(?:驾乘|意外伤害)[^，。,;；\n]{0,40}?(?:电子保险单|电子保单|保险单(?=\s*\n|[（(]))", "accident"),
    # 平安车主出行险/尊享保障等非标准标题："平安车主出行险电子保险单"、"平安车主尊享保障电子保单"
    (r"平安车主[^，。,;；\n]{1,20}?(?:电子保险单|电子保单|保险单|保\s*单)", "accident"),
    # 机动车辆保险/综合险标题行（平安/安盛天平等）："机动车辆保险保险单"、"机动车辆综合险保险单"
    (r"机动车辆(?:保险|综合险)[^，。,;；\n]{0,20}?(?:电子保险单|电子保单|保险单|保\s*单)", "commercial"),
]


def _find_policy_boundaries(text: str) -> List[dict]:
    """
    在文本中查找保单标题行的位置，作为多保单拆分的边界。

    策略：
    1. 优先用已知的保单标题 pattern 匹配
    2. 如果标题匹配不足，兜底检测多个不同的"保单号"来发现多保单

    Returns:
        [{"pos": int, "type_code": str, "match": str}, ...]
    """
    found = []
    seen_positions = set()

    for pattern, type_code in _POLICY_HEADER_PATTERNS:
        for m in re.finditer(pattern, text):
            pos = m.start()
            # 跳过已被更高优先级匹配覆盖的位置（±50字符内视为同一位置）
            if any(abs(pos - sp) < 50 for sp in seen_positions):
                continue
            found.append({
                "pos": pos,
                "type_code": type_code,
                "match": m.group(0),
            })
            seen_positions.add(pos)

    # 按位置排序
    found.sort(key=lambda x: x["pos"])

    # ===== 兜底：通过多个不同"保单号"自动检测多保单 =====
    # 当标题匹配不足时，检查是否存在多个不同的保单号
    if len(found) <= 1:
        # 匹配所有"保单号"/"保险单号"及其值
        policy_no_pattern = r"保[险]?单号[：:\s]*([A-Za-z0-9]{10,30})"
        policy_nos = []
        seen_nos = []  # 记录所有见过的保单号（含被跳过的），防止同一号码远距离重复出现被误判为新保单
        for m in re.finditer(policy_no_pattern, text):
            no_val = m.group(1)
            pos = m.start()
            # 排除重复/近似的保单号（互为前缀关系视为同一保单号，如 OCR 截断差异）
            is_dup = any(no_val.startswith(sn) or sn.startswith(no_val) for sn in seen_nos)
            if not is_dup:
                seen_nos.append(no_val)
                # 排除与已有标题边界距离太近的（已被标题覆盖）
                if not any(abs(pos - sp) < 200 for sp in seen_positions):
                    policy_nos.append({"pos": pos, "no": no_val})

        if len(policy_nos) + len(found) > 1:
            # 有多个不同的保单号，将它们作为额外的边界
            for pn in policy_nos:
                found.append({
                    "pos": pn["pos"],
                    "type_code": "auto",
                    "match": f"保单号：{pn['no']}",
                })
                seen_positions.add(pn["pos"])
            found.sort(key=lambda x: x["pos"])

    return found


def _extract_page_range(text: str) -> str:
    """
    从文本中提取页码标记 [PAGE:N]，返回页码范围字符串。
    例如：文本包含 [PAGE:1] 和 [PAGE:2] → 返回 "1-2"
    只有一页 [PAGE:3] → 返回 "3"
    无页码标记 → 返回空字符串
    """
    pages = sorted(set(int(m) for m in re.findall(r'\[PAGE:(\d+)\]', text)))
    if not pages:
        return ""
    if len(pages) == 1:
        return str(pages[0])
    # 连续页码用"-"连接，如 1,2,3 → "1-3"
    if pages[-1] - pages[0] == len(pages) - 1:
        return f"{pages[0]}-{pages[-1]}"
    # 不连续则逗号分隔
    return ",".join(str(p) for p in pages)


def parse_policy_text_multi(text: str) -> List[Dict[str, Any]]:
    """
    从保单文本中提取所有保单——支持一个 PDF 包含多份保单的情况。

    逻辑：
    1. 检测文本中是否出现多个保单标题行
    2. 如果只检测到 0 或 1 个标题，退化为单条解析
    3. 如果有多个标题（无论险种是否相同），按标题位置拆分文本，分别解析
       例如：平安等保司可能在一个 PDF 中包含多份同类型保单（如两份商业险）

    Args:
        text: 从 PDF 提取的完整文本

    Returns:
        保单结果列表（至少包含一条）
    """
    # 预处理：修复PDF双层文字（众安等）
    text = _dedup_doubled_text(text)

    boundaries = _find_policy_boundaries(text)

    if len(boundaries) <= 1:
        # 未检测到多个保单标题，直接单条解析
        page_range = _extract_page_range(text)
        # 清除页码标记后再解析
        clean = re.sub(r'\[PAGE:\d+\]\n?', '', text)
        result = parse_policy_text(clean)
        result["page_range"] = page_range
        return [result]

    # 检测到多个保单标题，按边界拆分文本并分别解析
    policies = []
    for i, boundary in enumerate(boundaries):
        # 每段从当前边界所在行的行首开始
        line_start = text.rfind("\n", 0, boundary["pos"])
        segment_start = line_start + 1 if line_start >= 0 else 0

        # 如果不是第一个边界，但前面有内容可能属于本段（如公司名在险种名之前）
        # 往前多取一些上下文（找到上一段的末尾）
        if i == 0:
            segment_start = 0

        # 每段到下一个边界所在行的行首结束
        if i == len(boundaries) - 1:
            segment_end = len(text)
        else:
            next_pos = boundaries[i + 1]["pos"]
            # 找到下一个边界所在行的行首作为本段结束点
            next_line_start = text.rfind("\n", 0, next_pos)
            segment_end = next_line_start + 1 if next_line_start >= 0 else next_pos

        segment = text[segment_start:segment_end].strip()
        if not segment:
            continue

        # 提取该段对应的页码范围（在清除标记前）
        page_range = _extract_page_range(segment)
        # 清除页码标记后再解析
        clean_segment = re.sub(r'\[PAGE:\d+\]\n?', '', segment)
        policy = parse_policy_text(clean_segment)
        # 只保留有效的保单（有字段且置信度>0）
        if policy.get("fields") and policy.get("confidence", 0) > 0:
            policy["page_range"] = page_range
            policies.append(policy)

    # 如果拆分后没有有效结果，退化为单条解析
    if not policies:
        page_range = _extract_page_range(text)
        clean = re.sub(r'\[PAGE:\d+\]\n?', '', text)
        result = parse_policy_text(clean)
        result["page_range"] = page_range
        return [result]

    return policies




# 字段排序（用于表头生成和导出）
FIELD_ORDER = [
    "文档类型", "保单号", "投保确认码", "保险公司", "保险公司简称", "险种类型",
    "被保险人", "投保人", "车主", "证件号码",
    "车牌号", "车架号VIN", "发动机号", "厂牌型号",
    "核定载客", "核定载质量", "使用性质", "机动车种类", "初次登记日期",
    "保费合计", "不含税保费", "增值税额", "车船税",
    "保险期间", "保险起期", "保险止期", "签单日期", "收费确认时间",
    "销售渠道", "中介机构", "争议解决方式", "制单人", "经办人",
]


def get_extraction_rules(company_short: str = "") -> Dict[str, Dict[str, str]]:
    """
    返回各字段的提取规则描述，按保司区分特殊规则

    Returns:
        {字段名: {"ocr_field": OCR字段名, "rules": 规则描述, "special": 保司特殊说明}}
    """
    # 通用规则
    rules = {
        "承保公司": {
            "ocr_field": "保险公司",
            "rules": "匹配公司全称列表（如'中国人民财产保险股份有限公司'），自动抓取分公司后缀；匹配失败时通过客服电话、域名等特征关键词兜底识别",
            "special": "",
        },
        "保单号": {
            "ocr_field": "保单号",
            "rules": "匹配'保险单号/保单号/保险凭证号'后的编号；支持冒号前后有空格、编号中有空格、每字符间有空格等异常格式；兜底匹配前3行中15-30位纯字母数字编号",
            "special": "",
        },
        "险种": {
            "ocr_field": "险种类型",
            "rules": "检测关键词判断：含'交强'→交强险；含'商业'→商业险；含'驾乘/意外/人身'→驾意险；以上都无→保单",
            "special": "",
        },
        "车牌": {
            "ocr_field": "车牌号",
            "rules": "匹配'号牌号码/车牌号码/车牌号'后的省份简称+字母+数字组合；支持'*-*'或'*新'等新车格式",
            "special": "",
        },
        "投保人": {
            "ocr_field": "投保人",
            "rules": "匹配'投保人名称/投保人'后的人名；过滤地址、手机号、证件类型等干扰词；过滤超20字或少于2字的无效值",
            "special": "",
        },
        "被保人": {
            "ocr_field": "被保险人",
            "rules": "匹配'被保险人名称/被保险人'后的人名；按保司分策略提取；兜底：投保人与被保险人互补（缺一则复制另一个）",
            "special": "",
        },
        "签单日期": {
            "ocr_field": "签单日期",
            "rules": "匹配'签单日期/出单日期/签发日期'后的日期；支持YYYY年MM月DD日、YYYY-MM-DD、YYYY/MM/DD格式",
            "special": "",
        },
        "起保日期": {
            "ocr_field": "保险起期",
            "rules": "匹配'保险期间'后的起始日期；支持'YYYY年MM月DD日HH时MM分SS秒起至...'、'自(From)...至(To)...'、'YYYY-MM-DD HH:MM:SS'等多种格式",
            "special": "",
        },
        "终保日期": {
            "ocr_field": "保险止期",
            "rules": "从'保险期间'中提取结束日期，与起保日期配对；支持跨行匹配",
            "special": "",
        },
        "保费": {
            "ocr_field": "保费合计",
            "rules": "匹配'保险费合计/总保险费/实收保费'后的金额；支持￥/¥/RMB/CNY前缀和'元'后缀；支持千分位逗号格式",
            "special": "",
        },
        "佣金率": {
            "ocr_field": "佣金率",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "佣金": {
            "ocr_field": "佣金",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "业务员": {
            "ocr_field": "业务员",
            "rules": "提取业务员/代理人名称/销售人员，经办人兜底",
            "special": "",
        },
        "采购费率": {
            "ocr_field": "采购费率",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "公司利润": {
            "ocr_field": "公司利润",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "跟单人": {
            "ocr_field": "跟单人",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "跟单人利润": {
            "ocr_field": "跟单人利润",
            "rules": "非OCR提取字段，需手动填写",
            "special": "",
        },
        "出单渠道": {
            "ocr_field": "出单渠道",
            "rules": "匹配'销售渠道'后的值（含√勾选格式）；匹配'中介机构名称/经纪公司名称/保险中介名称'",
            "special": "",
        },
        "车船税": {
            "ocr_field": "车船税",
            "rules": "匹配'车船税...合计'后的金额（含￥/¥前缀）；匹配'当年应缴'后的金额",
            "special": "",
        },
    }

    # 按保司补充特殊说明
    company_specials = {
        "平安": {
            "被保人": "平安表格格式：按'被保险人'列+行号提取；个意险特殊表格按列序提取",
            "保费": "标准格式",
            "投保人": "平安表格格式：按'投保人'列+行号提取",
        },
        "华泰": {
            "保单号": "格式'保险单号 ： 606EA...'（冒号前后都有空格）",
            "被保人": "华泰格式：'被保险人(名称)'后提取，过滤'行驶证车主'等干扰",
            "保费": "特殊格式'（¥： 1,850.00 元）'",
        },
        "永诚": {
            "保单号": "保单号可能每字符间有空格，如'1 1 2 B 5 0 3...'，自动合并",
            "保费": "支持'(小写)：200.00'和'小写：CNY 430.00'格式",
        },
        "京东安联": {
            "保费": "驾乘险格式'Total Premium: RMB 198.00'",
            "起保日期": "驾乘格式'Effective Date: 2026-04-19'",
            "终保日期": "驾乘格式'Expiry Date: 2027-04-19'",
        },
        "安盛天平": {
            "保费": "格式'（¥： 1,850.00 元）'；驾乘格式优先取'价税合计：￥190.00'",
            "起保日期": "格式'保险合同生效日: 2026-04-25'或数字间有空格",
            "终保日期": "格式'保险合同满期日: 2027-04-24'",
        },
        "前海联合": {
            "保费": "驾乘格式'保险费：(大写) 玖拾元整 RMB 90.0元'",
        },
        "国寿财产": {
            "保费": "驾乘格式'￥CNY299.00元'",
            "起保日期": "驾乘格式'自2026年4月29日零时起'",
            "被保人": "国寿格式：'被保险人姓名/名称'后提取",
        },
        "大家财险": {
            "保费": "交强险格式'本保单含税保费1080.0元'",
        },
        "申能": {
            "保费": "优先取'保险费总计...小写 300.00元'，而非不含税保费",
        },
        "中华联合": {
            "车牌": "格式'号1牌1号1码'需合并处理",
            "被保人": "格式'被保险人名称：XXX'或'被保险人姓名XXX'",
            "起保日期": "格式'保险期间：自2026年04月14日18:03:12起至...'",
        },
        "太平": {
            "被保人": "太平格式：多种标签'被保险人/被保人'，支持跨行提取",
            "保费": "支持'(小写)：200.00'格式；驾乘'总保费(含税)...小写CNY'；'小写RMB...保险费合计'；OCR空格'保 险 费 合 计...小写RMB'",
        },
        "紫金": {
            "被保人": "附录被保险人清单表格格式，支持多人用'、'拼接",
        },
    }

    if company_short and company_short in company_specials:
        for field, desc in company_specials[company_short].items():
            if field in rules:
                rules[field]["special"] = desc

    return rules

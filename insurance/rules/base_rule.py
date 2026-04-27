#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则基类 BaseRule
从 policy_parser.py 提取的通用字段提取逻辑，子类可 override 任意方法。
"""

import re
from typing import Dict, Any, List


# ============================================================
# 模块级常量和工具函数
# ============================================================

# 被保险人/投保人提取时需过滤的无效值
PERSON_BLACKLIST = {
    "地址", "手机号", "信息", "名称", "保单验真码", "保单验真码:",
    "行驶证车主", "行驶证", "为", "身份证", "证件",
    "姓名", "证件类型", "证件号码", "申请", "投保申请",
    "手机号码", "联系人", "通讯地址", "电子邮箱",
    "法定", "法定受益人", "受益人", "主被保人",
    # 各保司OCR常见误提取
    "出生日期", "手机电话", "联系电话", "机关",
    "公章并由投", "声明", "社会统一信用",
    "包括主", "Policy",
    # 投保单中的条款文字
    "须如实告知",
}


def merge_text(text: str) -> str:
    """合并中文字符间空格（需执行两次处理相邻情况）"""
    result = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text)
    result = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', result)
    return result


def is_valid_person(name: str) -> bool:
    """校验人名是否有效"""
    if not name or len(name) > 20 or len(name) < 2:
        return False
    if name in PERSON_BLACKLIST:
        return False
    # 过滤明显不是人名的内容
    if re.match(r'^[：:（(]', name):
        return False
    if re.search(
        r'保单|保险|交通|条款|验真|机动车|车辆|证件类型|证件号码|手机号|姓名|发动机|车架号|投保|信息序号|和受害人',
        name
    ):
        return False
    # 含冒号+数字序列的技术数据
    if re.search(r'[:：]\s*[A-Z0-9]', name):
        return False
    # 过滤长句、条款文字
    if re.search(
        r'本公司|提出|公章|有敬异|社会统一|出生日期|联系电话|手机电话|和受害人|信息序号|如实告知|另有约定|投保人的|保险人的',
        name
    ):
        return False
    # "信息"开头
    if name.startswith("信息"):
        return False
    # 纯英文非人名
    if re.match(r'^[A-Za-z]+$', name) and name not in ("Policy",):
        return False
    return True


def clean_person_name(name: str) -> str:
    """清理人名中的常见干扰字符"""
    name = name.strip()
    name = re.sub(r'[：:（(].*', '', name)
    name = re.sub(r'车主.*', '', name)
    name = re.sub(r'名称.*', '', name)
    name = re.sub(r'证件.*', '', name)
    # 永诚OCR干扰
    name = re.sub(r'有敬异的.*', '', name)
    name = re.sub(r'果尊.*', '', name)
    # 清理人名后面多余的职业/证件后缀（如"马玲玲执业证" → "马玲玲"）
    name = re.sub(r'(执业|资格|从业|代理|经纪)(证|号|资格).*', '', name)
    # 清理中文字符间的OCR空格（如"叶 文 燕" → "叶文燕"）
    name = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', name)
    name = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', name)
    return name.strip()


# ============================================================
# 基础规则类
# ============================================================

class BaseRule:
    """
    规则基类，包含所有通用字段的提取逻辑。
    子类通过 override 对应方法来实现保司/险种特有逻辑。
    """

    # 子类需设置的类属性
    company_id: str = ""      # 保司标识，如 "picc", "pingan"
    policy_type: str = ""     # 险种类型，如 "compulsory", "commercial", "accident"
    version: str = ""         # 规则版本号

    def _extract_by_label(self, text: str, label: str) -> str:
        """
        通过标签行切分提取值（通用兜底方法）。
        逐行扫描文本，找到包含标签的行，提取标签后的值部分。
        能处理同一行有多个字段的情况（以 2+ 空格分隔）。
        """
        for line in text.split('\n'):
            if label not in line:
                continue
            pos = line.find(label) + len(label)
            rest = line[pos:].lstrip()
            # 去掉分隔符
            if rest and rest[0] in '：: \t':
                rest = rest[1:].lstrip()
            if not rest:
                continue
            # 截断到下一个标签（2+ 空格后跟中文字符，通常是下一个标签）
            m = re.search(r'\s{2,}[\u4e00-\u9fff]', rest)
            if m:
                return rest[:m.start()].strip()
            return rest.strip()
        return ""

    def extract_all(self, text: str) -> dict:
        """
        主入口：从文本中提取所有字段

        Args:
            text: PDF提取的原始文本

        Returns:
            {
                "common": {},           # 公共字段（保单号、人员、车辆、费用、时间等）
                "type_specific": {},    # 险种特有字段（交强/商业/驾意各自的）
                "company_specific": {}, # 保司特有字段
                "insurance_items": [],  # 险种明细行
            }
        """
        text_merged = merge_text(text)
        return {
            "common": self._extract_common(text, text_merged),
            "type_specific": self.extract_type_specific(text, text_merged),
            "company_specific": self.extract_company_specific(text, text_merged),
            "insurance_items": self.extract_items(text, text_merged),
        }

    # ----------------------------------------------------------
    # 公共字段聚合
    # ----------------------------------------------------------

    def _extract_common(self, text: str, text_merged: str) -> dict:
        """调用所有公共字段提取方法，最后做投保人/被保险人互填"""
        fields = {}

        # 保单号
        val = self.extract_policy_no(text, text_merged)
        if val:
            fields["保单号"] = val

        # 投保确认码
        val = self.extract_confirm_code(text, text_merged)
        if val:
            fields["投保确认码"] = val

        # 被保险人
        val = self.extract_insured(text, text_merged)
        if val:
            fields["被保险人"] = val

        # 投保人
        val = self.extract_proposer(text, text_merged)
        if val:
            fields["投保人"] = val

        # 车辆信息（返回 dict）
        vehicle = self.extract_vehicle_info(text, text_merged)
        if vehicle:
            fields.update(vehicle)

        # 保费信息（返回 dict）
        premium = self.extract_premium(text, text_merged)
        if premium:
            fields.update(premium)

        # 车船税
        val = self.extract_tax(text, text_merged)
        if val:
            fields["车船税"] = val

        # 保险期间（返回 dict）
        period = self.extract_period(text, text_merged)
        if period:
            fields.update(period)

        # 签单日期
        val = self.extract_sign_date(text, text_merged)
        if val:
            fields["签单日期"] = val

        # 收费确认时间
        val = self.extract_fee_confirm_time(text, text_merged)
        if val:
            fields["收费确认时间"] = val

        # 销售渠道
        val = self.extract_sales_channel(text, text_merged)
        if val:
            fields["销售渠道"] = val

        # 中介机构
        val = self.extract_intermediary(text, text_merged)
        if val:
            fields["中介机构"] = val

        # 争议解决方式
        val = self.extract_dispute_resolution(text, text_merged)
        if val:
            fields["争议解决方式"] = val

        # 制单人
        val = self.extract_underwriter(text, text_merged)
        if val:
            fields["制单人"] = val

        # 经办人
        val = self.extract_handler_person(text, text_merged)
        if val:
            fields["经办人"] = val

        return fields

    # ----------------------------------------------------------
    # 各公共字段提取方法（子类可 override）
    # ----------------------------------------------------------

    def extract_policy_no(self, text: str, text_merged: str) -> str:
        """提取保单号"""
        # 紫金格式：保单号在"投保确认时间"行，与日期粘连
        # "投保确认时间：\n20590A44030226000BW12026-04-2716:33:54\n保险单号："
        m = re.search(r"投保确认时间[：:\s]*\n?\s*([A-Za-z0-9]+?)(\d{4}-\d{2}-\d{2})", text)
        if m:
            val = m.group(1)
            if len(val) > 5:
                return val

        # 标准格式："保险单号：XXX" / "保单号：XXX" / "保险凭证号：XXX"
        for p in [
            r"保险单号[码]?[：:\s]\s*(\S+)",
            r"保单号[码]?[：:\s]\s*(\S+)",
            r"保险单\s*号[码]?[：:\s]\s*(\S+)",
            r"保险凭证号[：:\s]\s*(\S+)",
        ]:
            m = re.search(p, text)
            if m:
                val = m.group(1).strip().lstrip("：:")
                if (val and not re.match(r'^[若如]', val)
                        and not re.match(r'^[A-Za-z]+$', val)
                        and not re.match(r'^Policy', val, re.IGNORECASE)
                        and len(val) > 3):
                    return val

        # 保单号中有空格："1 12B5032620260001099" → "112B5032620260001099"
        m = re.search(r"保险单号[码]?[：:\s]\s*(\d{1,3})\s+(\S+)", text)
        if m:
            val = m.group(1) + m.group(2)
            if len(val) > 6:
                return val

        # 冒号前后都有空格："保险单号 ： 606EA..."
        m = re.search(r"保险单号\s*[：:]\s*(\S+)", text)
        if m:
            val = m.group(1).strip()
            if val and len(val) > 3:
                return val

        # 保单号每个字符之间有空格，去除空格后重新匹配
        m = re.search(r"保险单号[码]?[：:\s]\s*(.+?)(?:\s*投\s*保|$)", text)
        if m:
            val = re.sub(r'\s+', '', m.group(1))
            if val and len(val) > 6 and re.match(r'^[A-Z0-9]', val):
                return val

        # 极简标志文件：保单号独立一行，无标签（仅检查前3行）
        lines = text.strip().split('\n')
        for line in lines[:3]:
            stripped = line.strip()
            if re.match(r'^[A-Z0-9]{15,30}$', stripped):
                return stripped

        return ""

    def extract_confirm_code(self, text: str, text_merged: str) -> str:
        """提取投保确认码"""
        m = re.search(r"(?:投保)?确认码[：:\s]*(\S+)", text)
        return m.group(1) if m else ""

    def extract_insured(self, text: str, text_merged: str) -> str:
        """提取被保险人（通用逻辑，保司特有逻辑由子类 override）"""
        # OCR带空格格式："被 保 险 人 深圳市中立咨询有限公司"
        m = re.search(r"被\s+保\s+险\s+人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w（()）)]{1,29})", text)
        if m:
            val = clean_person_name(m.group(1))
            if is_valid_person(val):
                return val

        # 标准冒号格式
        for p in [
            r"被保险人名称[：:]\s*(\S+)",
            r"被保险人[：:]\s*(\S+)",
            r"被保姓名[：:]\s*(\S+)",
            r"(?<!被)被保险人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]{1,9})(?:\s|$)",
        ]:
            m = re.search(p, text)
            if m:
                val = clean_person_name(m.group(1))
                if is_valid_person(val):
                    return val

        # 合并文本匹配无分隔符格式
        for p in [
            r"被保险人([\u4e00-\u9fff]{2,20}?)(?:被保险人|身份证|证件|地址|联系)",
            r"被保姓名[：:]\s*(\S+)",
        ]:
            m = re.search(p, text_merged)
            if m:
                val = clean_person_name(m.group(1))
                if is_valid_person(val):
                    return val

        # "被保险人"单独一行或"被保险人 承保公司"等，邻近行找"名称 XXX"
        lines = text.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == '被保险人' or re.match(r'^被保险人\s+(?:证件|信息|承保)', stripped):
                # 先查附近行的"名称 XXX"格式
                for j in range(max(0, i - 3), min(len(lines), i + 3)):
                    if j == i:
                        continue
                    m = re.match(r'名称\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+)', lines[j].strip())
                    if m and is_valid_person(m.group(1)):
                        return m.group(1)
                # 紫金等表格格式：被保人名字在"被保险人"标签的上一行
                if i > 0:
                    prev = lines[i - 1].strip()
                    prev_cleaned = clean_person_name(re.sub(r'\s+', '', prev))
                    if prev_cleaned and is_valid_person(prev_cleaned):
                        return prev_cleaned
                break

        # 特别约定中"被保险人为XXX"
        m = re.search(r"被保险人为([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)", text)
        if m and is_valid_person(m.group(1)):
            return m.group(1)

        # 表格格式："被保险人姓名 证件类型"，下一行为数据
        for i, line in enumerate(lines):
            if re.search(r'被保险人姓名\s+证件类型', line):
                for j in range(i + 1, min(i + 6, len(lines))):
                    parts = re.split(r'\s+', lines[j].strip())
                    if parts and is_valid_person(parts[0]):
                        return parts[0]
                break

        # "被保 姓 名:" / "被保 正式名称:" 格式（平安商业险）
        for p in [
            r'被保\s*(?:险人)?\s*姓?\s*名[：:]\s*(\S+)',
            r'被保\s*正式名称[：:]\s*(\S+)',
        ]:
            m = re.search(p, text)
            if m:
                val = re.sub(r'证件.*', '', m.group(1)).strip()
                if is_valid_person(val):
                    return val

        # "姓名：XXX；身份证号" 格式
        m = re.search(r"姓名[：:]\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[；;]|身份证|\s)", text)
        if m:
            val = clean_person_name(m.group(1))
            if is_valid_person(val):
                return val

        return ""

    def extract_proposer(self, text: str, text_merged: str) -> str:
        """提取投保人"""
        # 通用提取
        for p in [
            r"投保人\s*(?:姓名|名称)?[：:]\s*(\S+)",
            r"本保单[的]?投保人为[：:]?\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)",
            r"本保单投保人为[：:]?\s*(\S+)",
            r"投保人\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)",
        ]:
            m = re.search(p, text)
            if m:
                val = clean_person_name(m.group(1))
                if is_valid_person(val):
                    return val

        # 前海/紫金等格式：冒号后人名带OCR空格（如"投保人 ： 叶 文 燕"）
        lines = text.split('\n')
        for line in lines:
            m = re.match(r'\s*投保人\s*[：:]\s*(.+)', line)
            if m:
                raw = m.group(1).strip()
                val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', raw)
                val = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', val)
                val = clean_person_name(val)
                if is_valid_person(val):
                    return val

        # 合并文本兜底
        for p in [
            r"投保人为([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:[，,。\s]|$)",
            r"投保人([\u4e00-\u9fff]{2,20}?)(?:，|,|。|证件|保单|$)",
        ]:
            m = re.search(p, text_merged)
            if m:
                val = clean_person_name(m.group(1))
                if is_valid_person(val):
                    return val

        # 表格格式
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if re.search(r'投保人姓名\s+证件类型', line):
                for j in range(i + 1, min(i + 4, len(lines))):
                    parts = re.split(r'\s+', lines[j].strip())
                    if parts and is_valid_person(parts[0]):
                        return parts[0]
                break

        # "投保人名称：XXX"
        m = re.search(r"投保人名称[：:]\s*(\S+)", text)
        if m:
            val = clean_person_name(m.group(1))
            if is_valid_person(val):
                return val

        # "姓名/名称：XXX"
        m = re.search(r"姓名/名称[：:\s]*([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)(?:\s|$)", text)
        if m:
            val = clean_person_name(m.group(1))
            if is_valid_person(val):
                return val

        # "投保人姓名 XXX 性别" 格式
        m = re.search(r"投保人姓名\s+([\u4e00-\u9fff][\u4e00-\u9fff\w]+?)\s+(?:性别|证件)", text)
        if m:
            val = clean_person_name(m.group(1))
            if is_valid_person(val):
                return val

        return ""

    def extract_vehicle_info(self, text: str, text_merged: str) -> dict:
        """提取车辆信息：车牌号、车架号VIN、发动机号、厂牌型号等"""
        fields = {}

        # --- 车牌号 ---
        PROVINCE_CHARS = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁"
        # 处理跨行车牌
        plate_text = re.sub(r'([A-Z]-?)\s*\n\s*([A-Z0-9])', r'\1\2', text)

        for p in [
            rf"号\s*牌\s*号\s*码[：:\s]*([{PROVINCE_CHARS}]\S+)",
            rf"号牌号码[：:\s]*([{PROVINCE_CHARS}]\S+)",
            rf"车牌号码?[：:\s]*([{PROVINCE_CHARS}]\S+)",
            rf"车牌号?[：:\s]*([{PROVINCE_CHARS}]\S+)",
            rf"号牌号码\s+([{PROVINCE_CHARS}]\S+)",
            rf"([{PROVINCE_CHARS}][A-Z][-]?[A-Z0-9]{{4,6}})",
        ]:
            m = re.search(p, plate_text)
            if m:
                plate = m.group(1).replace("-", "").rstrip(";；,，")
                plate = re.split(r'[；;，,\s]', plate)[0]
                if len(plate) >= 6:
                    fields["车牌号"] = plate
                    break

        # 新车特殊处理
        if "车牌号" not in fields:
            m = re.search(rf"号牌号码[：:\s]*([{PROVINCE_CHARS}][A-Z][-\s]*\*?\s*新)", plate_text)
            if m:
                fields["车牌号"] = m.group(1).replace(" ", "").replace("-", "")
            elif (re.search(r"号牌号码[：:\s]*\*[-\s]*\*", text)
                  or re.search(r"车牌号[码]?[：:\s]*\*[-\s]*\*", text)):
                fields["车牌号"] = "新车"

        # --- 车架号/VIN ---
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

        # --- 发动机号 ---
        for p in [
            r"发\s*动\s*机\s*号[码：:\s]*([A-Za-z0-9]+)",
            r"发动机号码[：:\s]*(\S+)",
        ]:
            m = re.search(p, text)
            if m:
                fields["发动机号"] = m.group(1)
                break

        # --- 厂牌型号 ---
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

        # --- 核定载客 ---
        m = re.search(r"核\s*定\s*载\s*客[：:\s]*(\d+)\s*人", text)
        if m:
            fields["核定载客"] = m.group(1) + "人"

        # --- 核定载质量 ---
        m = re.search(r"核定载质量[：:\s]*([\d.]+)\s*千克", text)
        if m:
            fields["核定载质量"] = m.group(1) + "千克"

        # --- 使用性质 ---
        for p in [
            r"使用性质[：:\s]*(\S+?)(?:\s|$|核定|年平均|机动车种类)",
            r"使用性质\s+(\S+)",
        ]:
            m = re.search(p, text)
            if m:
                val = m.group(1)
                if len(val) <= 10:
                    fields["使用性质"] = val
                    break

        # --- 机动车种类 ---
        m = re.search(r"机动车种类[：:\s]*(\S+?)(?:\s|$)", text)
        if m:
            fields["机动车种类"] = m.group(1)

        # --- 初次登记日期 ---
        m = re.search(r"初[次登]*登[记日]*[期：:\s]*([\d\-/年月日]+)", text)
        if m:
            fields["初次登记日期"] = m.group(1)

        # --- 证件号码 ---
        m = re.search(r"证件号码[：:\s]*([A-Z0-9]+)", text)
        if not m:
            m = re.search(r"证件号[：:\s]*([A-Z0-9]+)", text)
        if m:
            fields["证件号码"] = m.group(1)

        return fields

    def extract_premium(self, text: str, text_merged: str) -> dict:
        """提取保费合计、不含税保费、增值税额"""
        fields = {}

        # 通用模式列表
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
            # 小写金额
            r"[（(]?小写[）)]?[：:\s]*(?:CNY\s*|[￥¥]\s*)?([\d,]+\.\d{2})",
            # 缴款通知书
            r"签单保费合计[为：:\s]*([\d,]+\.\d{2})",
            # 发票
            r"价税合计.*?[（(]小写[）)][￥¥]([\d,]+\.\d{2})",
        ]

        for p in patterns:
            m = re.search(p, text)
            if m:
                val = m.group(1).replace(",", "")
                if val and float(val) > 0:
                    fields["保费合计"] = val
                    break

        # 不含税保费
        m = re.search(r"不含税保[险]?费[总计：:\s]*([\d,]+\.\d{2})", text)
        if m:
            fields["不含税保费"] = m.group(1)

        # 增值税额
        m = re.search(r"(?:增值)?税额[总计：:\s]*([\d,]+\.\d{2})", text)
        if m:
            fields["增值税额"] = m.group(1)

        return fields

    def extract_tax(self, text: str, text_merged: str) -> str:
        """提取车船税"""
        # 处理OCR将"车船税"拆到多行的情况（如"车\n船 合计...元）\n税"）
        m_tax_block = re.search(
            r"车\n.*?船\s*合计.*?[（(]\s*[￥¥][：:\s]*([\d,.]+)\s*(?:元)?[)）].*?\n\s*税",
            text, re.DOTALL
        )
        if m_tax_block:
            val = m_tax_block.group(1).strip().replace(",", "")
            try:
                if float(val) >= 0:
                    return val
            except (ValueError, AttributeError):
                pass
        for p in [
            r"车\s*船\s*税[\s\S]*?合计.*?[（(]\s*[￥¥][：:\s]*([\d,.]+)\s*(?:元)?[)）]",
            r"车\s*船\s*税[\s\S]*?合计.*?([\d,.]+)\s*元",
            r"当年应缴\s*[（(\s]*[￥¥][：:\s]*([\d,.]+)\s*元",
            # 平安格式：合计(人民币大写)：...( ￥ XXX 元)
            r"合计\s*[（(]人民币大写[)）][：:\s]*.*?[（(\s]+[￥¥]\s*([\d,.]+)\s*元",
        ]:
            m = re.search(p, text)
            if m:
                val = m.group(1).strip().replace(",", "")
                try:
                    if float(val) >= 0:
                        return val
                except ValueError:
                    continue
                break
        return ""

    def extract_period(self, text: str, text_merged: str) -> dict:
        """提取保险期间、保险起期、保险止期"""
        fields = {}

        # 预处理：清理数字之间的空格
        period_text = text_merged
        for _ in range(5):
            period_text = re.sub(r'(\d)\s+(\d)', r'\1\2', period_text)
        period_text = re.sub(r'(\d)\s+(年|月|日)', r'\1\2', period_text)
        period_text = re.sub(r'(年|月)\s+(\d)', r'\1\2', period_text)
        period_text = re.sub(r'(\d)\s+(时|分|秒)', r'\1\2', period_text)
        period_text = re.sub(r'(时|分)\s+(\d)', r'\1\2', period_text)

        # 模式1："保险期间...YYYY年M月D日...起...至...YYYY年M月D日"
        for p in [
            r"保险期间.{0,30}?(\d{4}年\d{1,2}月\d{1,2}日?)[\s\d时分秒:]+?(?:起|时起)?\s*[至到]\s*(\d{4}年\d{1,2}月\d{1,2}日?)",
            r"保险期间[：:\s]*(\d{4}年?\d{1,2}月?\d{1,2}日?)\s*(?:\d{2}:\d{2}(?::\d{2})?)?\s*[至到]\s*(\d{4}年?\d{1,2}月?\d{1,2}日?)",
        ]:
            m = re.search(p, period_text, re.DOTALL)
            if m:
                fields["保险起期"] = re.sub(r'\s+', '', m.group(1))
                fields["保险止期"] = re.sub(r'\s+', '', m.group(2))
                fields["保险期间"] = f"{fields['保险起期']} 至 {fields['保险止期']}"
                return fields

        # 模式2："自 (From) ... 至 (To) ..."
        m = re.search(
            r"自\s*[（(]?From[)）]?\s*(\d{4}年\d{1,2}月\d{1,2}日?).*?"
            r"[至到]\s*[（(]?To[)）]?\s*(\d{4}年\d{1,2}月\d{1,2}日?)",
            period_text
        )
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式3：原始text中数字间有空格
        m = re.search(
            r"保险期间\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日.*?起\s*至\s*"
            r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
            text
        )
        if m:
            start = f"{m.group(1)}年{m.group(2)}月{m.group(3)}日"
            end = f"{m.group(4)}年{m.group(5)}月{m.group(6)}日"
            fields["保险起期"] = start
            fields["保险止期"] = end
            fields["保险期间"] = f"{start} 至 {end}"
            return fields

        # 模式4："保险期间自 YYYY年M月D日 零时起 至 YYYY年M月D日"
        m = re.search(
            r"保险期间\s*自?\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*"
            r"(?:\d+[时:]\d+[分:]?\d*[秒]?|零时|[零〇○]时)\s*起\s*[至到]\s*"
            r"(\d{4}年\d{1,2}月\d{1,2}日)",
            period_text
        )
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式5："保险期间：自2026年04月14日18:03:12起至2027年04月14日"
        m = re.search(
            r"保险期间[：:\s]*自?(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}:\d{2}(?::\d{2})?起至"
            r"(\d{4}年\d{1,2}月\d{1,2}日)",
            period_text
        )
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式6：跨行格式
        lines = text.split('\n')
        for i, line in enumerate(lines):
            m_start = re.search(
                r'(\d{4}年\d{1,2}月\d{1,2}日)\d{2}(?:时|:)\d{2}(?:分|:)\d{2}(?:秒)?\s*起\s*[至到]',
                line
            )
            if m_start:
                for j in range(i, min(i + 3, len(lines))):
                    m_end = re.search(
                        r'(\d{4}年\d{1,2}月\d{1,2}日)\d{2}(?:时|:)\d{2}(?:分|:)\d{2}(?:秒)?\s*止',
                        lines[j]
                    )
                    if m_end:
                        fields["保险起期"] = m_start.group(1)
                        fields["保险止期"] = m_end.group(1)
                        fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
                        return fields
                break

        # 模式7："保险期限：YYYY-MM-DD HH:MM:SS 至 YYYY-MM-DD"
        m = re.search(
            r"保险期[限间][：:\s]*(?:自\s*)?(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*"
            r"(?:\d{2}:\d{2}(?::\d{2})?)?\s*(?:起[，,]?\s*)?[至到\-]\s*"
            r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            text
        )
        if not m:
            # 生效日/到期日格式
            m_start = re.search(
                r"(?:保[单险](?:合同)?生效日|Effective\s*Date)[：:\s]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
                text
            )
            m_end = re.search(
                r"(?:保[单险](?:合同)?(?:到期|满期)日|Expiry\s*Date)[：:\s]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
                text
            )
            if m_start and m_end:
                fields["保险起期"] = m_start.group(1)
                fields["保险止期"] = m_end.group(1)
                fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
                return fields
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式8："自YYYY年M月D日...至YYYY年M月D日"（无"保险期间"前缀）
        m = re.search(r"自(\d{4}年\d{1,2}月\d{1,2}日).*?[至到](\d{4}年\d{1,2}月\d{1,2}日)", period_text)
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式9：跨行起止时间（阳光格式）
        for i, line in enumerate(lines):
            m_start = re.search(
                r'(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}[时:]\d{2}[分:]\d{2}[秒]?\s*起\s*[至到]?',
                line
            )
            if m_start and '保险期' in ''.join(lines[max(0, i - 3):i + 1]):
                for j in range(i, min(i + 4, len(lines))):
                    m_end = re.search(
                        r'(\d{4}年\d{1,2}月\d{1,2}日)\s*\d{2}[时:]\d{2}[分:]\d{2}[秒]?\s*止',
                        lines[j]
                    )
                    if m_end:
                        fields["保险起期"] = m_start.group(1)
                        fields["保险止期"] = m_end.group(1)
                        fields["保险期间"] = f"{m_start.group(1)} 至 {m_end.group(1)}"
                        return fields
                break

        # 模式10：日期与"保险期间自 起至 止"标签分离（紫金等）
        # OCR输出："2026年5月6日18时0分 2027年5月6日18时0分\n...\n保险期间自 起至 止"
        period_lines = period_text.split('\n')
        two_date_pat = r'(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}时\d{1,2}分\s+(\d{4}年\d{1,2}月\d{1,2}日)\d{1,2}时\d{1,2}分'
        for i, line in enumerate(period_lines):
            if re.search(r'保险期间\s*自\s*起\s*[至到]\s*止', line):
                for j in range(max(0, i - 8), i):
                    m = re.search(two_date_pat, period_lines[j])
                    if m:
                        fields["保险起期"] = m.group(1)
                        fields["保险止期"] = m.group(2)
                        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
                        return fields
                break

        # 模式10b：同一行两个"YYYY年M月D日HH时M分"日期，附近有保险期间关键词
        if not fields:
            for i, line in enumerate(period_lines):
                m = re.search(two_date_pat, line)
                if m:
                    context = '\n'.join(period_lines[max(0, i - 2):min(len(period_lines), i + 5)])
                    if re.search(r'保险期间|起保日期|终保日期', context):
                        fields["保险起期"] = m.group(1)
                        fields["保险止期"] = m.group(2)
                        fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
                        return fields

        # 模式11：缴款通知格式 "YYYY/MM/DD 00时至YYYY/MM/DD 00时"
        m = re.search(r"(\d{4}/\d{2}/\d{2})\s*\d{2}时[\s\S]{0,50}?至\s*(\d{4}/\d{2}/\d{2})", text)
        if m:
            fields["保险起期"] = m.group(1)
            fields["保险止期"] = m.group(2)
            fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
            return fields

        # 模式12：极简格式 "YYYY年M月D日至YYYY年M月D日" 独立一行
        for line in lines:
            m = re.match(r'\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*[至到]\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*$', line)
            if m:
                fields["保险起期"] = m.group(1)
                fields["保险止期"] = m.group(2)
                fields["保险期间"] = f"{m.group(1)} 至 {m.group(2)}"
                return fields

        return fields

    def extract_sign_date(self, text: str, text_merged: str) -> str:
        """提取签单日期"""
        for p in [
            r"签单日期[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
            r"签单日期[：:\s]*([\d\-/年月日]+)",
            r"签发时间[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
            r"签发日期[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
            r"保单生成时间[：:\s]*([\d]{4}[/\-]\d{1,2}[/\-]\d{1,2})",
            r"出单日期[：:\s]*([\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        ]:
            m = re.search(p, text)
            if m:
                val = m.group(1).strip()
                if len(val) >= 8:
                    return val

        # 带时间的签单日期
        m = re.search(r"签单日期[：:\s]*([\d]{4}[-/]\d{1,2}[-/]\d{1,2})\s*\d{2}:\d{2}", text)
        if m:
            return m.group(1)

        # 制单时间兜底
        m = re.search(r"制单时间[：:\s]*([\d]{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if m:
            return m.group(1)

        return ""

    def extract_fee_confirm_time(self, text: str, text_merged: str) -> str:
        """提取收费确认时间"""
        m = re.search(r"收费确认时间[：:\s]*([\d\-/:年月日时分秒\s]+?)(?:\s{2,}|$|投保|有效|打印)", text)
        return m.group(1).strip() if m else ""

    def extract_sales_channel(self, text: str, text_merged: str) -> str:
        """提取销售渠道"""
        # √勾选格式
        m = re.search(r"销售渠道[：:].*?√(\S+)", text)
        if m:
            return m.group(1)
        # 普通格式
        m = re.search(r"销售渠道\s+(\S+)", text)
        if m:
            val = m.group(1).strip()
            if 2 <= len(val) <= 20:
                return val
        return ""

    def extract_intermediary(self, text: str, text_merged: str) -> str:
        """提取中介机构"""
        for p in [
            r"中介机构名称[：:]\s*(.+?)(?:\(|（|\s*$)",
            r"经纪公��名称[：:]\s*(.+?)(?:\s+联系电话|$)",
            r"保险中介名称[：:]\s*(.+?)(?:\s|$)",
        ]:
            m = re.search(p, text)
            if m:
                return m.group(1).strip()
        return ""

    def extract_dispute_resolution(self, text: str, text_merged: str) -> str:
        """提取争议解决方式"""
        m = re.search(r"争议解决方式[：:\s]*(\S+)", text)
        return m.group(1) if m else ""

    def extract_underwriter(self, text: str, text_merged: str) -> str:
        """提取制单人（排除"制单时间"）"""
        m = re.search(r"制单(?!时间)[：:\s]*(\S+?)(?:\s|$)", text)
        return m.group(1) if m else ""

    def extract_handler_person(self, text: str, text_merged: str) -> str:
        """提取经办人"""
        m = re.search(r"经办(?:人员?)?[：:]\s*([\u4e00-\u9fff](?:[^\S\n]?[\u4e00-\u9fff]){0,5})", text)
        if m:
            val = re.sub(r'\s+', '', m.group(1))
            if val:
                return val

        # 紫金等表格格式：值在标签行上方
        # "999999999 赖晨 王丽霞\n核保： 制单： 经办："
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if re.search(r'核保[：:]\s*制单[：:]\s*经办[：:]', line) and i > 0:
                prev = lines[i - 1].strip()
                names = [p for p in re.split(r'\s+', prev) if re.match(r'^[\u4e00-\u9fff]{2,6}$', p)]
                if names:
                    return names[-1]  # 最后一个对应"经办"
            break
        return ""

    # ----------------------------------------------------------
    # 子类 override 用的空方法
    # ----------------------------------------------------------

    def extract_type_specific(self, text: str, text_merged: str) -> dict:
        """险种特有字段（由子类 override）"""
        return {}

    def extract_company_specific(self, text: str, text_merged: str) -> dict:
        """保司特有字段（由子类 override）"""
        return {}

    def extract_items(self, text: str, text_merged: str) -> list:
        """险种明细行（由子类 override）"""
        return []

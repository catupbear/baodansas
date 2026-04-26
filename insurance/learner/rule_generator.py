#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则代码生成模块
根据学习结果生成规则 .py 文件字符串。
"""

from typing import Dict, List

from insurance.learner.field_discoverer import DiscoveredField
from insurance.learner.pattern_inducer import InducedPattern


# ============================================================
# 标准字段名 → BaseRule 方法名映射
# ============================================================
FIELD_METHOD_MAP: Dict[str, str] = {
    # common 层字段
    "保单号": "extract_policy_no",
    "投保确认码": "extract_confirm_code",
    "被保险人": "extract_insured",
    "投保人": "extract_proposer",
    "签单日期": "extract_sign_date",
    "收费确认时间": "extract_fee_confirm_time",
    "销售渠道": "extract_sales_channel",
    "中介机构": "extract_intermediary",
    "争议解决方式": "extract_dispute_resolution",
    "制单人": "extract_underwriter",
    "经办人": "extract_handler_person",
    # vehicle_info 和 premium 是返回 dict 的，单独处理
    # type_specific 层字段由 extract_type_specific 返回
}

# 需要通过 extract_vehicle_info 返回的字段
VEHICLE_FIELDS = {
    "车牌号", "车架号VIN", "发动机号", "厂牌型号",
    "使用性质", "机动车种类", "核定载客", "核定载质量",
    "初次登记日期", "证件号码",
}

# 需要通过 extract_premium 返回的字段
PREMIUM_FIELDS = {"保费合计", "不含税保费", "增值税额"}


class RuleGenerator:
    """
    规则代码生成器
    从学习结果生成可直接保存为 .py 文件的规则代码。
    """

    def generate(
        self,
        company_id: str,
        policy_type: str,
        version: str,
        fields: List[DiscoveredField],
        patterns: Dict[str, InducedPattern],
    ) -> str:
        """
        生成规则 Python 代码

        Args:
            company_id: 保司标识，如 "picc"
            policy_type: 险种类型，如 "compulsory" / "commercial" / "accident"
            version: 版本号，如 "v1"
            fields: 发现的字段列表
            patterns: 字段名 → InducedPattern 映射

        Returns:
            Python 代码字符串
        """
        # 确定基类
        base_class, base_import = self._get_base_info(policy_type)

        # 生成类名
        class_name = self._make_class_name(company_id, policy_type, version)

        # 分类字段
        common_fields = [f for f in fields if f.layer == "common"]
        type_fields = [f for f in fields if f.layer == "type_specific"]
        company_fields = [f for f in fields if f.layer == "company_specific"]

        # 收集需要 override 的方法
        method_blocks = []

        # 1. 处理 common 层中有独立方法的字段
        for f in common_fields:
            if f.standard_name in FIELD_METHOD_MAP:
                method_name = FIELD_METHOD_MAP[f.standard_name]
                pat = patterns.get(f.standard_name)
                if pat:
                    block = self._gen_single_field_method(method_name, f, pat)
                    method_blocks.append(block)

        # 2. 处理 vehicle 相关字段（合并到 extract_vehicle_info）
        vehicle_fields = [f for f in common_fields if f.standard_name in VEHICLE_FIELDS]
        if vehicle_fields:
            block = self._gen_vehicle_method(vehicle_fields, patterns)
            method_blocks.append(block)

        # 3. 处理 premium 相关字段（合并到 extract_premium）
        premium_fields = [f for f in common_fields if f.standard_name in PREMIUM_FIELDS]
        if premium_fields:
            block = self._gen_premium_method(premium_fields, patterns)
            method_blocks.append(block)

        # 4. 处理车船税
        tax_field = next((f for f in common_fields if f.standard_name == "车船税"), None)
        if tax_field:
            pat = patterns.get("车船税")
            if pat:
                block = self._gen_tax_method(tax_field, pat)
                method_blocks.append(block)

        # 5. 处理 type_specific 层
        if type_fields:
            block = self._gen_type_specific_method(type_fields, patterns)
            method_blocks.append(block)

        # 6. 处理 company_specific 层
        if company_fields:
            block = self._gen_company_specific_method(company_fields, patterns)
            method_blocks.append(block)

        # 组装完整代码
        methods_code = "\n\n".join(method_blocks) if method_blocks else "    pass"

        code = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动生成的规则文件
保司: {company_id} | 险种: {policy_type} | 版本: {version}
由模板学习引擎自动生成，可手动调整。
"""

import re
{base_import}


class {class_name}({base_class}):
    """
    {company_id} {policy_type} {version} 规则
    自动生成，继承 {base_class} 并 override 保司特有的提取逻辑。
    """

    company_id = "{company_id}"
    policy_type = "{policy_type}"
    version = "{version}"

{methods_code}
'''
        return code

    def _get_base_info(self, policy_type: str) -> tuple:
        """获取基类名和 import 语句"""
        if policy_type == "compulsory":
            return "BaseCompulsoryRule", "from insurance.rules.base_compulsory import BaseCompulsoryRule"
        elif policy_type == "commercial":
            return "BaseCommercialRule", "from insurance.rules.base_commercial import BaseCommercialRule"
        elif policy_type == "accident":
            return "BaseAccidentRule", "from insurance.rules.base_accident import BaseAccidentRule"
        else:
            return "BaseRule", "from insurance.rules.base_rule import BaseRule"

    def _make_class_name(self, company_id: str, policy_type: str, version: str) -> str:
        """生成类名，如 PiccCompulsoryV1Rule"""
        parts = [
            company_id.capitalize(),
            policy_type.capitalize(),
            version.capitalize(),
            "Rule",
        ]
        return "".join(parts)

    def _gen_single_field_method(self, method_name: str, field_info: DiscoveredField, pattern: InducedPattern) -> str:
        """生成单字段提取方法"""
        label = field_info.original_label.rstrip("：: \t")
        return f'''    def {method_name}(self, text: str, text_merged: str) -> str:
        """提取{field_info.standard_name}（自动生成）"""
        # 正则匹配（MULTILINE 使 .+ 在换行处停止）
        m = re.search(r"{pattern.label_pattern}", text, re.MULTILINE)
        if m:
            return m.group(1).strip()
        # 行切分兜底
        val = self._extract_by_label(text, "{label}")
        if val:
            return val
        # 回退到基类通用逻辑
        return super().{method_name}(text, text_merged)'''

    def _gen_vehicle_method(self, fields: List[DiscoveredField], patterns: Dict[str, InducedPattern]) -> str:
        """生成 extract_vehicle_info 方法"""
        lines = []
        lines.append('    def extract_vehicle_info(self, text: str, text_merged: str) -> dict:')
        lines.append('        """提取车辆信息（自动生成）"""')
        lines.append('        fields = super().extract_vehicle_info(text, text_merged)')

        for f in fields:
            pat = patterns.get(f.standard_name)
            if not pat:
                continue
            label = f.original_label.rstrip("：: \t")
            lines.append(f'        # {f.standard_name}')
            lines.append(f'        if "{f.standard_name}" not in fields:')
            lines.append(f'            m = re.search(r"{pat.label_pattern}", text, re.MULTILINE)')
            lines.append(f'            if m:')
            lines.append(f'                fields["{f.standard_name}"] = m.group(1).strip()')
            lines.append(f'            else:')
            lines.append(f'                val = self._extract_by_label(text, "{label}")')
            lines.append(f'                if val:')
            lines.append(f'                    fields["{f.standard_name}"] = val')

        lines.append('        return fields')
        return "\n".join(lines)

    def _gen_premium_method(self, fields: List[DiscoveredField], patterns: Dict[str, InducedPattern]) -> str:
        """生成 extract_premium 方法"""
        lines = []
        lines.append('    def extract_premium(self, text: str, text_merged: str) -> dict:')
        lines.append('        """提取保费信息（自动生成）"""')
        lines.append('        fields = super().extract_premium(text, text_merged)')

        for f in fields:
            pat = patterns.get(f.standard_name)
            if not pat:
                continue
            label = f.original_label.rstrip("：: \t")
            lines.append(f'        # {f.standard_name}')
            lines.append(f'        if "{f.standard_name}" not in fields:')
            lines.append(f'            m = re.search(r"{pat.label_pattern}", text, re.MULTILINE)')
            lines.append(f'            if m:')
            lines.append(f'                fields["{f.standard_name}"] = m.group(1).replace(",", "")')
            lines.append(f'            else:')
            lines.append(f'                val = self._extract_by_label(text, "{label}")')
            lines.append(f'                if val:')
            lines.append(f'                    fields["{f.standard_name}"] = val.replace(",", "")')

        lines.append('        return fields')
        return "\n".join(lines)

    def _gen_tax_method(self, field_info: DiscoveredField, pattern: InducedPattern) -> str:
        """生成 extract_tax 方法"""
        label = field_info.original_label.rstrip("：: \t")
        return f'''    def extract_tax(self, text: str, text_merged: str) -> str:
        """提取车船税（自动生成）"""
        m = re.search(r"{pattern.label_pattern}", text, re.MULTILINE)
        if m:
            return m.group(1).strip().replace(",", "")
        val = self._extract_by_label(text, "{label}")
        if val:
            return val.replace(",", "")
        return super().extract_tax(text, text_merged)'''

    def _gen_type_specific_method(self, fields: List[DiscoveredField], patterns: Dict[str, InducedPattern]) -> str:
        """生成 extract_type_specific 方法"""
        lines = []
        lines.append('    def extract_type_specific(self, text: str, text_merged: str) -> dict:')
        lines.append('        """提取险种特有字段（自动生成）"""')
        lines.append('        fields = super().extract_type_specific(text, text_merged)')

        for f in fields:
            pat = patterns.get(f.standard_name)
            if not pat:
                continue
            label = f.original_label.rstrip("：: \t")
            lines.append(f'        # {f.standard_name}')
            lines.append(f'        if "{f.standard_name}" not in fields:')
            lines.append(f'            m = re.search(r"{pat.label_pattern}", text, re.MULTILINE)')
            lines.append(f'            if m:')
            lines.append(f'                fields["{f.standard_name}"] = m.group(1).strip()')
            lines.append(f'            else:')
            lines.append(f'                val = self._extract_by_label(text, "{label}")')
            lines.append(f'                if val:')
            lines.append(f'                    fields["{f.standard_name}"] = val')

        lines.append('        return fields')
        return "\n".join(lines)

    def _gen_company_specific_method(self, fields: List[DiscoveredField], patterns: Dict[str, InducedPattern]) -> str:
        """生成 extract_company_specific 方法"""
        lines = []
        lines.append('    def extract_company_specific(self, text: str, text_merged: str) -> dict:')
        lines.append('        """提取保司特有字段（自动生成）"""')
        lines.append('        fields = super().extract_company_specific(text, text_merged)')

        for f in fields:
            pat = patterns.get(f.standard_name)
            if not pat:
                continue
            label = f.original_label.rstrip("：: \t")
            lines.append(f'        # {f.standard_name}（原始标签: {label}）')
            lines.append(f'        m = re.search(r"{pat.label_pattern}", text, re.MULTILINE)')
            lines.append(f'        if m:')
            lines.append(f'            fields["{f.standard_name}"] = m.group(1).strip()')
            lines.append(f'        else:')
            lines.append(f'            val = self._extract_by_label(text, "{label}")')
            lines.append(f'            if val:')
            lines.append(f'                fields["{f.standard_name}"] = val')

        lines.append('        return fields')
        return "\n".join(lines)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字段自动发现模块
从对齐结果中识别标准字段，映射到三层结构（common / type_specific / company_specific）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from insurance.learner.text_aligner import AlignmentResult, AlignedLine


# ============================================================
# 已知标签 → 标准字段名映射（至少 40 个）
# ============================================================
KNOWN_LABELS: Dict[str, str] = {
    # --- 保单基本信息 ---
    "保险单号": "保单号",
    "保单号": "保单号",
    "保险单号码": "保单号",
    "保险凭证号": "保单号",
    "保单编号": "保单号",
    "投保确认码": "投保确认码",
    "确认码": "投保确认码",

    # --- 人员信息 ---
    "被保险人": "被保险人",
    "被保险人名称": "被保险人",
    "被保姓名": "被保险人",
    "投保人": "投保人",
    "投保人名称": "投保人",
    "投保人姓名": "投保人",

    # --- 车辆信息 ---
    "号牌号码": "车牌号",
    "车牌号码": "车牌号",
    "车牌号": "车牌号",
    "车牌": "车牌号",
    "车架号": "车架号VIN",
    "VIN": "车架号VIN",
    "VIN码": "车架号VIN",
    "车辆识别代号": "车架号VIN",
    "车辆识别代码": "车架号VIN",
    "识别代码": "车架号VIN",
    "发动机号": "发动机号",
    "发动机号码": "发动机号",
    "厂牌型号": "厂牌型号",
    "品牌型号": "厂牌型号",
    "使用性质": "使用性质",
    "机动车种类": "机动车种类",
    "核定载客": "核定载客",
    "核定载质量": "核定载质量",
    "初次登记日期": "初次登记日期",
    "证件号码": "证件号码",
    "证件号": "证件号码",

    # --- 费用信息 ---
    "保费合计": "保费合计",
    "保险费合计": "保费合计",
    "总保费": "保费合计",
    "总保险费": "保费合计",
    "实收保费": "保费合计",
    "签单保费合计": "保费合计",
    "不含税保费": "不含税保费",
    "不含税保险费": "不含税保费",
    "增值税额": "增值税额",
    "税额": "增值税额",
    "车船税": "车船税",

    # --- 时间信息 ---
    "保险期间": "保险期间",
    "保险期限": "保险期间",
    "签单日期": "签单日期",
    "签发时间": "签单日期",
    "签发日期": "签单日期",
    "出单日期": "签单日期",
    "制单时间": "签单日期",
    "收费确认时间": "收费确认时间",

    # --- 渠道信息 ---
    "销售渠道": "销售渠道",
    "中介机构名称": "中介机构",
    "中介机构": "中介机构",
    "经纪公司名称": "中介机构",
    "保险中介名称": "中介机构",

    # --- 其他公共字段 ---
    "争议解决方式": "争议解决方式",
    "制单": "制单人",
    "制单人": "制单人",
    "经办": "经办人",
    "经办人": "经办人",

    # --- 交强险特有 ---
    "责任限额": "责任限额",
    "死亡伤残赔偿限额": "死亡伤残赔偿限额",
    "医疗费用赔偿限额": "医疗费用赔偿限额",
    "财产损失赔偿限额": "财产损失赔偿限额",
    "抢救费用赔偿限额": "抢救费用赔偿限额",

    # --- 商业险特有 ---
    "特别约定": "特别约定",
    "绝对免赔额": "绝对免赔额",

    # --- 驾意险特有 ---
    "保障人数": "保障人数",
    "被保险人数": "保障人数",
    "投保人数": "保障人数",
    "每人保额": "每人保额",
    "每座保额": "每人保额",
    "意外身故保额": "意外身故保额",
    "意外医疗保额": "意外医疗保额",

    # --- 公司信息 ---
    "公司名称": "保险公司",
    "保险人": "保险公司",
    "保险公司": "保险公司",
    "公司地址": "公司地址",
    "服务电话": "服务电话",
    "客服电话": "服务电话",
}

# ============================================================
# 标准字段名 → 层级映射
# ============================================================
FIELD_LAYER: Dict[str, str] = {
    # common 层
    "保单号": "common",
    "投保确认码": "common",
    "被保险人": "common",
    "投保人": "common",
    "车牌号": "common",
    "车架号VIN": "common",
    "发动机号": "common",
    "厂牌型号": "common",
    "使用性质": "common",
    "机动车种类": "common",
    "核定载客": "common",
    "核定载质量": "common",
    "初次登记日期": "common",
    "证件号码": "common",
    "保费合计": "common",
    "不含税保费": "common",
    "增值税额": "common",
    "车船税": "common",
    "保险期间": "common",
    "签单日期": "common",
    "收费确认时间": "common",
    "销售渠道": "common",
    "中介机构": "common",
    "争议解决方式": "common",
    "制单人": "common",
    "经办人": "common",
    "保险公司": "common",
    "公司地址": "common",
    "服务电话": "common",

    # type_specific 层（交强/商业/驾意特有）
    "责任限额": "type_specific",
    "死亡伤残赔偿限额": "type_specific",
    "医疗费用赔偿限额": "type_specific",
    "财产损失赔偿限额": "type_specific",
    "抢救费用赔偿限额": "type_specific",
    "特别约定": "type_specific",
    "绝对免赔额": "type_specific",
    "保障人数": "type_specific",
    "每人保额": "type_specific",
    "意外身故保额": "type_specific",
    "意外医疗保额": "type_specific",
}


@dataclass
class DiscoveredField:
    """发现的字段"""
    standard_name: str = ""        # 标准字段名
    layer: str = ""                # common / type_specific / company_specific
    original_label: str = ""       # 原始标签文本
    line_index: int = -1           # 在对齐结果中的行号
    sample_values: List[str] = field(default_factory=list)  # 各样本中的值
    source: str = "exact"          # 发现来源：exact / fuzzy / unknown
    confidence: float = 1.0        # 置信度


class FieldDiscoverer:
    """
    字段自动发现器
    从对齐结果的 label_value 行中识别标准字段。
    """

    def discover(self, alignment: AlignmentResult) -> List[DiscoveredField]:
        """
        从对齐结果中发现字段

        Args:
            alignment: TextAligner.align() 的输出

        Returns:
            发现的字段列表
        """
        fields: List[DiscoveredField] = []

        for line in alignment.lines:
            if line.line_type != "label_value":
                continue
            if not line.label:
                continue

            # 清理标签：去除尾部分隔符
            clean_label = line.label.rstrip("：: \t")
            if not clean_label:
                continue

            # 提取各样本的值部分
            sample_values = self._extract_values(line)

            # 尝试精确匹配
            discovered = self._exact_match(clean_label, line.index, sample_values)
            if discovered:
                fields.append(discovered)
                continue

            # 尝试模糊匹配（包含关系）
            discovered = self._fuzzy_match(clean_label, line.index, sample_values)
            if discovered:
                fields.append(discovered)
                continue

            # 未知标签 → company_specific
            fields.append(DiscoveredField(
                standard_name=clean_label,
                layer="company_specific",
                original_label=line.label,
                line_index=line.index,
                sample_values=sample_values,
                source="unknown",
                confidence=0.5,
            ))

        return fields

    def _extract_values(self, line: AlignedLine) -> List[str]:
        """从对齐行中提取各样本的值部分（去掉标签前缀）"""
        values = []
        label = line.label
        for v in line.variants:
            if v.startswith(label):
                values.append(v[len(label):].strip())
            else:
                values.append(v.strip())
        return values

    def _exact_match(self, label: str, line_index: int, sample_values: List[str]) -> Optional[DiscoveredField]:
        """精确匹配已知标签"""
        if label in KNOWN_LABELS:
            std_name = KNOWN_LABELS[label]
            layer = FIELD_LAYER.get(std_name, "company_specific")
            return DiscoveredField(
                standard_name=std_name,
                layer=layer,
                original_label=label,
                line_index=line_index,
                sample_values=sample_values,
                source="exact",
                confidence=1.0,
            )
        return None

    def _fuzzy_match(self, label: str, line_index: int, sample_values: List[str]) -> Optional[DiscoveredField]:
        """
        模糊匹配：
        - 标签包含已知标签名，或已知标签名包含标签
        - 优先取最长匹配
        """
        best_match = None
        best_len = 0

        for known_label, std_name in KNOWN_LABELS.items():
            # 标签包含已知标签
            if known_label in label or label in known_label:
                match_len = len(known_label)
                if match_len > best_len:
                    best_len = match_len
                    best_match = std_name

        if best_match:
            layer = FIELD_LAYER.get(best_match, "company_specific")
            return DiscoveredField(
                standard_name=best_match,
                layer=layer,
                original_label=label,
                line_index=line_index,
                sample_values=sample_values,
                source="fuzzy",
                confidence=0.8,
            )

        return None

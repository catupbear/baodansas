#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
正则自动归纳模块
从样本值推导提取正则表达式。
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from insurance.learner.field_discoverer import DiscoveredField


# ============================================================
# 字段预定义模式（常见字段直接使用已知正则）
# ============================================================
FIELD_PRESETS: Dict[str, str] = {
    "车牌号": r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z][-]?[A-Z0-9]{4,6}",
    "车架号VIN": r"[A-Z0-9]{17}",
    "发动机号": r"[A-Za-z0-9]{5,20}",
    "保费合计": r"[\d,]+\.\d{2}",
    "不含税保费": r"[\d,]+\.\d{2}",
    "增值税额": r"[\d,]+\.\d{2}",
    "车船税": r"[\d,.]+",
    "保单号": r"\S{10,30}",
    "投保确认码": r"\S+",
    "保障人数": r"\d+",
    "每人保额": r"[\d,]+\.?\d*",
    "意外身故保额": r"[\d,]+\.?\d*",
    "意外医疗保额": r"[\d,]+\.?\d*",
    "责任限额": r"[\d,]+\.?\d*",
    "死亡伤残赔偿限额": r"[\d,]+\.?\d*",
    "医疗费用赔偿限额": r"[\d,]+\.?\d*",
    "财产损失赔偿限额": r"[\d,]+\.?\d*",
    "抢救费用赔偿限额": r"[\d,]+\.?\d*",
    "绝对免赔额": r"[\d,]+\.?\d*",
    "核定载客": r"\d+\s*人",
    "核定载质量": r"[\d.]+\s*千克",
    "签单日期": r"[\d]{4}[-/年]\d{1,2}[-/月]\d{1,2}日?",
    "收费确认时间": r"[\d\-/:年月日时分秒\s]+",
    "证件号码": r"[A-Z0-9]+",
}


@dataclass
class InducedPattern:
    """归纳出的正则模式"""
    field_name: str = ""           # 标准字段名
    pattern: str = ""              # 正则表达式（不含标签部分）
    label_pattern: str = ""        # 包含标签的完整正则（标签容错：字符间允许空格）
    source: str = "preset"         # preset / inferred
    sample_count: int = 0          # 样本数量
    match_rate: float = 1.0        # 模式在样本上的匹配率


class PatternInducer:
    """
    正则自动归纳器
    从样本值推导提取用的正则表达式。
    """

    def induce(self, field_info: DiscoveredField) -> InducedPattern:
        """
        为一个字段归纳正则模式

        Args:
            field_info: FieldDiscoverer 发现的字段信息

        Returns:
            InducedPattern
        """
        name = field_info.standard_name
        label = field_info.original_label
        values = [v for v in field_info.sample_values if v.strip()]

        # 优先查预定义模式
        if name in FIELD_PRESETS:
            value_pattern = FIELD_PRESETS[name]
            label_pat = self._build_label_pattern(label, value_pattern)
            match_rate = self._calc_match_rate(value_pattern, values)
            return InducedPattern(
                field_name=name,
                pattern=value_pattern,
                label_pattern=label_pat,
                source="preset",
                sample_count=len(values),
                match_rate=match_rate,
            )

        # 从样本值推断模式
        value_pattern = self._infer_pattern(values)
        label_pat = self._build_label_pattern(label, value_pattern)
        match_rate = self._calc_match_rate(value_pattern, values)

        return InducedPattern(
            field_name=name,
            pattern=value_pattern,
            label_pattern=label_pat,
            source="inferred",
            sample_count=len(values),
            match_rate=match_rate,
        )

    def _infer_pattern(self, values: List[str]) -> str:
        """
        从样本值推断正则模式

        分析策略：
        1. 纯数字 → r"\\d+"
        2. 数字+小数点 → r"[\\d,]+\\.\\d+"
        3. 纯中文 → r"[\\u4e00-\\u9fff]+"
        4. 字母数字混合 → r"[A-Za-z0-9]+"
        5. 日期格式 → 日期正则
        6. 混合 → r"\\S+"
        """
        if not values:
            return r"\S+"

        # 判断值类型
        all_digits = all(re.match(r'^[\d,]+\.?\d*$', v) for v in values)
        all_decimal = all(re.match(r'^[\d,]+\.\d+$', v) for v in values)
        all_chinese = all(re.match(r'^[\u4e00-\u9fff]+$', v) for v in values)
        all_alnum = all(re.match(r'^[A-Za-z0-9]+$', v) for v in values)
        all_date = all(re.match(r'^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$', v) for v in values)

        if all_date:
            return r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
        if all_decimal:
            return r"[\d,]+\.\d+"
        if all_digits:
            return r"[\d,]+"
        if all_chinese:
            # 估算长度范围
            min_len = min(len(v) for v in values)
            max_len = max(len(v) for v in values)
            if min_len == max_len:
                return rf"[\u4e00-\u9fff]{{{min_len}}}"
            return rf"[\u4e00-\u9fff]{{{min_len},{max_len}}}"
        if all_alnum:
            min_len = min(len(v) for v in values)
            max_len = max(len(v) for v in values)
            return rf"[A-Za-z0-9]{{{min_len},{max_len}}}"

        # 混合类型
        return r"\S+"

    def _build_label_pattern(self, label: str, value_pattern: str = "") -> str:
        """
        构建包含标签的完整正则（标签容错：中文字符间允许空格）

        用 .+ 捕获到行尾（配合 re.MULTILINE 使用），不限定值格式。
        标签是稳定锚点，值的格式千变万化，不应死板限定。

        Args:
            label: 原始标签，如 "保险单号："
            value_pattern: 已弃用，保留参数兼容

        Returns:
            完整正则，如 r"保\\s*险\\s*单\\s*号[：:\\s]*(.+)"
        """
        # 将标签中的中文字符间插入 \\s* 容错
        label_clean = label.rstrip("：: \t")
        tolerant = ""
        for i, ch in enumerate(label_clean):
            tolerant += re.escape(ch)
            # 中文字符后加 \\s*（最后一个字符除外）
            if i < len(label_clean) - 1 and '\u4e00' <= ch <= '\u9fff':
                tolerant += r"\s*"

        # 用 .+ 捕获到行尾，配合 re.MULTILINE 自然在换行处停止
        return rf"{tolerant}[：:\s]*(.+)"

    def _calc_match_rate(self, pattern: str, values: List[str]) -> float:
        """计算模式在样本值上的匹配率"""
        if not values:
            return 1.0
        try:
            regex = re.compile(pattern)
            matched = sum(1 for v in values if regex.search(v))
            return matched / len(values)
        except re.error:
            return 0.0

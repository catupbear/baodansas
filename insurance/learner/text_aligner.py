#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多文本结构对齐模块
将 N 份保单文本按行对齐，区分三种行类型：
- fixed：所有样本完全一致 → 模板指纹
- label_value：有公共前缀（标签）+ 变化部分（值）
- variable：整行不同
"""

from dataclasses import dataclass, field
from typing import List, Optional
from difflib import SequenceMatcher


@dataclass
class AlignedLine:
    """对齐后的单行信息"""
    index: int                          # 行号
    line_type: str = "variable"         # fixed / label_value / variable
    content: str = ""                   # 固定行的内容 或 标签值行的完整示例
    label: str = ""                     # label_value 类型时的公共前缀（标签部分）
    variants: List[str] = field(default_factory=list)  # 各样本在该行的值


@dataclass
class AlignmentResult:
    """对齐结果"""
    lines: List[AlignedLine] = field(default_factory=list)
    sample_count: int = 0
    fixed_lines: int = 0
    variable_lines: int = 0
    label_value_lines: int = 0


class TextAligner:
    """
    多文本结构对齐器
    将多份保单文本按行拆分后逐行比较，划分行类型。
    """

    # 标签分隔符模式（冒号、空格等）
    LABEL_SEPARATORS = ("：", ":", " ", "\t")

    # 最小标签长度（过短的前缀不视为标签）
    MIN_LABEL_LEN = 2

    def align(self, texts: List[str]) -> AlignmentResult:
        """
        对齐多份文本

        Args:
            texts: N 份 PDF 提取的原始文本列表

        Returns:
            AlignmentResult 包含对齐后的行信息
        """
        if not texts:
            return AlignmentResult()

        # 按行拆分所有样本
        all_lines = [t.split("\n") for t in texts]
        sample_count = len(texts)

        # 以最长样本的行数为基准
        max_lines = max(len(lines) for lines in all_lines)

        result_lines: List[AlignedLine] = []
        fixed_count = 0
        variable_count = 0
        label_value_count = 0

        for i in range(max_lines):
            # 收集每个样本在第 i 行的内容（不足则为空）
            row_values = []
            for lines in all_lines:
                if i < len(lines):
                    row_values.append(lines[i].strip())
                else:
                    row_values.append("")

            # 判断行类型
            aligned_line = self._classify_line(i, row_values)
            result_lines.append(aligned_line)

            if aligned_line.line_type == "fixed":
                fixed_count += 1
            elif aligned_line.line_type == "label_value":
                label_value_count += 1
            else:
                variable_count += 1

        return AlignmentResult(
            lines=result_lines,
            sample_count=sample_count,
            fixed_lines=fixed_count,
            variable_lines=variable_count,
            label_value_lines=label_value_count,
        )

    def _classify_line(self, index: int, values: List[str]) -> AlignedLine:
        """
        判断一行的类型

        Args:
            index: 行号
            values: 各样本在该行的值

        Returns:
            AlignedLine
        """
        # 过滤掉空行样本
        non_empty = [v for v in values if v]
        if not non_empty:
            return AlignedLine(index=index, line_type="fixed", content="", variants=values)

        # 所有非空值完全一致 → fixed
        unique = set(non_empty)
        if len(unique) == 1:
            return AlignedLine(
                index=index,
                line_type="fixed",
                content=non_empty[0],
                variants=values,
            )

        # 尝试提取公共前缀作为标签
        label = self._find_common_label(non_empty)
        if label:
            return AlignedLine(
                index=index,
                line_type="label_value",
                content=non_empty[0],
                label=label,
                variants=values,
            )

        # 否则为 variable
        return AlignedLine(
            index=index,
            line_type="variable",
            content=non_empty[0],
            variants=values,
        )

    def _find_common_label(self, values: List[str]) -> str:
        """
        从多个值中提取公共标签前缀

        策略：
        1. 找到所有值的最长公共前缀
        2. 前缀须以标签分隔符结尾（或分隔符在前缀之后紧跟）
        3. 前缀长度需 >= MIN_LABEL_LEN

        Args:
            values: 非空值列表

        Returns:
            标签字符串（含分隔符），如 "保单号：" ；未找到返回 ""
        """
        if len(values) < 2:
            return ""

        # 计算最长公共前缀
        prefix = values[0]
        for v in values[1:]:
            prefix = self._common_prefix(prefix, v)
            if not prefix:
                return ""

        # 前缀需要满足最小长度
        if len(prefix) < self.MIN_LABEL_LEN:
            return ""

        # 检查前缀是否以分隔符结尾，或分隔符紧跟前缀之后
        stripped = prefix.rstrip()
        for sep in self.LABEL_SEPARATORS:
            if stripped.endswith(sep):
                return stripped
            # 前缀本身不以分隔符结尾，但每个值在前缀后紧接分隔符
            all_have_sep = all(
                len(v) > len(prefix) and v[len(prefix):len(prefix) + len(sep)] == sep
                for v in values
            )
            if all_have_sep:
                return prefix + sep

        # 如果公共前缀占每个值的比例都不超过 80%，也视为标签
        # （避免把几乎一样的长行误判为 label_value）
        if all(len(prefix) < len(v) * 0.8 for v in values):
            # 确认后缀部分确实有差异
            suffixes = set(v[len(prefix):].strip() for v in values)
            if len(suffixes) > 1 and len(prefix) >= self.MIN_LABEL_LEN:
                return prefix.rstrip()

        return ""

    @staticmethod
    def _common_prefix(a: str, b: str) -> str:
        """计算两个字符串的公共前缀"""
        min_len = min(len(a), len(b))
        for i in range(min_len):
            if a[i] != b[i]:
                return a[:i]
        return a[:min_len]

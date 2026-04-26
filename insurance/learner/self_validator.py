#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自验证模块
用生成的规则反跑所有样本，验证字段提取的准确性。
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from insurance.rules.base_rule import BaseRule
from insurance.learner.field_discoverer import DiscoveredField


@dataclass
class FieldValidation:
    """单字段验证结果"""
    field_name: str = ""
    expected: str = ""         # 样本中的期望值
    actual: str = ""           # 规则提取的实际值
    matched: bool = False      # 是否匹配
    detail: str = ""           # 不匹配时的说明


@dataclass
class SampleValidation:
    """单样本验证结果"""
    sample_index: int = 0
    total_fields: int = 0
    matched_fields: int = 0
    hit_rate: float = 0.0
    field_results: List[FieldValidation] = field(default_factory=list)


@dataclass
class ValidationReport:
    """验证报告"""
    sample_count: int = 0
    total_fields: int = 0
    total_matched: int = 0
    overall_hit_rate: float = 0.0
    samples: List[SampleValidation] = field(default_factory=list)
    unstable_fields: List[str] = field(default_factory=list)  # 不稳定字段（部分样本命中部分未命中）
    passed: bool = False       # 是否通过验证

    @property
    def summary(self) -> str:
        """验证摘要"""
        status = "通过" if self.passed else "未通过"
        unstable = f"，不稳定字段: {', '.join(self.unstable_fields)}" if self.unstable_fields else ""
        return (
            f"验证{status} | 样本数: {self.sample_count} | "
            f"总字段: {self.total_fields} | 命中: {self.total_matched} | "
            f"命中率: {self.overall_hit_rate:.1%}{unstable}"
        )


class SelfValidator:
    """
    自验证器
    用生成的规则反跑所有样本文本，逐字段对比。
    """

    # 通过条件
    MIN_HIT_RATE = 0.9           # 最低总体命中率
    MAX_UNSTABLE_FIELDS = 0      # 最大允许不稳定字段数

    def validate(
        self,
        rule: BaseRule,
        texts: List[str],
        fields: List[DiscoveredField],
    ) -> ValidationReport:
        """
        验证规则在所有样本上的表现

        Args:
            rule: 生成的规则实例
            texts: 样本文本列表
            fields: 发现的字段列表（含 sample_values）

        Returns:
            ValidationReport
        """
        report = ValidationReport(sample_count=len(texts))
        field_hit_map: Dict[str, List[bool]] = {}  # 字段名 → 各样本是否命中

        for idx, text in enumerate(texts):
            # 用规则提取所有字段
            result = rule.extract_all(text)

            # 合并三层字段为扁平 dict
            extracted = {}
            for layer in ("common", "type_specific", "company_specific"):
                if layer in result:
                    extracted.update(result[layer])

            # 逐字段对比
            sample_result = SampleValidation(sample_index=idx)
            matched_count = 0
            total_count = 0

            for f in fields:
                # 获取该样本的期望值
                if idx < len(f.sample_values):
                    expected = f.sample_values[idx].strip()
                else:
                    expected = ""

                # 跳过无期望值的字段
                if not expected:
                    continue

                total_count += 1
                actual = str(extracted.get(f.standard_name, "")).strip()

                # 模糊比较（忽略空格和标点）
                is_match = self._fuzzy_equal(expected, actual)

                if is_match:
                    matched_count += 1

                field_result = FieldValidation(
                    field_name=f.standard_name,
                    expected=expected,
                    actual=actual,
                    matched=is_match,
                    detail="" if is_match else f"期望[{expected}] 实际[{actual}]",
                )
                sample_result.field_results.append(field_result)

                # 记录命中情况
                if f.standard_name not in field_hit_map:
                    field_hit_map[f.standard_name] = []
                field_hit_map[f.standard_name].append(is_match)

            sample_result.total_fields = total_count
            sample_result.matched_fields = matched_count
            sample_result.hit_rate = matched_count / total_count if total_count > 0 else 1.0
            report.samples.append(sample_result)

            report.total_fields += total_count
            report.total_matched += matched_count

        # 计算总体命中率
        report.overall_hit_rate = (
            report.total_matched / report.total_fields
            if report.total_fields > 0 else 1.0
        )

        # 找出不稳定字段（部分样本命中、部分未命中）
        for fname, hits in field_hit_map.items():
            if len(hits) >= 2 and any(hits) and not all(hits):
                report.unstable_fields.append(fname)

        # 判断是否通过
        report.passed = (
            report.overall_hit_rate >= self.MIN_HIT_RATE
            and len(report.unstable_fields) <= self.MAX_UNSTABLE_FIELDS
        )

        return report

    @staticmethod
    def _fuzzy_equal(expected: str, actual: str) -> bool:
        """
        模糊比较两个值（忽略空格和常见标点差异）

        策略：
        1. 完全相等
        2. 去除空格和标点后相等
        3. actual 包含 expected（或反过来）
        """
        if expected == actual:
            return True

        # 去除空格和标点
        clean_exp = re.sub(r'[\s,，.。、:：;；\-/]', '', expected)
        clean_act = re.sub(r'[\s,，.。、:：;；\-/]', '', actual)

        if clean_exp == clean_act:
            return True

        # 包含关系
        if clean_exp and clean_act:
            if clean_exp in clean_act or clean_act in clean_exp:
                return True

        return False

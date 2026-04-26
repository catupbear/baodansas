#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模板 JSON 生成模块
从学习结果生成模板 dict，用于保存为 JSON 文件。
"""

import time
from typing import Dict, List, Optional

from insurance.learner.text_aligner import AlignmentResult
from insurance.learner.field_discoverer import DiscoveredField
from insurance.learner.pattern_inducer import InducedPattern


class TemplateGenerator:
    """
    模板 JSON 生成器
    从对齐结果、字段发现和正则归纳结果生成模板 dict。
    """

    # 指纹中 must_have 的最大数量
    MAX_MUST_HAVE = 5

    def generate(
        self,
        company_id: str,
        policy_type: str,
        version: str,
        alignment: AlignmentResult,
        fields: List[DiscoveredField],
        patterns: Dict[str, InducedPattern],
    ) -> dict:
        """
        生成模板 dict

        Args:
            company_id: 保司标识
            policy_type: 险种类型
            version: 版本号
            alignment: 对齐结果
            fields: 发现的字段列表
            patterns: 字段名 → InducedPattern 映射

        Returns:
            模板 dict，可直接 json.dumps 保存
        """
        template_id = f"{company_id}/{policy_type}/{version}"

        # 1. 生成指纹
        fingerprint = self._build_fingerprint(alignment)

        # 2. 生成字段 schema
        field_schema = self._build_field_schema(fields, patterns)

        # 3. 规则文件路径（相对路径）
        rule_file = f"rules/{company_id}/{policy_type}_{version}.py"

        return {
            "id": template_id,
            "company_id": company_id,
            "policy_type": policy_type,
            "version": version,
            "status": "draft",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fingerprint": fingerprint,
            "field_schema": field_schema,
            "rule_file": rule_file,
        }

    def _build_fingerprint(self, alignment: AlignmentResult) -> dict:
        """
        构建指纹信息

        策略：
        - 固定行前 5 个作为 must_have（必须出现才匹配此模板）
        - 其余固定行作为 should_have（辅助匹配，提高置信度）

        Args:
            alignment: 对齐结果

        Returns:
            {
                "must_have": ["固定文本1", "固定文本2", ...],
                "should_have": ["固定文本6", "固定文本7", ...],
            }
        """
        # 收集非空固定行
        fixed_lines = [
            line.content
            for line in alignment.lines
            if line.line_type == "fixed" and line.content.strip()
        ]

        # 按内容长度降序排列（更具辨识度的长文本优先）
        fixed_lines.sort(key=lambda x: len(x), reverse=True)

        must_have = fixed_lines[:self.MAX_MUST_HAVE]
        should_have = fixed_lines[self.MAX_MUST_HAVE:]

        return {
            "must_have": must_have,
            "should_have": should_have,
        }

    def _build_field_schema(
        self,
        fields: List[DiscoveredField],
        patterns: Dict[str, InducedPattern],
    ) -> dict:
        """
        构建字段 schema，按层级组织

        Returns:
            {
                "common": {
                    "保单号": {"required": True, "used": True, "label": "保险单号", "pattern": "..."},
                    ...
                },
                "type_specific": {...},
                "company_specific": {...},
            }
        """
        schema: Dict[str, Dict] = {
            "common": {},
            "type_specific": {},
            "company_specific": {},
        }

        # 需要标记为 required 的核心字段
        REQUIRED_FIELDS = {"保单号", "被保险人", "保费合计"}

        for f in fields:
            layer = f.layer if f.layer in schema else "company_specific"
            pat = patterns.get(f.standard_name)

            entry = {
                "required": f.standard_name in REQUIRED_FIELDS,
                "used": True,
                "label": f.original_label.rstrip("：: \t"),
                "source": f.source,
                "confidence": f.confidence,
            }

            if pat:
                entry["pattern"] = pat.pattern
                entry["label_pattern"] = pat.label_pattern
                entry["match_rate"] = pat.match_rate

            # 附带样本值（取前 3 个作参考）
            if f.sample_values:
                entry["sample_values"] = [v for v in f.sample_values if v][:3]

            schema[layer][f.standard_name] = entry

        return schema

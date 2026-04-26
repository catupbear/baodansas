#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模板版本匹配引擎
根据文本指纹在注册表中匹配最合适的保单模板。
"""

import json
import os
import re
import logging
from typing import List, Dict, Optional

from insurance.engine.schema import MatchResult

logger = logging.getLogger(__name__)


class TemplateMatcher:
    """
    从 registry.json 加载模板注册表，
    对输入文本按 company_id + policy_type 筛选候选模板，
    通过指纹权重计算匹配度，返回最佳匹配结果。
    """

    def __init__(self, templates_dir: str):
        """
        Args:
            templates_dir: 模板目录路径，内含 registry.json
        """
        self._templates_dir = templates_dir
        self._registry_path = os.path.join(templates_dir, "registry.json")
        self._templates: List[Dict] = []
        self._version: str = ""
        self.reload()

    def reload(self):
        """重新加载注册表文件"""
        try:
            with open(self._registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._version = data.get("version", "")
            self._templates = data.get("templates", [])
            logger.info("模板注册表已加载，版本=%s，模板数=%d", self._version, len(self._templates))
        except FileNotFoundError:
            logger.warning("模板注册表文件不存在: %s", self._registry_path)
            self._templates = []
        except json.JSONDecodeError as e:
            logger.error("模板注册表 JSON 解析失败: %s", e)
            self._templates = []

    def match(self, text: str, company_id: str, policy_type: str) -> MatchResult:
        """
        对文本进行模板匹配

        Args:
            text: PDF 提取的原始文本
            company_id: 保司标识（如 "picc"）
            policy_type: 险种类型（如 "compulsory"）

        Returns:
            MatchResult: 匹配结果，包含模板、分数和状态
        """
        if not self._templates:
            return MatchResult(status="no_template", warning="注册表为空，无可用模板")

        # 筛选候选模板：company_id + policy_type + status=active
        candidates = [
            t for t in self._templates
            if t.get("company_id") == company_id
            and t.get("policy_type") == policy_type
            and t.get("status", "active") == "active"
        ]

        if not candidates:
            return MatchResult(
                status="no_template",
                warning=f"无匹配模板: company={company_id}, type={policy_type}"
            )

        # 计算每个候选模板的匹配分数
        best_template = None
        best_score = 0.0

        for tmpl in candidates:
            score = self._calc_score(text, tmpl)
            if score > best_score:
                best_score = score
                best_template = tmpl

        # 根据分数判定匹配状态
        if best_score >= 0.8:
            status = "matched"
            warning = ""
        elif best_score >= 0.5:
            status = "partial_match"
            warning = f"模板匹配度偏低: {best_score:.2f}"
        else:
            status = "no_match"
            warning = f"无法匹配到合适模板，最高分: {best_score:.2f}"

        template_id = best_template.get("template_id", "") if best_template else ""

        return MatchResult(
            template=best_template,
            template_id=template_id,
            score=best_score,
            status=status,
            warning=warning,
        )

    def _calc_score(self, text: str, template: dict) -> float:
        """
        计算文本与模板的指纹匹配度

        权重规则：
        - must_have:        每个 0.20（必须出现的关键词）
        - must_not_have:    命中扣 0.30（不应出现的关键词）
        - should_have:      每个 0.10（可选关键词）
        - company_signals:  每个 0.05（保司特征词）
        - policy_no_pattern: 0.15（保单号正则）

        Returns:
            归一化分数 score / total_weight，范围 [0, 1]
        """
        fingerprint = template.get("fingerprint", {})

        score = 0.0
        total_weight = 0.0

        # --- must_have: 每个权重 0.2 ---
        must_have = fingerprint.get("must_have", [])
        for keyword in must_have:
            total_weight += 0.2
            if keyword in text:
                score += 0.2

        # --- must_not_have: 命中扣 0.3 ---
        must_not_have = fingerprint.get("must_not_have", [])
        for keyword in must_not_have:
            total_weight += 0.3
            if keyword not in text:
                # 未出现则得分（排除性指纹匹配成功）
                score += 0.3
            # 命中则不加分（相当于扣 0.3）

        # --- should_have: 每个权重 0.1 ---
        should_have = fingerprint.get("should_have", [])
        for keyword in should_have:
            total_weight += 0.1
            if keyword in text:
                score += 0.1

        # --- company_signals: 每个权重 0.05 ---
        company_signals = fingerprint.get("company_signals", [])
        for signal in company_signals:
            total_weight += 0.05
            if signal in text:
                score += 0.05

        # --- policy_no_pattern: 权重 0.15 ---
        policy_no_pattern = fingerprint.get("policy_no_pattern", "")
        if policy_no_pattern:
            total_weight += 0.15
            try:
                if re.search(policy_no_pattern, text):
                    score += 0.15
            except re.error as e:
                logger.warning("保单号正则无效: %s, 错误: %s", policy_no_pattern, e)

        # 避免除零
        if total_weight == 0:
            return 0.0

        return score / total_weight

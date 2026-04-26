#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练器主入口模块
串联对齐、发现、归纳、生成、验证全流程。
"""

import json
import os
import types
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from insurance.learner.text_aligner import TextAligner, AlignmentResult
from insurance.learner.field_discoverer import FieldDiscoverer, DiscoveredField
from insurance.learner.pattern_inducer import PatternInducer, InducedPattern
from insurance.learner.template_generator import TemplateGenerator
from insurance.learner.rule_generator import RuleGenerator
from insurance.learner.self_validator import SelfValidator, ValidationReport
from insurance.rules.base_rule import BaseRule

logger = logging.getLogger(__name__)


@dataclass
class TrainResult:
    """训练结果"""
    success: bool = False
    error: str = ""
    template: Optional[dict] = None            # 模板 JSON dict
    rule_code: str = ""                        # 生成的规则 Python 代码
    validation: Optional[ValidationReport] = None  # 验证报告
    needs_review: bool = False                 # 是否需要人工审核
    fields: List[DiscoveredField] = field(default_factory=list)
    patterns: Dict[str, InducedPattern] = field(default_factory=dict)
    alignment: Optional[AlignmentResult] = None


class TemplateTrainer:
    """
    模板学习训练器
    将 N 份样本 PDF 送入全流程，输出模板 JSON + 规则代码 + 验证报告。
    """

    def __init__(self):
        self.aligner = TextAligner()
        self.discoverer = FieldDiscoverer()
        self.inducer = PatternInducer()
        self.template_gen = TemplateGenerator()
        self.rule_gen = RuleGenerator()
        self.validator = SelfValidator()

    def train(
        self,
        pdf_bytes_list: List[bytes],
        company_id: str,
        policy_type: str,
        version: str = "v1",
        min_samples: int = 2,
    ) -> TrainResult:
        """
        从 PDF 字节列表训练

        Args:
            pdf_bytes_list: N 份 PDF 的原始字节数据
            company_id: 保司标识
            policy_type: 险种类型
            version: 版本号
            min_samples: 最少样本数

        Returns:
            TrainResult
        """
        if len(pdf_bytes_list) < min_samples:
            return TrainResult(
                success=False,
                error=f"样本数不足，需要至少 {min_samples} 份，当前 {len(pdf_bytes_list)} 份",
            )

        # Step 1: pdfplumber 提取文本
        try:
            from insurance.ocr.pdfplumber_engine import PdfplumberEngine
            engine = PdfplumberEngine()
            texts = []
            for i, pdf_bytes in enumerate(pdf_bytes_list):
                result = engine.extract(pdf_bytes)
                if not result.success or not result.text:
                    return TrainResult(
                        success=False,
                        error=f"第 {i + 1} 份 PDF 文本提取失败: {result.error}",
                    )
                texts.append(result.text)
        except Exception as e:
            return TrainResult(success=False, error=f"OCR 提取异常: {e}")

        # 后续步骤委托给 train_from_texts
        return self.train_from_texts(
            texts=texts,
            company_id=company_id,
            policy_type=policy_type,
            version=version,
        )

    def train_from_texts(
        self,
        texts: List[str],
        company_id: str,
        policy_type: str,
        version: str = "v1",
    ) -> TrainResult:
        """
        从已提取的文本列表训练（跳过 OCR）

        Args:
            texts: N 份 PDF 提取的文本列表
            company_id: 保司标识
            policy_type: 险种类型
            version: 版本号

        Returns:
            TrainResult
        """
        try:
            # Step 2: 多文本结构对齐
            logger.info(f"[训练] 开始对齐 {len(texts)} 份文本...")
            alignment = self.aligner.align(texts)
            logger.info(
                f"[训练] 对齐完成: 固定行={alignment.fixed_lines}, "
                f"标签值行={alignment.label_value_lines}, "
                f"变化行={alignment.variable_lines}"
            )

            # Step 3: 字段自动发现
            logger.info("[训练] 开始字段发现...")
            fields = self.discoverer.discover(alignment)
            logger.info(f"[训练] 发现 {len(fields)} 个字段")

            if not fields:
                return TrainResult(
                    success=False,
                    error="未发现任何字段，请检查样本质量",
                    alignment=alignment,
                )

            # Step 4: 对每个字段归纳正则模式
            logger.info("[训练] 开始正则归纳...")
            patterns: Dict[str, InducedPattern] = {}
            for f in fields:
                pat = self.inducer.induce(f)
                patterns[f.standard_name] = pat

            # Step 5: 生成模板 JSON + 规则代码
            logger.info("[训练] 生成模板和规则代码...")
            template = self.template_gen.generate(
                company_id=company_id,
                policy_type=policy_type,
                version=version,
                alignment=alignment,
                fields=fields,
                patterns=patterns,
            )

            rule_code = self.rule_gen.generate(
                company_id=company_id,
                policy_type=policy_type,
                version=version,
                fields=fields,
                patterns=patterns,
            )

            # Step 6: 动态加载规则 → 自验证
            logger.info("[训练] 开始自验证...")
            validation = None
            needs_review = False

            try:
                rule_instance = self._load_rule_from_code(rule_code, policy_type)
                validation = self.validator.validate(rule_instance, texts, fields)
                logger.info(f"[训练] 验证结果: {validation.summary}")

                if not validation.passed:
                    needs_review = True
                    logger.warning(f"[训练] 验证未通过，需要人工审核")
            except Exception as e:
                logger.warning(f"[训练] 自验证异常: {e}")
                needs_review = True
                validation = None

            return TrainResult(
                success=True,
                template=template,
                rule_code=rule_code,
                validation=validation,
                needs_review=needs_review,
                fields=fields,
                patterns=patterns,
                alignment=alignment,
            )

        except Exception as e:
            logger.error(f"[训练] 训练异常: {e}", exc_info=True)
            return TrainResult(success=False, error=f"训练异常: {e}")

    def _load_rule_from_code(self, code: str, policy_type: str) -> BaseRule:
        """
        动态加载生成的规则代码，返回规则实例

        Args:
            code: Python 代码字符串
            policy_type: 险种类型（用于确定基类导入）

        Returns:
            BaseRule 子类实例
        """
        # 创建临时模块执行代码
        temp_module = types.ModuleType("_temp_rule_module")
        temp_module.__dict__["__builtins__"] = __builtins__

        # 执行代码
        exec(code, temp_module.__dict__)

        # 从模块中找到 BaseRule 的子类
        rule_class = None
        for name, obj in temp_module.__dict__.items():
            if (isinstance(obj, type)
                    and issubclass(obj, BaseRule)
                    and obj is not BaseRule
                    and hasattr(obj, 'company_id')
                    and obj.company_id):
                rule_class = obj
                break

        if not rule_class:
            raise ValueError("未在生成的代码中找到有效的规则类")

        return rule_class()


def save_training_result(
    templates_dir: str,
    rules_dir: str,
    result: TrainResult,
    company_id: str,
    policy_type: str,
    version: str,
) -> dict:
    """
    保存训练结果到文件系统

    Args:
        templates_dir: 模板根目录，如 "insurance/templates"
        rules_dir: 规则根目录，如 "insurance/rules"
        result: 训练结果
        company_id: 保司标识
        policy_type: 险种类型
        version: 版本号

    Returns:
        {
            "template_path": "...",
            "rule_path": "...",
            "registry_updated": True/False,
        }
    """
    saved = {
        "template_path": "",
        "rule_path": "",
        "registry_updated": False,
    }

    if not result.success or not result.template or not result.rule_code:
        logger.warning("[保存] 训练结果无效，跳过保存")
        return saved

    # 1. 保存模板 JSON
    template_dir = os.path.join(templates_dir, company_id, policy_type)
    os.makedirs(template_dir, exist_ok=True)
    template_path = os.path.join(template_dir, f"{version}.json")

    with open(template_path, "w", encoding="utf-8") as f:
        json.dump(result.template, f, ensure_ascii=False, indent=2)
    saved["template_path"] = template_path
    logger.info(f"[保存] 模板已保存: {template_path}")

    # 2. 保存规则 .py
    rule_dir = os.path.join(rules_dir, company_id)
    os.makedirs(rule_dir, exist_ok=True)
    rule_path = os.path.join(rule_dir, f"{policy_type}_{version}.py")

    with open(rule_path, "w", encoding="utf-8") as f:
        f.write(result.rule_code)
    saved["rule_path"] = rule_path
    logger.info(f"[保存] 规则已保存: {rule_path}")

    # 3. 更新 registry.json
    try:
        registry_path = os.path.join(templates_dir, "registry.json")
        registry = {}
        if os.path.exists(registry_path):
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)

        template_id = f"{company_id}/{policy_type}/{version}"
        registry[template_id] = {
            "template_path": os.path.relpath(template_path, templates_dir),
            "rule_path": os.path.relpath(rule_path, rules_dir),
            "status": "review" if result.needs_review else "active",
            "validation_passed": result.validation.passed if result.validation else False,
            "hit_rate": result.validation.overall_hit_rate if result.validation else 0.0,
        }

        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        saved["registry_updated"] = True
        logger.info(f"[保存] registry.json 已更新: {template_id}")
    except Exception as e:
        logger.warning(f"[保存] registry.json 更新失败: {e}")

    return saved

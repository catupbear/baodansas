#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构校验 + 变动检测
对提取的字段进行必填校验、匹配度告警和新字段检测。
"""

import logging
from typing import Dict, List, Set

from insurance.engine.schema import Alert, ValidationResult

logger = logging.getLogger(__name__)


class StructureValidator:
    """
    结构校验器
    - 检查必填字段是否缺失
    - 检测模板匹配度是否偏低
    - 发现模板未定义的新字段
    """

    def validate(
        self,
        extracted_fields: dict,
        template: dict,
        match_score: float,
    ) -> ValidationResult:
        """
        对提取结果进行结构校验

        Args:
            extracted_fields: extract_all() 的输出，包含 common / type_specific / company_specific 三层
            template: 匹配到的模板 JSON（可能为 None）
            match_score: 模板匹配分数

        Returns:
            ValidationResult: 校验结果，包含 is_valid 和 alerts 列表
        """
        alerts: List[Alert] = []
        missing: List[str] = []

        template_id = template.get("template_id", "") if template else ""

        # ==============================
        # 1. 必填字段缺失检查
        # ==============================
        if template:
            field_schema = template.get("field_schema", {})
            # 遍历三层：common / type_specific / company_specific
            for layer_name, layer_schema in field_schema.items():
                if not isinstance(layer_schema, dict):
                    continue

                # 获取该层实际提取到的字段
                actual_layer = extracted_fields.get(layer_name, {})

                for field_name, field_config in layer_schema.items():
                    if not isinstance(field_config, dict):
                        continue
                    # 检查 required=True 的字段
                    if field_config.get("required", False):
                        if field_name not in actual_layer or not actual_layer[field_name]:
                            missing.append(f"{layer_name}.{field_name}")

            if missing:
                alerts.append(Alert(
                    level="warning",
                    alert_type="missing_required_fields",
                    message=f"缺失必填字段: {', '.join(missing)}",
                    template_id=template_id,
                ))
                logger.warning("必填字段缺失: %s (模板=%s)", missing, template_id)

        # ==============================
        # 2. 模板匹配度偏低告警
        # ==============================
        if match_score > 0 and match_score < 0.8:
            alerts.append(Alert(
                level="warning",
                alert_type="low_match_score",
                message=f"模板匹配度偏低: {match_score:.2f}，提取结果可能不准确",
                template_id=template_id,
            ))

        # ==============================
        # 3. 新字段出现检测
        # ==============================
        if template:
            field_schema = template.get("field_schema", {})
            for layer_name in ("common", "type_specific", "company_specific"):
                schema_fields: Set[str] = set()
                layer_schema = field_schema.get(layer_name, {})
                if isinstance(layer_schema, dict):
                    schema_fields = set(layer_schema.keys())

                actual_layer = extracted_fields.get(layer_name, {})
                if isinstance(actual_layer, dict):
                    actual_fields = set(actual_layer.keys())
                    new_fields = actual_fields - schema_fields

                    if new_fields:
                        alerts.append(Alert(
                            level="info",
                            alert_type="new_fields_detected",
                            message=f"{layer_name} 层出现模板未定义的新字段: {', '.join(sorted(new_fields))}",
                            template_id=template_id,
                        ))
                        logger.info(
                            "检测到新字段: layer=%s, fields=%s (模板=%s)",
                            layer_name, new_fields, template_id,
                        )

        # 必填字段全部存在 → is_valid=True
        is_valid = len(missing) == 0

        return ValidationResult(is_valid=is_valid, alerts=alerts)

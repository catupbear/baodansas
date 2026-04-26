"""OCR 交叉比对引擎 — 多引擎结果逐字段对比与投票合并"""

import logging
from collections import Counter
from typing import Callable, Dict, List

from insurance.engine.schema import CompareReport, FieldDiff
from .base import OCRResult

logger = logging.getLogger(__name__)

# 关键字段列表，差异时标记为 critical
CRITICAL_FIELDS = [
    "保单号", "被保险人", "投保人", "车牌号", "保费合计", "保险起期", "保险止期",
]


class OCRComparator:
    """
    OCR 交叉比对器

    对多个 OCR 引擎的结果分别执行字段提取，逐字段对比并投票合并。
    """

    def compare(
        self,
        ocr_results: List[OCRResult],
        extract_fn: Callable[[str], Dict[str, Dict]],
    ) -> CompareReport:
        """
        交叉比对多引擎 OCR 结果

        Args:
            ocr_results: 多个引擎的 OCRResult 列表
            extract_fn: 字段提取函数，签名 (text) -> dict，返回三层字段结构
                        例如 {"common": {"保单号": "xxx"}, "type_specific": {...}, "company_specific": {...}}

        Returns:
            CompareReport 比对报告
        """
        # 只保留成功的结果
        valid_results = [r for r in ocr_results if r.success and r.text]

        if not valid_results:
            logger.warning("无有效 OCR 结果可供比对")
            return CompareReport(engine_count=0)

        # 单引擎直接返回，无需比对
        if len(valid_results) == 1:
            r = valid_results[0]
            fields = extract_fn(r.text)
            return CompareReport(
                merged_fields=fields,
                cross_validated=False,
                engine_count=1,
                source_engine=r.engine,
            )

        # 多引擎：分别提取字段
        engine_fields: Dict[str, Dict[str, Dict]] = {}
        engine_confidence: Dict[str, float] = {}
        for r in valid_results:
            try:
                fields = extract_fn(r.text)
                engine_fields[r.engine] = fields
                engine_confidence[r.engine] = r.confidence
            except Exception as e:
                logger.warning("引擎 %s 字段提取失败: %s", r.engine, e)

        if not engine_fields:
            return CompareReport(engine_count=len(valid_results))

        # 收集所有层和字段名
        all_layers = set()
        for fields in engine_fields.values():
            all_layers.update(fields.keys())

        # 逐字段对比
        diffs: List[FieldDiff] = []
        merged_fields: Dict[str, Dict] = {}

        for layer in sorted(all_layers):
            merged_fields[layer] = {}

            # 收集该层所有字段名
            field_names = set()
            for fields in engine_fields.values():
                layer_data = fields.get(layer, {})
                field_names.update(layer_data.keys())

            for field_name in sorted(field_names):
                # 收集各引擎对该字段的值
                values: Dict[str, str] = {}
                for engine, fields in engine_fields.items():
                    val = fields.get(layer, {}).get(field_name, "")
                    if val:  # 只统计非空值
                        values[engine] = str(val)

                if not values:
                    continue

                unique_values = set(values.values())
                is_critical = field_name in CRITICAL_FIELDS

                if len(unique_values) == 1:
                    # 所有引擎一致
                    merged_fields[layer][field_name] = list(values.values())[0]
                else:
                    # 存在差异 → 投票合并
                    resolved_value, method = self._resolve(values, engine_confidence)
                    merged_fields[layer][field_name] = resolved_value

                    diff = FieldDiff(
                        field_name=field_name,
                        layer=layer,
                        values=values,
                        is_critical=is_critical,
                        resolved_value=resolved_value,
                        resolution_method=method,
                    )
                    diffs.append(diff)

                    level = "CRITICAL" if is_critical else "INFO"
                    logger.info(
                        "[%s] 字段差异 %s.%s: %s -> 采用 %s (%s)",
                        level, layer, field_name, values, resolved_value, method,
                    )

        return CompareReport(
            merged_fields=merged_fields,
            cross_validated=True,
            diffs=diffs,
            engine_count=len(engine_fields),
            source_engine=",".join(engine_fields.keys()),
        )

    @staticmethod
    def _resolve(
        values: Dict[str, str],
        engine_confidence: Dict[str, float],
    ) -> tuple:
        """
        投票合并：多数一致 → 选多数；否则按置信度选

        Args:
            values: {engine_name: field_value}
            engine_confidence: {engine_name: confidence}

        Returns:
            (resolved_value, resolution_method)
        """
        # 统计每个值出现的次数
        value_counter = Counter(values.values())
        most_common_value, most_common_count = value_counter.most_common(1)[0]

        # 多数一致（出现次数 > 1 或只有一个唯一值）
        if most_common_count > 1:
            return most_common_value, "vote"

        # 无多数一致，按置信度选
        best_engine = max(
            values.keys(),
            key=lambda eng: engine_confidence.get(eng, 0.0),
        )
        return values[best_engine], "priority"

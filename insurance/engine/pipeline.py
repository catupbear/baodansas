#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保单识别主流水线 — 串联 6 个 Stage

Stage 1: 保司识别 (CompanyIdentifier)
Stage 2: 险种识别 (TypeIdentifier)
Stage 3: 模板匹配 (TemplateMatcher)
Stage 4: 字段提取 (FieldExtractor + BaseRule / OCRComparator)
Stage 5: 结构校验 (StructureValidator) + 告警通知
Stage 6: 字段映射 (apply_mapping)
"""

import logging
import os
import re
from typing import Dict, List, Optional

from insurance.engine.schema import (
    Alert,
    CompareReport,
    FieldDiff,
    PipelineResult,
)
from insurance.engine.company_identifier import CompanyIdentifier
from insurance.engine.type_identifier import TypeIdentifier
from insurance.engine.template_matcher import TemplateMatcher
from insurance.engine.field_extractor import FieldExtractor
from insurance.engine.structure_validator import StructureValidator
from insurance.ocr.dispatcher import OCRDispatcher
from insurance.ocr.comparator import OCRComparator
from insurance.alerts.notifier import AlertNotifier
from insurance.alerts.collector import LowMatchCollector
from insurance.field_mapping import apply_mapping

logger = logging.getLogger(__name__)

# 置信度计算用的关键字段列表
CONFIDENCE_KEY_FIELDS = ["保单号", "被保险人", "车牌号", "保费合计", "保险起期"]


class InsurancePipeline:
    """保单识别主流水线，串联识别 → 匹配 → 提取 → 校验 → 映射全流程"""

    def __init__(self, db=None, ocr_engines=None, alert_webhook: str = ""):
        """
        初始化流水线所有组件

        Args:
            db: 数据库连接（用于告警记录和低匹配样本收集）
            ocr_engines: 云 OCR 引擎列表（传给 OCRDispatcher）
            alert_webhook: 钉钉告警 webhook 地址
        """
        # 模板目录：insurance/templates/ 的绝对路径
        self._templates_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
        )

        # 初始化各 Stage 组件
        self._company_identifier = CompanyIdentifier()
        self._type_identifier = TypeIdentifier()
        self._template_matcher = TemplateMatcher(self._templates_dir)
        self._field_extractor = FieldExtractor()
        self._structure_validator = StructureValidator()
        self._ocr_dispatcher = OCRDispatcher(engines=ocr_engines)
        self._ocr_comparator = OCRComparator()
        self._alert_notifier = AlertNotifier(db=db, dingtalk_webhook=alert_webhook)
        self._low_match_collector = LowMatchCollector(db=db) if db else None

    def process_text(self, text: str, filename: str = "") -> PipelineResult:
        """
        处理已有文本（非 PDF 入口）

        Args:
            text: 保单 OCR 文本
            filename: 文件名（用于日志和告警上下文）

        Returns:
            PipelineResult 流水线输出
        """
        return self._process(text, filename=filename, ocr_engines=["text_input"])

    def process_pdf(self, pdf_bytes: bytes, filename: str = "") -> PipelineResult:
        """
        处理 PDF 文件

        Args:
            pdf_bytes: PDF 原始字节
            filename: 文件名

        Returns:
            PipelineResult 流水线输出
        """
        # 1. OCR 调度：提取文本
        ocr_results = self._ocr_dispatcher.dispatch(pdf_bytes, page=1)
        logger.info("OCR 调度完成，结果数=%d, 文件=%s", len(ocr_results), filename)

        # 2. 获取主文本（第一个成功的 OCR 结果）
        main_text = ""
        for r in ocr_results:
            if r.success and r.text:
                main_text = r.text
                break

        if not main_text:
            logger.warning("所有 OCR 引擎均未提取到文本: %s", filename)
            return PipelineResult(
                doc_category="其他",
                ocr_engines=[r.engine for r in ocr_results],
                raw_text="",
            )

        # 3. 判断是否扫描件（多个成功结果 = 扫描件，需要交叉比对）
        successful_results = [r for r in ocr_results if r.success and r.text]
        ocr_engine_names = [r.engine for r in ocr_results]

        # 4. 调用核心处理流程
        return self._process(
            main_text,
            filename=filename,
            ocr_results=successful_results if len(successful_results) > 1 else None,
            ocr_engines=ocr_engine_names,
        )

    def _process(
        self,
        text: str,
        filename: str = "",
        ocr_results=None,
        ocr_engines: Optional[List[str]] = None,
    ) -> PipelineResult:
        """
        核心处理流程，串联 6 个 Stage

        Args:
            text: 保单文本
            filename: 文件名
            ocr_results: 多引擎 OCR 结果列表（扫描件场景），None 表示文本层
            ocr_engines: 使用的 OCR 引擎名称列表

        Returns:
            PipelineResult
        """
        result = PipelineResult(
            raw_text=text,
            ocr_engines=ocr_engines or [],
        )

        # ============================
        # Stage 1: 保司识别
        # ============================
        company = self._company_identifier.identify(text)
        result.company = company
        logger.info(
            "Stage 1 保司识别: id=%s, name=%s, short=%s",
            company.company_id, company.company_name, company.company_short,
        )

        # ============================
        # Stage 2: 险种识别
        # ============================
        policy_type = self._type_identifier.identify(text)
        result.policy_type = policy_type
        logger.info(
            "Stage 2 险种识别: code=%s, name=%s",
            policy_type.type_code, policy_type.type_name,
        )

        # ============================
        # Stage 3: 模板匹配
        # ============================
        match_result = self._template_matcher.match(
            text, company.company_id, policy_type.type_code,
        )
        result.template_id = match_result.template_id
        result.match_score = match_result.score
        logger.info(
            "Stage 3 模板匹配: template_id=%s, score=%.2f, status=%s",
            match_result.template_id, match_result.score, match_result.status,
        )

        # 加载提取规则：有模板用专属规则，否则用险种基础规则
        rule = None
        if match_result.template and match_result.status in ("matched", "partial_match"):
            rule_file = match_result.template.get("rule_file", "")
            if rule_file:
                rule = self._field_extractor.load_rule(rule_file)
                logger.info("使用模板专属规则: %s", rule_file)
        if rule is None:
            rule = self._field_extractor.load_base_rule(policy_type.type_code)
            logger.info("使用险种基础规则: %s", policy_type.type_code)

        # ============================
        # Stage 4: 字段提取
        # ============================
        compare_report = None

        if ocr_results and len(ocr_results) > 1:
            # 扫描件：多引擎交叉比对
            compare_report = self._ocr_comparator.compare(
                ocr_results,
                extract_fn=lambda t: rule.extract_all(t),
            )
            fields = compare_report.merged_fields
            result.ocr_diffs = compare_report.diffs
            logger.info(
                "Stage 4 字段提取(交叉比对): engines=%d, diffs=%d, critical=%s",
                compare_report.engine_count,
                len(compare_report.diffs),
                compare_report.has_critical_diff,
            )
        else:
            # 文本层：直接提取
            fields = rule.extract_all(text)
            logger.info("Stage 4 字段提取(文本层): common=%d字段", len(fields.get("common", {})))

        result.fields = fields
        result.insurance_items = fields.get("insurance_items", [])

        # ============================
        # Stage 5: 结构校验 + 告警通知
        # ============================
        # 告警上下文信息
        alert_context = {
            "company_name": company.company_name,
            "company_short": company.company_short,
            "policy_type": policy_type.type_name,
            "filename": filename,
            "match_score": match_result.score,
        }

        # 有模板时进行结构校验
        if match_result.template:
            validation = self._structure_validator.validate(
                fields, match_result.template, match_result.score,
            )
            result.validation = validation

            # 发送校验产生的告警
            for alert in validation.alerts:
                self._alert_notifier.notify(alert, alert_context)
        else:
            # 无模板时，如果匹配度偏低也发告警
            if match_result.warning:
                alert = Alert(
                    level="info",
                    alert_type="no_template",
                    message=match_result.warning,
                    template_id="",
                )
                self._alert_notifier.notify(alert, alert_context)

        # OCR 关键字段差异告警
        if compare_report and compare_report.has_critical_diff:
            critical_diffs = [d for d in compare_report.diffs if d.is_critical]
            diff_details = "; ".join(
                f"{d.field_name}: {d.values}" for d in critical_diffs
            )
            alert = Alert(
                level="warning",
                alert_type="ocr_critical_diff",
                message=f"OCR 关键字段差异: {diff_details}",
                template_id=match_result.template_id,
            )
            self._alert_notifier.notify(alert, alert_context)

        # 低匹配样本收集
        if self._low_match_collector and company.company_id:
            collect_result = self._low_match_collector.collect(
                company_id=company.company_id,
                policy_type=policy_type.type_code,
                text=text,
                match_score=match_result.score,
            )
            if collect_result and collect_result.get("ready"):
                logger.info(
                    "低匹配样本达到训练条件: company=%s, type=%s, count=%d",
                    collect_result["company_id"],
                    collect_result["policy_type"],
                    collect_result["sample_count"],
                )

        # ============================
        # 文档分类
        # ============================
        result.doc_category = self._identify_doc_category(text, fields)

        # ============================
        # 置信度计算（5 个关键字段命中率）
        # ============================
        common_fields = fields.get("common", {})
        hit_count = sum(1 for f in CONFIDENCE_KEY_FIELDS if common_fields.get(f))
        result.confidence = hit_count / len(CONFIDENCE_KEY_FIELDS)

        # ============================
        # Stage 6: 字段映射
        # ============================
        # 展平三层字段为一维 dict
        flat_fields: Dict[str, str] = {}
        for layer_name in ("common", "type_specific", "company_specific"):
            layer_data = fields.get(layer_name, {})
            if isinstance(layer_data, dict):
                for k, v in layer_data.items():
                    if v and k not in flat_fields:
                        flat_fields[k] = str(v)

        # 加入保司和险种信息
        if company.company_name:
            flat_fields["保险公司"] = company.company_name
        if company.company_short:
            flat_fields["保险公司简称"] = company.company_short
        if policy_type.type_name and policy_type.type_name != "未知":
            flat_fields["险种类型"] = policy_type.type_name

        # 应用字段映射 → 导出列格式
        result.mapped_fields = apply_mapping(flat_fields, company.company_short)

        logger.info(
            "Pipeline 完成: company=%s, type=%s, category=%s, confidence=%.1f%%, mapped=%d字段",
            company.company_short or "未知",
            policy_type.type_name,
            result.doc_category,
            result.confidence * 100,
            len([v for v in result.mapped_fields.values() if v]),
        )

        return result

    def _identify_doc_category(self, text: str, fields: dict) -> str:
        """
        文档分类规则（从 policy_parser.py 迁移）

        分类结果：
        - "电子标志"：交强险电子标志/贴纸
        - "发票"：保险发票
        - "缴款通知"：缴费通知书
        - "条款"：保险条款文件
        - "其他"：无法识别的文件（如 CID 编码的扫描件）
        - "保单"：正式保单文件（含投保单、批单）
        """
        text_head = text[:500]
        text_len = len(text)

        # 电子标志：文本极短（<500字）+ 包含"保险人签章" → 仅有标志信息
        if text_len < 500 and "保险人签章" in text:
            return "电子标志"

        # 交强险标志：包含"此标志粘贴在机动车前窗"等特征
        if "此标志粘贴在机动车前窗" in text or "此标志正面的年份" in text:
            return "电子标志"

        # 发票
        if "电子发票" in text_head or "发票号码" in text_head:
            return "发票"

        # 缴款通知
        if "缴款通知书" in text_head:
            return "缴款通知"

        # 条款/免责说明
        if any(x in text_head for x in ["免责事项说明书", "投保人声明", "保险示范条款"]):
            return "条款"
        if re.search(r"示范条款[（(]\d{4}版[)）]", text_head):
            return "条款"
        if (re.search(r"保险条款[（(](?:\d{4}版|条款编码)", text_head)
                and "保单号" not in text_head
                and "保险单号" not in text_head):
            return "条款"
        # 有"总则"+"第一条"但无保单号/被保险人等关键字段
        if ("总则" in text_head and "第一条" in text_head
                and not any(x in text_head for x in ["保险单号", "保单号", "被保险人", "投保人"])):
            return "条款"

        # CID 编码（PDF 无法正常提取文字）
        if "(cid:" in text.lower()[:200]:
            return "其他"

        # 默认：保单（含投保单、批单）
        return "保单"

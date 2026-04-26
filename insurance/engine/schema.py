"""保单识别引擎数据类型定义"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class CompanyResult:
    """保司识别结果"""
    company_id: str = ""           # 如 "picc"
    company_name: str = ""         # 全称
    company_short: str = ""        # 简称
    raw_match: str = ""            # 匹配到的原始文本


@dataclass
class PolicyTypeResult:
    """险种识别结果"""
    type_code: str = "unknown"     # compulsory / commercial / accident / unknown
    type_name: str = "未知"        # 交强险 / 商业险 / 驾乘/意外险
    raw_match: str = ""            # 匹配到的原始文本


@dataclass
class MatchResult:
    """模板匹配结果"""
    template: Optional[Dict] = None   # 匹配到的模板 JSON
    template_id: str = ""             # 如 "picc/compulsory/v1"
    score: float = 0.0
    status: str = "no_template"       # matched / partial_match / no_match / no_template
    warning: str = ""


@dataclass
class Alert:
    """告警"""
    level: str = "info"           # info / warning / critical
    alert_type: str = ""          # missing_required_fields / low_match_score / ocr_critical_diff / ...
    message: str = ""
    template_id: str = ""


@dataclass
class ValidationResult:
    """结构校验结果"""
    is_valid: bool = True
    alerts: List[Alert] = field(default_factory=list)


@dataclass
class FieldDiff:
    """OCR 字段差异"""
    field_name: str = ""
    layer: str = ""               # common / type_specific / company_specific
    values: Dict[str, str] = field(default_factory=dict)  # {engine: value}
    is_critical: bool = False
    resolved_value: str = ""
    resolution_method: str = ""   # vote / priority


@dataclass
class CompareReport:
    """OCR 交叉比对报告"""
    merged_fields: Dict[str, Dict] = field(default_factory=dict)
    cross_validated: bool = False
    diffs: List[FieldDiff] = field(default_factory=list)
    engine_count: int = 1
    source_engine: str = ""

    @property
    def has_critical_diff(self) -> bool:
        return any(d.is_critical for d in self.diffs)


@dataclass
class PipelineResult:
    """流水线最终输出"""
    company: CompanyResult = field(default_factory=CompanyResult)
    policy_type: PolicyTypeResult = field(default_factory=PolicyTypeResult)
    template_id: str = ""
    match_score: float = 0.0
    fields: Dict[str, Dict] = field(default_factory=dict)       # 三层字段
    mapped_fields: Dict[str, str] = field(default_factory=dict)  # 导出列
    validation: ValidationResult = field(default_factory=ValidationResult)
    ocr_diffs: List[FieldDiff] = field(default_factory=list)
    doc_category: str = "保单"
    confidence: float = 0.0
    ocr_engines: List[str] = field(default_factory=list)
    insurance_items: List[Dict] = field(default_factory=list)
    raw_text: str = ""

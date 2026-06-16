#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字段映射配置管理
支持按保司配置 OCR提取字段 → 导出列 的对应关系
"""

import json
from pathlib import Path
from typing import Dict, Any, List, Optional

MAPPING_PATH = Path(__file__).parent / "config" / "field_mapping.json"

# 固定导出模板列（顺序固定）
OUTPUT_COLUMNS = [
    "承保公司", "保单号", "险种", "车牌", "投保人", "被保人", "车主",
    "被保险人身份证号码", "投保人身份证号码", "车架号",
    "签单日期", "起保日期", "终保日期", "保费",
    "佣金率", "佣金", "业务员", "采购费率", "公司利润",
    "跟单人", "跟单人利润", "出单渠道", "车船税",
    "保司公司名称", "保司地址", "保司（带地区）", "保司联系电话",
    "交强到期时间",
]

# 默认映射：OCR提取的字段名 → 导出列名
# 适用于大部分保司
DEFAULT_MAPPING = {
    "保险公司": "承保公司",
    "保险公司简称": "",         # 不导出，仅用于匹配保司规则
    "保单号": "保单号",
    "险种类型": "险种",
    "车牌号": "车牌",
    "投保人": "投保人",
    "被保险人": "被保人",
    "车主": "车主",
    "签单日期": "签单日期",
    "保险起期": "起保日期",
    "保险止期": "终保日期",
    "保费合计": "保费",
    "车船税": "车船税",
    "业务员": "业务员",
    "被保险人身份证号码": "被保险人身份证号码",
    "投保人身份证号码": "投保人身份证号码",
    "车架号VIN": "车架号",
    "保司公司名称": "保司公司名称",
    "保司地址": "保司地址",
    "保司带地区": "保司（带地区）",
    "保司联系电话": "保司联系电话",
    # 以下字段OCR通常提取不到，需手动填写或留空
    # "": "佣金率",
    # "": "佣金",
    # "": "采购费率",
    # "": "公司利润",
    # "": "跟单人",
    # "": "跟单人利润",
    # "": "出单渠道",
}


def _default_config() -> Dict[str, Any]:
    """生成默认配置"""
    return {
        "output_columns": OUTPUT_COLUMNS,
        "default_mapping": DEFAULT_MAPPING,
        "company_mappings": {
            # 示例：按保司简称配置差异化映射
            # "人保PICC": { "保险公司": "承保公司", ... },
        },
    }


def load_mapping_config() -> Dict[str, Any]:
    """加载映射配置，自动补充新增的默认映射字段"""
    if not MAPPING_PATH.exists():
        config = _default_config()
        save_mapping_config(config)
        return config
    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    # 自动补充 DEFAULT_MAPPING 中新增的字段（不覆盖已有配置）
    saved_mapping = config.get("default_mapping", {})
    updated = False
    for ocr_field, out_col in DEFAULT_MAPPING.items():
        if ocr_field not in saved_mapping:
            saved_mapping[ocr_field] = out_col
            updated = True
    if updated:
        config["default_mapping"] = saved_mapping
        save_mapping_config(config)
    return config


def save_mapping_config(config: Dict[str, Any]):
    """保存映射配置"""
    with open(MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_mapping_for_company(company_short: str) -> Dict[str, str]:
    """
    获取指定保司的字段映射

    Args:
        company_short: 保险公司简称（如"人保PICC"、"平安"等）

    Returns:
        OCR字段名 → 导出列名 的映射字典
    """
    config = load_mapping_config()
    # 优先用保司专属映射，没有则用默认映射
    if company_short and company_short in config.get("company_mappings", {}):
        # 合并：默认映射为基础，保司映射覆盖
        mapping = dict(config["default_mapping"])
        mapping.update(config["company_mappings"][company_short])
        return mapping
    return dict(config["default_mapping"])


def apply_mapping(fields: Dict[str, str], company_short: str = "") -> Dict[str, str]:
    """
    将OCR提取的字段按映射规则转换为导出列格式

    Args:
        fields: OCR提取的原始字段 {"保险公司": "xxx", "保单号": "xxx", ...}
        company_short: 保险公司简称

    Returns:
        导出列格式 {"承保公司": "xxx", "保单号": "xxx", ...}
    """
    config = load_mapping_config()
    output_columns = config.get("output_columns", OUTPUT_COLUMNS)
    mapping = get_mapping_for_company(company_short)

    # 构建反向映射：导出列名 → OCR字段名（可能多个OCR字段映射到同一导出列）
    reverse_map = {}
    for ocr_field, out_col in mapping.items():
        if out_col:  # 跳过空映射
            if out_col not in reverse_map:
                reverse_map[out_col] = []
            reverse_map[out_col].append(ocr_field)

    # 按导出列顺序生成结果
    result = {}
    # 收集所有已知 OCR 字段名（映射表中的 key）
    known_ocr_fields = set(mapping.keys())
    for col in output_columns:
        value = ""
        if col in reverse_map:
            # 尝试所有映射的OCR字段，取第一个有值的
            for ocr_field in reverse_map[col]:
                v = fields.get(ocr_field, "")
                if v:
                    value = v
                    break
        result[col] = value

    # 透传自定义字段：不在已知 OCR 字段中、也不在 output_columns 中的字段直接保留
    for key, val in fields.items():
        if key not in known_ocr_fields and key not in result:
            result[key] = val

    # 承保公司优先使用签单分公司全名：当"保司公司名称"是基础名的扩展（同一保司的分公司）时，
    # 用分公司全名覆盖承保公司（如"现代财产保险（中国）有限公司" → "...广东分公司"）。
    # 保险公司（base）保持不变，故保司（带地区）/规则分派/别名包含匹配均不受影响。
    _branch = fields.get("保司公司名称", "")
    _base = fields.get("保险公司", "")
    if _branch and _base and _base in _branch and _branch != _base:
        result["承保公司"] = _branch

    # 兼容历史记录：如果"保司（带地区）"为空，从地址+承保公司实时计算；无地址时直接用承保公司
    if not result.get("保司（带地区）"):
        address = fields.get("保司地址", "")
        company = fields.get("保险公司", "")
        if company:
            if address:
                from insurance.policy_parser import extract_city_from_address
                city = extract_city_from_address(address)
                result["保司（带地区）"] = (city + company) if city else company
            else:
                result["保司（带地区）"] = company

    return result


def get_available_ocr_fields() -> List[str]:
    """获取所有可能的OCR提取字段名（用于前端配置界面）"""
    return [
        "保单号", "投保确认码", "保险公司", "保险公司简称", "险种类型",
        "被保险人", "投保人", "车主", "证件号码",
        "被保险人身份证号码", "投保人身份证号码",
        "车牌号", "车架号VIN", "发动机号", "厂牌型号",
        "核定载客", "核定载质量", "使用性质", "机动车种类", "初次登记日期",
        "保费合计", "不含税保费", "增值税额", "车船税",
        "保险期间", "保险起期", "保险止期", "签单日期", "收费确认时间",
        "销售渠道", "中介机构", "争议解决方式", "制单人", "经办人",
    ]

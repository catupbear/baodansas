#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保司识别模块
从PDF文本中识别保险公司，支持4层策略：
1. "公司名称："格式直接提取
2. 遍历公司基础名称列表匹配
3. 特征关键词（电话、网址等）兜底识别
4. 保单号前缀推断
"""

import re
from typing import Dict

from insurance.engine.schema import CompanyResult


class CompanyIdentifier:
    """保司识别器"""

    # 公司基础名称列表（匹配后会自动抓取分公司等后缀）
    COMPANY_BASES = [
        "中国人民财产保险股份有限公司",
        "中国平安财产保险股份有限公司",
        "中国太平洋财产保险股份有限公司",
        "大家财产保险有限责任公司",
        "阳光财产保险股份有限公司",
        "太平财产保险有限公司",
        "国寿财产保险股份有限公司",
        "中国人寿财产保险股份有限公司",
        "紫金财产保险股份有限公司",
        "申能财产保险股份有限公司",
        "浙商财产保险股份有限公司",
        "中华联合财产保险股份有限公司",
        "天安财产保险股份有限公司",
        "华安财产保险股份有限公司",
        "永安财产保险股份有限公司",
        "华泰财产保险有限公司",
        "众安在线财产保险股份有限公司",
        "亚太财产保险有限公司",
        "都邦财产保险股份有限公司",
        "渤海财产保险股份有限公司",
        "锦泰财产保险股份有限公司",
        # 新增保司
        "永诚财产保险股份有限公司",
        "新疆前海联合财产保险股份有限公司",
        "前海联合财产保险股份有限公司",
        "京东安联财产保险有限公司",
        "安盛天平财产保险有限公司",
    ]

    # 保司简称映射
    COMPANY_SHORT_MAP = {
        "人民财产": "人保PICC",
        "平安财产": "平安",
        "太平洋财产": "太平洋",
        "大家财产": "大家财险",
        "阳光财产": "阳光",
        "太平财产": "太平",
        "国寿财产": "国寿财产",
        "人寿财产": "国寿财产",
        "紫金财产": "紫金",
        "申能财产": "申能",
        "浙商财产": "浙商",
        "中华联合": "中华联合",
        "天安财产": "天安",
        "华安财产": "华安",
        "永安财产": "永安",
        "华泰财产": "华泰",
        "众安在线": "众安",
        "亚太财产": "亚太",
        "都邦财产": "都邦",
        "渤海财产": "渤海",
        "锦泰财产": "锦泰",
        "永诚财产": "永诚",
        "前海联合": "前海联合",
        "京东安联": "京东安联",
        "安盛天平": "安盛天平",
    }

    # 通过特征关键词辅助识别保司（当公司名未直接出现时的兜底）
    COMPANY_FALLBACK_SIGNALS = [
        # (特征关键词/正则, 公司全称)
        (r"95552|alltrust\.com|yongcheng\.com|永诚", "永诚财产保险股份有限公司"),
        (r"4008110110|qhins\.com|前海联合", "新疆前海联合财产保险股份有限公司"),
        (r"950610|JDallianz|京东安联", "京东安联财产保险有限公司"),
        (r"95550|axa\.cn|安盛天平|AXAINS", "安盛天平财产保险有限公司"),
        (r"95519|中国人寿财产|国寿财产", "中国人寿财产保险股份有限公司"),
        (r"95585|cic\.cn|中华保|中华联合", "中华联合财产保险股份有限公司"),
        (r"95518|epicc\.com|人保财险|PICC", "中国人民财产保险股份有限公司"),
        (r"95511|pingan\.com|平安好车主|平安好生活|平安产险", "中国平安财产保险股份有限公司"),
        (r"95500|cpic\.com|太平洋财产", "中国太平洋财产保险股份有限公司"),
        (r"95589|大家财险|大家财产", "大家财产保险有限责任公司"),
        (r"95510|阳光财产", "阳光财产保险股份有限公司"),
        (r"95589.*太平|太平财产|太平保险", "太平财产保险有限公司"),
        (r"紫金财产", "紫金财产保险股份有限公司"),
        (r"申能财产", "申能财产保险股份有限公司"),
        (r"95154|浙商财产", "浙商财产保险股份有限公司"),
        (r"4006095509|ehuatai\.com|华泰财险", "华泰财产保险有限公司"),
    ]

    # 简称 → company_id slug 映射
    COMPANY_ID_MAP = {
        "人保PICC": "picc",
        "平安": "pingan",
        "太平洋": "cpic",
        "大家财险": "dajia",
        "阳光": "sunshine",
        "太平": "taiping",
        "国寿财产": "clic",
        "紫金": "zijin",
        "申能": "shenneng",
        "浙商": "zheshang",
        "中华联合": "zhonghua",
        "天安": "tianan",
        "华安": "huaan",
        "永安": "yongan",
        "华泰": "huatai",
        "众安": "zhongan",
        "亚太": "yatai",
        "都邦": "dubang",
        "渤海": "bohai",
        "锦泰": "jintai",
        "永诚": "yongcheng",
        "前海联合": "qianhai",
        "京东安联": "jdallianz",
        "安盛天平": "axa",
        "浙商": "zheshang",
    }

    # 保单号前缀 → 公司全称映射
    POLICY_NO_PREFIX_MAP = [
        (r'^1[01][\d]{2,3}003', "中国平安财产保险股份有限公司"),  # 平安车险
        (r'^1[01][\d]{2,3}005', "中国平安财产保险股份有限公司"),  # 平安交强
        (r'^1[01][\d]{2,3}006', "中国平安财产保险股份有限公司"),  # 平安意外险
        (r'^1355', "中国平安财产保险股份有限公司"),            # 平安（1355前缀）
        (r'^1045', "中国平安财产保险股份有限公司"),            # 平安（1045前缀）
        (r'^606EA', "华泰财产保险有限公司"),                  # 华泰
        (r'^112B', "永诚财产保险股份有限公司"),                # 永诚
        (r'^183', "新疆前海联合财产保险股份有限公司"),          # 前海联合
        (r'^2104', "安盛天平财产保险有限公司"),                # 安盛天平
        (r'^29104', "安盛天平财产保险有限公司"),               # 安盛天平（29104前缀）
        (r'^P26', "中华联合财产保险股份有限公司"),             # 中华联合
        (r'^29104', "紫金财产保险股份有限公司"),               # 紫金
        (r'^2136', "大家财产保险有限责任公司"),                # 大家
        (r'^6605', "阳光财产保险股份有限公司"),                # 阳光
        (r'^6627', "中国人寿财产保险股份有限公司"),            # 国寿
        (r'^P8100', "京东安联财产保险有限公司"),               # 京东安联驾乘
        (r'^ASHZ', "中国太平洋财产保险股份有限公司"),          # 太平洋
        (r'^BSHZ', "中国太平洋财产保险股份有限公司"),          # 太平洋批单
        (r'^P432', "太平财产保险有限公司"),                   # 太平
        (r'^6203', "申能财产保险股份有限公司"),                # 申能
    ]

    def identify(self, text: str) -> CompanyResult:
        """
        识别保司，返回 CompanyResult
        策略：
        1. 先从文本中直接匹配"公司名称："格式
        2. 遍历公司基础名称列表匹配
        3. 通过特征关键词（电话、网址等）兜底识别
        4. 通过保单号前缀推断保司
        """
        company_name = ""
        raw_match = ""

        # 先处理公司名中可能存在的字间空格
        company_text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2',
                              re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text))

        # 方式1："公司名称："格式
        m = re.search(r"公司名称[：:\s]*(.+?)(?:\s+公司地址|$)", company_text)
        if m:
            val = m.group(1).strip()
            if len(val) > 6:
                # 清理尾部多余内容
                val = re.sub(r'(公司|营业部|服务部|业务部|支公司).*', r'\1', val)
                company_name = val
                raw_match = m.group(0)

        # 方式2：遍历公司基础名称列表匹配
        if not company_name:
            for base in self.COMPANY_BASES:
                p = re.escape(base) + r'[\S]*'
                m = re.search(p, company_text)
                if m:
                    val = m.group(0).strip()
                    val = re.sub(r'\s+公司地址.*', '', val)
                    val = re.sub(r'(公司|营业部|服务部|业务部|支公司).*', r'\1', val)
                    if len(val) > 6:
                        company_name = val
                        raw_match = m.group(0)
                        break

        # 方式3：特征关键词兜底识别
        if not company_name:
            for pattern, fallback_name in self.COMPANY_FALLBACK_SIGNALS:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    company_name = fallback_name
                    raw_match = m.group(0)
                    break

        # 方式4：通过保单号前缀推断保司
        if not company_name:
            company_name, raw_match = self._infer_from_policy_no(text)

        # 映射简称
        short_name = self._get_short_name(company_name)

        # 映射 company_id
        company_id = self.COMPANY_ID_MAP.get(short_name, "")

        return CompanyResult(
            company_id=company_id,
            company_name=company_name,
            company_short=short_name,
            raw_match=raw_match,
        )

    def _get_short_name(self, company_name: str) -> str:
        """全称 → 简称"""
        for keyword, short in self.COMPANY_SHORT_MAP.items():
            if keyword in company_name:
                return short
        return ""

    def _infer_from_policy_no(self, text: str) -> tuple:
        """
        从保单号前缀推断保司（极简标志文件等场景）
        返回 (公司全称, 原始匹配文本)
        """
        # 从文本中提取可能的保单号
        m = re.search(r'(\d{15,30})', re.sub(r'\s+', '', text))
        if m:
            pno = m.group(1)
            for prefix_pat, company_name in self.POLICY_NO_PREFIX_MAP:
                if re.match(prefix_pat, pno):
                    return company_name, pno
        return "", ""

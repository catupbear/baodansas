#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
驾意险基础规则
继承 BaseRule，提取驾乘人员意外伤害保险特有的字段和保障项目明细。
"""

import re
from insurance.rules.base_rule import BaseRule


class BaseAccidentRule(BaseRule):
    """
    驾意险基础规则类
    override extract_type_specific 和 extract_items。
    """

    policy_type: str = "accident"

    def extract_type_specific(self, text: str, text_merged: str) -> dict:
        """
        提取驾意险特有字段：
        - 保障人数
        - 每人保额
        - 意外身故保额
        - 意外医疗保额
        """
        fields = {}

        # --- 保障人数 ---
        for p in [
            r"保障人数[：:\s]*(\d+)\s*人",
            r"被保险人数[：:\s]*(\d+)\s*人",
            r"投保人数[：:\s]*(\d+)",
            r"(?:Num\s*of\s*Insured|承保人数)[：:\s]*(\d+)",
            r"(\d+)\s*座",  # "5座" 格式（按座位投保）
        ]:
            m = re.search(p, text)
            if m:
                fields["保障人数"] = m.group(1)
                break

        # --- 每人保额 ---
        for p in [
            r"每人保[额险]金?[额]?[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"每座保额[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"保险金额.*?每人[：:\s]*([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["每人保额"] = m.group(1).replace(",", "")
                break

        # --- 意外身故保额 ---
        for p in [
            r"意外身故[及/和]?(?:伤残)?.*?保[额险]金?[额]?[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"身故[及/和]?伤残.*?保[额险]金?[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"意外身故\s+([\d,]+\.?\d*)",
            r"[Aa]ccidental.*?[Dd]eath.*?([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["意外身故保额"] = m.group(1).replace(",", "")
                break

        # --- 意外医疗保额 ---
        for p in [
            r"意外医疗.*?保[额险]金?[额]?[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"医疗费用.*?保[额险]金?[额]?[：:\s]*([\d,]+\.?\d*)(?:\s*(?:元|万元))?",
            r"意外医疗\s+([\d,]+\.?\d*)",
            r"[Aa]ccidental.*?[Mm]edical.*?([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["意外医疗保额"] = m.group(1).replace(",", "")
                break

        return fields

    def extract_items(self, text: str, text_merged: str) -> list:
        """
        提取驾意险保障项目明细：
        - 意外身故/伤残
        - 意外医疗
        - 住院津贴
        - 猝死责任
        - 其他保障项目
        """
        items = []
        lines = text.split("\n")

        for line in lines:
            # 匹配驾意险保障项目关键词
            if not re.search(
                r"(意外身故|意外伤残|意外医疗|住院津贴|猝死|救护车|交通工具"
                r"|驾乘.*?身故|驾乘.*?伤残|驾乘.*?医疗|法定节假日"
                r"|[Aa]ccidental|[Dd]eath|[Mm]edical)",
                line
            ):
                continue

            # 提取金额
            amounts = re.findall(r"[\d,]+\.?\d*", line)
            name_match = re.match(r"(.+?)\s+(?:[\d/]|—)", line)
            name = name_match.group(1).strip() if name_match else line.strip()[:40]

            # 跳过纯标题行（无金额数据）
            if not amounts:
                continue

            # 过滤掉太短的数字（序号等）
            valid_amounts = [a for a in amounts if len(a.replace(",", "").replace(".", "")) >= 2]
            if not valid_amounts:
                continue

            item = {"险种名称": name}

            # 保额通常是较大的数字，保费是较小的
            if len(valid_amounts) >= 2:
                item["保额/限额"] = valid_amounts[0]
                item["保费"] = valid_amounts[-1]
            elif len(valid_amounts) == 1:
                item["保额/限额"] = valid_amounts[0]

            items.append(item)

        return items

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交强险基础规则
继承 BaseRule，提取交强险特有的责任限额字段。
"""

import re
from insurance.rules.base_rule import BaseRule


class BaseCompulsoryRule(BaseRule):
    """
    交强险基础规则类
    override extract_type_specific 提取责任限额相关字段。
    """

    policy_type: str = "compulsory"

    def extract_type_specific(self, text: str, text_merged: str) -> dict:
        """
        提取交强险特有字段：
        - 责任限额
        - 死亡伤残赔偿限额
        - 医疗费用赔偿限额
        - 抢救费用赔偿限额（部分保单有）
        - 财产损失赔偿限额
        """
        fields = {}

        # --- 责任限额（总额） ---
        m = re.search(r"责任限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?", text)
        if m:
            fields["责任限额"] = m.group(1).replace(",", "")

        # --- 死亡伤残赔偿限额 ---
        for p in [
            r"死亡伤残赔偿限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"死亡伤残.*?限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"死亡伤残\s+([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["死亡伤残赔偿限额"] = m.group(1).replace(",", "")
                break

        # --- 医疗费用赔偿限额 ---
        for p in [
            r"医疗费用赔偿限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"医疗费用.*?限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"医疗费用\s+([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["医疗费用赔偿限额"] = m.group(1).replace(",", "")
                break

        # --- 抢救费用赔偿限额（部分保单可能无此项） ---
        for p in [
            r"抢救费用赔偿限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"抢救费用.*?限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
        ]:
            m = re.search(p, text)
            if m:
                fields["抢救费用赔偿限额"] = m.group(1).replace(",", "")
                break

        # --- 财产损失赔偿限额 ---
        for p in [
            r"财产损失赔偿限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"财产损失.*?限额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"财产损失\s+([\d,]+\.?\d*)",
        ]:
            m = re.search(p, text)
            if m:
                fields["财产损失赔偿限额"] = m.group(1).replace(",", "")
                break

        return fields

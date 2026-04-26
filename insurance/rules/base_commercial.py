#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
商业险基础规则
继承 BaseRule，提取商业险特有的特别约定、绝对免赔额和险种明细行。
"""

import re
from insurance.rules.base_rule import BaseRule


class BaseCommercialRule(BaseRule):
    """
    商业险基础规则类
    override extract_type_specific 和 extract_items。
    """

    policy_type: str = "commercial"

    def extract_type_specific(self, text: str, text_merged: str) -> dict:
        """
        提取商业险特有字段：
        - 特别约定
        - 绝对免赔额
        """
        fields = {}

        # --- 特别约定 ---
        # 常见格式：
        #   "特别约定：XXX"
        #   "特别约定\nXXX\n..." 直到下一个大标题或分隔线
        m = re.search(
            r"特别约定[：:\s]*([\s\S]+?)(?=\n\s*(?:重要提示|投保人声明|保险条款|签单日期|制单|打印|$))",
            text
        )
        if m:
            val = m.group(1).strip()
            # 清理过长内容（保留前500字）
            if len(val) > 500:
                val = val[:500] + "..."
            if val:
                fields["特别约定"] = val

        # --- 绝对免赔额 ---
        for p in [
            r"绝对免赔额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"绝对免赔[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
            r"每次事故绝对免赔额[：:\s]*([\d,]+\.?\d*)(?:\s*元)?",
        ]:
            m = re.search(p, text)
            if m:
                fields["绝对免赔额"] = m.group(1).replace(",", "")
                break

        return fields

    def extract_items(self, text: str, text_merged: str) -> list:
        """
        提取商业险险种明细行：
        - 机动车损失保险
        - 第三者责任保险
        - 车上人员责任保险（司机/乘客）
        - 医保外用药责任险
        - 精神损害抚慰金
        - 增值服务（代为送检、代为驾驶、车辆安全检测等）
        - 绝对免赔额（行级）
        - 其他附加险
        """
        items = []
        lines = text.split("\n")

        for line in lines:
            # 匹配包含险种关键词的行
            if not re.search(
                r"(损失保险|第三者责任|车上人员|座位险|司机|乘客|医疗费用|医保外"
                r"|精神损害|增值服务|救援服务|盗抢|自燃|涉水|玻璃|划痕|不计免赔"
                r"|代为送检|代为驾驶|车辆安全检测|绝对免赔额)",
                line
            ):
                continue

            # 提取金额
            amounts = re.findall(r"[\d,]+\.\d{2}", line)
            # 提取险种名称（到第一个数字/分隔符前）
            name_match = re.match(r"(.+?)\s+(?:[\d/共]|—)", line)
            name = name_match.group(1).strip() if name_match else line.strip()[:40]

            if amounts:
                item = {"险种名称": name, "保费": amounts[-1]}
                # 尝试提取保额/限额（第一个金额通常是保额，最后一个是保费）
                amt_match = re.search(r"([\d,]+\.?\d*(?:元|万元)?(?:/座\*\d+座)?)", line)
                if amt_match and amt_match.group(1) != amounts[-1]:
                    item["保额/限额"] = amt_match.group(1)
                items.append(item)
            elif "0元/次" in line or "次" in line:
                items.append({"险种名称": name, "保费": "0.00"})

        return items

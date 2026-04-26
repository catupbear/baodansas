"""险种识别模块

从 OCR 文本中识别保单的险种类型（交强险/商业险/驾乘意外险等）。
从 insurance/policy_parser.py 的 _identify_policy_type 和 _get_policy_type_code 提取而来。
"""

import re
from typing import Optional

from insurance.engine.schema import PolicyTypeResult

# 险种匹配正则模式列表（按优先级排序）
POLICY_TYPE_PATTERNS = [
    r"(新能源汽车商业保险)",
    r"(机动车商业保险)",
    r"(机动车辆商业保险)",             # 大家财险格式
    r"(特种车商业保险)",               # 太平/京东安联特种车格式
    r"(新能源汽车交通事故责任强制保险)",
    r"(机动车交通事故责任强制保险)",
    r"(非?家庭自用车?驾乘人员人身意外伤害保险)",
    r"(驾乘综合保险)",
    r"(驾乘人员人身意外伤害保险)",
    r"(驾乘人员意外伤害保险)",          # 大家财险格式（无"人身"）
    r"(驾乘无忧\S+)",
    r"(机动车驾乘人员意外伤害保险(?:[（(]\S+?[)）])?)",
    r"(机动车辆驾乘人员意外伤害保险)",  # 大家财险格式
    r"(平安车主尊享保障)",
    r"(E车无忧\S*保险?)",              # 阳光E车无忧守护版
    r"(交通出行人身意外伤害保险)",
    r"(交通出行意外伤害保险)",
    r"(交通工具意外伤害保险)",          # 安盛天平/平安格式
    r"(锦程交通意外伤害保险)",          # 申能格式
    r"(个人意外伤害保险)",
    r"(\u201c如意行\u201d驾乘综合保险)",
    r"(机动车辆保险)",
    r"(机动车辆综合险)",               # 安盛天平格式
    r"(平安车主出行险)",
    r"(守护出行无忧[\d.]*(?:[（(]\S+?[)）])?)",  # 太平守护出行无忧3.0
    r"(安心行\S*驾乘意外险)",          # 京东安联安心行驾乘意外险
    r"(卓越全意保\S*保险)",            # 安盛天平卓越全意保
]


class TypeIdentifier:
    """险种识别器"""

    def identify(self, text: str) -> PolicyTypeResult:
        """识别保单险种类型

        Args:
            text: OCR 提取的保单全文

        Returns:
            PolicyTypeResult 包含 type_code、type_name 和原始匹配文本
        """
        raw_match = self._match_policy_type(text)
        type_code, type_name = self._get_type_code(raw_match)
        return PolicyTypeResult(
            type_code=type_code,
            type_name=type_name,
            raw_match=raw_match or "",
        )

    def _match_policy_type(self, text: str) -> Optional[str]:
        """从文本中匹配险种名称

        依次尝试正则模式列表，以及太平洋神行车保和京东安联 POLICY SCHEDULE 特殊格式。
        """
        # 标准正则模式匹配
        for p in POLICY_TYPE_PATTERNS:
            m = re.search(p, text)
            if m:
                return m.group(1)

        # 太平洋"神行车保"系列：标题不含标准险种关键词，需从标题推断
        # "神行车保机动车保险单" / "神行车保新能源汽车保险单" / "神行车保系列产品批单"
        m = re.search(r"神行车保\S*?(新能源汽车|机动车)\S*?保险单", text)
        if m:
            if "新能源" in m.group(0):
                return "新能源汽车商业保险"
            return "机动车商业保险"

        # 京东安联 POLICY SCHEDULE 格式：从"Total Premium"等判断
        if re.search(r"POLICY\s*SCHEDULE|保\s*险\s*单", text[:300]):
            if re.search(r"Total\s*Premium|总保费", text):
                # 检查是否为驾乘意外险
                if re.search(r"Accidental|意外.*身故|意外.*伤残|驾乘", text):
                    return "驾乘人员意外伤害保险"

        return None

    def _get_type_code(self, policy_type: Optional[str]) -> tuple:
        """根据险种类型返回 (type_code, type_name)

        Args:
            policy_type: 匹配到的险种名称文本

        Returns:
            (type_code, type_name) 元组
        """
        if not policy_type:
            return "unknown", "未知"
        if "交通事故责任强制" in policy_type:
            return "compulsory", "交强险"
        if "商业保险" in policy_type or "机动车辆保险" in policy_type or "机动车辆综合险" in policy_type:
            return "commercial", "商业险"
        if ("驾乘" in policy_type or "意外" in policy_type or "出行险" in policy_type
                or "出行无忧" in policy_type or "安心行" in policy_type
                or "E车无忧" in policy_type or "卓越全意保" in policy_type):
            return "accident", "驾乘/意外险"
        return "unknown", policy_type

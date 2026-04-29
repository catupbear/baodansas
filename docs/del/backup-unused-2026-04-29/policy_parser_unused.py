"""
备份来源: insurance/policy_parser.py
备份日期: 2026-04-29
备份原因: get_all_field_names() 全项目无调用，为调试用工具函数
恢复方式: 见同目录 README.md
"""


def get_all_field_names(policies: List[Dict[str, Any]]) -> List[str]:
    """
    从所有识别结果中收集所有出现过的字段名（用于生成表头）
    """
    all_fields = set()
    for policy in policies:
        all_fields.update(policy.get("fields", {}).keys())

    ordered = [f for f in FIELD_ORDER if f in all_fields]
    remaining = sorted(all_fields - set(ordered))
    return ordered + remaining

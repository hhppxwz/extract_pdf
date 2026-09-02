"""
存放通用的用于抽取metadata的工具。
"""

import re


def normalize_date(year: str) -> str:
    """标准化年份，确保输出四位数"""
    if re.match(r'^\d{4}$', year):
        return year
    return ""

def extract_common_field(text: str, pattern: str) -> str:
    """通用的正则抽取函数"""
    match = re.search(pattern, text)
    return match.group(1) if match else ""
"""文号规范化：原文由调用方保存，匹配统一括号、空白和顺序号。"""
import re


def normalize_document_number(value: str) -> str:
    text = ''.join(str(value or '').split()).translate(str.maketrans({
        '（': '[', '〔': '[', '﹝': '[', '［': '[', '【': '[', '(': '[',
        '）': ']', '〕': ']', '﹞': ']', '］': ']', '】': ']', ')': ']',
    }))
    # OCR 可能漏掉年份右括号；仅修复完整的“机关字[四位年份顺序号号”格式。
    text = re.sub(r'^([\u4e00-\u9fff]+字\[)((?:19|20)\d{2})(\d{1,6})(号)$',
                  lambda m: m[1] + m[2] + ']' + m[3] + m[4], text)
    # 只处理四位年份后、号字前的顺序号，不改年份和其他标识中的数字。
    return re.sub(r'(\[\d{4}\])([0-9]+)(号)$',
                  lambda m: m[1] + (m[2].lstrip('0') or '0') + m[3], text)


def display_document_number(value: str) -> str:
    return normalize_document_number(value).translate(str.maketrans({'[': '〔', ']': '〕'}))

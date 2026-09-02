# metadata/form.py
import re
from typing import Dict, List
from models import ContentBlock


def extract_form_metadata(blocks: List[ContentBlock]) -> Dict[str, str]:
    form_blocks = [b for b in blocks if b.page_num <= 1]
    form_text = " ".join([b.content for b in form_blocks])

    meta = {}

    # 姓名
    name_match = re.search(r'姓名\s*[：:]\s*([^\s,，、]{2,4})', form_text)
    if name_match:
        meta['applicant'] = name_match.group(1)

    # 部门
    dept_match = re.search(r'(?:部门|单位)\s*[：:]\s*([^\s,，、]{2,20})', form_text)
    if dept_match:
        meta['department'] = dept_match.group(1)

    # 金额
    amount_match = re.search(r'金额\s*[：:]\s*([\d,，.]+)\s*元', form_text)
    if amount_match:
        meta['amount'] = amount_match.group(1).replace(',', '').replace('，', '')

    return meta
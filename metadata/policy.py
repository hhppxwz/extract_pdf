# metadata/policy.py
import re
from typing import Dict, List
from models import ContentBlock


def extract_policy_metadata(blocks: List[ContentBlock]) -> Dict[str, str]:
    header_blocks = [b for b in blocks if b.page_num <= 2]
    header_text = " ".join([b.content for b in header_blocks])

    meta = {}

    # 文号
    doc_num_match = re.search(r'([\u4e00-\u9fa5]{2,4}〔\d{4}〕\d+号)', header_text)
    if doc_num_match:
        meta['doc_number'] = doc_num_match.group(1)

    # 签发单位
    org_match = re.search(r'(?:根据|依据|按照)\s*《?([\u4e00-\u9fa5]{2,10}[部委办局院])》?', header_text)
    if org_match:
        meta['issuer'] = org_match.group(1)

    # 发布日期
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', header_text)
    if date_match:
        meta['issue_date'] = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    return meta
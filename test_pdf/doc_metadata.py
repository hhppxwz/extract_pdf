# doc_metadata.py
"""
文档内容元数据提取器（策略模式）
根据文档类型提取metadata信息

"""
import re
from typing import Dict, List
from models import ContentBlock

def extract_document_metadata(blocks: List[ContentBlock], doc_type: str) -> Dict[str, str]:
    """统一入口，根据 doc_type 调度"""
    if doc_type == "academic_paper":
        return _extract_academic_metadata(blocks)
    elif doc_type == "policy_regulation":
        return _extract_policy_metadata(blocks)
    elif doc_type == "admin_form_metadata":
        return _extract_form_metadata(blocks)
    else:
        return {}

def _extract_academic_metadata(blocks):
    meta = {}
    header_blocks = [b for b in blocks if b.page_num <= 1 and hasattr(b, 'raw') and b.raw]
    header_text = " ".join([b.content for b in header_blocks])

    # 标题：优先取 MinerU 标为 title 的块
    for b in header_blocks:
        if b.raw.get('type') == 'title':
            meta['title'] = b.content.strip()
            break
    if not meta.get('title'):
        for b in header_blocks:
            if len(b.content) > 20:
                meta['title'] = b.content.strip()
                break

    # 年份
    year_match = re.search(r'(19|20)\d{2}', header_text)
    if year_match:
        meta['year'] = year_match.group()

    # 作者（示例：中文姓名或英文名）
    authors = re.findall(r'([\u4e00-\u9fa5]{2,4})\s*(?:[,，、])?', header_text[:500])
    if authors:
        meta['authors'] = list(set(authors))[:3]

    # DOI
    doi_match = re.search(r'10\.\d+/[-._;()/:A-Z0-9]+', header_text, re.I)
    if doi_match:
        meta['doi'] = doi_match.group()

    return meta

def _extract_policy_metadata(blocks):
    # ... 制度逻辑 ...
    pass

def _extract_form_metadata(blocks):
    # ... 表单逻辑 ...
    pass
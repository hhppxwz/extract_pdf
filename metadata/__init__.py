# metadata/__init__.py
from typing import Dict, List
from models import ContentBlock
from .academic import extract_academic_metadata
from .policy import extract_policy_metadata
from .form import extract_form_metadata

def extract_document_metadata(blocks: List[ContentBlock], doc_type: str) -> Dict[str, str]:
    """
    根据文档类型路由到对应的元数据抽取模块
    """
    if doc_type == "academic_paper":
        return extract_academic_metadata(blocks)
    elif doc_type == "policy_regulation":
        return extract_policy_metadata(blocks)
    elif doc_type == "admin_form":
        return extract_form_metadata(blocks)
    else:
        return {}
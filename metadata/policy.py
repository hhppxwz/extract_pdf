# metadata/policy.py
import re
from typing import Any, Dict, List
from models import BlockType, ContentBlock
from metadata.document_number import display_document_number


_BODY_HEADING_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+(?:编|章|节|条)")
_POLICY_TITLE_RE = re.compile(r"^[^《》。；：:]{4,100}(?:办法|细则|规定|规则|条例|章程|意见|方案|制度)(?:[（(][^）)]{1,30}[）)])?$")


def _policy_title_metadata(blocks: List[ContentBlock], source_title: str = "") -> Dict[str, str]:
    """以独立正文标题及条款起始确认制度名称，通知标题单独保留。"""
    lines = ["".join(line.split()) for block in blocks if block.type == BlockType.TEXT
             for line in block.content.splitlines() if line.strip()]
    source_title = "".join(str(source_title or "").split())
    notice = source_title if re.fullmatch(r"关于.{2,500}的通知", source_title) else ""
    for i, line in enumerate(lines[:12]):
        if notice:
            break
        if not line.startswith("关于"):
            continue
        for count in range(1, 9):
            candidate = "".join(lines[i:i + count])
            if re.fullmatch(r"关于.{2,500}的通知", candidate):
                notice = candidate
                break
        if notice:
            break
    titles = set()
    notice_titles = re.findall(r"《([^《》]+)》", notice)
    for i, line in enumerate(lines):
        title = re.sub(r"^附件\s*[一二三四五六七八九十\d]*[：:]?", "", line)
        # 通知书名与独立正文标题相互印证时，允许前言和编号结构，无需正式章条。
        confirmed = ""
        for count in range(1, 5):
            if i - count + 1 < 0:
                break
            candidate = "".join(lines[i - count + 1:i + 1])
            candidate = re.sub(r"^附件\s*[一二三四五六七八九十\d]*[：:]?", "", candidate)
            if candidate in notice_titles and _POLICY_TITLE_RE.fullmatch(candidate):
                confirmed = candidate
                break
        if confirmed:
            titles.add(confirmed)
            continue
        if not _POLICY_TITLE_RE.fullmatch(title) or title.startswith("关于") or _BODY_HEADING_RE.match(title):
            continue
        following = lines[i + 1:i + 3]
        if following and ( _BODY_HEADING_RE.match(following[0]) or
            (len(following) > 1 and re.fullmatch(r"[（(].{1,30}[）)]", following[0]) and _BODY_HEADING_RE.match(following[1]))):
            # 标题可能跨段落换行；只合并与通知中书名完全一致的连续片段，避免吞入正文。
            for count in range(2, 5):
                if i - count + 1 < 0:
                    break
                candidate = "".join(lines[i - count + 1:i + 1])
                candidate = re.sub(r"^附件\s*[一二三四五六七八九十\d]*[：:]?", "", candidate)
                if candidate in notice_titles and _POLICY_TITLE_RE.fullmatch(candidate):
                    title = candidate
                    break
            titles.add(title)
    result = {}
    if notice:
        result.update(title=notice, notice_title=notice)
    if len(titles) == 1:
        result["title"] = next(iter(titles))
    return result

_DOC_NUMBER_PATTERN = (
    r"[\u4e00-\u9fa5]{2,12}\s*[〔\[［﹝【（(]\s*\d{4}\s*[〕\]］﹞】）)]\s*\d+\s*号"
)
_DOC_NUMBER_LINE_RE = re.compile(
    rf"(?:文号|发文字号)\s*[：:]\s*({_DOC_NUMBER_PATTERN})|^[ \t]*({_DOC_NUMBER_PATTERN})[ \t]*$",
    re.M,
)


def _normalize_doc_number(value: str) -> str:
    """将文号的括号和内部空白统一为入库格式。"""
    return display_document_number(value)


def _attach_policy_metadata(blocks: List[ContentBlock], meta: dict) -> dict:
    """已确认多个附件时，分别返回制度元数据，不以通知名代替制度名。"""
    from policy.document_parts import split_policy_document
    parts = split_policy_document(blocks, meta, '')
    if len(parts) > 1:
        meta['policies'] = [part['metadata'] for part in parts]
        meta.pop('title', None)
    return meta


def extract_policy_metadata(blocks: List[ContentBlock]) -> Dict[str, Any]:
    for block in blocks:
        raw = block.raw if isinstance(block.raw, dict) else {}
        if raw.get("source_format") == "mhtml":
            source_metadata = raw.get("source_metadata")
            meta = dict(source_metadata) if isinstance(source_metadata, dict) else {}
            if meta.get("title"):
                meta["title"] = "".join(str(meta["title"]).split())
            if meta.get("doc_number"):
                meta.setdefault("doc_number_raw", meta["doc_number"])
                meta["doc_number"] = _normalize_doc_number(meta["doc_number"])
            titles = _policy_title_metadata(blocks, meta.get("title", ""))
            if titles.get("notice_title"):
                meta.update(titles)
            return _attach_policy_metadata(blocks, meta)
    header_blocks = [b for b in blocks if b.page_num <= 2]
    header_text = "\n".join([b.content for b in header_blocks])

    meta = _policy_title_metadata(blocks)

    # 页码缺失时可能把全文都纳入首页块，因此文号只在正文标题之前查找。
    body_start = re.search(
        r"(?<!\S)第[一二三四五六七八九十百千万零〇\d]+(?:章|条)",
        header_text,
    )
    document_header = header_text[:body_start.start()] if body_start else header_text
    # 文号必须单独成行或带明确字段标签，避免把正文引用误认成本文件文号。
    doc_num_match = _DOC_NUMBER_LINE_RE.search(document_header)
    if doc_num_match:
        doc_number = doc_num_match.group(1) or doc_num_match.group(2)
        meta['doc_number_raw'] = doc_number
        meta['doc_number'] = _normalize_doc_number(doc_number)

    # 仅从明确标注的字段提取发布机构，引用依据不是发文单位。
    org_match = re.search(r'(?:发布机构|发文机关|印发单位)\s*[：:]\s*([\u4e00-\u9fa5（）()]{2,40})', header_text)
    if org_match:
        meta['issuer'] = org_match.group(1)

    # 发布日期
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', document_header)
    if date_match:
        meta['issue_date'] = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    return _attach_policy_metadata(blocks, meta)

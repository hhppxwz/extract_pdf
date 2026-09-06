"""规章制度的文档元数据与条款结构化管线。

本阶段只处理结构，不调用大模型。实体和关系由 policy_extractor.py 单独处理。
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from models import BlockType, ContentBlock, PolicyClause, PolicyStructureStatus, ReviewItem
from policy_storage import (
    insert_review_item,
    mark_policy_structure_failed,
    replace_policy_clauses,
    upsert_policy_document,
)


STRUCTURE_VERSION = os.getenv("POLICY_STRUCTURE_VERSION", "policy-structure-v1")

_NUMBER = r"[0-9一二三四五六七八九十百千万零〇两]+"
_BOOK_RE = re.compile(rf"^第\s*({_NUMBER})\s*编(?:[：:、.\s]+(.*))?$", re.S)
_CHAPTER_RE = re.compile(rf"^第\s*({_NUMBER})\s*章(?:[：:、.\s]+(.*))?$", re.S)
_SECTION_RE = re.compile(rf"^第\s*({_NUMBER})\s*节(?:[：:、.\s]+(.*))?$", re.S)
_ARTICLE_RE = re.compile(rf"^第\s*({_NUMBER})\s*条(?:[：:、.\s]+(.*))?$", re.S)
_ARTICLE_INLINE_RE = re.compile(rf"(?<!\S)(第\s*{_NUMBER}\s*条)")
_PAREN_ITEM_RE = re.compile(r"^[（(]([^）)]+)[）)]\s*(?:[：:、.]\s*)?(.*)$", re.S)
_CN_ITEM_RE = re.compile(r"^([一二三四五六七八九十百千万]+)、(?:\s*)(.*)$", re.S)
_NUM_ITEM_RE = re.compile(r"^(\d+)[.、](?:\s*)(.*)$", re.S)
_PARAGRAPH_RE = re.compile(rf"^第\s*({_NUMBER})\s*款(?:[：:、.\s]+(.*))?$", re.S)


@dataclass
class _ClauseDraft:
    """条款构建过程中的内部节点。"""

    sequence_no: int
    policy_id: str
    structure_version: str
    level: str
    label: str
    parent: Optional["_ClauseDraft"] = None
    raw_parts: list[str] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    bboxes: list[list[float]] = field(default_factory=list)
    article_no: str = ""
    paragraph_no: str = ""
    item_no: str = ""

    @property
    def clause_id(self) -> str:
        content_hash = hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()[:16]
        seed = f"{self.policy_id}:{self.structure_version}:{self.sequence_no}:{content_hash}"
        return f"clause_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"

    @property
    def raw_text(self) -> str:
        return "\n".join(part for part in self.raw_parts if part.strip()).strip()

    @property
    def chapter_path(self) -> list[str]:
        path: list[str] = []
        node: Optional[_ClauseDraft] = self
        while node:
            if node.level in {"book", "chapter", "section"} and node.label:
                path.append(node.label)
            node = node.parent
        return list(reversed(path))

    def append(self, text: str, page_num: int, bbox: Any = None) -> None:
        text = str(text or "").strip()
        if not text:
            return
        self.raw_parts.append(text)
        if not self.page_start:
            self.page_start = page_num or 0
        self.page_end = max(self.page_end, page_num or 0)
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            try:
                normalized = [float(v) for v in bbox[:4]]
                if normalized not in self.bboxes:
                    self.bboxes.append(normalized)
            except (TypeError, ValueError):
                pass


def _html_to_text(value: str) -> str:
    """把表格HTML转成适合检索和抽取的纯文本。"""
    if not value:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(value, "html.parser")
        rows = []
        for tr in soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows) or soup.get_text(" ", strip=True)
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", value))


def _lines_from_block(block: ContentBlock) -> list[str]:
    """按行处理文本块，并拆出同一块中紧邻的第X条标记。"""
    content = str(block.content or "").replace("\r\n", "\n")
    if block.type == BlockType.TABLE:
        table_text = _html_to_text(block.table_html or content)
        return [f"[表格]\n{table_text}".strip()] if table_text.strip() else []

    lines: list[str] = []
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # 部分解析结果把连续条款放在同一行，按条号前的空白切开。
        matches = list(_ARTICLE_INLINE_RE.finditer(line))
        if len(matches) <= 1 or matches[0].start() != 0:
            lines.append(line)
            continue
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            part = line[start:end].strip()
            if part:
                lines.append(part)
    return lines


def _marker(line: str) -> Optional[tuple[str, str, str]]:
    """识别层级标记，返回(level, label, remainder)。"""
    for level, pattern in (
        ("book", _BOOK_RE),
        ("chapter", _CHAPTER_RE),
        ("section", _SECTION_RE),
        ("article", _ARTICLE_RE),
        ("paragraph", _PARAGRAPH_RE),
    ):
        match = pattern.match(line)
        if match:
            number = match.group(1).replace(" ", "")
            remainder = (match.group(2) or "").strip()
            label_word = {"book": "编", "chapter": "章", "section": "节", "article": "条", "paragraph": "款"}[level]
            return level, f"第{number}{label_word}", remainder

    match = _PAREN_ITEM_RE.match(line)
    if match:
        return "item", f"（{match.group(1).strip()}）", (match.group(2) or "").strip()
    match = _CN_ITEM_RE.match(line)
    if match:
        return "item", f"{match.group(1)}、", (match.group(2) or "").strip()
    match = _NUM_ITEM_RE.match(line)
    if match:
        return "item", f"{match.group(1)}.", (match.group(2) or "").strip()
    return None


def _nearest_parent(current: Optional[_ClauseDraft], level: str) -> Optional[_ClauseDraft]:
    """寻找新节点的最近合法父节点。"""
    allowed = {
        "book": set(),
        "chapter": {"book"},
        "section": {"book", "chapter"},
        "article": {"book", "chapter", "section"},
        "paragraph": {"article"},
        "item": {"article", "paragraph"},
        "preamble": set(),
    }
    node = current
    while node:
        if node.level in allowed.get(level, set()):
            return node
        node = node.parent
    return None


def build_policy_clauses(
    policy_id: str,
    blocks: list[ContentBlock],
    structure_version: str = STRUCTURE_VERSION,
) -> list[PolicyClause]:
    """从解析块建立带父子层级的制度条款。"""
    drafts: list[_ClauseDraft] = []
    current: Optional[_ClauseDraft] = None
    sequence = 0

    def create_node(level: str, label: str, remainder: str, block: ContentBlock) -> _ClauseDraft:
        nonlocal sequence, current
        parent = _nearest_parent(current, level)
        sequence += 1
        draft = _ClauseDraft(
            sequence_no=sequence,
            policy_id=policy_id,
            structure_version=structure_version,
            level=level,
            label=label,
            parent=parent,
        )
        draft.article_no = label[1:-1] if level == "article" else (parent.article_no if parent else "")
        if level == "paragraph":
            draft.paragraph_no = label[1:-1]
        if level == "item":
            draft.item_no = label
        draft.append(f"{label} {remainder}".strip(), block.page_num, block.bbox)
        drafts.append(draft)
        current = draft
        return draft

    ordered_blocks = [item for _, item in sorted(
        enumerate(blocks), key=lambda pair: (pair[1].page_num, pair[0])
    )]
    for block in ordered_blocks:
        if block.type not in {BlockType.TEXT, BlockType.TABLE}:
            continue
        for line in _lines_from_block(block):
            marker = _marker(line)
            if marker:
                level, label, remainder = marker
                create_node(level, label, remainder, block)
                continue

            if current is None:
                create_node("preamble", "前言", line, block)
            else:
                current.append(line, block.page_num, block.bbox)

    clauses: list[PolicyClause] = []
    for draft in drafts:
        raw_text = draft.raw_text
        if not raw_text:
            continue
        prefix = " / ".join(draft.chapter_path)
        search_text = " ".join(
            part for part in (prefix, draft.label, raw_text) if part
        ).strip()
        confidence = 0.95 if draft.level in {"book", "chapter", "section", "article", "paragraph", "item"} else 0.75
        clauses.append(
            PolicyClause(
                clause_id=draft.clause_id,
                policy_id=policy_id,
                parent_clause_id=draft.parent.clause_id if draft.parent else None,
                level=draft.level,
                chapter_path=draft.chapter_path,
                article_no=draft.article_no,
                paragraph_no=draft.paragraph_no,
                item_no=draft.item_no,
                raw_text=raw_text,
                search_text=search_text,
                page_start=draft.page_start,
                page_end=draft.page_end,
                bboxes=draft.bboxes,
                confidence=confidence,
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                sequence_no=draft.sequence_no,
                structure_version=structure_version,
            )
        )
    return clauses


def calculate_policy_parse_quality(blocks: list[ContentBlock], clauses: list[PolicyClause]) -> float:
    """根据文本覆盖、坐标和OCR异常计算结构化质量分数。"""
    meaningful = [
        b for b in blocks
        if b.type in {BlockType.TEXT, BlockType.TABLE}
        and (b.content or b.table_html or "").strip()
    ]
    if not meaningful:
        return 0.0
    block_texts = [str(b.content or b.table_html or "") for b in meaningful]
    total_chars = sum(len(text) for text in block_texts)
    replacement_chars = sum(text.count("�") for text in block_texts)
    bbox_ratio = sum(1 for b in meaningful if len(b.bbox) >= 4) / len(meaningful)
    coverage = min(1.0, sum(len(c.raw_text) for c in clauses) / max(total_chars, 1))
    ocr_score = max(0.0, 1.0 - replacement_chars / max(total_chars, 1) * 20)
    return round(max(0.0, min(1.0, coverage * 0.45 + bbox_ratio * 0.25 + ocr_score * 0.30)), 4)


def _guess_title(file_name: str, blocks: list[ContentBlock]) -> str:
    """从前两页选择最像制度标题的文本，失败时使用文件名。"""
    ordered_blocks = [item for _, item in sorted(
        enumerate(blocks), key=lambda pair: (pair[1].page_num, pair[0])
    )]
    for block in ordered_blocks:
        if block.page_num > 2 or block.type != BlockType.TEXT:
            continue
        for line in str(block.content).splitlines():
            text = line.strip()
            if 4 <= len(text) <= 100 and not re.match(r"^第\s*" + _NUMBER, text):
                return text
    return Path(file_name).stem


def structure_policy_document(
    file_id: str,
    file_name: str,
    blocks: list[ContentBlock],
    metadata: Optional[dict[str, Any]] = None,
    structure_version: str = STRUCTURE_VERSION,
) -> dict[str, Any]:
    """自动保存一份制度及其条款，返回结构化统计。"""
    metadata = dict(metadata or {})
    metadata.setdefault("title", _guess_title(file_name, blocks))
    policy_id = upsert_policy_document(
        file_id=file_id,
        file_name=file_name,
        metadata=metadata,
        parse_quality=0.0,
        structure_version=structure_version,
        structure_status=PolicyStructureStatus.RUNNING,
    )
    try:
        clauses = build_policy_clauses(policy_id, blocks, structure_version)
        quality = calculate_policy_parse_quality(blocks, clauses)
        if not clauses:
            raise ValueError("未识别到任何制度条款")
        replace_policy_clauses(policy_id, clauses, structure_version)
        # replace_policy_clauses 已标记成功，这里只补写实际质量分数。
        from storage_adapter import storage
        storage.relational.update_rows(
            "policy_documents",
            {"parse_quality": quality},
            '"policy_id" = %s',
            (policy_id,),
        )
        if quality < 0.75:
            insert_review_item(
                ReviewItem(
                    review_id=f"review_{uuid.uuid4().hex}",
                    policy_id=policy_id,
                    issue_type="low_parse_quality",
                    description=f"制度结构化质量分数为 {quality:.3f}",
                    payload={"parse_quality": quality, "structure_version": structure_version},
                )
            )
        return {
            "policy_id": policy_id,
            "clause_count": len(clauses),
            "parse_quality": quality,
            "structure_version": structure_version,
            "status": PolicyStructureStatus.SUCCEEDED.value,
        }
    except Exception as exc:
        mark_policy_structure_failed(policy_id, str(exc), structure_version)
        raise

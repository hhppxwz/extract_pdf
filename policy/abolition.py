"""制度条款中的明确废止关系抽取与目标制度匹配。"""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from models import PolicyDocumentRelation, PolicyDocumentRelationType, PolicyReviewStatus
from policy.storage import (
    find_policy_documents_by_doc_number,
    find_policy_documents_by_normalized_title,
    get_policy_clauses,
    get_policy_document,
    get_policy_documents_for_batch,
    list_policy_document_relations,
    review_policy_document_relation,
    upsert_policy_document_relation,
)


_EFFECTIVE_DATE_RE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起(?:施行|执行|生效)"
)
_QUOTED_POLICY_RE = re.compile(
    r"《(?P<title>[^》]{2,100})》\s*(?:[（(](?P<number>[^）)]{1,80})[）)])?"
)
_ABOLITION_RE = re.compile(r"(?:同时|即)?\s*废止")
_TRAILING_ABOLITION_GAP_RE = re.compile(
    r"^[\s，,、；;。:：]*(?:(?:即|同时|予以|一并)[\s，,、；;。:：]*)*$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?\r\n]")
_NEGATED_ABOLITION_PREFIX_RE = re.compile(
    r"(?:没有|未予|不再|并非|禁止|不应|不予|不得|不|未)\s*$"
)
_PARTIAL_ABOLITION_SUFFIX_RE = re.compile(r"^\s*第[^。！？!?]{0,20}[条款项]")
_LEADING_TARGET_SUFFIX_RE = re.compile(r"^[\s，,、；;。:：]*$")


def _is_negated_abolition_prefix(raw_text: str, trigger_start: int) -> bool:
    """只检查当前废止触发词所在句子的前缀是否为否定表述。"""
    sentence_start = 0
    for boundary in _SENTENCE_BOUNDARY_RE.finditer(raw_text, 0, trigger_start):
        sentence_start = boundary.end()
    return bool(_NEGATED_ABOLITION_PREFIX_RE.search(
        raw_text[sentence_start:trigger_start]
    ))


def normalize_doc_number(value: str) -> str:
    """规范文号括号和空白，使等价的全半角写法可精确比较。"""
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text.translate(str.maketrans({
        "（": "[", "〔": "[", "(": "[",
        "）": "]", "〕": "]", ")": "]",
    }))


def extract_abolition_candidates(clause: dict[str, Any]) -> list[dict[str, Any]]:
    """只提取同句中与废止词直接连接的整份制度引用。"""
    raw_text = str(clause.get("raw_text") or "")
    date_match = _EFFECTIVE_DATE_RE.search(raw_text)
    effective_date = (
        f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        if date_match else None
    )
    abolition_matches = list(_ABOLITION_RE.finditer(raw_text))
    candidates: list[dict[str, Any]] = []
    for quoted_match in _QUOTED_POLICY_RE.finditer(raw_text):
        trailing_trigger = next(
            (
                match for match in abolition_matches
                if (
                    0 <= match.start() - quoted_match.end() <= 24
                    and not _SENTENCE_BOUNDARY_RE.search(
                        raw_text[quoted_match.end():match.start()]
                    )
                    and _TRAILING_ABOLITION_GAP_RE.fullmatch(
                        raw_text[quoted_match.end():match.start()]
                    )
                )
            ),
            None,
        )
        leading_trigger = next(
            (
                match for match in abolition_matches
                if (
                    quoted_match.start() == match.end()
                    and not _is_negated_abolition_prefix(raw_text, match.start())
                    and _LEADING_TARGET_SUFFIX_RE.fullmatch(raw_text[quoted_match.end():])
                )
            ),
            None,
        )
        if (
            (trailing_trigger is None and leading_trigger is None)
            or _PARTIAL_ABOLITION_SUFFIX_RE.match(raw_text[quoted_match.end():])
        ):
            continue
        candidates.append({
            "source_policy_id": str(clause.get("policy_id") or ""),
            "evidence_clause_id": str(clause.get("clause_id") or ""),
            "target_title": quoted_match.group("title").strip(),
            "target_doc_number": (quoted_match.group("number") or "").strip(),
            "effective_date": effective_date,
            # 完整条款确保引用在原文中连续、可复核。
            "evidence_text": raw_text,
            "page_start": int(clause.get("page_start") or 0),
            "page_end": int(clause.get("page_end") or 0),
        })
    return candidates


def resolve_abolition_target(target_title: str, target_doc_number: str) -> str | None:
    """按文号优先、标题精确唯一的顺序匹配目标制度。"""
    normalized_number = normalize_doc_number(target_doc_number)
    if normalized_number:
        by_number = find_policy_documents_by_doc_number(normalized_number)
        if len(by_number) == 1:
            return str(by_number[0]["policy_id"])
    by_title = find_policy_documents_by_normalized_title(target_title)
    if len(by_title) == 1:
        return str(by_title[0]["policy_id"])
    return None


def build_abolition_relations(clause: dict[str, Any]) -> list[PolicyDocumentRelation]:
    """构造待审核废止关系，不写库且不改变任何制度效力状态。"""
    relations: list[PolicyDocumentRelation] = []
    for candidate in extract_abolition_candidates(clause):
        relations.append(PolicyDocumentRelation(
            relation_id=f"relation_{uuid.uuid4().hex}",
            source_policy_id=candidate["source_policy_id"],
            target_policy_id=resolve_abolition_target(
                candidate["target_title"], candidate["target_doc_number"]
            ),
            target_title=candidate["target_title"],
            target_doc_number=candidate["target_doc_number"],
            relation_type=PolicyDocumentRelationType.ABOLISHES,
            effective_date=candidate["effective_date"],
            evidence_clause_id=candidate["evidence_clause_id"],
            evidence_text=candidate["evidence_text"],
            page_start=candidate["page_start"],
            page_end=candidate["page_end"],
            confidence=1.0 if candidate["effective_date"] else 0.7,
            review_status=PolicyReviewStatus.PENDING,
        ))
    return relations


def extract_batch_abolition_relations(batch_id: str) -> dict[str, int]:
    """抽取一个已结构化批次中的废止候选，并保留未解析目标。"""
    documents = get_policy_documents_for_batch(batch_id)
    if not documents:
        raise ValueError(f"批次没有已结构化的制度文档: {batch_id}")

    summary = {"policies": len(documents), "clauses": 0, "candidates": 0, "resolved": 0, "unresolved": 0}
    for document in documents:
        clauses = get_policy_clauses(str(document["policy_id"]))
        summary["clauses"] += len(clauses)
        for clause in clauses:
            for relation in build_abolition_relations(clause):
                upsert_policy_document_relation(relation)
                summary["candidates"] += 1
                if relation.target_policy_id:
                    summary["resolved"] += 1
                else:
                    summary["unresolved"] += 1
    return summary


def get_batch_abolition_relation_status(batch_id: str) -> dict[str, Any]:
    """汇总一个批次候选的审核和目标解析状态。"""
    summary: dict[str, Any] = {
        "total": 0, "pending": 0, "approved": 0, "rejected": 0,
        "resolved": 0, "unresolved": 0,
    }
    for relation in list_policy_document_relations(batch_id=batch_id):
        summary["total"] += 1
        status = str(relation.get("review_status") or "")
        if status in {"pending", "approved", "rejected"}:
            summary[status] += 1
        if str(relation.get("target_policy_id") or "").strip():
            summary["resolved"] += 1
        else:
            summary["unresolved"] += 1
    return summary


def _print_abolition_review_candidate(relation: dict[str, Any], position: int, total: int) -> None:
    """打印审核制度废止候选所需的完整上下文。"""
    source = get_policy_document(policy_id=str(relation.get("source_policy_id") or "")) or {}
    print("\n" + "=" * 72)
    print(f"审核 {position}/{total} | 关系 ID: {relation.get('relation_id', '')}")
    print(f"来源制度: {source.get('title') or source.get('file_name') or relation.get('source_policy_id', '')}")
    print(f"原文证据: {relation.get('evidence_text', '')}")
    print(f"目标标题: {relation.get('target_title', '')}")
    print(f"目标文号: {relation.get('target_doc_number', '')}")
    print(f"目标制度 ID: {relation.get('target_policy_id') or '未解析'}")
    print(f"废止日期: {relation.get('effective_date') or '未识别'}")


def _read_review_date() -> str:
    """读取并校验人工确认的 ISO 废止日期。"""
    value = input("废止日期（YYYY-MM-DD）: ").strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("废止日期必须是 YYYY-MM-DD") from exc


def _read_review_date_or_keep(existing_date: str | None) -> str | None:
    """读取人工日期；直接回车时保留已识别日期。"""
    value = input("废止日期（YYYY-MM-DD，直接回车保留原日期）: ").strip()
    if not value:
        return existing_date
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("废止日期必须是 YYYY-MM-DD") from exc


def run_interactive_abolition_review(batch_id: str, reviewer: str, limit: int = 20) -> dict[str, int]:
    """在终端审核待处理的废止候选。"""
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("审核人不能为空")
    if limit < 1:
        raise ValueError("审核上限必须大于等于 1")
    candidates = [
        relation for relation in list_policy_document_relations(batch_id=batch_id)
        if relation.get("review_status") == PolicyReviewStatus.PENDING.value
    ][:limit]
    summary = {"approved": 0, "rejected": 0, "skipped": 0}
    if not candidates:
        print("没有待人工审核的废止候选。")
        return summary

    for position, relation in enumerate(candidates, start=1):
        _print_abolition_review_candidate(relation, position, len(candidates))
        while True:
            action = input("选择 [a]通过 [r]拒绝 [t]指定目标及日期后通过 [d]指定日期及目标后通过 [s]跳过 [q]结束: ").strip().lower()
            if action == "q":
                return summary
            if action == "s":
                summary["skipped"] += 1
                break
            if action not in {"a", "r", "t", "d"}:
                print("请输入 a、r、t、d、s 或 q。")
                continue
            try:
                target_policy_id = None
                effective_date = None
                decision = PolicyReviewStatus.APPROVED.value
                if action == "r":
                    decision = PolicyReviewStatus.REJECTED.value
                elif action == "t":
                    target_policy_id = input("目标制度 ID: ").strip()
                    if not target_policy_id:
                        raise ValueError("目标制度 ID 不能为空")
                    effective_date = _read_review_date_or_keep(relation.get("effective_date"))
                elif action == "d":
                    effective_date = _read_review_date()
                    target_policy_id = str(relation.get("target_policy_id") or "").strip() or None
                    if not target_policy_id:
                        target_policy_id = input("目标制度 ID: ").strip()
                        if not target_policy_id:
                            raise ValueError("目标制度 ID 不能为空")
                review_policy_document_relation(
                    str(relation["relation_id"]), decision, reviewer,
                    target_policy_id=target_policy_id, effective_date=effective_date,
                )
                summary[decision] += 1
                print("已保存审核结果。")
                break
            except ValueError as exc:
                print(f"未保存：{exc}")
    return summary

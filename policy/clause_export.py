"""批次处理完成后导出制度条款重组 JSON。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def build_batch_clause_structure(
    batch_id: str,
    documents: list[dict[str, Any]],
    clauses_by_policy: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """按文档与条款顺序组织可人工核查的结构化结果。"""
    exported_documents: list[dict[str, Any]] = []
    clause_fields = (
        "clause_id",
        "parent_clause_id",
        "level",
        "chapter_path",
        "article_no",
        "paragraph_no",
        "item_no",
        "raw_text",
        "page_start",
        "page_end",
        "sequence_no",
    )
    document_fields = (
        "policy_id",
        "file_id",
        "file_name",
        "title",
        "document_no",
        "structure_version",
    )
    for document in sorted(
        documents,
        key=lambda item: (str(item.get("file_name") or ""), str(item.get("policy_id") or "")),
    ):
        policy_id = str(document.get("policy_id") or "")
        clauses = sorted(
            clauses_by_policy.get(policy_id, []),
            key=lambda item: (int(item.get("sequence_no") or 0), str(item.get("clause_id") or "")),
        )
        exported_document = {
            field: document.get(field) or ""
            for field in document_fields
        }
        exported_document["clauses"] = [
            {
                field: list(clause.get(field) or []) if field == "chapter_path" else clause.get(field) or ""
                for field in clause_fields
            }
            for clause in clauses
        ]
        exported_documents.append(exported_document)
    return {"batch_id": str(batch_id), "documents": exported_documents}


def export_batch_clause_structure(
    batch_id: str,
    output_path: str | Path,
    document_loader: Callable[[str], list[dict[str, Any]]] | None = None,
    clause_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> Path:
    """导出批次内所有已完成制度的条款重组结果。"""
    if document_loader is None or clause_loader is None:
        from policy.storage import get_policy_clauses, get_policy_documents_for_batch

        document_loader = document_loader or get_policy_documents_for_batch
        clause_loader = clause_loader or get_policy_clauses

    documents = document_loader(str(batch_id))
    clauses_by_policy = {
        str(document.get("policy_id") or ""): clause_loader(str(document.get("policy_id") or ""))
        for document in documents
    }
    payload = build_batch_clause_structure(str(batch_id), documents, clauses_by_policy)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination

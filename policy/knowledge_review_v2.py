"""制度知识 V2 的抽样复核与事项流程视图导出。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Callable

from policy.knowledge_v2 import build_document_type_graph, build_workflow_view


REVIEW_FIELDS = (
    "运行ID", "制度名称", "文件名", "条款ID", "系统原文", "断言ID", "断言类型",
    "规范模态", "断言内容", "所属类别", "类别来源条款ID", "原文证据", "机器状态", "无效原因", "人工结论", "修正JSON", "备注",
)


def _text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else ""


def build_assertion_review_rows(run_id: str, assertions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """生成系统字段只读、人工字段留空的抽样复核记录。"""
    rows: list[dict[str, str]] = []
    for item in assertions:
        document = item.get("document") if isinstance(item.get("document"), dict) else {}
        clause = item.get("clause") if isinstance(item.get("clause"), dict) else {}
        subject, predicate, object_value = _text(item.get("subject")), _text(item.get("predicate")), _text(item.get("object"))
        category = item.get("category") if isinstance(item.get("category"), dict) else {}
        content = f"{subject or '(无主体)'} --{predicate}--> {object_value or '(无客体)'}"
        rows.append({
            "运行ID": run_id,
            "制度名称": str(document.get("title") or document.get("file_name") or ""),
            "文件名": str(document.get("file_name") or ""),
            "条款ID": str(item.get("clause_id") or ""),
            "系统原文": str(clause.get("raw_text") or ""),
            "断言ID": str(item.get("assertion_id") or ""),
            "断言类型": str(item.get("kind") or ""),
            "规范模态": str(item.get("modality") or ""),
            "断言内容": content,
            "所属类别": str(category.get("text") or ""),
            "类别来源条款ID": str(category.get("source_clause_id") or ""),
            "原文证据": _text(item.get("evidence")) or str(item.get("evidence_text") or ""),
            "机器状态": str(item.get("status") or ""),
            "无效原因": str(item.get("error") or item.get("_validation_error") or ""),
            "人工结论": "",
            "修正JSON": "",
            "备注": "",
        })
    return rows


def export_assertion_review_v2(run_id: str, output_path: str | Path, limit: int = 200) -> Path:
    """导出最多指定数量的断言复核 CSV。"""
    from policy.storage import get_policy_assertion_review_rows_v2

    rows = build_assertion_review_rows(run_id, get_policy_assertion_review_rows_v2(run_id)[:limit])
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def validate_review_target(row: dict[str, str], assertion: dict[str, Any] | None) -> dict[str, Any]:
    """防止复核文件通过篡改 ID 将一条款内容写入另一断言。"""
    if not assertion:
        raise ValueError("复核目标断言不存在或不属于指定运行")
    if str(assertion.get("clause_id") or "") != str(row.get("条款ID") or ""):
        raise ValueError("复核行的条款 ID 与目标断言不一致")
    return assertion


def import_assertion_review_v2(csv_path: str | Path, reviewer: str) -> dict[str, Any]:
    """导入抽样复核；修正内容再次经过 V2 证据校验。"""
    from policy.knowledge_v2 import validate_assertion
    from policy.storage import (
        get_policy_clause,
        get_policy_clauses,
        get_policy_assertion_v2,
        review_policy_assertion_v2,
    )

    if not str(reviewer or "").strip():
        raise ValueError("审核人不能为空")
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    decisions = {"通过": "approved", "approved": "approved", "拒绝": "rejected", "rejected": "rejected", "修正": "corrected", "corrected": "corrected"}
    prepared: list[tuple[dict[str, str], str, dict[str, Any] | None]] = []
    affected_runs: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        raw_decision = str(row.get("人工结论") or "").strip().lower()
        if not raw_decision:
            continue
        decision = decisions.get(raw_decision)
        if not decision:
            raise ValueError(f"第 {line_no} 行人工结论只能填写通过、修正或拒绝")
        run_id = str(row.get("运行ID") or "")
        assertion_id = str(row.get("断言ID") or "")
        target = validate_review_target(row, get_policy_assertion_v2(run_id, assertion_id))
        corrected = None
        if decision == "corrected":
            try:
                corrected = json.loads(str(row.get("修正JSON") or ""))
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_no} 行修正JSON非法") from exc
            clause = get_policy_clause(str(target.get("clause_id") or ""))
            if not clause:
                raise ValueError(f"第 {line_no} 行条款不存在")
            context = {str(item["clause_id"]): item for item in get_policy_clauses(str(clause["policy_id"]))}
            corrected = validate_assertion(corrected, context)
            corrected["assertion_id"] = assertion_id
        prepared.append((row, decision, corrected))
        affected_runs.add(run_id)
    from storage_adapter import storage

    counts = {"approved": 0, "corrected": 0, "rejected": 0}
    with storage.relational.transaction():
        for row, decision, corrected in prepared:
            review_policy_assertion_v2(
                str(row.get("运行ID") or ""), str(row.get("断言ID") or ""), decision,
                reviewer, str(row.get("备注") or ""), corrected,
            )
            counts[decision] += 1
    from policy.knowledge_retrieval_v2 import sync_assertion_index_v2

    for run_id in affected_runs:
        sync_assertion_index_v2(run_id)
    reviewed_count = sum(counts.values())
    report: dict[str, Any] = {
        **counts,
        "reviewed_count": reviewed_count,
        "accepted_rate": (counts["approved"] + counts["corrected"]) / reviewed_count if reviewed_count else 0.0,
        "exact_match_rate": counts["approved"] / reviewed_count if reviewed_count else 0.0,
    }
    report_path = Path(csv_path).with_name("V2断言质量报告.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**report, "report_path": str(report_path)}


def export_knowledge_view_v2(
    run_id: str,
    output_path: str | Path,
    *,
    matter_loader: Callable[[str], list[dict[str, Any]]] | None = None,
    clause_loader: Callable[[str], list[dict[str, Any]]] | None = None,
    assertion_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> Path:
    """导出事项、流程及公文种类关系；每个成员保留条款证据。"""
    if matter_loader is None or clause_loader is None or assertion_loader is None:
        from policy.storage import get_policy_assertions_v2, get_policy_clauses, get_policy_matters_v2

        matter_loader = matter_loader or get_policy_matters_v2
        clause_loader = clause_loader or get_policy_clauses
        assertion_loader = assertion_loader or (lambda value: get_policy_assertions_v2(value, usable_only=True))
    matters = matter_loader(run_id)
    assertions = assertion_loader(run_id)
    clauses_by_policy: dict[str, list[dict[str, Any]]] = {}
    output: list[dict[str, Any]] = []
    for matter in matters:
        policy_id = str(matter.get("policy_id") or "")
        if policy_id not in clauses_by_policy:
            clauses_by_policy[policy_id] = clause_loader(policy_id)
        clauses = clauses_by_policy[policy_id]
        output.append({**matter, "workflow": build_workflow_view(matter, clauses, assertions)})
    for assertion in assertions:
        payload = assertion.get("payload") if isinstance(assertion.get("payload"), dict) else assertion
        if isinstance(payload.get("category"), dict):
            policy_id = str(assertion.get("policy_id") or "")
            if policy_id not in clauses_by_policy:
                clauses_by_policy[policy_id] = clause_loader(policy_id)
    document_type_graph = build_document_type_graph(
        [clause for clauses in clauses_by_policy.values() for clause in clauses], assertions,
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "matters": output, "document_type_graph": document_type_graph}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

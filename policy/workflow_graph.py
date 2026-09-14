"""将已审核制度候选动态投影为可追溯的办事流程图谱。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


_VISIBLE_REVIEW_STATUSES = {"auto_approved", "approved"}


def _as_mapping(value: object) -> dict[str, Any]:
    """兼容数据库 JSONB 返回值与测试传入的字典。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _citation(item: dict[str, Any], evidence_text: str) -> dict[str, Any]:
    """为每个投影节点或边保留可回查的制度、条款和页码证据。"""
    clause = _as_mapping(item.get("clause"))
    document = _as_mapping(item.get("document"))
    return {
        "policy_id": str(document.get("policy_id") or clause.get("policy_id") or ""),
        "file_name": str(document.get("file_name") or ""),
        "title": str(document.get("title") or document.get("file_name") or ""),
        "clause_id": str(clause.get("clause_id") or item.get("clause_id") or ""),
        "page_start": int(clause.get("page_start") or 0),
        "page_end": int(clause.get("page_end") or clause.get("page_start") or 0),
        "evidence_text": str(evidence_text or ""),
    }


def _latest_annotations(annotations: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """按候选 ID 取最后一条人工结论，并单独保留人工补充项。"""
    by_candidate: dict[str, dict[str, Any]] = {}
    added: list[dict[str, Any]] = []
    for annotation in annotations:
        decision = str(annotation.get("decision") or "")
        if decision == "added":
            added.append(annotation)
            continue
        candidate_id = str(annotation.get("candidate_id") or "")
        if candidate_id:
            by_candidate[candidate_id] = annotation
    return by_candidate, added


def build_policy_workflow_graph(
    run_id: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """仅以已审核候选和人工标注投影流程图谱，不写入新的事实表。"""
    if not str(run_id or "").strip():
        raise ValueError("流程图谱抽取运行 ID 不能为空")
    if candidates is None:
        from policy.storage import get_policy_process_graph_candidates

        candidates = get_policy_process_graph_candidates(run_id)
    if annotations is None:
        from policy.storage import list_policy_manual_annotations

        annotations = list_policy_manual_annotations(run_id)

    overrides, additions = _latest_annotations(annotations)
    nodes: dict[str, dict[str, Any]] = {}
    relation_rows: list[dict[str, Any]] = []

    def add_node(node_id: str, data: dict[str, Any], source_item: dict[str, Any]) -> None:
        entity_type = str(data.get("entity_type") or "")
        name = str(data.get("name") or "").strip()
        evidence = str(data.get("evidence_text") or "").strip()
        if not node_id or not entity_type or not name or not evidence:
            return
        nodes[node_id] = {
            "id": node_id,
            "type": entity_type,
            "name": name,
            "citations": [_citation(source_item, evidence)],
        }

    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        annotation = overrides.get(candidate_id)
        if annotation and str(annotation.get("decision") or "") == "rejected":
            continue
        if str(candidate.get("review_status") or "") not in _VISIBLE_REVIEW_STATUSES:
            continue
        replacement = _as_mapping(annotation.get("label_data")) if annotation else {}
        data = {**candidate, **replacement}
        if str(candidate.get("candidate_type") or "") == "entity":
            add_node(str(candidate.get("entity_id") or candidate_id), data, candidate)
        elif str(candidate.get("candidate_type") or "") == "relation":
            relation_rows.append(data)

    for annotation in additions:
        data = _as_mapping(annotation.get("label_data"))
        kind = str(annotation.get("candidate_kind") or "")
        source = {"clause_id": annotation.get("clause_id")}
        if kind == "entity":
            add_node(f"annotation:{annotation.get('annotation_id')}", data, source)
        elif kind == "relation":
            relation_rows.append({**data, "relation_id": f"annotation:{annotation.get('annotation_id')}", **source})

    edges: list[dict[str, Any]] = []
    for relation in relation_rows:
        source = str(relation.get("subject_entity_id") or "")
        target = str(relation.get("object_entity_id") or "")
        relation_type = str(relation.get("relation_type") or "")
        evidence = str(relation.get("evidence_text") or "")
        if not source or not target or source not in nodes or target not in nodes or not relation_type or not evidence:
            continue
        edges.append({
            "id": str(relation.get("relation_id") or ""),
            "type": relation_type,
            "source": source,
            "target": target,
            "citations": [_citation(relation, evidence)],
        })

    return {
        "run_id": run_id,
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges, key=lambda item: item["id"]),
    }


def export_policy_workflow_graph(
    run_id: str,
    output_path: str | Path,
    *,
    candidate_loader: Any = None,
    annotation_loader: Any = None,
) -> Path:
    """导出动态投影的办事流程图谱 JSON，不回写数据库。"""
    candidates = candidate_loader(run_id) if candidate_loader else None
    annotations = annotation_loader(run_id) if annotation_loader else None
    graph = build_policy_workflow_graph(run_id, candidates=candidates, annotations=annotations)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

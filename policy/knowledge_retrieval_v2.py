"""制度知识 V2 的断言索引和条款检索增强。"""
from __future__ import annotations

from typing import Any


POLICY_ASSERTION_INDEX_V2 = "policy_assertion_search_v2"


def _update_index_status(run_id: str, status: str, error: str = "") -> None:
    """记录索引发布状态；旧库迁移期间记录失败不应掩盖原始索引异常。"""
    try:
        from policy.storage import update_policy_knowledge_run_v2

        update_policy_knowledge_run_v2(run_id, {"index_status": status, "index_error": error[:2000]})
    except Exception:
        pass


def assertion_index_text(assertion: dict[str, Any]) -> str:
    """将结构化断言转换为只用于召回的文本。"""
    payload = assertion.get("payload") if isinstance(assertion.get("payload"), dict) else assertion
    parts = [
        str(assertion.get("kind") or payload.get("kind") or ""),
        str(assertion.get("modality") or payload.get("modality") or ""),
        str(assertion.get("subject_text") or (payload.get("subject") or {}).get("text") or ""),
        str(assertion.get("predicate_text") or (payload.get("predicate") or {}).get("text") or ""),
        str(assertion.get("object_text") or (payload.get("object") or {}).get("text") or ""),
        str(assertion.get("receiver_text") or (payload.get("receiver") or {}).get("text") or ""),
        str(assertion.get("evidence_text") or (payload.get("evidence") or {}).get("text") or ""),
        str((payload.get("category") or {}).get("text") or ""),
    ]
    return " ".join(part.strip() for part in parts if part.strip())


def sync_assertion_index_v2(run_id: str) -> int:
    """为一次成功运行建立独立断言向量索引。"""
    from policy.retrieval import _encode_policy_clause_texts
    from policy.storage import (
        get_policy_assertions_v2, get_policy_documents_for_batch, get_policy_knowledge_run_v2,
        is_latest_successful_policy_knowledge_run_v2,
    )
    from storage_adapter import storage

    try:
        if not is_latest_successful_policy_knowledge_run_v2(run_id):
            return 0
    except Exception:
        # 测试桩和旧库迁移阶段无法判断时，继续依靠调用方保证运行有效。
        pass
    all_assertions = get_policy_assertions_v2(run_id, usable_only=False)
    assertions = [item for item in all_assertions if str(item.get("status") or "") in {
        "machine_extracted", "approved", "corrected",
    }]
    policy_ids = {str(item.get("policy_id") or "") for item in all_assertions if item.get("policy_id")}
    try:
        run = get_policy_knowledge_run_v2(run_id) or {}
        policy_ids.update(
            str(item.get("policy_id") or "")
            for item in get_policy_documents_for_batch(str(run.get("batch_id") or ""))
            if item.get("policy_id")
        )
    except Exception:
        # 兼容旧库或测试桩；已有断言仍足以确定需要替换的制度。
        pass
    # 同一制度的新运行替换旧断言，避免历史运行重复或陈旧结果参与问答。
    for policy_id in sorted(policy_ids):
        try:
            storage.vector.delete_vectors_by_metadata(POLICY_ASSERTION_INDEX_V2, "policy_id", policy_id)
        except Exception:
            # 首次运行索引尚不存在时无需清理。
            pass
    texts = [assertion_index_text(item) for item in assertions]
    try:
        vectors = _encode_policy_clause_texts(texts) if texts else []
    except Exception as exc:
        _update_index_status(run_id, "failed", str(exc))
        raise
    if not vectors:
        _update_index_status(run_id, "synced")
        return 0
    storage.vector.create_index(POLICY_ASSERTION_INDEX_V2, len(vectors[0]))
    metadata = [{
        "run_id": run_id,
        "assertion_id": str(item.get("assertion_id") or ""),
        "policy_id": str(item.get("policy_id") or ""),
        "clause_id": str(item.get("clause_id") or ""),
        "kind": str(item.get("kind") or ""),
        "modality": str(item.get("modality") or ""),
        "subject_text": str(item.get("subject_text") or ""),
        "predicate_text": str(item.get("predicate_text") or ""),
        "object_text": str(item.get("object_text") or ""),
        "receiver_text": str(item.get("receiver_text") or ""),
    } for item in assertions]
    try:
        count = storage.vector.insert_vectors(POLICY_ASSERTION_INDEX_V2, vectors, metadata)
    except Exception as exc:
        _update_index_status(run_id, "failed", str(exc))
        raise
    _update_index_status(run_id, "synced")
    return count


def search_assertions_v2(query: str, top_k: int = 20) -> list[dict[str, Any]]:
    """搜索可用断言；索引不存在时由调用方降级。"""
    from policy.retrieval import _encode_policy_clause_texts
    from storage_adapter import storage

    if storage.vector.count_vectors(POLICY_ASSERTION_INDEX_V2) < 1:
        raise RuntimeError("V2 断言索引为空")
    vector = _encode_policy_clause_texts([query])[0]
    rows = storage.vector.search(POLICY_ASSERTION_INDEX_V2, vector, top_k=top_k)
    results: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row.get("metadata") or {})
        results.append({
            **metadata,
            "score": float(row.get("similarity") or row.get("score") or 0.0),
        })
    return results


def merge_assertion_matches(
    candidates: list[dict[str, Any]], matches: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """断言只提升已有条款，不成为最终引用。"""
    if not matches:
        return list(candidates), {
            "retrieval_mode": "clause_only", "matched_assertions": [],
            "matched_matter": None, "workflow_summary": None,
        }
    best_by_clause: dict[str, float] = {}
    for match in matches:
        clause_id = str(match.get("clause_id") or "")
        best_by_clause[clause_id] = max(best_by_clause.get(clause_id, 0.0), float(match.get("score") or 0.0))
    enhanced: list[dict[str, Any]] = []
    for candidate in candidates:
        metadata = dict(candidate.get("metadata") or {})
        clause_id = str(metadata.get("clause_id") or candidate.get("id") or "")
        original = float(candidate.get("score", candidate.get("similarity", 0.0)) or 0.0)
        boost = min(0.2, best_by_clause.get(clause_id, 0.0) * 0.2)
        enhanced.append({**candidate, "metadata": metadata, "score": round(original + boost, 6), "knowledge_boost": round(boost, 6)})
    enhanced.sort(key=lambda item: (-float(item.get("score") or 0.0), str((item.get("metadata") or {}).get("clause_id") or "")))
    public_matches = [{
        key: match.get(key) for key in (
            "assertion_id", "clause_id", "kind", "modality", "subject_text",
            "predicate_text", "object_text", "receiver_text", "score",
        ) if match.get(key) not in (None, "")
    } for match in matches]
    return enhanced, {
        "retrieval_mode": "clause_plus_knowledge",
        "matched_assertions": public_matches,
        "matched_matter": None,
        "workflow_summary": None,
    }


def resolve_match_context(
    matches: list[dict[str, Any]],
    matters: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    """把首个断言命中解析为明确事项及其非权威流程摘要。"""
    from policy.knowledge_v2 import build_workflow_view

    matched_clause_ids = {str(item.get("clause_id") or "") for item in matches}
    matter = next(
        (item for item in matters if matched_clause_ids.intersection(set(item.get("clause_ids") or []))),
        None,
    )
    if not matter:
        return {"matched_matter": None, "workflow_summary": None}
    return {
        "matched_matter": {
            "matter_id": str(matter.get("matter_id") or ""),
            "name": str(matter.get("name") or ""),
            "policy_id": str(matter.get("policy_id") or ""),
            "clause_ids": list(matter.get("clause_ids") or []),
        },
        "workflow_summary": build_workflow_view(matter, clauses, assertions),
    }


def _load_match_context(matches: list[dict[str, Any]]) -> dict[str, Any]:
    if not matches:
        return {"matched_matter": None, "workflow_summary": None}
    run_id = str(matches[0].get("run_id") or "")
    if not run_id:
        return {"matched_matter": None, "workflow_summary": None}
    from policy.storage import get_policy_assertions_v2, get_policy_clauses, get_policy_matters_v2

    matters = get_policy_matters_v2(run_id)
    policy_id = str(next((item.get("policy_id") for item in matters if set(item.get("clause_ids") or []).intersection(
        {str(match.get("clause_id") or "") for match in matches}
    )), "") or "")
    clauses = get_policy_clauses(policy_id) if policy_id else []
    assertions = get_policy_assertions_v2(run_id, policy_id=policy_id, usable_only=True) if policy_id else []
    return resolve_match_context(matches, matters, clauses, assertions)


def expand_matched_matter_candidates(
    candidates: list[dict[str, Any]], matches: list[dict[str, Any]], as_of: str | None,
) -> list[dict[str, Any]]:
    """把命中事项中的少量原始条款加入候选，并继续执行制度时效过滤。"""
    context = _load_match_context(matches)
    matter = context.get("matched_matter") or {}
    member_ids = [str(value) for value in matter.get("clause_ids") or []]
    if not member_ids:
        return list(candidates)
    from policy.retrieval import build_clause_metadata
    from policy.storage import get_policy_clause, get_policy_document

    seen = {
        str((item.get("metadata") or {}).get("clause_id") or item.get("id") or "")
        for item in candidates
    }
    expansion_score = max(
        (float(item.get("score", item.get("similarity", 0.0)) or 0.0) for item in candidates),
        default=0.1,
    ) * 0.9
    additions: list[dict[str, Any]] = []
    for clause_id in member_ids:
        if clause_id in seen or len(additions) >= 6:
            continue
        clause = get_policy_clause(clause_id)
        if not clause:
            continue
        document = get_policy_document(policy_id=str(clause.get("policy_id") or "")) or {}
        additions.append({
            "id": clause_id,
            "metadata": build_clause_metadata(clause, document),
            "score": round(expansion_score, 6),
            "similarity": 0.0,
            "keyword_score": 0.0,
            "knowledge_expansion": True,
            "policy_document": document,
        })
    if as_of and additions:
        from policy.temporal import rank_temporal_candidates

        documents = {
            str((item.get("metadata") or {}).get("policy_id") or ""): dict(item.get("policy_document") or {})
            for item in additions
        }
        additions, warnings = rank_temporal_candidates(additions, documents, as_of, len(additions))
        for item in additions:
            item["temporal_warnings"] = warnings
    return [*candidates, *additions]


def enhance_clause_candidates_v2(
    query: str, candidates: list[dict[str, Any]], as_of: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """最佳努力增强；任何 V2 故障都安全退回原条款顺序。"""
    try:
        matches = search_assertions_v2(query)
        expanded = expand_matched_matter_candidates(candidates, matches, as_of)
        enhanced, info = merge_assertion_matches(expanded, matches)
        info.update(_load_match_context(matches))
        return enhanced, info
    except Exception:
        return merge_assertion_matches(candidates, [])

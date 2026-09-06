"""制度抽取结果的命令行人工审核服务。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from models import (
    PolicyAnnotationDecision,
    PolicyCandidateKind,
    PolicyEntityType,
    PolicyManualAnnotation,
    PolicyRelationType,
    PolicyReviewStatus,
)
from policy_storage import (
    get_policy_clause,
    get_policy_extraction_run,
    get_policy_review_candidate,
    insert_manual_annotation,
    list_policy_review_candidates,
    update_policy_candidate_review_status,
)


GUIDELINE_VERSION = "manual-annotation-v1"


def _candidate_id(candidate_kind: PolicyCandidateKind, candidate: dict[str, Any]) -> str:
    """从实体或关系候选中取得对应主键。"""
    key = "entity_id" if candidate_kind == PolicyCandidateKind.ENTITY else "relation_id"
    candidate_id = str(candidate.get(key) or "").strip()
    if not candidate_id:
        raise ValueError(f"{candidate_kind.value} 候选缺少 {key}")
    return candidate_id


def _candidate_payload(candidate_kind: PolicyCandidateKind, candidate: dict[str, Any]) -> dict[str, Any]:
    """复制需要留存的原始候选字段，避免审核时修改模型结果。"""
    if candidate_kind == PolicyCandidateKind.ENTITY:
        keys = ("entity_type", "name", "raw_text", "normalized_value", "evidence_text")
    else:
        keys = (
            "relation_type", "subject_entity_id", "object_entity_id", "target_policy_id",
            "target_text", "evidence_text",
        )
    return {key: candidate.get(key) for key in keys if key in candidate}


def _find_evidence_span(
    source_text: str,
    evidence_text: str,
    evidence_start: Optional[int] = None,
) -> tuple[Optional[int], Optional[int]]:
    """定位连续证据文本；重复出现时必须由审核人指定起始位置。"""
    evidence = str(evidence_text or "")
    if not evidence:
        return None, None
    if evidence_start is not None:
        if evidence_start < 0 or source_text[evidence_start:evidence_start + len(evidence)] != evidence:
            raise ValueError("证据起始位置与条款原文不匹配")
        return evidence_start, evidence_start + len(evidence)

    positions: list[int] = []
    offset = 0
    while True:
        position = source_text.find(evidence, offset)
        if position < 0:
            break
        positions.append(position)
        offset = position + 1
    if not positions:
        raise ValueError("证据文本不在条款原文中，不能保存可训练标注")
    if len(positions) > 1:
        raise ValueError("证据文本在条款中重复出现，请输入 evidence_start 指定位置")
    return positions[0], positions[0] + len(evidence)


def _validate_label_data(
    candidate_kind: PolicyCandidateKind,
    decision: PolicyAnnotationDecision,
    label_data: dict[str, Any],
) -> None:
    """校验人工修正或补充的数据符合首期实体和关系白名单。"""
    if decision not in {PolicyAnnotationDecision.CORRECTED, PolicyAnnotationDecision.ADDED}:
        return
    if candidate_kind == PolicyCandidateKind.ENTITY:
        entity_type = str(label_data.get("entity_type") or "").strip()
        if not entity_type or not str(label_data.get("name") or "").strip():
            raise ValueError("实体修正或补充必须填写 entity_type 和 name")
        try:
            PolicyEntityType(entity_type)
        except ValueError as exc:
            raise ValueError("entity_type 不在允许范围内") from exc
    else:
        relation_type = str(label_data.get("relation_type") or "").strip()
        if not relation_type:
            raise ValueError("关系修正或补充必须填写 relation_type")
        try:
            PolicyRelationType(relation_type)
        except ValueError as exc:
            raise ValueError("relation_type 不在允许范围内") from exc


def build_manual_annotation(
    run_id: str,
    clause: dict[str, Any],
    candidate_kind: str | PolicyCandidateKind,
    candidate: Optional[dict[str, Any]],
    decision: str | PolicyAnnotationDecision,
    reviewer: str,
    label_data: Optional[dict[str, Any]] = None,
    review_note: str = "",
    evidence_start: Optional[int] = None,
) -> PolicyManualAnnotation:
    """构造含证据字符位置的人工标注，不执行数据库写入。"""
    kind = PolicyCandidateKind(candidate_kind)
    annotation_decision = PolicyAnnotationDecision(decision)
    if annotation_decision == PolicyAnnotationDecision.ADDED:
        candidate_id = None
    elif candidate is None:
        raise ValueError("通过、拒绝或修正必须指定原始候选")
    else:
        candidate_id = _candidate_id(kind, candidate)

    payload = dict(label_data or _candidate_payload(kind, candidate or {}))
    _validate_label_data(kind, annotation_decision, payload)
    evidence_text = str(payload.get("evidence_text") or "").strip()
    needs_span = annotation_decision in {
        PolicyAnnotationDecision.APPROVED,
        PolicyAnnotationDecision.CORRECTED,
        PolicyAnnotationDecision.ADDED,
    }
    if needs_span and not evidence_text:
        raise ValueError("通过、修正或补充必须提供 evidence_text")
    if needs_span:
        start, end = _find_evidence_span(
            str(clause.get("raw_text") or ""),
            evidence_text,
            evidence_start,
        )
    else:
        # 拒绝标签不作为正例训练数据，不需要为重复证据强行选择位置。
        start, end = None, None
    return PolicyManualAnnotation(
        annotation_id=f"annotation_{uuid.uuid4().hex}",
        run_id=run_id,
        clause_id=str(clause["clause_id"]),
        candidate_kind=kind,
        candidate_id=candidate_id,
        decision=annotation_decision,
        label_data=payload,
        evidence_start=start,
        evidence_end=end,
        reviewer=str(reviewer or "").strip(),
        review_note=str(review_note or "").strip(),
        guideline_version=GUIDELINE_VERSION,
    )


def review_policy_candidate(
    run_id: str,
    candidate_kind: str | PolicyCandidateKind,
    candidate_id: str,
    decision: str | PolicyAnnotationDecision,
    reviewer: str,
    label_data: Optional[dict[str, Any]] = None,
    review_note: str = "",
    evidence_start: Optional[int] = None,
) -> PolicyManualAnnotation:
    """审核一个模型候选，并以追加记录方式保存人工结论。"""
    kind = PolicyCandidateKind(candidate_kind)
    annotation_decision = PolicyAnnotationDecision(decision)
    if annotation_decision == PolicyAnnotationDecision.ADDED:
        raise ValueError("补充信息请使用 add_policy_annotation")
    candidate = get_policy_review_candidate(run_id, kind, candidate_id)
    if not candidate:
        raise ValueError("未找到指定运行中的审核候选")
    clause = get_policy_clause(candidate["clause_id"])
    if not clause:
        raise ValueError("候选对应的条款不存在")
    annotation = build_manual_annotation(
        run_id=run_id,
        clause=clause,
        candidate_kind=kind,
        candidate=candidate,
        decision=annotation_decision,
        reviewer=reviewer,
        label_data=label_data,
        review_note=review_note,
        evidence_start=evidence_start,
    )
    insert_manual_annotation(annotation)
    status = (
        PolicyReviewStatus.APPROVED
        if annotation_decision == PolicyAnnotationDecision.APPROVED
        else PolicyReviewStatus.REJECTED
    )
    update_policy_candidate_review_status(kind, candidate_id, status)
    return annotation


def add_policy_annotation(
    run_id: str,
    clause_id: str,
    candidate_kind: str | PolicyCandidateKind,
    reviewer: str,
    label_data: dict[str, Any],
    review_note: str = "",
    evidence_start: Optional[int] = None,
) -> PolicyManualAnnotation:
    """保存模型遗漏的人工补充标注。"""
    if not get_policy_extraction_run(run_id):
        raise ValueError("抽取运行不存在")
    clause = get_policy_clause(clause_id)
    if not clause:
        raise ValueError("条款不存在")
    annotation = build_manual_annotation(
        run_id=run_id,
        clause=clause,
        candidate_kind=candidate_kind,
        candidate=None,
        decision=PolicyAnnotationDecision.ADDED,
        reviewer=reviewer,
        label_data=label_data,
        review_note=review_note,
        evidence_start=evidence_start,
    )
    insert_manual_annotation(annotation)
    return annotation


def _read_optional_int(prompt: str) -> Optional[int]:
    """读取可留空的非负整数。"""
    value = input(prompt).strip()
    if not value:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError("请输入整数") from exc
    if result < 0:
        raise ValueError("证据起始位置不能小于 0")
    return result


def _read_json(prompt: str, default: Any) -> Any:
    """读取可留空的 JSON；留空时保持原值。"""
    value = input(prompt).strip()
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("请输入合法 JSON") from exc


def _read_label_data(
    candidate_kind: PolicyCandidateKind,
    candidate: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], Optional[int]]:
    """在终端逐字段读取修正或补充内容。"""
    base = _candidate_payload(candidate_kind, candidate or {})
    if candidate_kind == PolicyCandidateKind.ENTITY:
        data = {
            "entity_type": input(f"实体类型 [{base.get('entity_type', '')}]: ").strip() or base.get("entity_type", ""),
            "name": input(f"实体名称 [{base.get('name', '')}]: ").strip() or base.get("name", ""),
            "raw_text": input(f"原始片段 [{base.get('raw_text', '')}]: ").strip() or base.get("raw_text", ""),
            "evidence_text": input(f"原文证据 [{base.get('evidence_text', '')}]: ").strip() or base.get("evidence_text", ""),
            "normalized_value": _read_json(
                f"标准值 JSON [{json.dumps(base.get('normalized_value', {}), ensure_ascii=False)}]: ",
                base.get("normalized_value", {}),
            ),
        }
    else:
        data = {
            "relation_type": input(f"关系类型 [{base.get('relation_type', '')}]: ").strip() or base.get("relation_type", ""),
            "subject_entity_id": input(f"主体实体 ID [{base.get('subject_entity_id', '') or ''}]: ").strip() or base.get("subject_entity_id"),
            "object_entity_id": input(f"客体实体 ID [{base.get('object_entity_id', '') or ''}]: ").strip() or base.get("object_entity_id"),
            "target_policy_id": input(f"目标制度 ID [{base.get('target_policy_id', '') or ''}]: ").strip() or base.get("target_policy_id"),
            "target_text": input(f"目标文本 [{base.get('target_text', '')}]: ").strip() or base.get("target_text", ""),
            "evidence_text": input(f"原文证据 [{base.get('evidence_text', '')}]: ").strip() or base.get("evidence_text", ""),
        }
    start = _read_optional_int("证据起始位置（从 0 开始，留空自动定位）: ")
    return data, start


def _print_candidate(entry: dict[str, Any], position: int, total: int) -> None:
    """输出单条候选及其来源，便于人工逐项判断。"""
    candidate = entry["candidate"]
    clause = entry["clause"]
    document = entry["document"]
    print("\n" + "=" * 72)
    print(f"审核 {position}/{total} | {entry['candidate_kind']} | {entry['candidate_id']}")
    print(f"制度: {document.get('title') or document.get('file_name') or '未命名'}")
    print(f"页码: {clause.get('page_start', 0)}-{clause.get('page_end', 0)}")
    print(f"条款: {clause.get('raw_text', '')}")
    print("模型候选:")
    print(json.dumps(_candidate_payload(PolicyCandidateKind(entry["candidate_kind"]), candidate), ensure_ascii=False, indent=2))


def run_interactive_policy_review(run_id: str, reviewer: str, limit: int = 20) -> dict[str, int]:
    """按顺序审核一批候选，支持通过、拒绝、修正、补充和跳过。"""
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("审核人不能为空")
    candidates = list_policy_review_candidates(run_id, limit=limit)
    summary = {"approved": 0, "rejected": 0, "corrected": 0, "added": 0, "skipped": 0}
    if not candidates:
        print("没有待人工审核的候选。")
        return summary

    for position, entry in enumerate(candidates, start=1):
        _print_candidate(entry, position, len(candidates))
        kind = PolicyCandidateKind(entry["candidate_kind"])
        candidate = entry["candidate"]
        while True:
            action = input("选择 [a]通过 [r]拒绝 [c]修正 [n]补充 [s]跳过 [q]结束: ").strip().lower()
            if action == "q":
                return summary
            if action == "s":
                summary["skipped"] += 1
                break
            if action == "n":
                added_kind = input("补充类型 [entity/relation，默认 entity]: ").strip().lower() or "entity"
                try:
                    parsed_kind = PolicyCandidateKind(added_kind)
                    label_data, evidence_start = _read_label_data(parsed_kind, None)
                    note = input("审核备注（可留空）: ")
                    add_policy_annotation(
                        run_id=run_id,
                        clause_id=entry["clause"]["clause_id"],
                        candidate_kind=parsed_kind,
                        reviewer=reviewer,
                        label_data=label_data,
                        review_note=note,
                        evidence_start=evidence_start,
                    )
                    summary["added"] += 1
                    print("已保存补充标注，请继续处理当前候选。")
                except ValueError as exc:
                    print(f"未保存：{exc}")
                continue
            if action not in {"a", "r", "c"}:
                print("请输入 a、r、c、n、s 或 q。")
                continue
            decision = {
                "a": PolicyAnnotationDecision.APPROVED,
                "r": PolicyAnnotationDecision.REJECTED,
                "c": PolicyAnnotationDecision.CORRECTED,
            }[action]
            try:
                label_data, evidence_start = (
                    _read_label_data(kind, candidate)
                    if decision == PolicyAnnotationDecision.CORRECTED
                    else (None, None)
                )
                note = input("审核备注（可留空）: ")
                review_policy_candidate(
                    run_id=run_id,
                    candidate_kind=kind,
                    candidate_id=entry["candidate_id"],
                    decision=decision,
                    reviewer=reviewer,
                    label_data=label_data,
                    review_note=note,
                    evidence_start=evidence_start,
                )
                summary[decision.value] += 1
                print("已保存审核结果。")
                break
            except ValueError as exc:
                print(f"未保存：{exc}")
    return summary

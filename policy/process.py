"""首期报销、差旅、采购流程条款的可解释分流规则。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Sequence

from policy.domain_catalog import ProcessDomain, ProcessDomainCatalog, load_process_domain_catalog


class PolicyProcessDecision(str, Enum):
    """条款是否可以作为办事流程图谱的来源。"""

    PROCESS = "process"
    NON_PROCESS = "non_process"
    PENDING = "pending"


@dataclass(frozen=True)
class ProcessClassification:
    """保留系统判定及其可回查依据，不改写原始条款。"""

    decision: PolicyProcessDecision
    domains: list[str]
    evidence_text: str
    reason: str
    confidence: float
    source: str = "rule"


def validate_process_evidence(text: str, evidence: str) -> bool:
    """证据必须是条款原文中的连续非空片段，避免模型编造依据。"""
    return bool(evidence and evidence.strip() and evidence.strip() in text)


def is_process_label_eligible(
    clause_text: str,
    label: dict[str, object],
    catalog: ProcessDomainCatalog | None = None,
) -> bool:
    """判断一条已保存的流程标签能否作为实体关系抽取的唯一入口。"""
    domains = label.get("domains")
    if not isinstance(domains, list) or not domains:
        return False
    try:
        active_catalog = catalog or load_process_domain_catalog()
        selected_domains = active_catalog.select_domains([str(domain) for domain in domains])
    except ValueError:
        return False
    return (
        str(label.get("decision") or "") == PolicyProcessDecision.PROCESS.value
        and str(label.get("review_status") or "") in {"auto_approved", "approved"}
        and len(selected_domains) == len(domains)
        and validate_process_evidence(clause_text, str(label.get("evidence_text") or ""))
    )


def _matched_domains(text: str, domains: Sequence[ProcessDomain]) -> list[str]:
    """按配置顺序返回命中的事项域。"""
    return [
        domain.code
        for domain in domains
        if any(keyword in text for keyword in domain.keywords)
    ]


def _has_process_signal(text: str, domains: Sequence[ProcessDomain]) -> bool:
    return any(keyword in text for domain in domains for keyword in domain.process_signals)


def _is_direct_non_process(text: str, catalog: ProcessDomainCatalog) -> bool:
    return any(
        keyword in text
        for domain in catalog.enabled_domains
        for keyword in domain.exclude_signals
    )


def _build_evidence(text: str) -> str:
    """首版以完整条款作为证据，确保 PDF 回查和连续性校验都无歧义。"""
    return text.strip()


def classify_process_clause(
    text: str,
    selected_domains: Sequence[str] | None = None,
    catalog: ProcessDomainCatalog | None = None,
) -> ProcessClassification:
    """用配置化规则筛选指定事项域的可执行流程条款。"""
    original_text = text or ""
    normalized_text = re.sub(r"\s+", "", original_text)
    active_catalog = catalog or load_process_domain_catalog()
    routing_domains = active_catalog.select_domains(selected_domains)
    if not normalized_text:
        return ProcessClassification(
            decision=PolicyProcessDecision.NON_PROCESS,
            domains=[],
            evidence_text="",
            reason="条款为空，不能构成办事流程。",
            confidence=1.0,
        )

    domains = _matched_domains(normalized_text, routing_domains)
    has_process_signal = _has_process_signal(normalized_text, routing_domains)
    evidence = _build_evidence(original_text)

    # 明确的办事动作和首期事项同时出现时，优先保留为流程条款。
    if domains and has_process_signal and validate_process_evidence(original_text, evidence):
        return ProcessClassification(
            decision=PolicyProcessDecision.PROCESS,
            domains=domains,
            evidence_text=evidence,
            reason="命中指定事项域和可执行办事动作，已保留原文证据。",
            confidence=0.96,
        )

    if _is_direct_non_process(normalized_text, active_catalog):
        return ProcessClassification(
            decision=PolicyProcessDecision.NON_PROCESS,
            domains=[],
            evidence_text="",
            reason="命中总则、目的、解释或执行性表述，不是可执行办事流程。",
            confidence=0.98,
        )

    if domains:
        return ProcessClassification(
            decision=PolicyProcessDecision.PENDING,
            domains=domains,
            evidence_text="",
            reason="命中指定事项域，但缺少足够的办事动作证据，需要人工或模型复核。",
            confidence=0.5,
        )

    return ProcessClassification(
        decision=PolicyProcessDecision.NON_PROCESS,
        domains=[],
        evidence_text="",
        reason="未命中指定事项域范围和办事流程信号。",
        confidence=0.95,
    )


def build_process_label(
    clause: dict[str, object],
    selected_domains: Sequence[str] | None = None,
    catalog: ProcessDomainCatalog | None = None,
) -> dict[str, object]:
    """将纯规则判定转换为可持久化的流程标签字段。"""
    clause_text = str(clause.get("raw_text") or clause.get("search_text") or "")
    classification = classify_process_clause(
        clause_text,
        selected_domains=selected_domains,
        catalog=catalog,
    )
    label = {
        "clause_id": str(clause.get("clause_id") or ""),
        "decision": classification.decision.value,
        "domains": classification.domains,
        "evidence_text": classification.evidence_text,
        "reason": classification.reason,
        "confidence": classification.confidence,
        "source": classification.source,
        "review_status": "pending",
    }
    if classification.decision is PolicyProcessDecision.NON_PROCESS:
        label["review_status"] = "auto_approved"
    elif is_process_label_eligible(clause_text, {
        **label,
        "review_status": "auto_approved",
    }, catalog=catalog):
        label["review_status"] = "auto_approved"
    return label

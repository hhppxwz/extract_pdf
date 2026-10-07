"""制度条款义务、许可、禁止多标签分类。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re


ALLOWED_LABELS = frozenset({"obligation", "permission", "prohibition", "other"})
LABEL_ORDER = ("obligation", "permission", "prohibition", "other")

_RULE_PATTERNS = {
    "obligation": re.compile(r"应当|必须|须(?=[予由向将对按依在于为])|有义务"),
    "permission": re.compile(r"可以|可(?=[向由在按依申请办理享有])|有权"),
    "prohibition": re.compile(r"不得|严禁|禁止|不允许"),
}


@dataclass(frozen=True)
class ClauseClassification:
    """一次分类判断及其可回查依据。"""

    labels: list[str]
    evidence: dict[str, str]
    reason: str
    confidence: float
    source: str
    needs_model: bool = False

def _normalize(s: str) -> str:
    # 去所有空白，避免空格/换行差异
    return re.sub(r"\s+", "", s)


def validate_classification(
    text: str,
    labels: list[str],
    evidence: dict[str, str],
) -> None:
    """校验标签组合和每个标签的连续原文证据。"""
    if not labels:
        raise ValueError("分类标签不能为空")
    if len(labels) != len(set(labels)):
        raise ValueError("分类标签不能重复")
    unknown = set(labels) - ALLOWED_LABELS
    if unknown:
        raise ValueError(f"存在非法分类标签: {', '.join(sorted(unknown))}")
    if "other" in labels and len(labels) > 1:
        raise ValueError("other 不能与其他分类标签同时出现")
    norm_text=_normalize(text)
    for label in labels:
        snippet = str(evidence.get(label) or "").strip()
        if not snippet or _normalize(snippet) not in norm_text:
            raise ValueError(
                f"标签 {label} 的证据不是条款原文的连续片段; "
                f"raw_evidence={evidence.get(label)!r}, "
                f"all_evidence={evidence!r}"
            )


def classify_clause_by_rules(text: str) -> ClauseClassification:
    """对具有明确规范词的条款进行高精度规则分类。"""
    original = str(text or "")
    if not original.strip():
        return ClauseClassification(
            labels=["other"],
            evidence={"other": original},
            reason="条款正文为空。",
            confidence=1.0,
            source="rule",
        )

    evidence: dict[str, str] = {}
    for label in LABEL_ORDER:
        pattern = _RULE_PATTERNS.get(label)
        if pattern is None:
            continue
        match = pattern.search(original)
        if match:
            evidence[label] = match.group(0)
    labels = [label for label in LABEL_ORDER if label in evidence]
    if labels:
        validate_classification(original, labels, evidence)
        return ClauseClassification(
            labels=labels,
            evidence=evidence,
            reason="命中明确的规范性表达。",
            confidence=0.96,
            source="rule",
        )
    return ClauseClassification(
        labels=[],
        evidence={},
        reason="未命中高精度规则，需要模型判断。",
        confidence=0.0,
        source="rule",
        needs_model=True,
    )


def parse_model_classification(text: str, payload: dict[str, Any]) -> ClauseClassification:
    """把模型 JSON 转换为经过严格证据校验的分类结果。"""
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list):
        raise ValueError("模型分类 labels 必须是数组")
    provided_labels = [str(label).strip() for label in raw_labels if str(label).strip()]
    unknown = set(provided_labels) - ALLOWED_LABELS
    if unknown:
        raise ValueError(f"存在非法分类标签: {', '.join(sorted(unknown))}")
    labels = [label for label in LABEL_ORDER if label in provided_labels]
    raw_evidence = payload.get("evidence")
    evidence = {
        str(label): str(snippet).strip()
        for label, snippet in (raw_evidence.items() if isinstance(raw_evidence, dict) else [])
    }
    if labels == ["other"] and not evidence.get("other"):
        evidence["other"] = text
    validate_classification(text, labels, evidence)
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("模型分类 confidence 必须是数字") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("模型分类 confidence 必须在 0 到 1 之间")
    return ClauseClassification(
        labels=labels,
        evidence={label: evidence[label] for label in labels},
        reason=str(payload.get("reason") or "").strip(),
        confidence=confidence,
        source="llm",
    )

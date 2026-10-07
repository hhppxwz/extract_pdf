"""制度条款中的明确废止关系抽取与目标制度匹配。"""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Callable, Literal

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
    r"自\s*((?:\d\s*){4})年\s*((?:\d\s*){1,2})月\s*((?:\d\s*){1,2})日\s*起(?:施行|执行|实施|生效)"
)
_QUOTED_POLICY_RE = re.compile(
    r"《(?P<title>[^》]{2,100})》\s*(?:[（(](?P<number>[^）)]{1,80})[）)])?"
)
_ABOLITION_RE = re.compile(r"(?:同时|自行|即)?\s*(?:废止|作废)")
_TRAILING_ABOLITION_GAP_RE = re.compile(
    r"^[\s，,、；;。:：]*(?:(?:即|同时|予以|一并)[\s，,、；;。:：]*)*$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?\r\n]")
_NEGATED_ABOLITION_PREFIX_RE = re.compile(
    r"(?:没有|未予|不再|并非|禁止|不应|不予|不得|不|未)\s*$"
)
_PARTIAL_ABOLITION_SUFFIX_RE = re.compile(r"^\s*第[^。！？!?]{0,20}[条款项]")
_LEADING_TARGET_SUFFIX_RE = re.compile(r"^[\s，,、；;。:：]*$")
_TARGET_LIST_CONNECTOR_RE = re.compile(
    r"^(?:[ \t]*(?:原[ \t]*)?|[ \t\r\n]*(?:、|和|及|以及|与)[ \t\r\n]*(?:原[ \t]*)?)$"
)

AbolitionInsertionDecision = Literal["insert", "skip", "quit"]
AbolitionInsertionConfirmation = Callable[
    [PolicyDocumentRelation], AbolitionInsertionDecision
]
AbolitionApprovalDecision = Literal["approve", "skip", "quit"]
AbolitionApprovalConfirmation = Callable[[dict[str, Any]], AbolitionApprovalDecision]


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
    from metadata.document_number import normalize_document_number
    return normalize_document_number(value)


def _display_doc_number(value: str) -> str:
    """入库和展示时统一文号括号，证据原文不受影响。"""
    return normalize_doc_number(value).translate(str.maketrans({"[": "〔", "]": "〕"}))


def _abolition_evidence(raw_text: str, target_start: int, statement_end: int) -> str:
    """截取连续原文证据，保留紧邻的生效日期句，不带入附件正文。"""
    boundaries = list(re.finditer(r"[。！？!?]", raw_text[:target_start]))
    start = boundaries[-1].end() if boundaries else 0
    if boundaries:
        previous_start = boundaries[-2].end() if len(boundaries) > 1 else 0
        if _EFFECTIVE_DATE_RE.search(raw_text[previous_start:start]):
            start = previous_start
    # 终止标点保留在证据中；缺少标点时在换行处结束，避免带入附件或页脚。
    ending = re.search(r"[。！？!?\r\n]", raw_text[statement_end:])
    end = len(raw_text)
    if ending:
        end = statement_end + ending.start()
        if ending.group() not in "\r\n":
            end += 1
    return raw_text[start:end].strip()


def extract_abolition_candidates(clause: dict[str, Any]) -> list[dict[str, Any]]:
    """只提取同句中与废止词直接连接的整份制度引用。"""
    raw_text = str(clause.get("raw_text") or "")
    date_match = _EFFECTIVE_DATE_RE.search(raw_text)
    effective_date = None
    if date_match:
        try:
            numbers = [int("".join(part.split())) for part in date_match.groups()]
            effective_date = date(*numbers).isoformat()
        except ValueError:
            pass
    abolition_matches = list(_ABOLITION_RE.finditer(raw_text))
    candidates: list[dict[str, Any]] = []
    quoted_matches = list(_QUOTED_POLICY_RE.finditer(raw_text))
    # 相邻书名或明确并列连接词组成列表；逗号和句号不能把依据文件并入目标列表。
    groups: list[list[Any]] = []
    for match in quoted_matches:
        if groups and _TARGET_LIST_CONNECTOR_RE.fullmatch(raw_text[groups[-1][-1].end():match.start()]):
            groups[-1].append(match)
        else:
            groups.append([match])
    bounds = {match.start(): (group[0].start(), group[-1].end()) for group in groups for match in group}
    for quoted_match in quoted_matches:
        group_start, group_end = bounds[quoted_match.start()]
        trailing_trigger = next(
            (
                match for match in abolition_matches
                if (
                    0 <= match.start() - group_end <= 24
                    and not _SENTENCE_BOUNDARY_RE.search(
                        raw_text[group_end:match.start()]
                    )
                    and _TRAILING_ABOLITION_GAP_RE.fullmatch(
                        raw_text[group_end:match.start()]
                    )
                )
            ),
            None,
        )
        leading_trigger = next(
            (
                match for match in abolition_matches
                if (
                    group_start == match.end()
                    and not _is_negated_abolition_prefix(raw_text, match.start())
                    and _LEADING_TARGET_SUFFIX_RE.fullmatch(raw_text[group_end:])
                )
            ),
            None,
        )
        if (
            (trailing_trigger is None and leading_trigger is None)
            or _PARTIAL_ABOLITION_SUFFIX_RE.match(raw_text[group_end:])
        ):
            continue
        candidates.append({
            "source_policy_id": str(clause.get("policy_id") or ""),
            "evidence_clause_id": str(clause.get("clause_id") or ""),
            # 清除排版或识别产生的空白，证据仍保留原始文本。
            "target_title": "".join(quoted_match.group("title").split()),
            "target_doc_number": _display_doc_number(quoted_match.group("number") or ""),
            "effective_date": effective_date,
            # 证据只截取相关连续原文，原始条款在条款表中完整保留。
            "evidence_text": _abolition_evidence(
                raw_text, group_start,
                trailing_trigger.end() if trailing_trigger is not None else group_end,
            ),
            "page_start": int(clause.get("page_start") or 0),
            "page_end": int(clause.get("page_end") or 0),
        })
    return candidates


def target_publication_year(evidence_text: str, target_title: str) -> int | None:
    """仅从目标书名紧邻的印发描述读取年份，不把实施日期当作版本年份。"""
    years = set()
    for quoted in _QUOTED_POLICY_RE.finditer(str(evidence_text or "")):
        if "".join(quoted.group("title").split()) != "".join(str(target_title).split()):
            continue
        prefix = evidence_text[:quoted.start()]
        match = re.search(
            r"((?:19|20)\d{2})\s*年\s*(?:\d{1,2}\s*月\s*(?:\d{1,2}\s*日\s*)?)?"
            r"(?:印发|发布|颁布|制定)\s*的?\s*$", prefix,
        )
        if match:
            years.add(int(match.group(1)))
    return next(iter(years)) if len(years) == 1 else None


def document_publication_year(document: dict[str, Any]) -> int | None:
    """文号年份优先，其次使用发文日期；不使用生效日期推算。"""
    match = re.search(r"\[((?:19|20)\d{2})\]", normalize_doc_number(str(document.get("doc_number") or "")))
    if match:
        return int(match.group(1))
    try:
        return date.fromisoformat(str(document.get("issue_date"))).year
    except (ValueError, TypeError):
        return None


def resolve_abolition_target(
    target_title: str, target_doc_number: str, *, source_policy_id: str = "",
    target_issue_year: int | None = None,
) -> str | None:
    """排除来源自身，并结合文号、目标印发年份匹配唯一版本。"""
    def eligible(documents):
        return [document for document in documents
                if str(document.get("policy_id") or "") != source_policy_id
                and (target_issue_year is None or document_publication_year(document) == target_issue_year)]

    normalized_number = normalize_doc_number(target_doc_number)
    if normalized_number:
        by_number = eligible(find_policy_documents_by_doc_number(normalized_number))
        if len(by_number) == 1:
            return str(by_number[0]["policy_id"])
        # 明确文号无法唯一匹配时，不能用同名但不同文号的文件替代。
        return None
    by_title = eligible(find_policy_documents_by_normalized_title(target_title))
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
                candidate["target_title"], candidate["target_doc_number"],
                source_policy_id=candidate["source_policy_id"],
                target_issue_year=target_publication_year(candidate["evidence_text"], candidate["target_title"]),
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


def scan_policy_abolition_relations(
    policy_id: str,
    confirm: AbolitionInsertionConfirmation,
) -> dict[str, int]:
    """扫描单份已结构化制度，经用户确认后保存废止候选。"""
    relations = [
        relation
        for clause in get_policy_clauses(policy_id)
        for relation in build_abolition_relations(clause)
    ]
    summary = {"candidates": len(relations), "inserted": 0, "skipped": 0}
    for position, relation in enumerate(relations):
        decision = confirm(relation)
        if decision == "insert":
            upsert_policy_document_relation(relation)
            summary["inserted"] += 1
        elif decision == "skip":
            summary["skipped"] += 1
        elif decision == "quit":
            summary["skipped"] += len(relations) - position
            break
        else:
            raise ValueError(f"未知的废止关系确认结果: {decision}")
    return summary


def reconcile_unresolved_abolition_relations(
    policy_id: str,
    confirm: AbolitionApprovalConfirmation,
) -> dict[str, int]:
    """将新入库制度与历史未解析废止关系匹配，并在确认后批准。"""
    matched = []
    for relation in list_policy_document_relations():
        if (
            relation.get("review_status") != PolicyReviewStatus.PENDING.value
            or relation.get("target_policy_id")
        ):
            continue
        resolved_id = resolve_abolition_target(
            str(relation.get("target_title") or ""),
            str(relation.get("target_doc_number") or ""),
            source_policy_id=str(relation.get("source_policy_id") or ""),
            target_issue_year=target_publication_year(
                str(relation.get("evidence_text") or ""), str(relation.get("target_title") or "")),
        )
        if resolved_id == policy_id:
            matched.append(relation)

    summary = {"matched": len(matched), "approved": 0, "skipped": 0}
    for position, relation in enumerate(matched):
        decision = confirm(relation)
        if decision == "approve":
            review_policy_document_relation(
                str(relation["relation_id"]),
                PolicyReviewStatus.APPROVED.value,
                "pdf_import_interactive",
                review_note="目标制度后入库时经用户确认",
                target_policy_id=policy_id,
                effective_date=relation.get("effective_date"),
            )
            summary["approved"] += 1
        elif decision == "skip":
            summary["skipped"] += 1
        elif decision == "quit":
            summary["skipped"] += len(matched) - position
            break
        else:
            raise ValueError(f"未知的历史废止关系确认结果: {decision}")
    return summary


class InteractiveAbolitionPrompter:
    """在一次命令中共享退出状态的废止关系终端确认器。"""

    def __init__(self) -> None:
        self.stopped = False

    @staticmethod
    def _value(relation: Any, name: str) -> Any:
        return relation.get(name) if isinstance(relation, dict) else getattr(relation, name, None)

    def _show(self, relation: Any, title: str) -> None:
        source_policy_id = str(self._value(relation, "source_policy_id") or "")
        source = get_policy_document(policy_id=source_policy_id) or {}
        print("\n" + "=" * 72)
        print(title)
        print(f"来源制度: {source.get('title') or source.get('file_name') or source_policy_id}")
        print(f"目标标题: {self._value(relation, 'target_title') or ''}")
        print(f"目标文号: {self._value(relation, 'target_doc_number') or '未识别'}")
        print(f"目标制度 ID: {self._value(relation, 'target_policy_id') or '未解析'}")
        print(f"废止日期: {self._value(relation, 'effective_date') or '未识别'}")
        page_start = self._value(relation, "page_start") or 0
        page_end = self._value(relation, "page_end") or 0
        print(f"页码: {page_start}-{page_end}")
        print(f"证据原文: {self._value(relation, 'evidence_text') or ''}")

    def _read(self, prompt: str, accepted: dict[str, str]) -> str:
        while True:
            try:
                action = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.stopped = True
                print("\n输入已结束，本次命令不再询问废止关系。")
                return "quit"
            if action in accepted:
                if action == "q":
                    self.stopped = True
                return accepted[action]
            print("请输入 y、n 或 q。")

    def __call__(self, relation: PolicyDocumentRelation) -> AbolitionInsertionDecision:
        if self.stopped:
            return "quit"
        self._show(relation, "识别到制度废止关系")
        return self._read(
            "是否插入废止关系？[y]插入 [n]跳过 [q]结束询问: ",
            {"y": "insert", "n": "skip", "q": "quit"},
        )  # type: ignore[return-value]

    def confirm_approval(self, relation: dict[str, Any]) -> AbolitionApprovalDecision:
        if self.stopped:
            return "quit"
        self._show(relation, "发现指向本制度的历史废止关系")
        return self._read(
            "是否批准并将本制度标记为废止？[y]批准 [n]跳过 [q]结束询问: ",
            {"y": "approve", "n": "skip", "q": "quit"},
        )  # type: ignore[return-value]


def prompt_abolition_relation_insertion() -> InteractiveAbolitionPrompter:
    """创建一次命令共用的废止关系终端确认会话。"""
    return InteractiveAbolitionPrompter()


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

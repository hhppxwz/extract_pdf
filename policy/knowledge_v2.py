"""学校规章制度知识抽取 V2 的纯逻辑核心。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


ASSERTION_KINDS = frozenset({"action", "condition", "definition", "scope", "standard", "status", "reference"})
MODALITIES = frozenset({"required", "permitted", "prohibited", "factual"})
USABLE_STATUSES = frozenset({"machine_extracted", "approved", "corrected"})
STRUCTURAL_LEVELS = frozenset({"chapter", "section", "article"})
SUBSTANTIVE_LEVELS = frozenset({"preamble", "article", "paragraph", "item"})
MATTER_TITLE_RE = re.compile(r"申请|申报|办理|审批|认定|报销|聘任|变更|终止|登记|备案|采购|验收|入学|毕业|资助")
ORDER_RE = re.compile(r"首先|其次|然后|之后|最后|审核后|批准后|经.+后|第一步|第二步|第三步")
DOCUMENT_TYPE_ITEM_RE = re.compile(
    r"^\s*[（(][一二三四五六七八九十百\d]+[）)]\s*"
    r"(?P<name>[^。．.：:；;\n]{1,30})[。．.]\s*"
    r"(?P<predicate>适用于)(?P<usage>[^。！？!?]+)"
)


def is_substantive_clause(clause: dict[str, Any]) -> bool:
    """排除空记录和纯结构标题，保留可回答问题的正文。"""
    return (
        str(clause.get("level") or "") in SUBSTANTIVE_LEVELS
        and bool(str(clause.get("raw_text") or clause.get("search_text") or "").strip())
    )


def extract_document_type_item(
    clause: dict[str, Any], parent: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """从明确的公文种类列表项提取种类及用途，保持逐项原文证据。"""
    if str(clause.get("level") or "") != "item" or not parent:
        return None
    parent_text = str(parent.get("raw_text") or parent.get("search_text") or "")
    if not re.search(r"公文种类包括\s*[：:]?", parent_text):
        return None
    raw_text = str(clause.get("raw_text") or "")
    match = DOCUMENT_TYPE_ITEM_RE.match(raw_text)
    if not match:
        return None
    name = match.group("name").strip()
    usage = match.group("usage").strip()
    if not name or not usage:
        return None
    evidence_start = match.start("name")
    evidence_end = match.end("usage")
    return {
        "kind": "scope",
        "subject": {"text": name, "type": "other", "inferred_from_context": False},
        "predicate": {"text": match.group("predicate"), "normalized": "适用于"},
        "object": {"text": usage, "type": "other", "inferred_from_context": False},
        "receiver": None,
        "modality": "factual",
        "category": {
            "text": re.search(r"学校公文种类|公文种类", parent_text).group(0),
            "source_clause_id": str(parent.get("clause_id") or ""),
        },
        "qualifiers": {},
        "evidence": {
            "clause_id": str(clause.get("clause_id") or ""),
            "text": raw_text[evidence_start:evidence_end],
            "start": evidence_start,
            "end": evidence_end,
        },
    }


def build_document_type_graph(
    clauses: list[dict[str, Any]], assertions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """将同一父条款下的公文种类汇为一条包含关系，并保留逐项用途。"""
    by_id = {str(item.get("clause_id") or ""): item for item in clauses}
    nodes: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    usage_edges: list[dict[str, Any]] = []
    for assertion in assertions:
        payload = assertion.get("payload") if isinstance(assertion.get("payload"), dict) else assertion
        if str(assertion.get("status") or payload.get("status") or "") not in USABLE_STATUSES:
            continue
        category = payload.get("category") if isinstance(payload.get("category"), dict) else {}
        subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
        object_value = payload.get("object") if isinstance(payload.get("object"), dict) else {}
        predicate = payload.get("predicate") if isinstance(payload.get("predicate"), dict) else {}
        if payload.get("kind") != "scope" or predicate.get("text") != "适用于" or not category:
            continue
        parent_id = str(category.get("source_clause_id") or "")
        child_id = str(assertion.get("clause_id") or payload.get("clause_id") or "")
        parent, child = by_id.get(parent_id), by_id.get(child_id)
        name, usage, category_name = (str(subject.get("text") or ""), str(object_value.get("text") or ""), str(category.get("text") or ""))
        if not parent or not child or str(child.get("parent_clause_id") or "") != parent_id or not all((name, usage, category_name)):
            continue
        parent_text = str(parent.get("raw_text") or parent.get("search_text") or "")
        child_text = str(child.get("raw_text") or child.get("search_text") or "")
        category_start = parent_text.find(category_name)
        name_start = child_text.find(name)
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        if category_start < 0 or name_start < 0 or str(evidence.get("clause_id") or "") != child_id:
            continue
        if child_text[evidence.get("start", -1):evidence.get("end", -1)] != evidence.get("text"):
            continue
        policy_id = str(child.get("policy_id") or parent.get("policy_id") or assertion.get("policy_id") or "")
        category_node = f"{policy_id}:category:{parent_id}:{category_name}"
        type_node = f"{policy_id}:type:{child_id}:{name}"
        usage_node = f"{policy_id}:usage:{child_id}:{usage}"
        for node_id, label, node_type in ((category_node, category_name, "category"), (type_node, name, "document_type"), (usage_node, usage, "usage")):
            nodes[node_id] = {"id": node_id, "name": label, "type": node_type}
        parent_evidence = {"clause_id": parent_id, "text": category_name, "start": category_start, "end": category_start + len(category_name)}
        name_evidence = {"clause_id": child_id, "text": name, "start": name_start, "end": name_start + len(name)}
        group = groups.setdefault(category_node, {"source": category_node, "predicate": "包含", "evidence": parent_evidence, "members": []})
        if not any(member["node_id"] == type_node for member in group["members"]):
            group["members"].append({"node_id": type_node, "name": name, "evidence": name_evidence})
        usage_edges.append({"source": type_node, "predicate": "适用于", "target": usage_node,
                            "assertion_id": str(assertion.get("assertion_id") or ""), "evidence": [evidence]})
    return {"nodes": list(nodes.values()), "edges": [*groups.values(), *usage_edges]}


def _context_item(clause: dict[str, Any] | None) -> dict[str, Any] | None:
    if not clause:
        return None
    return {
        "clause_id": str(clause.get("clause_id") or ""),
        "level": str(clause.get("level") or ""),
        "text": str(clause.get("raw_text") or clause.get("search_text") or ""),
        "sequence_no": int(clause.get("sequence_no") or 0),
    }


def build_clause_context(
    clause: dict[str, Any],
    clauses: list[dict[str, Any]],
    document: dict[str, Any],
    ancestor_depth: int = 1,
) -> dict[str, Any]:
    """构造可寻址上下文，避免模型混淆当前条款和上下文证据。"""
    ordered = sorted(clauses, key=lambda item: int(item.get("sequence_no") or 0))
    by_id = {str(item.get("clause_id") or ""): item for item in ordered}
    current_id = str(clause.get("clause_id") or "")
    ancestors = []
    seen = {current_id}
    ancestor_id = str(clause.get("parent_clause_id") or "")
    for _ in range(max(0, ancestor_depth)):
        if not ancestor_id or ancestor_id in seen or ancestor_id not in by_id:
            break
        ancestor = by_id[ancestor_id]
        ancestors.append(_context_item(ancestor))
        seen.add(ancestor_id)
        ancestor_id = str(ancestor.get("parent_clause_id") or "")
    siblings = [
        item for item in ordered
        if str(item.get("parent_clause_id") or "") == str(clause.get("parent_clause_id") or "")
    ]
    position = next((index for index, item in enumerate(siblings) if str(item.get("clause_id") or "") == current_id), -1)
    previous = siblings[position - 1] if position > 0 else None
    following = siblings[position + 1] if 0 <= position < len(siblings) - 1 else None
    return {
        "document": {
            "title": str(document.get("title") or document.get("file_name") or ""),
            "document_no": str(document.get("document_no") or ""),
        },
        "chapter_path": list(clause.get("chapter_path") or []),
        "parent": ancestors[0] if ancestors else None,
        "ancestors": ancestors,
        "previous": _context_item(previous),
        "current": _context_item(clause),
        "next": _context_item(following),
    }


def build_assertion_prompt(context: dict[str, Any]) -> str:
    """构造严格、可回查的制度断言抽取提示词。"""
    return f"""从学校规章制度中抽取制度断言，只输出合法 JSON，不解释。
断言类型仅限 action, condition, definition, scope, standard, status, reference。
规范模态仅限 required, permitted, prohibited, factual。
例如“不得重复申报”属于 kind=action、modality=prohibited；prohibited 不是断言类型。
“某部门负责管理”是 action，不是 scope；“本办法适用于……”才是 scope；“某概念包括……”是 definition，predicate.text 应为原文中的“包括”。
所有类型都必须给出原文支持的 predicate.text；找不到时不要输出该断言。
每条断言必须给出 evidence.clause_id、原文连续片段 text、从 0 开始且左闭右开的 start/end。
可以用提供的上级条款补全省略主体，但必须设置 inferred_from_context=true 和 source_clause_id；不得把多处原文拼成证据。上级条款仅用于理解当前条款，不要重复抽取上级条款自身的事实。
action 必须有 subject 和 predicate；其他类型允许 subject 为 null。object 和 receiver 不存在时填 null。
qualifiers 固定包含 conditions、materials、deadline、amount、location、result、exceptions；没有内容时使用空数组或 null。
单条原文包含多个动作或不同规范模态时拆成多条断言。定义、范围、标准、生效废止和制度引用不得强行改写成 action。
输出结构：
{{"assertions":[{{"kind":"action","subject":{{"text":"","type":"person|department|organization|role|other","inferred_from_context":false}},"predicate":{{"text":"","normalized":""}},"object":{{"text":"","type":"material|matter|document|person|other"}},"receiver":null,"modality":"required","qualifiers":{{"conditions":[],"materials":[],"deadline":null,"amount":null,"location":null,"result":null,"exceptions":[]}},"evidence":{{"clause_id":"","text":"","start":0,"end":1}},"confidence":0.0}}]}}。
无法确定时输出 {{"assertions":[]}}。证据只能来自当前条款或明确标识的上下文条款。
上下文中的每段文字均带 clause_id，证据来源必须与该段一致。
上下文：{json.dumps(context, ensure_ascii=False)}"""


def _validate_party(value: Any, field: str, clauses: dict[str, dict[str, Any]], required: bool) -> dict[str, Any] | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict) or not str(value.get("text") or "").strip():
        raise ValueError(f"{field} 必须包含非空 text")
    inferred = bool(value.get("inferred_from_context"))
    source_clause_id = str(value.get("source_clause_id") or "")
    if inferred and (not source_clause_id or source_clause_id not in clauses):
        raise ValueError(f"{field} 的上下文推断缺少有效 source_clause_id")
    if inferred:
        source_text = str(clauses[source_clause_id].get("raw_text") or clauses[source_clause_id].get("search_text") or "")
        if str(value["text"]).strip() not in source_text:
            raise ValueError(f"{field} 不存在于标记的上下文来源")
    return {
        "text": str(value["text"]).strip(),
        "type": str(value.get("type") or "unknown").strip() or "unknown",
        "inferred_from_context": inferred,
        **({"source_clause_id": source_clause_id} if inferred else {}),
    }


def validate_assertion(payload: dict[str, Any], clauses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """严格验证一条断言及其连续原文证据。"""
    if not isinstance(payload, dict):
        raise ValueError("断言必须是对象")
    kind = str(payload.get("kind") or "")
    modality = str(payload.get("modality") or "")
    if kind not in ASSERTION_KINDS:
        if not kind:
            for alias in ("assert_type", "assertion_type", "type"):
                if payload.get(alias):
                    raise ValueError(f"缺少 kind；模型使用了 {alias}={payload[alias]}，需按 V2 结构重新抽取")
        if kind in MODALITIES:
            raise ValueError(f"断言 kind 非法: {kind} 属于规范模态；禁止、许可或义务动作应使用 kind=action")
        raise ValueError("断言 kind 非法")
    if modality not in MODALITIES:
        raise ValueError("断言 modality 非法")
    predicate = payload.get("predicate")
    if not isinstance(predicate, dict) or not str(predicate.get("text") or "").strip():
        raise ValueError("predicate 必须包含非空 text")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("evidence 必须是对象")
    clause_id = str(evidence.get("clause_id") or "")
    if clause_id not in clauses:
        raise ValueError("证据条款不存在于当前上下文")
    source = str(clauses[clause_id].get("raw_text") or clauses[clause_id].get("search_text") or "")
    text = str(evidence.get("text") or "")
    start, end = evidence.get("start"), evidence.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise ValueError("证据字符位置非法")
    if end > len(source) or source[start:end] != text:
        raise ValueError("证据文本与字符位置不一致")
    subject = _validate_party(payload.get("subject"), "subject", clauses, required=kind == "action")
    object_value = _validate_party(payload.get("object"), "object", clauses, required=False)
    receiver = _validate_party(payload.get("receiver"), "receiver", clauses, required=False)
    category = payload.get("category")
    if category is not None:
        if not isinstance(category, dict):
            raise ValueError("category 必须是对象")
        category_id = str(category.get("source_clause_id") or "")
        category_text = str(category.get("text") or "").strip()
        source_clause = clauses.get(category_id) or {}
        if not category_text or category_text not in str(source_clause.get("raw_text") or source_clause.get("search_text") or ""):
            raise ValueError("category 与来源条款不一致")
        category = {"text": category_text, "source_clause_id": category_id}
    qualifiers = payload.get("qualifiers")
    if qualifiers is None:
        qualifiers = {}
    if not isinstance(qualifiers, dict):
        raise ValueError("qualifiers 必须是对象")
    for field, party in (("subject", subject), ("object", object_value), ("receiver", receiver)):
        if party and not party.get("inferred_from_context") and str(party.get("text") or "") not in text:
            raise ValueError(f"{field} 不存在于原文证据")
    if str(predicate["text"]).strip() not in text:
        raise ValueError("predicate 不存在于原文证据")
    qualifier_defaults: dict[str, Any] = {
        "conditions": [], "materials": [], "deadline": None, "amount": None,
        "location": None, "result": None, "exceptions": [],
    }
    normalized_qualifiers = {**qualifier_defaults, **qualifiers}
    for field in ("conditions", "materials", "exceptions"):
        if not isinstance(normalized_qualifiers[field], list) or any(
            not isinstance(value, str) for value in normalized_qualifiers[field]
        ):
            raise ValueError(f"qualifiers.{field} 必须是字符串数组")
    for field in ("deadline", "amount", "location", "result"):
        if normalized_qualifiers[field] is not None and not isinstance(normalized_qualifiers[field], str):
            raise ValueError(f"qualifiers.{field} 必须是字符串或 null")
    for field, value in normalized_qualifiers.items():
        values = value if isinstance(value, list) else ([] if value is None else [value])
        if any(str(part).strip() and str(part).strip() not in text for part in values):
            raise ValueError(f"qualifiers.{field} 不存在于原文证据")
    return {
        "kind": kind,
        "subject": subject,
        "predicate": {
            "text": str(predicate["text"]).strip(),
            "normalized": str(predicate.get("normalized") or predicate["text"]).strip(),
        },
        "object": object_value,
        "receiver": receiver,
        **({"category": category} if category else {}),
        "modality": modality,
        "qualifiers": normalized_qualifiers,
        "evidence": {"clause_id": clause_id, "text": text, "start": start, "end": end},
        "clause_id": clause_id,
        "status": "machine_extracted",
        "confidence": max(0.0, min(1.0, float(payload.get("confidence") or 0.0))),
    }


def _assertion_key(item: dict[str, Any]) -> tuple[str, ...]:
    def text_of(value: Any) -> str:
        return str(value.get("text") or "") if isinstance(value, dict) else ""
    evidence = item["evidence"]
    return (
        item["kind"], text_of(item.get("subject")), text_of(item.get("predicate")),
        text_of(item.get("object")), text_of(item.get("receiver")), item["modality"],
        evidence["clause_id"], evidence["text"],
    )


def normalize_assertions(
    payloads: Iterable[Any], clauses: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """校验并去重；无效结果单独保留原因，不能进入下游。"""
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for payload in payloads:
        try:
            item = validate_assertion(payload, clauses)
        except (TypeError, ValueError) as exc:
            # 模型常把连续原文的偏移量算错；仅当原文片段在指定条款中唯一出现时重定位。
            if str(exc) == "证据文本与字符位置不一致" and isinstance(payload, dict):
                evidence = payload.get("evidence")
                if isinstance(evidence, dict):
                    clause = clauses.get(str(evidence.get("clause_id") or "")) or {}
                    source = str(clause.get("raw_text") or clause.get("search_text") or "")
                    snippet = str(evidence.get("text") or "")
                    position = source.find(snippet) if snippet else -1
                    if position >= 0 and source.find(snippet, position + 1) < 0:
                        repaired = {**payload, "evidence": {**evidence, "start": position, "end": position + len(snippet)}}
                        try:
                            item = validate_assertion(repaired, clauses)
                        except (TypeError, ValueError) as repaired_exc:
                            exc = repaired_exc
                        else:
                            key = _assertion_key(item)
                            if key not in seen:
                                seen.add(key)
                                digest = hashlib.sha256("\u241f".join(key).encode("utf-8")).hexdigest()[:24]
                                item["assertion_id"] = f"assertion_{digest}"
                                valid.append(item)
                            continue
            invalid.append({"payload": payload, "status": "invalid", "error": str(exc)})
            continue
        key = _assertion_key(item)
        if key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha256("\u241f".join(key).encode("utf-8")).hexdigest()[:24]
        item["assertion_id"] = f"assertion_{digest}"
        valid.append(item)
    return valid, invalid


def _descendant_ids(container_id: str, clauses: list[dict[str, Any]]) -> list[str]:
    children: dict[str, list[dict[str, Any]]] = {}
    for clause in clauses:
        children.setdefault(str(clause.get("parent_clause_id") or ""), []).append(clause)
    result: list[str] = []
    stack = list(reversed(sorted(children.get(container_id, []), key=lambda item: int(item.get("sequence_no") or 0))))
    while stack:
        item = stack.pop()
        item_id = str(item.get("clause_id") or "")
        result.append(item_id)
        stack.extend(reversed(sorted(children.get(item_id, []), key=lambda child: int(child.get("sequence_no") or 0))))
    return result


def build_explicit_matters(
    run_id: str,
    policy_id: str,
    clauses: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只从明确结构标题生成事项，拒绝跨章节猜测。"""
    assertion_clause_ids = {
        str(item.get("clause_id") or "") for item in assertions
        if str(item.get("status") or "") in USABLE_STATUSES
    }
    ordered = sorted(clauses, key=lambda item: int(item.get("sequence_no") or 0))
    candidate_ids: set[str] = set()
    for clause in ordered:
        level = str(clause.get("level") or "")
        title = str(clause.get("raw_text") or "").strip()
        clause_id = str(clause.get("clause_id") or "")
        descendants = _descendant_ids(clause_id, clauses)
        if level not in STRUCTURAL_LEVELS or not descendants or not MATTER_TITLE_RE.search(title):
            continue
        if assertion_clause_ids.intersection([clause_id, *descendants]):
            candidate_ids.add(clause_id)

    matters: list[dict[str, Any]] = []
    for clause in ordered:
        clause_id = str(clause.get("clause_id") or "")
        if clause_id not in candidate_ids:
            continue
        descendants = _descendant_ids(clause_id, clauses)
        # 明确子标题应拆成独立事项，父容器不再产生一个重叠的大事项。
        if candidate_ids.intersection(descendants):
            continue
        title = str(clause.get("raw_text") or "").strip()
        member_ids = [clause_id, *descendants]
        matter_id = "matter_" + hashlib.sha256(f"{run_id}:{clause_id}".encode("utf-8")).hexdigest()[:24]
        matters.append({
            "matter_id": matter_id,
            "run_id": run_id,
            "policy_id": policy_id,
            "container_clause_id": clause_id,
            "name": title,
            "clause_ids": member_ids,
            "source": "explicit_structure",
            "review_status": "machine_extracted",
        })
    return matters


def build_workflow_view(
    matter: dict[str, Any],
    clauses: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    """从事项动作断言派生流程，不把文档顺序冒充业务顺序。"""
    sequence = {str(item.get("clause_id") or ""): int(item.get("sequence_no") or 0) for item in clauses}
    member_ids = set(matter.get("clause_ids") or [])
    actions = [
        item for item in assertions
        if item.get("kind") == "action"
        and str(item.get("clause_id") or "") in member_ids
        and str(item.get("status") or "") in USABLE_STATUSES
    ]
    actions.sort(key=lambda item: (sequence.get(str(item.get("clause_id") or ""), 0), str(item.get("assertion_id") or "")))
    # 只有后续动作均带明确顺序信号，才足以确认整条动作链可排序。
    confirmed = len(actions) > 1 and all(
        ORDER_RE.search(str(next((clause.get("raw_text") for clause in clauses if clause.get("clause_id") == item.get("clause_id")), "")))
        for item in actions[1:]
    )
    if confirmed:
        workflow_type = "workflow"
    elif actions:
        workflow_type = "partial_workflow"
    else:
        workflow_type = "non_workflow"
    return {
        "matter_id": str(matter.get("matter_id") or ""),
        "workflow_type": workflow_type,
        "order_confirmed": confirmed,
        "order_basis": "explicit_text" if confirmed else "document_sequence",
        "steps": [
            {
                "assertion_id": str(item.get("assertion_id") or ""),
                "clause_id": str(item.get("clause_id") or ""),
                "predicate": str((item.get("predicate") or {}).get("text") or ""),
            }
            for item in actions
        ],
    }

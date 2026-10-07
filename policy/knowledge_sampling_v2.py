"""按可解释规则挑选抽取评估样本；不调用模型。"""
import csv
import hashlib
import re
from collections import Counter
from pathlib import Path

from policy.knowledge_v2 import is_substantive_clause


FIELDS = ["选用", "批次ID", "条款ID", "制度名称", "章节", "入选理由", "原文", "父条款原文", "原文指纹"]
CATEGORIES = [
    ("条件材料期限金额", r"如果|符合|条件|材料|申请表|日内|期限|不超过|元|标准"),
    ("上下文与省略主体", None),
    ("多动作或许可禁止", r"不得|禁止|可以|并|分别"),
    ("定义与适用范围", r"是指|适用于|本办法所称|定义"),
    ("责任与动作", r"应当|必须|负责|提交|审核|盘点|应"),
]


def select_samples(documents, clauses, count=20):
    """按类别轮转，并优先选择当前入选较少的制度，保证结果可复现。"""
    if count < 1:
        raise ValueError("样本数必须大于零")
    candidates = [c for c in clauses if is_substantive_clause(c)]
    selected, seen, policy_counts = [], set(), Counter()
    while len(selected) < min(count, len(candidates)):
        added = False
        for reason, pattern in [*CATEGORIES, ("补充覆盖", r".")]:
            eligible = [c for c in candidates if c["clause_id"] not in seen and (
                bool(c.get("parent_clause_id")) if pattern is None else bool(re.search(pattern, c.get("raw_text") or ""))
            )]
            if not eligible:
                continue
            chosen = min(eligible, key=lambda c: (policy_counts[c.get("policy_id")], str(c.get("policy_id")), int(c.get("sequence_no") or 0), c["clause_id"]))
            selected.append((chosen, reason))
            seen.add(chosen["clause_id"])
            policy_counts[chosen.get("policy_id")] += 1
            added = True
            if len(selected) == count:
                break
        if not added:
            break
    return selected


def export_samples(batch_id, output_path, count=20):
    from policy.storage import get_policy_documents_for_batch, get_policy_clauses

    documents = get_policy_documents_for_batch(batch_id)
    clauses = [c for d in documents for c in get_policy_clauses(d["policy_id"])]
    by_id = {c["clause_id"]: c for c in clauses}
    titles = {d["policy_id"]: d.get("title") or d.get("file_name") or "" for d in documents}
    selected = select_samples(documents, clauses, count)
    if not selected:
        raise ValueError("批次没有可选的实质性条款")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for clause, reason in selected:
            text = clause.get("raw_text") or ""
            writer.writerow(dict(zip(FIELDS, ["是", batch_id, clause["clause_id"], titles.get(clause.get("policy_id"), ""),
                " / ".join(clause.get("chapter_path") or []), reason, text,
                by_id.get(clause.get("parent_clause_id"), {}).get("raw_text", ""), hashlib.sha256(text.encode()).hexdigest()])))
    return path


def load_sample_ids(path, batch_id, clauses):
    """只接受属于目标批次且原文未变化的样本。"""
    by_id = {c["clause_id"]: c for c in clauses}
    selected = []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("选用", "").strip() not in {"是", "1", "yes"}:
                continue
            clause = by_id.get(row.get("条款ID"))
            if row.get("批次ID") != batch_id or not clause or not is_substantive_clause(clause):
                raise ValueError("样本条款不属于该批次或不可抽取")
            if row.get("原文指纹") != hashlib.sha256((clause.get("raw_text") or "").encode()).hexdigest():
                raise ValueError("样本原文已变化，请重新导出清单")
            if clause["clause_id"] not in selected:
                selected.append(clause["clause_id"])
    if not selected:
        raise ValueError("样本清单没有选用条款")
    return selected

# 制度文档废止关系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从已结构化制度条款抽取、审核并持久化可回查的文档级废止关系；审核通过后更新被废止制度的状态与失效日期。

**Architecture:** 新建 `policy/abolition.py`，将规则抽取、目标匹配和审核编排从通用实体关系抽取中隔离。`policy/storage.py` 管理关系表及审核写入；`main.py` 仅提供命令入口。候选必须先以 `pending` 保存，只有审核通过才更新目标制度。

**Tech Stack:** Python 3.10+、Pydantic、PostgreSQL/psycopg2、现有 `storage_adapter`、标准库 `re`、`unittest.mock`。

**Spec:** `docs/superpowers/specs/2026-09-14-policy-document-abolition-design.md`

## Global Constraints

- 本期仅支持 `abolishes`；不实现修订、替代、引用、条款沿革或 `as_of` 检索。
- 每个候选保存证据条款、原文、页码、置信度；没有明确日期时 `effective_date` 必须为空。
- 规则抽取或自动匹配不得修改 `policy_documents` 状态。
- 文号精确匹配优先；标题只允许规范化精确候选匹配，不做模糊自动绑定。
- 同一目标制度的已批准废止关系出现不同日期时必须报错，禁止静默覆盖。
- 沿用 `unittest`，新增代码注释使用中文。

---

## 文件结构

| 文件 | 变更 | 职责 |
| --- | --- | --- |
| `models.py` | 修改 | 废止关系枚举和 Pydantic 模型。 |
| `policy/storage.py` | 修改 | 关系表、幂等读写、审核时状态更新。 |
| `policy/abolition.py` | 新建 | 规则抽取、目标匹配、批次运行、交互审核。 |
| `main.py` | 修改 | 新命令和参数校验。 |
| `README.md` | 修改 | 使用说明与边界。 |
| `tests/test_policy_abolition.py` | 新建 | 规则、匹配、存储、审核、冲突。 |
| `tests/test_policy_abolition_cli.py` | 新建 | CLI 与批次编排。 |

## Task 1: 模型、关系表和审核写入

**Files:**

- Modify: `models.py:240-460`
- Modify: `policy/storage.py:35-335, 340-520`
- Create: `tests/test_policy_abolition.py`

**Interfaces:**

- Produces `PolicyDocumentRelation`，字段：`relation_id`、`source_policy_id`、`target_policy_id`、`target_title`、`target_doc_number`、`relation_type`、`effective_date`、`evidence_clause_id`、`evidence_text`、`page_start`、`page_end`、`confidence`、`review_status`、`reviewer`、`review_note`。
- Produces `upsert_policy_document_relation(relation: PolicyDocumentRelation) -> str`。
- Produces `list_policy_document_relations(batch_id: str = "", source_policy_id: str = "") -> list[dict[str, Any]]`。
- Produces `review_policy_document_relation(relation_id: str, decision: str, reviewer: str, review_note: str = "", target_policy_id: str | None = None, effective_date: str | None = None) -> dict[str, Any]`。

- [ ] **Step 1: 写入失败测试**

    def test_approved_relation_invalidates_target_with_relation_date(self) -> None:
        relation = {"relation_id": "abolition_1", "target_policy_id": "policy_old",
                    "effective_date": "2016-01-01", "review_status": "pending"}
        with patch("policy.storage.get_policy_document_relation", return_value=relation), \
             patch("policy.storage._approved_abolition_dates", return_value=[]), \
             patch("policy.storage.storage.relational.update_rows") as update:
            result = review_policy_document_relation("abolition_1", "approved", "张三")
        self.assertEqual(result["review_status"], "approved")
        self.assertTrue(any(call.args[0] == "policy_documents"
                            and call.args[1]["validity_status"] == "invalid"
                            and call.args[1]["expiry_date"] == "2016-01-01"
                            for call in update.call_args_list))

    def test_conflicting_approved_dates_raise_without_target_update(self) -> None:
        relation = {"relation_id": "abolition_1", "target_policy_id": "policy_old",
                    "effective_date": "2016-01-01", "review_status": "pending"}
        with patch("policy.storage.get_policy_document_relation", return_value=relation), \
             patch("policy.storage._approved_abolition_dates", return_value=["2018-01-01"]), \
             patch("policy.storage.storage.relational.update_rows") as update:
            with self.assertRaisesRegex(ValueError, "废止日期冲突"):
                review_policy_document_relation("abolition_1", "approved", "张三")
        self.assertFalse(any(call.args[0] == "policy_documents" for call in update.call_args_list))

- [ ] **Step 2: 运行测试确认失败**

Run: `python -B -m unittest tests.test_policy_abolition.PolicyAbolitionStorageTests -v`

Expected: FAIL，提示模型或存储接口不存在。

- [ ] **Step 3: 实现最小模型与关系表**

在 `models.py` 新增 `PolicyDocumentRelationType.ABOLISHES` 与 `PolicyDocumentRelation`。在 `ensure_policy_tables()` 新建 `policy_document_relations`，来源、目标、证据条款均使用外键，并对 `(source_policy_id, evidence_clause_id, relation_type, target_title, target_doc_number)` 建唯一约束。

- [ ] **Step 4: 实现幂等写入和审核规则**

审核必须拒绝不是 `approved`/`rejected` 的结论；批准时要求目标制度已选定，检查其他已批准关系日期是否冲突。批准后执行：

    values = {"validity_status": PolicyValidityStatus.INVALID.value}
    if resolved_effective_date:
        values["expiry_date"] = resolved_effective_date
    storage.relational.update_rows(TABLE_POLICY_DOCUMENTS, values,
                                   '"policy_id" = %s', (resolved_target_policy_id,))

拒绝时只更新关系审核字段；upsert 不得覆盖已审核结论。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -B -m unittest tests.test_policy_abolition.PolicyAbolitionStorageTests -v`

Expected: PASS。

- [ ] **Step 6: 提交该任务**

    git add models.py policy/storage.py tests/test_policy_abolition.py
    git commit -m "feat: store reviewable policy abolition relations"

## Task 2: 条款规则抽取和目标匹配

**Files:**

- Create: `policy/abolition.py`
- Modify: `policy/storage.py:500-690`
- Modify: `tests/test_policy_abolition.py`

**Interfaces:**

- Produces `extract_abolition_candidates(clause: dict[str, Any]) -> list[dict[str, Any]]`。
- Produces `resolve_abolition_target(target_title: str, target_doc_number: str) -> str | None`。
- Produces `build_abolition_relations(clause: dict[str, Any]) -> list[PolicyDocumentRelation]`。

- [ ] **Step 1: 写入失败测试**

    def test_extracts_date_title_number_and_evidence(self) -> None:
        clause = {"clause_id": "clause_104", "policy_id": "policy_new",
                  "page_start": 18, "page_end": 18,
                  "raw_text": "第一百零四条 本办法自2016年1月1日起施行，《武汉大学财务管理办法》（武大[2000]25号）即废止。"}
        candidate = extract_abolition_candidates(clause)[0]
        self.assertEqual(candidate["effective_date"], "2016-01-01")
        self.assertEqual(candidate["target_title"], "武汉大学财务管理办法")
        self.assertEqual(candidate["target_doc_number"], "武大[2000]25号")
        self.assertIn("即废止", candidate["evidence_text"])

    def test_missing_date_keeps_candidate(self) -> None:
        candidate = extract_abolition_candidates({"clause_id": "c1", "policy_id": "new",
            "raw_text": "《旧办法》（武大〔2000〕25号）同时废止。"})[0]
        self.assertIsNone(candidate["effective_date"])

    def test_doc_number_match_wins_over_title_candidate(self) -> None:
        with patch("policy.abolition.find_policy_documents_by_doc_number",
                   return_value=[{"policy_id": "old_2000"}]), \
             patch("policy.abolition.find_policy_documents_by_normalized_title") as by_title:
            self.assertEqual(resolve_abolition_target("武汉大学财务管理办法", "武大[2000]25号"), "old_2000")
        by_title.assert_not_called()

- [ ] **Step 2: 运行测试确认失败**

Run: `python -B -m unittest tests.test_policy_abolition.PolicyAbolitionExtractionTests -v`

Expected: FAIL，提示 `policy.abolition` 不存在。

- [ ] **Step 3: 实现规则抽取器**

在 `policy/abolition.py` 定义以下正则：

    _EFFECTIVE_DATE_RE = re.compile(
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起(?:施行|执行|生效)"
    )
    _QUOTED_POLICY_RE = re.compile(
        r"《(?P<title>[^》]{2,100})》\s*(?:（(?P<number>[^）]{1,80})）)?"
    )
    _ABOLITION_RE = re.compile(r"(?:同时|即)?\s*废止")

仅当引用结束位置到废止词之间不超过 24 个字符时创建候选。证据取整个 `raw_text`，以连续覆盖日期、引用和触发词。`normalize_doc_number()` 统一全半角括号、空白和年份方括号形式。

- [ ] **Step 4: 实现目标匹配**

在 `policy/storage.py` 增加 `find_policy_documents_by_doc_number(doc_number: str) -> list[dict[str, Any]]` 和 `find_policy_documents_by_normalized_title(title: str) -> list[dict[str, Any]]`。匹配顺序是唯一文号、唯一标题、未匹配；不得使用模糊搜索。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -B -m unittest tests.test_policy_abolition.PolicyAbolitionExtractionTests -v`

Expected: PASS。

- [ ] **Step 6: 提交该任务**

    git add policy/abolition.py policy/storage.py tests/test_policy_abolition.py
    git commit -m "feat: extract policy abolition candidates"

## Task 3: 批次运行、审核命令和文档

**Files:**

- Modify: `policy/abolition.py`
- Modify: `main.py:116-640`
- Create: `tests/test_policy_abolition_cli.py`
- Modify: `README.md:130-225`

**Interfaces:**

- Produces `extract_batch_abolition_relations(batch_id: str) -> dict[str, int]`，键为 `policies`, `clauses`, `candidates`, `resolved`, `unresolved`。
- Produces `get_batch_abolition_relation_status(batch_id: str) -> dict[str, Any]`。
- Produces `run_interactive_abolition_review(batch_id: str, reviewer: str, limit: int = 20) -> dict[str, int]`。
- Adds `--extract-policy-abolition-relations`, `--policy-abolition-relation-status`, `--review-policy-abolition-relations`, `--abolition-reviewer`, `--abolition-review-limit`。

- [ ] **Step 1: 写入失败测试**

    def test_batch_extraction_keeps_unresolved_reference(self) -> None:
        clauses = [{"clause_id": "c1", "policy_id": "new", "raw_text": "《旧办法》即废止。",
                    "page_start": 3, "page_end": 3}]
        with patch("policy.abolition.get_policy_documents_for_batch", return_value=[{"policy_id": "new"}]), \
             patch("policy.abolition.get_policy_clauses", return_value=clauses), \
             patch("policy.abolition.upsert_policy_document_relation") as save:
            result = extract_batch_abolition_relations("batch_1")
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["unresolved"], 1)
        self.assertIsNone(save.call_args.args[0].target_policy_id)

    def test_full_help_lists_abolition_commands(self) -> None:
        completed = subprocess.run([sys.executable, "-B", "main.py", "--help-all"],
                                   capture_output=True, check=True)
        self.assertIn(b"--extract-policy-abolition-relations", completed.stdout)

- [ ] **Step 2: 运行测试确认失败**

Run: `python -B -m unittest tests.test_policy_abolition_cli -v`

Expected: FAIL，提示批次接口或命令行参数不存在。

- [ ] **Step 3: 实现批次、状态和审核入口**

批次运行遍历 `get_policy_documents_for_batch(batch_id)` 和每份制度的 `get_policy_clauses(policy_id)`；候选经 `resolve_abolition_target()` 后 upsert。状态按 `pending`、`approved`、`rejected`、`resolved`、`unresolved` 汇总。交互审核支持 `a` 通过、`r` 拒绝、`t` 指定目标后通过、`d` 修正日期后通过、`s` 跳过、`q` 退出；审核人必须非空且限制值大于等于 1。

- [ ] **Step 4: 补充 README**

添加：

    python main.py --extract-policy-abolition-relations batch_xxxxxxxxxxxxxxxx
    python main.py --policy-abolition-relation-status batch_xxxxxxxxxxxxxxxx
    python main.py --review-policy-abolition-relations batch_xxxxxxxxxxxxxxxx --abolition-reviewer 张三

说明只有明确废止措辞才会产生候选，未匹配旧制度或未识别日期时需要人工确认，审核通过后才更新旧制度。

- [ ] **Step 5: 运行 CLI 测试确认通过**

Run: `python -B -m unittest tests.test_policy_abolition_cli -v`

Expected: PASS。

- [ ] **Step 6: 提交该任务**

    git add policy/abolition.py main.py README.md tests/test_policy_abolition_cli.py
    git commit -m "feat: add policy abolition review workflow"

## Task 4: 完整回归与提交范围审查

**Files:**

- Modify: `tests/test_policy_abolition.py`
- Modify: `tests/test_policy_abolition_cli.py`

- [ ] **Step 1: 写入内存端到端验收测试**

使用 fake adapter 或 `patch` 模拟“抽取 → 保存 pending → 审核批准”。断言审核前旧制度保持 `unknown`，审核后为 `invalid` 且 `expiry_date == "2016-01-01"`。

- [ ] **Step 2: 运行新增模块回归**

Run: `python -B -m unittest tests.test_policy_abolition tests.test_policy_abolition_cli -v`

Expected: PASS。

- [ ] **Step 3: 运行全量验证**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B main.py --help`

Run: `python -B main.py --help-all`

Run: `git diff --check`

Expected: 已配置测试通过；真实 PostgreSQL/pgvector 测试可因缺少 `POLICY_RETRIEVAL_TEST_DATABASE` 跳过；帮助展示新命令；差异无空白错误。

- [ ] **Step 4: 审查并提交本功能文件**

不得暂存 `__pycache__/`、`only_policy/` PDF、批处理 JSON、`before.txt`、`after.txt` 或无关既有改动。

    git add models.py policy/storage.py policy/abolition.py main.py README.md tests/test_policy_abolition.py tests/test_policy_abolition_cli.py
    git commit -m "test: verify policy abolition workflow"

## 计划自检

- 规格中的抽取、无日期保留、文号优先、标题多候选不绑定、审核写入、冲突保护和 CLI 分别由 Tasks 1-4 覆盖。
- 计划未包含条款沿革、历史检索、SHACL、Word、网页或图谱页面。
- 所有公开接口均在使用前定义，测试命令沿用项目 `unittest`。

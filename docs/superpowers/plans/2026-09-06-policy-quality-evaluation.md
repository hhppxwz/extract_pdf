# 制度条款与图谱质检工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为单人审核者导出制度条款、图谱候选的 CSV 抽检表，并根据回填结果生成可追溯的质量报告。

**Architecture:** 新建 `policy_quality.py` 负责纯数据转换、CSV 导出和报告汇总；`policy_storage.py` 只补充查询批次文档和抽取候选的读取接口；`main.py` 只注册三个命令行模式。人工结论始终保留在 CSV，不写回现有制度、条款或图谱候选表。

**Tech Stack:** Python 3.10+、标准库 `csv/json/pathlib/unittest`、PostgreSQL 既有存储适配层。

**Spec:** `docs/superpowers/specs/2026-09-06-policy-quality-evaluation-design.md`

## Global Constraints

- CSV 必须使用 `utf-8-sig` 编码，确保 Windows Excel 可直接打开。
- 不新增第三方依赖；测试使用 Python 标准库 `unittest`。
- 不更新 `policy_documents`、`policy_clauses`、`policy_entities`、`policy_relations` 或人工审核表。
- 代码注释、错误提示和文档均使用中文。

---

### Task 1: 纯质检数据与报告模块

**Files:**
- Create: `policy_quality.py`
- Create: `tests/test_policy_quality.py`

**Interfaces:**
- Produces: `select_clause_sample(clauses, limit) -> list[dict]`
- Produces: `build_clause_review_rows(documents, clauses_by_policy, clauses_per_policy) -> list[dict]`
- Produces: `build_graph_review_rows(run_id, candidates) -> list[dict]`
- Produces: `write_review_csvs(output_dir, clause_rows, graph_rows) -> dict[str, Path]`
- Produces: `summarize_quality_reviews(clause_rows, graph_rows) -> dict`
- Produces: `write_quality_report(output_dir) -> dict[str, Path]`

- [x] **Step 1: Write failing tests for deterministic clause sampling and blank human columns**

```python
rows = build_clause_review_rows(documents, clauses_by_policy, clauses_per_policy=2)
assert [row["条款ID"] for row in rows] == ["c1", "c3"]
assert rows[0]["边界正确"] == ""
```

- [x] **Step 2: Run the test and verify it fails because `policy_quality` does not exist**

Run: `python -B -m unittest tests.test_policy_quality.PolicyQualityTests.test_build_clause_review_rows_selects_evenly`

- [x] **Step 3: Implement the minimal pure data builders and CSV exporter**

```python
def select_clause_sample(clauses: list[dict], limit: int) -> list[dict]:
    if len(clauses) <= limit:
        return list(clauses)
    indexes = [round(index * (len(clauses) - 1) / (limit - 1)) for index in range(limit)]
    return [clauses[index] for index in dict.fromkeys(indexes)]
```

- [x] **Step 4: Run the sampling and CSV tests and verify they pass**

Run: `python -B -m unittest tests.test_policy_quality -v`

- [x] **Step 5: Write failing tests for Chinese review values, error-code counting and graph omissions**

```python
summary = summarize_quality_reviews(
    [{"边界正确": "对", "层级正确": "错", "文字完整": "对", "页码正确": "对", "错误类型": "H01, S01"}],
    [{"正确性": "错", "缺失内容": "遗漏审批单"}],
)
assert summary["clauses"]["完全正确"] == 0
assert summary["clauses"]["错误类型"]["H01"] == 1
assert summary["graph"]["遗漏数"] == 1
```

- [x] **Step 6: Implement summary and Markdown/JSON report writing, then rerun all module tests**

Run: `python -B -m unittest tests.test_policy_quality -v`

### Task 2: 制度存储读取接口与命令行入口

**Files:**
- Modify: `policy_storage.py`
- Modify: `main.py`
- Modify: `tests/test_policy_quality.py`

**Interfaces:**
- Produces: `get_policy_graph_candidates(run_id) -> list[dict]`
- Consumes: `get_policy_documents_for_batch(batch_id)` and `get_policy_clauses(policy_id)`
- Consumes: `export_clause_review(batch_id, output_dir, clauses_per_policy)` and `export_graph_review(run_id, output_dir)` from `policy_quality.py`

- [x] **Step 1: Write a failing test for graph row rendering with entity-name references**

```python
rows = build_graph_review_rows("run_1", candidates)
assert rows[1]["候选内容"] == "报销 --requires_material--> 发票"
assert rows[1]["原文证据"] == "提交发票"
```

- [x] **Step 2: Run the test and verify it fails because graph-row rendering is incomplete**

Run: `python -B -m unittest tests.test_policy_quality.PolicyQualityTests.test_build_graph_review_rows_renders_relation`

- [x] **Step 3: Add read-only candidate loader and CLI handlers**

```python
parser.add_argument("--export-policy-clause-review", type=str)
parser.add_argument("--export-policy-graph-review", type=str)
parser.add_argument("--report-policy-quality", type=str)
```

The CLI must require `--quality-output` for exports, validate a positive `--clauses-per-policy`, make all modes mutually exclusive, and print every generated output path.

- [x] **Step 4: Run module tests and CLI help smoke test**

Run: `python -B -m unittest tests.test_policy_quality -v`

Run: `python -B main.py --help`

### Task 3: 使用说明与最终验证

**Files:**
- Modify: `README.md`

**Interfaces:**
- Documents: exact three commands, CSV 回填规则和产物路径。

- [x] **Step 1: Add a concise quality-review workflow to README**

The documentation must explain that the first command needs a completed batch, the second needs a completed extraction run, CSV is opened and filled in Excel, and the third command reads the same output folder.

- [x] **Step 2: Run the full test suite and Python compilation without generating bytecode**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B -m compileall -q .`

- [ ] **Step 3: Inspect the final diff and commit only the new feature files**

Run: `git diff --check`

Run: `git status --short`

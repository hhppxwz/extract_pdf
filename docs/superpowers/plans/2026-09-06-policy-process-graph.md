# 报销、差旅、采购流程图谱 Implementation Plan

> **For agentic workers:** 按任务顺序实施；每个生产功能必须先写失败测试，再写最小实现并验证。当前用户要求先自行测试，实施完成后不得自动提交、合并或推送。

**Goal:** 让制度条款先经过可复核的流程分流，且只有报销、差旅、采购的已确认流程条款进入实体关系抽取。

**Architecture:** `policy_process.py` 负责无副作用的规则判定和证据校验；`policy_storage.py` 保存版本化判定运行及其标签，并筛选可抽取条款；`policy_extractor.py` 复用既有抽取器但必须关联流程运行；`policy_quality.py` 导出两张流程质检表并汇总结果。

**Tech Stack:** Python 3.10+、Pydantic、PostgreSQL 既有存储层、标准库 `unittest/csv/json`。

**Spec:** `docs/superpowers/specs/2026-09-06-policy-process-graph-design.md`

## Global Constraints

- 首期事项域固定为 `reimbursement`、`travel`、`procurement`。
- 只有 `process` 且证据是原文连续片段的条款可以进入图谱抽取。
- 非流程和待确认条款必须保留记录、理由和版本，不得删除源条款。
- 新命令不能改变旧的全量抽取命令语义。
- CSV 使用 `utf-8-sig`；代码注释、报错和文档均用中文。
- 本次变更保持未提交，交由用户测试后再决定是否提交。

---

### Task 1: 流程条款规则与证据校验

**Files:**
- Create: `policy_process.py`
- Create: `tests/test_policy_process.py`

**Interfaces:**
- Produces: `classify_process_clause(text: str) -> ProcessClassification`
- Produces: `validate_process_evidence(text: str, evidence: str) -> bool`

- [ ] 写出报销材料提交、采购审批、总则排除、执行口号排除的失败测试。
- [ ] 运行 `python -B -m unittest tests.test_policy_process -v`，确认因模块不存在而失败。
- [ ] 实现最小规则分类器和连续原文证据校验。
- [ ] 重新运行同一测试，确认通过。

### Task 2: 版本化流程判定运行与可抽取条款筛选

**Files:**
- Modify: `models.py`
- Modify: `policy_storage.py`
- Modify: `policy_process.py`
- Modify: `tests/test_policy_process.py`

**Interfaces:**
- Produces: `create_policy_process_run(batch_id, versions, ...) -> str`
- Produces: `run_policy_process_classification(run_id) -> dict`
- Produces: `get_eligible_process_clauses(process_run_id, limit=None) -> list[dict]`

- [ ] 先测试只返回 `process`、证据有效、审核已通过的条款。
- [ ] 扩展模型和数据库表，保存运行、判定、理由、证据、置信度和审核状态。
- [ ] 实现稳定选择、幂等写入、运行计数和筛选接口。
- [ ] 运行流程模块测试。

### Task 3: 流程图谱抽取、CLI 与单人质检表

**Files:**
- Modify: `policy_extractor.py`
- Modify: `main.py`
- Modify: `policy_quality.py`
- Modify: `README.md`
- Modify: `tests/test_policy_process.py`

**Interfaces:**
- Produces: `create_and_run_process_policy_extraction(process_run_id, limit=None) -> dict`
- Produces CLI: `--classify-policy-process-batch`、`--extract-policy-process-run`、`--export-policy-process-review`

- [ ] 先测试流程抽取不会调用全量条款选择器，并且 CLI 帮助列出新命令。
- [ ] 为抽取运行关联流程运行，只向抽取器传递筛选后的条款。
- [ ] 导出 `流程条款抽检.csv` 和仅限流程来源的 `流程图谱抽检.csv`。
- [ ] 在 README 写明 10 份制度试运行、回填和报告命令。
- [ ] 运行 `python -B -m unittest discover -s tests -v` 与 `git diff --check`；只报告结果，不执行 git add/commit/push。

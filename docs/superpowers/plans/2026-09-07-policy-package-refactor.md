# 制度模块目录重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use subagents for this plan.

**Goal:** 将制度处理、检索、流程、图谱和质量模块收拢到 `policy/` 包，保持现有 HTTP、CLI、数据库和导入行为不变。

**Architecture:** 根目录继续承载启动入口、通用 PDF 导入和共享基础设施；新增 `policy/` 包承载所有制度领域模块。通过统一更新内部导入和测试导入消除根目录重复实现，不保留兼容包装器，以免后续维护出现两份逻辑。

**Tech Stack:** Python 3.14、标准库 `unittest`、FastAPI、PostgreSQL/pgvector。

**Spec:** `docs/superpowers/specs/2026-09-07-policy-package-refactor-design.md`

## Global Constraints

- 已创建快照 `D:\mycode\pycharm_project\extract_pdf\.worktrees\policy-quality-evaluation-before-refactor-20260907-161113`，不得删除或覆盖。
- 只移动制度模块；不移动 `main.py`、`api.py`、通用 `pipeline.py`、`config.py`、`models.py`、`storage_adapter.py`、`parsers/`、`metadata/` 或通用内容管线。
- 保持 `/policy-search`、旧 `/search`、上传接口和全部 CLI 参数不变。
- 保持自动制度结构化与条款索引同步、数据库表名、流程/图谱基础和原文证据行为不变。
- 代码注释、错误信息和文档使用中文；不新增依赖。
- 所有改动保持未暂存、未提交、未合并、未推送，等待用户测试。

---

### Task 1: 创建制度包并迁移领域模块

**Files:**
- Create: `policy/__init__.py`
- Move: `policy_pipeline.py` → `policy/pipeline.py`
- Move: `policy_retrieval.py` → `policy/retrieval.py`
- Move: `policy_storage.py` → `policy/storage.py`
- Move: `policy_process.py` → `policy/process.py`
- Move: `policy_process_runner.py` → `policy/process_runner.py`
- Move: `policy_extractor.py` → `policy/extraction.py`
- Move: `policy_reviewer.py` → `policy/reviewer.py`
- Move: `policy_quality.py` → `policy/quality.py`
- Modify: `tests/test_policy_retrieval.py`

**Interfaces:**
- Produces package imports: `policy.pipeline`, `policy.retrieval`, `policy.storage`, `policy.process`, `policy.process_runner`, `policy.extraction`, `policy.reviewer`, `policy.quality`.
- Preserves callable signatures such as `structure_policy_document`, `search_indexed_policy_clauses`, `create_and_run_policy_clause_index` and `create_and_run_policy_extraction`.

- [x] **Step 1: 写入制度包导入失败测试。**

```python
def test_policy_modules_are_available_from_one_package():
    from policy import extraction, pipeline, process, process_runner, quality, retrieval, reviewer, storage

    assert callable(pipeline.structure_policy_document)
    assert callable(retrieval.search_indexed_policy_clauses)
    assert callable(storage.get_policy_clauses)
    assert callable(process.classify_process_clause)
    assert callable(process_runner.run_policy_process_classification)
    assert callable(extraction.run_policy_extraction)
    assert callable(reviewer.run_interactive_policy_review)
    assert callable(quality.write_quality_report)
```

- [x] **Step 2: 运行导入测试并确认因不存在 `policy` 包失败。**

Run: `python -B -m unittest tests.test_policy_retrieval.PolicyPackageLayoutTests -v`

Expected: `ModuleNotFoundError: No module named 'policy'`。

- [x] **Step 3: 创建 `policy/` 包并移动八个制度模块。**

使用保留文件内容的文件移动操作。移动后将领域模块的相互导入替换为 `policy.*`：

```python
# policy/pipeline.py
from policy.storage import insert_review_item, mark_policy_structure_failed, replace_policy_clauses, upsert_policy_document

# policy/retrieval.py
from policy.storage import get_policy_clauses, get_policy_document

# policy/process_runner.py
from policy.process import build_process_label
from policy.storage import get_policy_process_items, get_policy_process_run
```

根目录不保留同名 `policy_*.py` 复制文件，避免出现两套实现。

- [x] **Step 4: 运行制度包导入测试并确认通过。**

Run: `python -B -m unittest tests.test_policy_retrieval.PolicyPackageLayoutTests -v`

Expected: PASS。

### Task 2: 重连导入入口与现有测试

**Files:**
- Modify: `pipeline.py`
- Modify: `api.py`
- Modify: `main.py`
- Modify: `tests/test_policy_retrieval.py`
- Modify: `tests/test_policy_process.py`
- Modify: `tests/test_policy_quality.py`

**Interfaces:**
- Consumes: `policy.*` 包中的原有函数。
- Preserves: `POST /upload`、`GET /policy-search`、全部 `main.py` 参数及原有测试行为。

- [x] **Step 1: 将测试导入改为目标 `policy.*` 路径。**

```python
from policy.retrieval import search_indexed_policy_clauses
from policy.storage import create_policy_extraction_run
from policy.process import classify_process_clause
from policy.quality import write_quality_report
```

保留测试行为和断言，不将真实检索测试改为 mock。

- [x] **Step 2: 运行制度测试并确认入口仍引用旧路径而失败。**

Run: `python -B -m unittest discover -s tests -v`

Expected: `api.py`、`main.py` 或通用 `pipeline.py` 的旧根目录导入导致失败。

- [x] **Step 3: 将生产入口改为 `policy.*`。**

```python
# pipeline.py
from policy.pipeline import structure_policy_document

# api.py
from policy.retrieval import search_indexed_policy_clauses

# main.py
from policy.retrieval import create_and_run_policy_clause_index
from policy.extraction import create_and_run_policy_extraction
```

只改导入路径；不改变路由、参数、处理顺序、数据表或返回 JSON。

- [x] **Step 4: 运行制度测试与命令帮助确认通过。**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B main.py --help`

Expected: 所有非真实数据库测试通过；真实 PostgreSQL/pgvector 集成测试仅在未配置隔离库时跳过；帮助文本仍包含三条条款索引命令。

### Task 3: 更新架构说明与最终验证

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-07-policy-package-refactor-design.md`

**Interfaces:**
- Documents: 根目录通用导入层和 `policy/` 制度领域层的职责边界。

- [x] **Step 1: 更新 README 项目结构表。**

将根目录中的九个 `policy_*.py` 条目替换为 `policy/` 包说明，并标注其包含条款结构化、跨制度检索、流程/图谱和质量抽检子模块。

- [x] **Step 2: 进行导入和目录存在性检查。**

```powershell
python -B -c "from policy import pipeline, retrieval, storage; print('policy package ok')"
Test-Path policy\pipeline.py
Test-Path policy\retrieval.py
Test-Path policy\storage.py
Test-Path policy_pipeline.py
```

Expected: 三个包文件存在；旧根目录 `policy_pipeline.py` 不存在。

- [x] **Step 3: 执行最终验证，不产生提交。**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B main.py --help`

Run: `git diff --check`

Run: `git status --short`

Expected: 测试通过或仅真实隔离库测试跳过；CLI 入口不变；差异无空白错误；快照和重构改动均保留且未提交。

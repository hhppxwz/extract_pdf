# 跨制度条款级检索 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use subagents for this plan.

**Goal:** 为已结构化学校制度建立真实的全局条款索引，并提供返回制度名称、条款号、页码与原文证据的跨制度检索 API。

**Architecture:** 新增 `policy_retrieval.py`，独立负责条款筛选、索引文本、真实向量写入、关键词重排和 API 结果组装；`policy_storage.py` 只提供条款读取、索引运行状态及向量删除边界；`api.py` 仅注册 `GET /policy-search`。条款结构化完成后触发增量索引，历史制度通过可恢复的批次重建命令建立索引。流程和图谱模块保留，不在本计划改动。

**Tech Stack:** Python 3.10+、FastAPI、PostgreSQL、pgvector、sentence-transformers、标准库 `unittest`。

**Spec:** `docs/superpowers/specs/2026-09-07-policy-clause-retrieval-api-design.md`

## Global Constraints

- 基线为远程 `origin/main` 的 `22b9efa`；在现有隔离工作目录修改，不触碰主目录 `only_policy` PDF。
- 正式 API、索引重建、集成测试和人工验收均使用真实 PostgreSQL/pgvector、真实嵌入模型与真实条款；测试库与生产库隔离。
- 结果必须返回制度名称、文件名、条款号、章节路径、页码和 `raw_text`，不生成问答答案。
- 保留现有流程/知识图谱模块、条款溯源字段和历史条款；不删除它们。
- 不新增外部检索服务或第三方依赖；注释、错误信息、文档用中文。
- 本计划所有变更保持未暂存、未提交、未合并、未推送，等用户测试决定。

---

### Task 1: 条款级检索的纯规则与结果结构

**Files:**
- Create: `policy_retrieval.py`
- Create: `tests/test_policy_retrieval.py`

**Interfaces:**
- Produces: `is_searchable_policy_clause(clause: dict[str, Any]) -> bool`
- Produces: `build_clause_index_text(clause: dict[str, Any]) -> str`
- Produces: `build_clause_metadata(clause: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]`
- Produces: `rerank_clause_candidates(query: str, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]`
- Produces: `build_policy_search_response(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]`

- [x] **Step 1: 写入失败测试，明确标题排除、条款号优先级和可回查响应。**

```python
def test_build_policy_search_response_returns_citation_fields():
    response = build_policy_search_response("差旅报销材料", [{
        "similarity": 0.9,
        "metadata": {
            "clause_id": "c1", "title": "差旅费管理办法",
            "file_name": "差旅费管理办法.pdf", "article_no": "第十二条",
            "paragraph_no": "", "item_no": "", "chapter_path": ["第三章"],
            "page_start": 8, "page_end": 8, "raw_text": "应提交发票。",
            "search_text": "第三章 第十二条 应提交发票。",
        },
    }])
    assert response["results"][0]["clause"]["clause_no"] == "第十二条"
    assert response["results"][0]["clause"]["page_start"] == 8
    assert response["results"][0]["clause"]["raw_text"] == "应提交发票。"
```

- [x] **Step 2: 运行失败测试。**

Run: `python -B -m unittest tests.test_policy_retrieval -v`

Expected: 因 `policy_retrieval` 不存在而失败。

- [x] **Step 3: 实现最小纯函数。**

规则仅索引 `article`、`paragraph`、`item` 的非空条款；`book`、`chapter`、`section` 不作为结果。排序以向量分数为基准，连续关键词命中增加确定性分数；按 `clause_id` 去重；输出原文和元数据，不调用模型生成文本。

- [x] **Step 4: 运行纯规则测试。**

Run: `python -B -m unittest tests.test_policy_retrieval -v`

Expected: 全部通过。

### Task 2: 真实全局条款索引与可恢复重建

**Files:**
- Modify: `storage_adapter.py`
- Modify: `policy_storage.py`
- Modify: `policy_pipeline.py`
- Modify: `policy_retrieval.py`
- Modify: `tests/test_policy_retrieval.py`

**Interfaces:**
- Produces: `sync_policy_clause_index(policy_id: str) -> int`
- Produces: `create_policy_clause_index_run(batch_id: str, versions: ProcessingVersion) -> str`
- Produces: `run_policy_clause_index(run_id: str, resume: bool = False) -> dict[str, Any]`
- Produces: `search_indexed_policy_clauses(query: str, top_k: int) -> list[dict[str, Any]]`

- [x] **Step 1: 写入真实测试库的失败集成测试。**

测试在隔离 PostgreSQL 数据库中写入两份制度和三条条款，调用真实嵌入模型建立 `policy_clause_search`，再查询“差旅报销材料”。断言返回的第一条含目标 `clause_id`，且按 `policy_id` 重建时不会删除另一份制度的向量。测试开始前检查 `POLICY_RETRIEVAL_TEST_DATABASE`；未配置时明确跳过，绝不改写其他数据库。

- [x] **Step 2: 运行集成测试并确认缺少索引实现而失败。**

Run: `python -B -m unittest tests.test_policy_retrieval.PolicyRetrievalIntegrationTests -v`

Expected: 未配置测试库时跳过；配置后因索引函数不存在而失败。

- [x] **Step 3: 实现真实索引表、按制度替换向量和运行状态。**

使用固定表名 `policy_clause_search`，metadata 必须包含规范中列出的制度、条款、页码和原文字段。新增索引运行/条目表记录待处理、运行中、成功、失败状态。删除语句限定目标 `policy_id`；重建时读取当前 `structure_version` 和活跃条款。`structure_policy_document()` 在成功写入条款后调用增量同步，索引失败记录错误但不回滚条款结构化。

- [x] **Step 4: 再次运行真实集成测试。**

Run: `python -B -m unittest tests.test_policy_retrieval.PolicyRetrievalIntegrationTests -v`

Expected: 配置测试库时通过，并能在数据库中检查到真实向量与条款 metadata。

### Task 3: 检索 API 与批次命令行入口

**Files:**
- Modify: `api.py`
- Modify: `main.py`
- Modify: `policy_retrieval.py`
- Modify: `tests/test_policy_retrieval.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `GET /policy-search?q=<问题>&top_k=<1..20>`
- Produces CLI: `--rebuild-policy-clause-index <batch_id>`
- Produces CLI: `--resume-policy-clause-index <run_id>`
- Produces CLI: `--policy-clause-index-status <run_id>`

- [x] **Step 1: 写入失败 API 测试。**

```python
def test_policy_search_returns_original_clause_citation(client):
    response = client.get("/policy-search", params={"q": "差旅报销材料", "top_k": 3})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["policy"]["title"]
    assert result["clause"]["clause_no"]
    assert result["clause"]["raw_text"]
    assert result["clause"]["page_start"] > 0
```

另写空查询返回 422、空索引返回中文 409、真实存储/嵌入故障返回中文 503，以及 `main.py --help` 包含三条重建命令的测试。

- [x] **Step 2: 运行 API 测试确认失败。**

Run: `python -B -m unittest tests.test_policy_retrieval.PolicyRetrievalApiTests -v`

Expected: `/policy-search` 不存在而失败。

- [x] **Step 3: 注册 API 和命令行处理函数。**

`api.py` 只调用 `search_indexed_policy_clauses()` 和 `build_policy_search_response()`，不得调用 LLM；`main.py` 打印索引运行 ID、总制度数、成功数、失败数和失败原因。保留旧 `/search`、流程和图谱命令，不能改变其行为。

- [x] **Step 4: 运行 API、命令行与集成测试。**

Run: `python -B -m unittest tests.test_policy_retrieval -v`

Run: `python -B main.py --help`

Expected: 已配置真实测试库时所有检索测试通过；帮助文本包含三个索引命令。

### Task 4: 真实样本验收说明与最终验证

**Files:**
- Modify: `README.md`
- Create: `docs/policy-search-evaluation-template.csv`

**Interfaces:**
- Documents: `问题,期望条款ID,事项类型,Top-3命中,Top-5命中,实际首位条款ID,备注`

- [x] **Step 1: 写入 20 题真实人工验收模板。**

模板只提供表头和填写说明，不填造任何制度条款或检索结果。README 明确说明：先处理制度批次、重建条款索引、调用 `/policy-search`、人工回填 Top-3/Top-5 命中。

- [x] **Step 2: 执行最终验证，不产生提交。**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B main.py --help`

Run: `git diff --check`

Run: `git status --short`

Expected: 测试通过或只因未配置隔离真实测试库而跳过；帮助文本可见新命令；差异无空白错误；所有改动未暂存、未提交。

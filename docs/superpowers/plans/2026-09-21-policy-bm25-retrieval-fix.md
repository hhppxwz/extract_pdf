# 政策问答 BM25 混合检索修复实施计划

> **For agentic workers:** 本计划在当前工作区内执行；每个任务都必须先写失败测试，再实现最小修复并运行验证。

**Goal:** 修复政策条款 BM25 混合检索，使其能够正确加载当前版本条款、按需建立并刷新内存索引、保留可引用 metadata，并让 RRF 融合结果真正参与最终排序。

**Architecture:** 保留现有 pgvector 作为语义召回，使用 BM25 作为中文关键词召回，二者通过 RRF 合并候选。BM25 索引采用进程内按需初始化与显式失效机制；数据库读取复用项目已有的 `storage.relational.query` 接口，并在内存中限制到制度当前结构版本。

**Tech Stack:** Python、unittest、jieba、rank-bm25、PostgreSQL/pgvector 存储适配层。

**Spec:** 本次会话中关于“解决 BM25 检索实现问题”的用户需求及前一轮审查结论。

## Global Constraints

- 保留现有向量检索、时间过滤和 API 返回结构。
- 只把 `article`、`paragraph`、`item` 且有原文的当前条款加入 BM25。
- 代码注释和新增 docstring 使用中文。
- 不覆盖当前工作区中与本任务无关的已有修改。
- 完成前运行 BM25 针对性测试、原有政策检索测试和完整测试套件。

---

### Task 1: 为 BM25 生命周期、当前版本过滤和融合排序补充失败测试

**Files:**
- Modify: `tests/test_policy_retrieval.py`
- Read: `policy/retrieval.py`

**Interfaces:**
- Tests will exercise `load_policy_clauses_for_bm25()` and `search_policy_clauses_bm25()`.
- Tests will verify `rerank_clause_candidates(..., use_fusion_score=True)` preserves RRF order.

- [x] **Step 1: Write the failing tests**

Add tests that assert: BM25 search lazily builds only once; loader uses relational storage and excludes stale structure versions and non-searchable headings while retaining citation metadata; and fusion-aware reranking puts a strong BM25/vector agreement result ahead of a higher raw vector-similarity-only result.

- [x] **Step 2: Run the focused tests and verify the expected failures**

Run: `\.venv\Scripts\python.exe -B -m unittest tests.test_policy_retrieval.PolicyRetrievalBM25Tests -v`

Expected: failures caused by the missing lazy initialization, incorrect database accessor/current-version filtering, and reranker ignoring RRF.

### Task 2: 修复 BM25 索引构建、刷新和数据库数据装配

**Files:**
- Modify: `policy/retrieval.py`
- Modify: `requirements.txt`

**Interfaces:**
- `rebuild_policy_clause_bm25_index() -> int` rebuilds the current active corpus and returns its document count.
- `invalidate_policy_clause_bm25_index() -> None` marks the process-local index stale.
- `search_policy_clauses_bm25(query, top_k=50) -> list[dict[str, Any]]` lazily builds the index and returns complete citation metadata.

- [x] **Step 1: Implement the minimal lifecycle and tokenization changes**

Use a reentrant lock, a ready flag, and normalized Jieba tokens. Empty corpora must return an empty result instead of raising an unhelpful index error. Validate `top_k`.

- [x] **Step 2: Implement relational loading against current policy versions**

Read `policy_clauses` and `policy_documents` through `storage.relational.query`, map each policy to its current `structure_version`, filter active searchable clauses, and attach title/file/page/chapter/raw-text fields.

- [x] **Step 3: Declare direct runtime dependencies**

Add pinned minimum versions for `jieba` and `rank-bm25` to `requirements.txt`. Avoid a direct NumPy import where Python sorting is sufficient.

- [x] **Step 4: Run the focused BM25 tests and verify they pass**

Run: `\.venv\Scripts\python.exe -B -m unittest tests.test_policy_retrieval.PolicyRetrievalBM25Tests -v`

Expected: PASS.

### Task 3: 让混合召回真正使用 RRF 排序并在数据更新后失效

**Files:**
- Modify: `policy/retrieval.py`
- Modify: `policy/storage.py`

**Interfaces:**
- `rerank_clause_candidates(..., use_fusion_score=False)` keeps legacy ranking by default and uses `rrf_score` when explicitly requested.
- `search_indexed_policy_clauses()` calls fusion-aware reranking after RRF.

- [x] **Step 1: Extend the failing test to cover index invalidation**

After `invalidate_policy_clause_bm25_index()`, the next search must rebuild the corpus and observe changed source documents.

- [x] **Step 2: Implement fusion-aware final ranking**

Preserve the existing reranker behavior for non-hybrid callers. For the hybrid path, use RRF score as the primary score, keep vector and keyword scores for diagnostics, and retain clause de-duplication.

- [x] **Step 3: Invalidate BM25 after clause replacement**

Call the retrieval invalidation function after `replace_policy_clauses()` completes so a subsequent query cannot use a stale in-memory corpus.

- [x] **Step 4: Run focused tests and the existing policy retrieval regression tests**

Run: `\.venv\Scripts\python.exe -B -m unittest tests.test_policy_retrieval -v`

Expected: all non-integration tests PASS; integration tests may remain skipped when `POLICY_RETRIEVAL_TEST_DATABASE` is not configured.

### Task 4: 最终验证和回归检查

**Files:**
- Read: `policy/retrieval.py`
- Read: `requirements.txt`
- Read: `tests/test_policy_retrieval.py`

- [x] **Step 1: Run the full test suite**

Run: `\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -v`

- [x] **Step 2: Inspect the final diff**

Confirm only the planned retrieval, dependency, test, and plan files changed as part of this task; do not revert unrelated existing worktree changes.

- [x] **Step 3: Report verification evidence**

Report the exact test command, pass/fail result, and any skipped integration tests or environment limitations.

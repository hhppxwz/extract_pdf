# 项目工作交接总结

> 更新时间：2026-09-14  
> 当前分支：`codex/policy-quality-evaluation`  
> 基线分支：`main`（基线提交 `22b9efa`）  
> 当前状态：功能代码大部分仍在工作区，**尚未整体提交**，交接时不要直接清理或重置工作区。

## 1. 当前目标

项目原本是 PDF 多模态解析与入仓服务，目前工作重点已经扩展为：

1. 将制度 PDF 结构化为可回查的章、节、条、款、项；
2. 跨制度检索原始条款，不生成无依据答案；
3. 识别真正包含办事动作的流程条款；
4. 从流程条款抽取事项、流程、步骤、角色、材料、条件、时限、结果及其关系；
5. 通过人工审核和 CSV 质检，生成带制度名称、条款号、页码和原文证据的流程图谱；
6. 以报销作为首期试点，后续通过事项域配置扩展到采购、合同、用章、人事等主题。

核心原则是：原始 PDF 和条款是权威依据，模型只生成候选；没有连续原文证据的节点或关系不能直接进入已确认图谱。

## 2. 已提交到当前分支的内容

当前分支比 `main` 多 4 个提交：

| 提交 | 内容 |
| --- | --- |
| `0757523` | 通用办事流程知识图谱设计 |
| `a376611` | 通用办事流程知识图谱实施计划 |
| `6dd1e42` | 可配置事项域目录及测试 |
| `713679d` | 在线流程图谱可视化设计 |

已提交文件主要包括：

- `policy/domain_catalog.json`：版本化事项域配置；
- `policy/domain_catalog.py`：事项域加载、启用状态及选择校验；
- `tests/test_policy_domain_catalog.py`：事项域顺序、禁用项和未知代码测试；
- `docs/superpowers/specs/2026-09-09-general-process-knowledge-graph-design.md`；
- `docs/superpowers/plans/2026-09-09-general-process-knowledge-graph.md`；
- `docs/superpowers/specs/2026-09-10-policy-workflow-visualization-design.md`。

## 3. 已实现但尚未提交的主要内容

### 3.1 制度领域包重构

原根目录中的 `policy_extractor.py`、`policy_pipeline.py`、`policy_reviewer.py`、`policy_storage.py` 已迁移到统一的 `policy/` 包，并补齐以下模块：

| 模块 | 职责 |
| --- | --- |
| `policy/pipeline.py` | 制度文档和条款层级结构化 |
| `policy/storage.py` | 制度、条款、索引运行、流程运行、抽取候选和人工标注持久化 |
| `policy/extraction.py` | 规则或 LLM 实体关系候选抽取、恢复和运行状态维护 |
| `policy/reviewer.py` | 候选通过、拒绝、修正、补充及证据位置记录 |
| `policy/retrieval.py` | 条款索引、重建、恢复、检索和可回查响应 |
| `policy/process.py` | 流程条款分类、事项域约束和证据校验 |
| `policy/process_runner.py` | 流程分流运行、恢复及状态统计 |
| `policy/quality.py` | 条款、流程和图谱抽检 CSV，以及 Markdown/JSON 质量报告 |
| `policy/workflow_graph.py` | 将已审核候选只读投影并导出为流程图谱 JSON |
| `policy/clause_export.py` | 按批次导出保留层级、条款号、页码和原文的 JSON |

项目入口、API 和测试已经改用 `policy.*` 导入路径。旧根目录文件当前显示为删除，这是目录迁移的一部分，不应在未核对新包内容的情况下恢复。

### 3.2 条款级跨制度检索

已实现：

- 只索引原文非空的条、款、项，排除编、章、节标题；
- 使用 PostgreSQL/pgvector 保存全局条款索引；
- 重建单份制度时只替换该制度向量，不误删其他制度；
- 支持索引运行状态、失败恢复和空索引判断；
- `GET /policy-search?q=...&top_k=...` 返回制度名、文件名、完整条款号、章节路径、页码和原文证据；
- 索引未准备好返回 409，服务不可用返回 503，参数错误返回 422；
- `docs/policy-search-evaluation-template.csv` 提供 20 题人工验收模板。

现阶段定位是“检索并展示依据”，不是生成办事结论，也未实现制度冲突裁决。

### 3.3 流程条款分流与通用流程图谱

已实现：

- 事项域可配置，并可通过 `--policy-domains` 限定本次运行范围；
- 每条制度条款保存 `process`、`non_process` 或 `pending` 判定；
- 只有判定明确、审核满足要求且证据是原文连续片段的流程条款才可进入后续抽取；
- 抽取实体已扩展到 `matter`、`process`、`step`、`role`、`location`、`condition`、`outcome` 等；
- 抽取关系已扩展到 `has_process`、`has_step`、`next_step`、`performed_by`、`performed_at`、`has_precondition`、`routes_to`、`produces`、`exception_to` 等；
- 对本地回环 LLM 地址绕过系统代理，父条款上下文长度可通过 `LLM_PARENT_CONTEXT_CHARS` 限制；
- 人工修正后的候选可替换原候选进入图谱，人工补充的关系会校验两端实体；
- 图谱节点和边均携带制度、条款、页码、证据文本等引用信息；
- 支持命令行导出 JSON，也支持 `GET /policy-workflow-graph/{run_id}` 只读 API。

### 3.4 人工质检闭环

已实现以下导出与汇总：

- `条款抽检.csv`：核验条款边界、层级、文字和页码；
- `图谱抽检.csv`：核验实体、关系及原文证据；
- `流程条款抽检.csv`：核验流程分流是否正确；
- `流程图谱抽检.csv`：只导出流程来源候选；
- `错误字典.csv`：统一人工错误代码；
- `质量报告.md` 和 `质量报告.json`：汇总正确数、错误数、待确认数、遗漏和错分原因。

质检模块只读业务结果，人工填写保留在 CSV，不覆盖原始制度、条款、候选或既有人工标注。

### 3.5 PDF、上传和表格链路改进

工作区还包含以下并行改进：

- 新增 `parsers/mineru.py`，支持将 PDF 页面渲染后调用 OpenAI 兼容的 MinerU 服务；
- 上传接口改为立即返回 HTTP 202 和 `file_id`，后台继续处理；
- 加入 CORS 配置，保留上传时的原始文件名；临时/下载器文件名可回退到清洗后的 PDF 标题；
- 表格解析支持合并表头展开、原始表头映射、千分位数值转换和相邻标题识别；
- 新增表格目录、按编号/名称/原文件名搜索、分页读取，以及 `contains`、`equals`、`gte`、`lte` 安全筛选；
- 通用文本 `/search` 与表格搜索保持分离。

## 4. 当前主要使用入口

最短制度实体关系链路：

```powershell
python main.py --process-dir "D:\policies"
python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx --limit 10
python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 张三
python main.py --export-policy-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"
```

报销流程图谱试点链路：

```powershell
python main.py --classify-policy-process-batch batch_xxxxxxxxxxxxxxxx --policy-domains reimbursement
python main.py --export-policy-process-review policy_process_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"
python main.py --extract-policy-process-run policy_process_xxxxxxxxxxxxxxxx --limit 20
python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 张三
python main.py --export-policy-workflow-graph policy_run_xxxxxxxxxxxxxxxx --workflow-graph-output "D:\policy_quality\workflow_graph.json"
python main.py --report-policy-quality "D:\policy_quality"
```

条款检索链路：

```powershell
python main.py --rebuild-policy-clause-index batch_xxxxxxxxxxxxxxxx
python main.py
# GET /policy-search?q=出差报销需要什么材料&top_k=5
```

默认 `python main.py --help` 只展示核心三步，`python main.py --help-all` 展示全部恢复、检索、流程和质检命令。

## 5. 验证结果

2026-09-14 在当前工作区执行：

```powershell
python -B -m unittest discover -s tests -v
git diff --check
python -B main.py --help
python -B main.py --help-all
```

结果：

- 共运行 79 项测试；77 项通过，2 项跳过；
- 跳过项均为真实 PostgreSQL/pgvector 集成测试，原因是未设置 `POLICY_RETRIEVAL_TEST_DATABASE`；
- `git diff --check` 无空白错误，仅提示部分文件下次由 Git 处理时可能从 LF 转为 CRLF；
- 核心帮助和完整帮助均可执行。

这说明当前纯逻辑、模拟存储、API 和命令行测试通过，但**不等于真实 PostgreSQL、pgvector、MinIO、CloudMinerU/MinerU、嵌入模型和 LLM 的全链路验收已完成**。

## 6. 未完成事项和已知风险

### 高优先级

1. **工作区尚未整理提交。** 当前存在大量已修改、已删除和未跟踪文件。应先按功能分组审查，再决定提交；不要执行会丢失工作区的重置操作。
2. **真实集成测试未跑。** 需要配置独立测试数据库并设置 `POLICY_RETRIEVAL_TEST_DATABASE`，验证 pgvector 建表、按制度替换索引和 API 原始引用。
3. **在线图谱浏览页未实现。** 目前只有设计和计划；尚无 `GET /policy-workflow-view/{run_id}` 页面路由、页面渲染器或对应测试。现有交付只有图谱 JSON API/文件。
4. **MinerU 命令行入口不一致。** `parsers/dispatcher.py` 已支持 `mineru`，但 `main.py --backend` 的 choices 仍只有 `cloudmineru`、`pymupdf`，README 也没有 MinerU 配置说明。直接从 CLI 选择 `mineru` 会被参数校验拒绝。

### 提交前必须确认

1. `__pycache__/*.pyc` 和 `metadata/__pycache__/*.pyc` 是运行产物，不应作为功能改动提交；当前它们已经被 Git 跟踪，需要单独处理忽略策略。
2. `before.txt`、`after.txt`、`test_mineru1.py`、`test_mineru2.py` 更像本地调试或对照文件，提交前应确认是否保留。
3. `only_policy/` 下有两个已跟踪 PDF 被删除，同时新增一个 PDF 和一个 `batch_*_条款重组结果.json`。这些属于样例/运行数据，不能在未确认数据用途和版权/体积要求前提交。
4. 多份早期设计与计划仍为未跟踪文件；部分计划复选框没有同步实际实现状态，不能只根据复选框判断完成度，应以代码、测试和本交接文档为准。
5. `.env` 位于仓库中但本次未显示修改。提交或共享前仍应确认其中没有真实密钥；交接文档不记录任何密钥值。
6. 当前控制台中文测试说明出现乱码，测试结果本身不受影响，但建议统一 PowerShell/Python 输出编码，便于人工排查失败日志。

## 7. 建议的后续顺序

1. 先修复 MinerU CLI choices，并为命令行选择 `mineru` 增加测试和 README 配置说明；
2. 配置隔离的 PostgreSQL/pgvector 测试库，补跑 2 项被跳过的集成测试；
3. 对工作区按“制度包重构 → 检索 → 流程图谱 → 质检 → 表格/API → MinerU”分组审查和提交，排除缓存与本地样例；
4. 选取少量真实制度执行端到端试点：PDF 导入、条款结构化、流程分流、候选抽取、人工审核、图谱导出和质量报告；
5. 使用 `docs/policy-search-evaluation-template.csv` 完成至少 20 个真实问题的 Top-3/Top-5 命中率验收；
6. 在数据链路和真实验收稳定后，再执行在线图谱浏览页计划，避免前端先于图谱数据质量定型。

## 8. 关键文档索引

- `README.md`：当前完整使用说明；
- `docs/superpowers/specs/2026-09-09-general-process-knowledge-graph-design.md`：通用流程图谱目标、模型和交付边界；
- `docs/superpowers/plans/2026-09-09-general-process-knowledge-graph.md`：通用流程图谱实施计划；
- `docs/superpowers/specs/2026-09-10-policy-workflow-visualization-design.md`：在线图谱页面设计；
- `docs/superpowers/plans/2026-09-10-policy-workflow-visualization.md`：尚待执行的页面实施计划；
- `docs/superpowers/specs/2026-09-07-policy-clause-retrieval-api-design.md`：条款检索边界与 API 约定；
- `docs/superpowers/specs/2026-09-06-policy-quality-evaluation-design.md`：人工质检表和报告规则。

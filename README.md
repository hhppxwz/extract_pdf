# PDF 多模态提取入仓

基于 cloudmineru + PostgreSQL(pgvector+JSONB) + MinIO 的 PDF 多模态提取 Pipeline。将一个 PDF 同时产出**文本向量、图片、关系表、键值文档**四种结构化产物。

## 架构

```
PDF 上传 → cloudmineru 解析 → 分类决策(规则+LLM) → 多模态分流存储
                                              ├─ 文本 → pgvector 向量索引
                                              ├─ 图片 → MinIO 对象存储
                                              ├─ 数据表 → PostgreSQL 关系表
                                              └─ 表单   → PostgreSQL JSONB
```

## 环境要求

- Python 3.10+
- PostgreSQL 14+（需安装 pgvector 扩展）
- MinIO

## 快速开始

### 制度图谱三步上手

如果你的目标是从制度 PDF 中抽取实体和关系，先走下面三步即可；不要先使用流程分流、条款索引或质量报告等进阶命令。

```powershell
# 1. 批量导入 PDF；命令结束后记下输出的 batch_id
python main.py --process-dir "D:\policies"

# 2. 用 batch_id 抽取制度条款中的实体和关系；首次建议先验证 10 条
python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx --limit 10

# 3. 用 policy_run_id 审核候选，并按需导出实体关系抽检表
python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 张三
python main.py --export-policy-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"
```

第 1 步完成后，输入目录会自动生成 `batch_xxx_条款重组结果.json`；其中保留每份制度的条款顺序、父条款 ID、章节路径、页码和原文，便于直接核查。

每一步的输出 ID 都是下一步的输入：PDF 导入产生 `batch_id`，实体关系抽取产生 `policy_run_id`。命令行默认帮助也只显示这条路径：

```powershell
python main.py --help
```

需要办事流程图谱、断点恢复、条款检索或质量报告时，再查看完整命令分类：

```powershell
python main.py --help-all
```

### 1. 安装依赖

```powershell
cd D:\mycode\pycharm_project\extract_pdf
.\.venv\Scripts\activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 命令行处理 PDF

```powershell
python main.py --process "C:\path\to\your.pdf"
```

输出示例：
```
正在处理: C:\path\to\your.pdf

处理结果:
  文件 ID:    pdf_abc123def456...
  状态:       done
  总块数:     4
  文本块:     1
  图片:       1
  数据表:     1
  表单:       1
  待审核表:   0
  耗时:       0.85s
```

### 3. 启动 FastAPI 服务

```powershell
python main.py
```

访问 http://127.0.0.1:8000/docs 查看 API 文档。

**上传 PDF：**
```powershell
curl -X POST http://127.0.0.1:8000/upload -F "file=@C:\path\to\your.pdf"
```

上传成功立即返回 HTTP `202`，而不是等待解析完成：

```json
{"file_id":"pdf_xxx","status":"pending","status_url":"/status/pdf_xxx"}
```

前端应轮询 `status_url` 或 `GET /status/{file_id}` 获取处理进度和最终结果。跨域前端默认可访问；生产环境建议以逗号分隔的域名列表设置 `CORS_ALLOW_ORIGINS`，例如 `https://app.example.com`。

**查询状态：**
```powershell
curl http://127.0.0.1:8000/status/{file_id}
```

**语义检索：**
```powershell
curl "http://127.0.0.1:8000/search?q=学生成绩&file_id={file_id}&top_k=5"
```

**表格搜索与展示：**
```powershell
# 搜索表格目录：q 可匹配表格编号、表格名称或原始 PDF 文件名，并附带前 N 行预览
curl "http://127.0.0.1:8000/tables/search?q=X010102&row_limit=100"

# 单独读取一张表，返回 columns + rows + page，可直接渲染为表格
curl "http://127.0.0.1:8000/tables/{file_id}/{block_id}/data?limit=100&offset=0"

# 按列筛选：支持 contains、equals、gte、lte；filters 是 JSON 条件数组
curl.exe -G "http://127.0.0.1:8000/tables/{file_id}/{block_id}/data" `
  --data-urlencode 'filters=[{"column":"项目","operator":"contains","value":"Rcpy"}]'
```

`/search` 始终只返回文本检索结果，表格不会混入其中。新处理的表格会保存编号、名称、来源文件名、页码、列定义和存储位置。名称优先取 `table_caption`，为空时回退到同页相邻的标题文本（如 `X010102 经费数额`）。`/tables/{file_id}/{block_id}/data` 的响应固定包含 `columns`、`rows` 和 `page`，可直接渲染为独立表格；关系表内部行号和页码字段不会暴露。筛选列必须存在于该表目录，条件值使用参数化查询；历史表格需重新处理后才会进入搜索目录。

**批量处理与断点续跑：**

```powershell
# 递归处理目录中的全部 PDF，返回 batch_id
python main.py --process-dir "D:\policies"

# 中断后恢复未完成或到期重试的文件
python main.py --resume-batch batch_xxxxxxxxxxxxxxxx

# 只查看批次状态
python main.py --batch-status batch_xxxxxxxxxxxxxxxx
```

批处理按文件 SHA256 去重，临时错误最多自动重试 3 次；批次和文件任务状态、错误信息以及解析器/模型版本保存在 PostgreSQL 中。

**制度条款与实体关系抽取：**

目录批处理识别为规章制度的 PDF 后，会自动写入制度文档和章/节/条/款/项层级的条款记录。条款结构化完成后，再手动启动实体关系抽取：

```powershell
# 为已完成的 PDF 批次创建一个新的抽取版本并执行
python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx

# 先只抽取前 10 条实质性条款，验证结果和模型消耗
python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx --limit 10

# 实体关系抽取中断后恢复
python main.py --resume-policy-extraction policy_run_xxxxxxxxxxxxxxxx

# 查看实体关系抽取状态
python main.py --policy-extraction-status policy_run_xxxxxxxxxxxxxxxx

# 在终端逐项审核 Qwen 候选；每次最多显示 20 条
python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 张三 --review-limit 20
```

未配置 LLM 时使用规则抽取；配置 LLM 后只采用模型候选。Qwen 等 LLM 候选默认进入人工审核。审核时输入 `a` 通过、`r` 拒绝、`c` 修正、`n` 补充、`s` 跳过或 `q` 结束。原始模型候选不会被覆盖，人工结论会写入 `policy_manual_annotations`，并保存原文证据的字符起止位置，后续可转换为 BERT 训练标注。制度效力默认是 `unknown`，未经人工核验不会自动判断为现行或废止。

**制度废止关系审核：**

制度条款结构化完成后，可以单独从明确的废止措辞提取制度级候选；这不会调用大模型，也不会自动改变任何旧制度的状态。

```powershell
# 从已结构化批次中提取废止关系候选
python main.py --extract-policy-abolition-relations batch_xxxxxxxxxxxxxxxx

# 查看候选的审核和目标解析状态
python main.py --policy-abolition-relation-status batch_xxxxxxxxxxxxxxxx

# 在终端审核待处理候选；默认最多显示 20 条
python main.py --review-policy-abolition-relations batch_xxxxxxxxxxxxxxxx --abolition-reviewer 张三
```

只有同句中明确、直接连接“废止”谓词与 `《制度名》` 的表述才会产生候选；否定表述、跨句依据引用和“第…条/款/项”等局部废止不会被提取。未能唯一匹配旧制度、或未能识别废止日期的候选，都需要人工确认；终端审核可输入 `a` 通过、`r` 拒绝、`t` 指定目标制度并输入或保留日期后通过、`d` 指定日期并在目标未解析时输入目标制度后通过、`s` 跳过或 `q` 结束。只有审核通过，系统才会把已确认的目标制度标为失效并写入已确认的废止日期；已审核关系不能重复审核。

**跨制度条款检索（当前阶段目标）：**

当前交付目标是：用户提出办事问题后，系统跨制度检索并展示可回查的条款，不生成“应该怎么办”的答案。每条结果固定包含制度名称、文件名、最具体条款号、章节路径、起止页码和原文证据。知识图谱、流程推理和冲突裁决仍保留为后续方向，现有条款、实体、关系和流程表不会删除。

新完成结构化的制度会自动尝试同步自己的条款索引；已有制度需要对批次执行一次重建。索引只纳入原文非空的“条/款/项”，不把编、章、节标题作为检索结果。重建过程使用 PostgreSQL/pgvector 和配置的真实嵌入模型，按制度记录状态；失败不会改写源条款，也可单独恢复。

```powershell
# 1. 对已经完成 PDF 解析和制度条款结构化的批次建立全局条款索引
python main.py --rebuild-policy-clause-index batch_xxxxxxxxxxxxxxxx

# 2. 中断或失败后，只重试失败的制度
python main.py --resume-policy-clause-index policy_index_xxxxxxxxxxxxxxxx

# 3. 查看逐制度状态和失败原因
python main.py --policy-clause-index-status policy_index_xxxxxxxxxxxxxxxx

# 4. 启动 API 后，以办事问题跨制度查询；top_k 范围是 1 到 20
curl "http://127.0.0.1:8000/policy-search?q=出差报销需要什么材料&top_k=5"
```

若索引尚未建立或没有可检索条款，`/policy-search` 返回 HTTP 409 和中文提示；问题为空返回 422；真实嵌入或数据库不可用返回 503。原有 `/search` 仍是按 `file_id` 检索通用文本块，不能替代此接口。

完成首批索引后，从报销、差旅、采购等真实办事问题中准备至少 20 题，在 [检索验收模板](docs/policy-search-evaluation-template.csv) 中逐题填写期望条款 ID、Top-3/Top-5 是否命中和实际首位结果。模板不包含虚构制度或模拟检索结果；没有达到预期命中率前，不进入基于条款的办事流程或图谱推理阶段。

**报销、差旅、采购流程图谱（首期试运行）：**

这条路径用于先验证“哪些条款是办事流程”，再验证实体关系；它不会使用旧的全量抽取命令。每个当前条款都会被保留为 `process`、`non_process` 或 `pending`：只有同时命中三类事项与明确办事动作、并能回到原文证据的 `process` 条款，才会自动进入后续抽取。当前版本采用高精度规则；模糊条款保留为 `pending`，不会用大模型猜测性纳入图谱。

```powershell
# 1. 对一个已完成的制度批次创建流程条款分流运行；报销试点时显式收窄范围
python main.py --classify-policy-process-batch batch_xxxxxxxxxxxxxxxx --policy-domains reimbursement

# 2. 查看统计，或在中断后恢复失败条款
python main.py --policy-process-status policy_process_xxxxxxxxxxxxxxxx
python main.py --resume-policy-process policy_process_xxxxxxxxxxxxxxxx

# 3. 导出流程分流抽检表，在 Excel 回填“分流正确、正确判定、错分原因、备注”
python main.py --export-policy-process-review policy_process_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"

# 4. 只从已确认的流程条款抽取实体和关系；先限制 20 条做小样本验证
python main.py --extract-policy-process-run policy_process_xxxxxxxxxxxxxxxx --limit 20

# 5. 导出只包含流程来源候选的图谱抽检表
python main.py --export-policy-process-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"

# 6. 审核完成后导出可被前端或问答服务消费的办事流程图谱 JSON
python main.py --export-policy-workflow-graph policy_run_xxxxxxxxxxxxxxxx --workflow-graph-output "D:\policy_quality\workflow_graph.json"

# 7. 回填后生成报告；报告会包括流程分流正确/错误/待确认数和错分原因
python main.py --report-policy-quality "D:\policy_quality"
```

导出目录中的 `流程条款抽检.csv` 用于判断“这条是否应该进入办事图谱”；`流程图谱抽检.csv` 用于判断“已入图的实体和关系是否有原文证据、是否遗漏”。办事流程图谱不是另一套事实库：它仅投影已通过的实体、关系和人工标注，每个节点和边都包含制度、条款、页码及连续原文证据；也可通过 `GET /policy-workflow-graph/{policy_run_id}` 获取同一份 JSON。总则、目的、解释权、施行日期和一般执行要求会留有 `non_process` 判定记录，不会被删除，也不会进入图谱。`pending` 条款默认不入图，待你在抽检中确认规则边界后再调整。

**制度条款与图谱质量抽检：**

质检工具不会更新制度、条款、实体、关系或既有人工审核结论。它只从数据库读取结果，导出可用 Excel 打开的 UTF-8 CSV；人工回填后再生成汇总报告。

```powershell
# 1. 从已完成的制度 PDF 批次中，为每份制度均匀抽取 10 条，导出条款抽检表和错误字典
python main.py --export-policy-clause-review batch_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"

# 可按需调整为每份制度抽检 5 条
python main.py --export-policy-clause-review batch_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality" --clauses-per-policy 5

# 2. 从已完成的实体关系抽取运行中导出图谱候选抽检表
python main.py --export-policy-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"

# 3. 在 Excel 中回填“对/错”、错误类型、缺失内容和备注后，生成 Markdown 与 JSON 报告
python main.py --report-policy-quality "D:\policy_quality"
```

导出目录会包含 `条款抽检.csv`、`图谱抽检.csv`、`错误字典.csv`；报告命令会新增 `质量报告.md` 与 `质量报告.json`。条款表核验边界、层级、文字和页码；图谱表核验实体/关系是否有原文证据。人工填写只保存在 CSV 中，系统原始结果不会被覆盖。

## 服务配置

设置以下环境变量连接真实服务：

```powershell
# cloudmineru API
$env:CLOUDMINERU_API_URL = "https://your-host/api/v1"
$env:CLOUDMINERU_API_KEY = "your-key"

# LLM API（OpenAI 兼容）
$env:LLM_API_URL = "https://api.openai.com/v1"
$env:LLM_API_KEY = "sk-xxx"
$env:LLM_MODEL = "gpt-4o"

# PostgreSQL
$env:PG_HOST = "127.0.0.1"
$env:PG_PORT = "5432"
$env:PG_USER = "postgres"
$env:PG_PASSWORD = "your-password"
$env:PG_DATABASE = "pdf_warehouse"

# MinIO
$env:MINIO_ENDPOINT = "127.0.0.1:9000"
$env:MINIO_ACCESS_KEY = "minioadmin"
$env:MINIO_SECRET_KEY = "minioadmin"

```

PostgreSQL 初始化：
```sql
CREATE DATABASE pdf_warehouse;
\c pdf_warehouse
CREATE EXTENSION vector;
```

## 项目结构

| 文件 | 职责 |
|------|------|
| `config.py` | 全局配置（环境变量） |
| `models.py` | 数据模型（Pydantic） |
| `pdf_parser.py` | cloudmineru API 封装 |
| `classifier.py` | 三级表格分类（规则+LLM+人工） |
| `storage_adapter.py` | PG+pgvector+JSONB + MinIO 统一存储 |
| `text_pipeline.py` | 文本分块 → Embedding → pgvector |
| `image_pipeline.py` | 图片提取 → 过滤 → MinIO |
| `table_pipeline.py` | 表格解析 → 关系表 / JSONB |
| `table_catalog.py` | 表格标题目录 → 安全搜索与数据展示 |
| `metadata_service.py` | 元数据/溯源 |
| `policy/` | 制度领域包：条款结构化、跨制度检索、索引运行、流程分流、实体关系抽取、人工审核与质量抽检 |
| `pipeline.py` | 主流程编排 |
| `api.py` | FastAPI REST 接口 |
| `main.py` | 启动入口 |

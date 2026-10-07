# PDF 多模态提取入仓

## 外部 Agent 的 MCP 接入

新增 `policy_mcp` 服务，通过现有 HTTP API 提供条款检索、制度目录、全文读取和带引用问答。默认使用 Streamable HTTP，地址为 `http://127.0.0.1:8001/mcp`，HTTP 模式要求配置 `POLICY_MCP_TOKEN`。业务服务和 MCP 服务分别启动，详细配置及客户端示例见 [制度 MCP 使用说明](policy_mcp/README.md)。

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

### 清理目录内已入库文件并重新解析

批次状态 `running` 表示存在运行中的文件；`retry_wait` 表示尚有文件等待重试，当前命令可能已经返回。等待重试的文件不会在后台自动执行，使用 `python main.py --resume-batch BATCH_ID` 续跑；`--batch-status BATCH_ID` 会列出未完成文件及原因。遇到其他任务占用时会先等待，进程中断遗留的文件状态默认在 30 分钟保护期后可由续跑接管。

批处理按文件内容哈希复用已完成结果，复制文件或改名不会触发重跑。需要重新解析时，先预览再重置：

```powershell
# 预览目录及子目录中匹配的入库文件、共享文件和废止关系影响范围
python main.py --reset-folder "D:\policies\sample"

# 确认清理；若其他目录的历史批次也复用了同一文件，显式允许共享结果同步失效
python main.py --reset-folder "D:\policies\sample" --confirm-reset-folder --allow-shared-reset

# 创建新批次重新解析
python main.py --process-dir "D:\policies\sample"
```

如果重置被旧的 `processing/running` 占用阻止，先核实原处理进程已退出，再追加 `--release-stale-reset`。此参数只在确认重置时释放超过批处理保护期（默认 30 分钟）的旧占用，包括文件处理及条款分类、抽取、V2 知识抽取和索引任务。条款任务会锁定并检查整个共享运行的启动、结束和心跳时间；任一近期活动或无法核实时间的活动任务都会阻止释放。遗留运行标为失败，清理失败时释放状态与数据库清理一起回滚。预览不修改状态。目录扫描及历史批次匹配均忽略 Office `~$` 临时锁文件。

如果旧代码抽取不准，需要清除制度元数据后重新入库，追加 `--purge-imported-data`：除常规结果、真实条款问答索引和断言索引外，还清除标题、文号、日期、效力、归族与当前抽取指针，删除所有指向或源自目标制度的废止关系、归族候选，以及本次涉及且无剩余任务的运行。删除废止关系可能改变其他制度的效力，依据剩余关系重算。跨制度共享运行仍有任务时保留；源文件、MinIO 原件、文件/制度稳定 ID 占位和历史 PDF 批次、问答历史、已导出文件不删除。此模式清理入库抽取数据，不是删除历史备份。

```bash
python main.py --reset-folder "D:\policies\sample" --purge-imported-data
python main.py --reset-folder "D:\policies\sample" --confirm-reset-folder --allow-shared-reset --release-stale-reset --purge-imported-data
```

`--purge-imported-data` 已包含强制重置，在 Ctrl+C 后也不受旧任务状态或时间限制，无需另外添加参数：

```bash
python main.py --reset-folder "D:\policies\sample" --confirm-reset-folder --allow-shared-reset --purge-imported-data
```

彻底清理忽略文件处理、结构化及条款任务的活动状态，包括近期心跳和无法核实的时间；将关联 PDF 批任务释放为待处理，将关联共享条款运行及其活动任务标为失败，再清理目标文件产物。共享运行中目录外的原始条款不删除，但运行会中止。仍保留共享文件授权、目录范围、读取上限及事务回滚检查；不加 `--confirm-reset-folder` 仍仅预览。此操作不终止后台进程，执行前需停止旧处理进程，避免清理后继续写回。与 `--release-stale-reset` 同用时以强制模式为准。原有 `--force-reset-folder` 保留兼容，彻底清理不需要它。

重置会清理条款、向量、表格、分类、知识抽取及相关审核产物，保留源文件、MinIO 对象、文件和制度主记录，文件状态改为待处理。原批次保留为历史记录，不会自动重跑；已有导出的 JSON 文件也不会自动删除。

重置废止来源制度 A 时，会清理 A 发起的废止关系（含人工审核结论）及引用这些关系的归族候选。若目标制度 B 仍有其他已批准废止关系，则保留失效状态并按剩余关系更新日期；否则仅清除与删除关系相符的失效日期，没有其他失效日期或待处理冲突时恢复为默认有效。独立的人工失效日期继续保留，已确认的制度族归属保留。

重置被废止制度 B 时，保留其他制度发起的入向废止关系及 B 的效力状态。其他 PDF 的条款和向量不会因效力重算被删除。

单文件 `--reset-file FILE_ID` 使用常规关联清理和效力重算逻辑。近期 PDF 批任务占用、制度结构化以及近期或无法核实过期的条款任务禁止重置。目录内任一文件清理失败会回滚整个目录的数据库变更；`--allow-shared-reset`、`--release-stale-reset` 和 `--purge-imported-data` 仅允许与 `--reset-folder` 配合使用。

### 制度图谱三步上手

如果你的目标是从制度 PDF 中抽取实体和关系，先走下面三步即可；不要先使用流程分流、条款索引或质量报告等进阶命令。

```powershell
# 1. 批量导入 PDF/DOC/DOCX；命令结束后记下输出的 batch_id
python main.py --process-dir "D:\policies"

# 2. 用 batch_id 抽取制度条款中的实体和关系；首次建议先验证 10 条
python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx --limit 10

# 3. 用 policy_run_id 审核候选，并按需导出实体关系抽检表
python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 张三
python main.py --export-policy-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"
```

第 1 步完成后，输入目录会自动生成 `batch_xxx_条款重组结果.json`；其中保留每份制度的条款顺序、父条款 ID、章节路径、页码和原文，便于直接核查。

一份印发通知包含多个《制度名称》，且正文中能确认各制度独立标题及章节起点时，会自动拆成多份制度记录，共享源文件和通知文号。条款层级、效力识别、检索索引及废止关系分别归属各制度；单文件/批次导出和文件重置覆盖全部制度。第一份制度沿用原制度 ID，其余按源文件 ID 和制度标题生成 ID，重复处理相同文件不会新增重复制度。无法确认完整正文边界时保留单制度处理，不按引用书名猜测附件。

已确认多份制度时，提取的源文件 metadata 通过 `policies` 数组返回各制度的 `title`、通知文号和来源页码，通知名称保存为 `notice_title`，源文件层不再用通知名充当制度 `title`。单制度文件仍使用原来的 `title` 字段。

问答服务启动时会预热本地向量模型（含一次编码）和 BM25 索引，预热结束后开始接受请求，避免首个问题承担初始化耗时。预热不调用 DeepSeek，不消耗问答接口 token；某阶段失败会记录警告，后续检索仍可重试。入库、重置后的索引刷新和缓存失效逻辑保持有效；每个服务进程分别预热，启动耗时会相应增加。

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

### 2. 命令行处理文档

```powershell
python main.py --process "C:\path\to\your.pdf"
```

目录批处理和上传接口支持 `.pdf`、`.doc`、`.docx`。真正的 Word 文件直接交给 CloudMinerU，不进行本地 PDF 转换；使用 Word 文件时解析后端必须为 `cloudmineru`。部分学校网站下载的 `.doc` 实际是 MIME 封装的 HTML 网页归档，系统会按文件内容识别并在本地提取正文，无需 CloudMinerU；此类文件的章、条、项按提取后的段落解析。

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

未配置 LLM 时使用规则抽取；配置 LLM 后只采用模型候选。Qwen 等 LLM 候选默认进入人工审核。审核时输入 `a` 通过、`r` 拒绝、`c` 修正、`n` 补充、`s` 跳过或 `q` 结束。原始模型候选不会被覆盖，人工结论会写入 `policy_manual_annotations`，并保存原文证据的字符起止位置，后续可转换为 BERT 训练标注。制度默认为 `current`（默认有效），不补造缺失的生效日期。确认废止后标为 `invalid`；未解决的日期或效力冲突保留 `unknown`。未来生效或废止仍按查询日期判断；已确认废止但日期未知时，当前问答排除，历史查询保留待核实。

**制度废止关系审核：**

使用 `--process` 或 `--process-dir` 解析制度 PDF 时，条款结构化完成后会自动检查明确的废止措辞。只有识别到候选才会显示来源制度、目标制度、日期、页码和证据，并提示输入 `y` 插入、`n` 跳过或 `q` 结束本次命令的后续询问。插入的候选状态仍为 `pending`，不会自动改变旧制度状态。

如果被废止制度晚于来源制度入库，系统会在目标制度结构化后反向匹配历史未解析候选，并询问是否批准；确认后会回填 `target_policy_id`，同时把目标制度标记为 `invalid` 并写入已识别的失效日期。文号精确唯一匹配优先，标题规范化唯一匹配作为后备；歧义关系不会自动绑定。API 和批次恢复不会等待终端输入。

也可以单独为已有结构化批次补录制度级候选；该命令保持自动插入全部候选，不调用大模型：

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

## 文档级版本治理与时间检索

PDF 条款结构化完成后，系统会从整份文本识别明确生效日期；已有生效日期不会被自动结果覆盖。历史批次无需重新解析 PDF，可执行：

```powershell
python main.py --backfill-policy-governance batch_xxxxxxxxxxxxxxxx
python main.py --policy-governance-status batch_xxxxxxxxxxxxxxxx
python main.py --review-policy-families batch_xxxxxxxxxxxxxxxx --family-reviewer 张三
```

归族候选来自已批准的废止关系和规范标题匹配，但废止关系本身不等于版本继承，必须人工确认。条款不单独保存有效期，而是继承所属 `policy_documents` 的 `effective_date` 与 `expiry_date`。

普通检索保持原行为；只有提供 `as_of` 时才应用左闭右开的有效区间过滤。日期未知的制度不会被丢弃，但排在可证明适用的制度之后：

```http
GET /policy-search?q=差旅住宿标准&top_k=10
GET /policy-search?q=差旅住宿标准&top_k=10&as_of=2020-06-01
```

时间检索实时读取 PostgreSQL 中的文档治理字段，无需因日期或制度族调整而重建向量索引。同一制度族在指定日期存在多个适用版本时，接口保留全部结果并返回重叠警告。

### 基于制度证据的初步问答

跨制度检索先合并向量和 BM25 候选，再复用问答模型对融合排名靠前的候选进行语义评分，默认最多 30 条，通过 `POLICY_SEMANTIC_RERANK_CANDIDATES` 可调整为 1 至 100 条。评分发生在最终结果截断和证据名额分配之前，不针对特定问法扩展关键词。效力未知的相关条款仍可引用并解释原文，同时提示待核实；明确不适用的条款继续排除。

语义重排默认启用，可通过 `POLICY_SEMANTIC_RERANK_ENABLED=false` 关闭；`POLICY_SEMANTIC_RERANK_TIMEOUT_SECONDS` 默认 45 秒。缓存未命中时增加一次问答模型请求；模型不可用或评分不合法时保留混合召回排序并返回降级警告。只引用适用性待核实条款的回答不能给出确定结论。

重排评分使用有容量上限的进程内缓存：`POLICY_SEMANTIC_RERANK_CACHE_TTL_SECONDS` 默认 600 秒，`POLICY_SEMANTIC_RERANK_CACHE_MAX_ENTRIES` 默认 256，任一设为 0 可禁用缓存。相同问题、日期、模型及有序候选内容复用评分，并合并相同的并发评分请求。缓存只保存评分，不保存回答或制度效力；条款、标题或结构版本变化会产生新缓存键，本进程入库更新、索引重建和重置也会清空缓存。重启后缓存为空，多进程各自缓存，其他进程的数据变更依靠候选内容变化识别。命中缓存仍会召回候选并实时判断效力，最终回答生成仍调用问答模型。

`POST /policy-answer` 会从最多三份制度中选择最多八条相关条款，再使用独立的 `ANSWER_LLM_API_URL`、`ANSWER_LLM_API_KEY` 和 `ANSWER_LLM_MODEL` 配置生成带引用的初步回答。默认接入 DeepSeek 官方 API，不会改变制度抽取和表格分类继续使用的本地 Qwen：

启动 API 服务后，可直接打开 `http://127.0.0.1:8000/policy-qa`。页面提供制度问答、条款检索、制度库与完整结构化条款、服务端历史会话和回答反馈。历史会话依赖当前浏览器的 HttpOnly Cookie；清除 Cookie 后不能恢复旧会话。制度效力为 `unknown` 时，页面显示“待核实”。

面向页面的新接口包括 `GET /api/policies`（`q`、`status`、`limit`、`offset`）、`GET /api/policies/{policy_id}`、`POST/GET /api/conversations`、`GET/PATCH/DELETE /api/conversations/{conversation_id}`、`POST /api/conversations/{conversation_id}/messages` 和 `POST /api/messages/{message_id}/feedback`。问答消息接口使用 JSON 请求 `{ "question": "...", "as_of": "YYYY-MM-DD" }`，反馈使用 `{ "rating": "helpful" }` 或 `unhelpful`。完整设计取舍见 [前端实施说明](docs/policy-frontend-implementation.md)。

```http
POST /policy-answer
Content-Type: application/json

{
  "question": "差旅住宿标准是否适用于校外专家？",
  "as_of": "2026-09-16"
}
```

未提供 `as_of` 时，系统只自动识别问题中的完整日期；模糊历史时间会要求补充具体日期，完全没有时间表达则按请求当天检索。模型只能引用服务端提供的证据编号，响应中的制度原文、条款编号和页码由服务端回填。

该接口提供的是基于现有制度库的初步判断，不是正式审批或最终合规裁决。明确的符合、不符合和有条件符合结论都会标记 `requires_human_review=true`。没有明确适用证据、模型未配置、模型超时或输出无法校验时，接口返回 `undetermined` 和已检索原文，不会编造结论。

## 条款义务、许可、禁止分类

制度结构化完成后，可单独对正文非空的条、款、项执行多标签分类。明确的“应当、必须、可以、不得、严禁”等表述优先使用规则；规则无法确定时调用现有 `LLM_*` 配置的本地模型。模型不可用时任务会保留为失败状态，不会误标为“其他”。

```powershell
python main.py --classify-policy-clauses batch_xxxxxxxxxxxxxxxx
python main.py --policy-clause-classification-status classification_run_xxxxxxxxxxxxxxxx
python main.py --resume-policy-clause-classification classification_run_xxxxxxxxxxxxxxxx

python main.py --export-policy-classification-review classification_run_xxxxxxxxxxxxxxxx --quality-output "D:\policy_quality"
python main.py --import-policy-classification-review "D:\policy_quality\条款分类复核.csv" --classification-reviewer 张三
```

一条条款可同时属于义务、许可和禁止；“其他”不能与这三类同时出现。复核表最多导出 50 条，人工填写“是否正确”，错误时在“修正标签”中用顿号填写，例如 `义务、禁止`。导入后会生成 `条款分类质量报告.json`，统计多标签完全一致率以及三类标签各自的精确率和召回率。人工标签与系统原始结果分开保存，后续使用时人工结论优先。

**制度知识抽取 V2（推荐试运行）：**

V2 不再把条款级 `process/non_process` 作为抽取入口，而是从全部实质性条款中抽取带连续原文证据的制度断言，再从明确的章、节或父条款结构中聚合办事事项并派生流程视图。旧实体关系和流程图谱链路继续保留，便于同批数据对比。

```powershell
# 创建并执行 V2 抽取，输出 knowledge_v2_run_id
python main.py --extract-policy-knowledge-v2 batch_xxxxxxxxxxxxxxxx

# 查看状态或恢复失败条款
python main.py --policy-knowledge-v2-status knowledge_v2_run_xxxxxxxxxxxxxxxx
python main.py --resume-policy-knowledge-v2 knowledge_v2_run_xxxxxxxxxxxxxxxx

# 导出、回填并导入断言抽样复核表
python main.py --export-policy-knowledge-v2-review knowledge_v2_run_xxxxxxxxxxxxxxxx --knowledge-v2-output "D:\policy_quality\V2断言复核.csv"
python main.py --import-policy-knowledge-v2-review "D:\policy_quality\V2断言复核.csv" --knowledge-v2-reviewer 张三

# 导出事项与流程视图
python main.py --export-policy-knowledge-v2-view knowledge_v2_run_xxxxxxxxxxxxxxxx --knowledge-v2-output "D:\policy_quality\V2事项流程.json"
```

V2 默认将同一制度的 4 条目标条款合并为一次模型请求，并在批量响应失败时自动回退为逐条抽取。可在 `.env` 中通过 `KNOWLEDGE_V2_BATCH_SIZE=4` 调整批大小；本地 7B 模型建议使用 2～6，过大会增加 JSON 截断概率。`KNOWLEDGE_V2_MAX_TOKENS=2400` 控制每次批量响应上限。`KNOWLEDGE_V2_CONTEXT_ANCESTOR_DEPTH=1` 控制提供给模型的上级条款层数：0 不提供，1 仅直接父条款，2 再包含祖父条款；层数越高，提示词越长。新运行创建时会固定该值，修改配置不会影响已有运行的断点续跑。运行期间会持续输出完成数、平均耗时和预计剩余时间。

对“学校公文种类包括：”下的“（一）决议。适用于……”等明确列表，V2 逐项生成用途断言，并保留父条款中的“学校公文种类”及其条款 ID；复核 CSV 会显示“所属类别”和“类别来源条款ID”。这类列表项不调用模型，避免把用途说明误当成办事动作。导出的事项流程 JSON 另含 `document_type_graph`：同一父条款的“学校公文种类 → 包含 → [决议、决定、请示……]”合并为一条关系，每个成员附原文条款证据；“请示 → 适用于 → 向上级单位请求指示、批准”等用途关系仍逐项保留。该汇聚视图从已保存的断言派生，旧运行无需重新抽取即可重新导出。

模型结果只有通过类型、端点、证据条款、连续原文及字符位置校验后才会以 `machine_extracted` 状态参与检索增强；`rejected`、`invalid` 结果不会参与。问答仍以条款向量召回为主，并且最终引用只包含原始条款。V2 断言索引不存在或服务失败时会自动退回原有条款检索。

## 服务配置

设置以下环境变量连接真实服务：

```powershell
# cloudmineru API
$env:CLOUDMINERU_API_URL = "https://your-host/api/v1"
$env:CLOUDMINERU_API_KEY = "your-key"

# 制度抽取和表格分类 LLM（当前为本地 Qwen，OpenAI 兼容）
$env:LLM_API_URL = "https://api.openai.com/v1"
$env:LLM_API_KEY = "sk-xxx"
$env:LLM_MODEL = "gpt-4o"

# 制度问答 LLM（DeepSeek 官方 OpenAI 兼容接口）
$env:ANSWER_LLM_API_URL = "https://api.deepseek.com"
$env:ANSWER_LLM_API_KEY = "your-deepseek-key"
$env:ANSWER_LLM_MODEL = "deepseek-flash"
$env:ANSWER_LLM_THINKING = "false"

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

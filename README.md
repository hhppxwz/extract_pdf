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

**查询状态：**
```powershell
curl http://127.0.0.1:8000/status/{file_id}
```

**语义检索：**
```powershell
curl "http://127.0.0.1:8000/search?q=学生成绩&file_id={file_id}&top_k=5"
```

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
| `metadata_service.py` | 元数据/溯源 |
| `policy_pipeline.py` | 制度文档和条款层级结构化 |
| `policy_extractor.py` | 制度实体、关系和审核项抽取 |
| `policy_storage.py` | 制度对象和抽取运行持久化 |
| `pipeline.py` | 主流程编排 |
| `api.py` | FastAPI REST 接口 |
| `main.py` | 启动入口 |

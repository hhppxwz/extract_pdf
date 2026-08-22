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
- MinIO（mock 模式下不需要）

## 快速开始

### 1. 安装依赖

```powershell
cd D:\mycode\pycharm_project\extract_pdf
.\.venv\Scripts\activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 命令行处理 PDF（mock 模式，无需后端）

```powershell
$env:MOCK_MODE = "true"
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
$env:MOCK_MODE = "true"
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

## 真实模式配置

设置环境变量后关闭 mock 模式：

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

# 关闭 mock 模式
$env:MOCK_MODE = "false"
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
| `pipeline.py` | 主流程编排 |
| `api.py` | FastAPI REST 接口 |
| `main.py` | 启动入口 |

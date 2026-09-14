# 制度模块目录重构设计

## 目标

将当前散落在项目根目录的制度处理代码收拢到 `policy/` 包，使“通用 PDF 导入”和“制度知识服务”在目录层面清晰分离，同时保持现有 HTTP 接口、命令行参数、数据库表名和处理行为不变。

重构前已创建源码快照：

`D:\mycode\pycharm_project\extract_pdf\.worktrees\policy-quality-evaluation-before-refactor-20260907-161113`

快照包含当前未提交实现和测试代码，不含 Git 元数据及 `.env` 环境密钥文件。

## 范围

本次只移动以下制度模块：

```text
policy/
├── __init__.py
├── pipeline.py          # 制度识别后的条款层级结构化
├── retrieval.py         # 跨制度条款索引、检索和结果组装
├── storage.py           # 制度、条款、索引、流程和图谱运行持久化
├── process.py           # 流程条款分流规则
├── process_runner.py    # 流程分流运行和恢复
├── extraction.py        # 实体关系抽取运行
├── reviewer.py          # 人工审核交互
└── quality.py           # 条款/图谱抽检导出和质量报告
```

保持在根目录的模块：

- `main.py`：CLI 和 FastAPI 启动入口；
- `api.py`：HTTP 路由；
- `pipeline.py`：通用 PDF 导入编排；
- `config.py`、`models.py`、`storage_adapter.py`：共享基础设施；
- `text_pipeline.py`、`image_pipeline.py`、`table_pipeline.py`：通用内容处理；
- `parsers/`、`metadata/`：已有的合理包结构。

不移动测试目录；测试仍位于 `tests/`，仅更新导入路径。

## 迁移边界

- 目录重构不改动 `GET /policy-search`、旧 `/search`、上传接口或任意 CLI 参数；
- 数据库表名和字段保持原样，包括 `policy_clause_search`、制度条款、流程及图谱表；
- 制度 PDF 导入后的“结构化成功即同步条款索引”行为保持原样；
- 不删除未来图谱基础，也不改动通用 PDF、图片或表格的业务行为；
- 原根目录的 `policy_*.py` 文件在迁移后不保留重复实现，项目内部全部改用 `policy.*` 导入；
- 本次不提交、不合并、不推送，等待用户自行测试。

## 导入关系

```text
main.py ───────────────┬── policy.retrieval
                       ├── policy.extraction
                       ├── policy.process_runner
                       ├── policy.reviewer
                       └── policy.quality

api.py ─────────────────── policy.retrieval
pipeline.py ────────────── policy.pipeline

policy.pipeline ────────── policy.storage / policy.retrieval
policy.retrieval ───────── policy.storage
policy.extraction ──────── policy.storage
policy.process_runner ──── policy.process / policy.storage
policy.quality ─────────── policy.storage
policy.reviewer ────────── policy.storage
```

共享的 `models`、`config`、`storage_adapter`、`batch_processor` 与 `metadata_service` 保持根目录导入，避免为本次整理引入额外层级。

## 验证

1. 新增包导入回归测试，确保 `policy.pipeline`、`policy.retrieval` 等模块可被独立导入；
2. 更新现有制度流程、质量、检索测试的导入路径；
3. 运行完整 `unittest` 测试套件；
4. 运行 `python -B main.py --help`，确认 API/CLI 入口没有变化；
5. 运行 `git diff --check`，确认无空白错误；
6. 保持快照与重构工作区均未提交。

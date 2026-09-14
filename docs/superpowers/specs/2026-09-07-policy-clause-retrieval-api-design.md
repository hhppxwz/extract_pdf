# 跨制度条款级检索 API 设计

## 目标

在远程 `origin/main` 的 `22b9efa` 基线上，为已完成结构化的学校规章制度提供跨制度条款级检索。用户输入办事问题后，系统返回可直接回查的制度条款，而不是 PDF 文本块或生成式答案。

单条结果必须包含：制度名称、文件名、条款号、章节路径、起止页码和原文。首期不回答“应该怎么办”，不生成办事流程，不做实体关系图谱推理。

## 非目标

- 不替换现有 `/search`；该接口仍服务于指定 `file_id` 的通用文本块检索。
- 不调用大模型改写条款、总结答案或推断不存在的规定。
- 不建立流程图谱、跨制度规则冲突裁决、制度效力自动判定或前端页面。
- 不删除旧条款、旧向量或现有流程图谱试验代码。

## 检索单位

检索单位是当前结构版本的实质性 `policy_clauses`，而非 CloudMinerU 原始块：

- 可索引条款：`article`、`paragraph`、`item`，原文非空；标题节点 `book`、`chapter`、`section` 不进入检索结果。
- 每条索引文本为 `章节路径 + 条款号 + 原文`，从而让“差旅”“报销”等问题同时匹配章节和条款正文。
- 结果原文始终取 `raw_text`；页码取 `page_start/page_end`；条款号优先 `item_no`、其次 `paragraph_no`、最后 `article_no`。
- 对相同条款 ID 的旧索引按当前结构版本替换；源条款和历史结构版本仍保留。

## 数据与索引

新增全局 pgvector 索引表 `policy_clause_search`。每条向量 metadata 至少保存：

```json
{
  "clause_id": "...",
  "policy_id": "...",
  "file_id": "...",
  "file_name": "...",
  "title": "...",
  "level": "paragraph",
  "article_no": "第十二条",
  "paragraph_no": "第一款",
  "item_no": "（一）",
  "chapter_path": ["第三章 报销管理"],
  "page_start": 8,
  "page_end": 8,
  "raw_text": "...",
  "search_text": "...",
  "structure_version": "..."
}
```

索引模块负责按单份制度删除旧向量、嵌入当前可索引条款、写入新 metadata。条款结构化成功后尝试增量同步索引；索引失败不能将 PDF 结构化标为失败，但必须记录清晰错误，后续可通过重建命令恢复。

已有 574 份制度通过批次重建命令建立索引。该命令按制度记录成功、失败和跳过数，允许恢复失败制度，不覆盖源条款。

## 排序策略

首期采用不增加第三方依赖的混合排序：

1. 以查询向量从全局条款索引召回候选；
2. 根据查询中的连续关键词是否出现在条款 `search_text` 中增加确定性分数；
3. 按混合分数排序，去除同一 `clause_id` 的重复候选；
4. 返回前 `top_k`，默认 10，范围 1–20。

语义相似度用于识别“报账”和“费用报销”等表达差异；关键词加分用于保证制度术语、金额、材料名称等精确命中不会被稀释。API 不设隐藏阈值：即使分数低也返回实际候选与分数，便于人工发现索引或检索质量问题。

## API

```text
GET /policy-search?q=出差报销需要什么材料&top_k=5
```

成功响应：

```json
{
  "query": "出差报销需要什么材料",
  "result_count": 1,
  "results": [
    {
      "rank": 1,
      "score": 0.91,
      "semantic_score": 0.88,
      "keyword_score": 0.03,
      "policy": {
        "policy_id": "...",
        "file_id": "...",
        "title": "差旅费管理办法",
        "file_name": "差旅费管理办法.pdf"
      },
      "clause": {
        "clause_id": "...",
        "clause_no": "第十二条",
        "chapter_path": ["第三章 报销管理"],
        "page_start": 8,
        "page_end": 8,
        "raw_text": "出差人员报销差旅费应提交……"
      }
    }
  ]
}
```

空问题返回 422；索引不存在或没有任何当前制度条款时返回中文 409；向量模型或存储故障返回中文 503。所有结果保留原文，不输出模型生成答案。

## 命令行

```powershell
# 为一个已完成的制度批次建立或重建条款索引
python main.py --rebuild-policy-clause-index batch_xxx

# 只恢复上次重建失败的制度
python main.py --resume-policy-clause-index policy_index_run_xxx

# 查看重建进度和失败原因
python main.py --policy-clause-index-status policy_index_run_xxx
```

## 验收与测试

- 单元测试验证：可索引条款筛选、索引 metadata、关键词加分、去重、条款号格式化和 API JSON 结构。
- 集成测试使用独立的真实 PostgreSQL/pgvector 测试库和真实嵌入模型：索引重建只替换目标制度的旧向量，不删除其他制度。
- API 测试连接真实测试库，验证跨制度返回、页码和原文完整、空索引与非法参数的中文错误。
- 人工验收：从报销、差旅、采购中准备至少 20 个真实问题，每题记录目标条款 ID；分别统计正确条款是否进入 Top-3 和 Top-5。未达到目标前，不扩大到流程图谱开发。

## 实施约束

- 不新增外部检索服务；复用 PostgreSQL pgvector 和现有嵌入模型。
- 正式 API、索引重建、集成测试和人工验收均不使用模拟条款、模拟向量或模拟检索结果；测试库与生产库隔离。
- 代码注释、错误信息和文档使用中文。
- 不触碰主目录 `only_policy` 中的用户 PDF 变更。
- 所有本次改动保持未暂存、未提交、未合并、未推送，等待用户测试决定。

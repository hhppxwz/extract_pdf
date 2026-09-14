"""
FastAPI REST 接口
提供 PDF 上传、状态查询、检索等功能。
"""
import os
import tempfile
import json
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from models import ProcessingStatus
from pipeline import process_pdf
from metadata_service import get_file_status, get_file_blocks, record_file_start
from storage_adapter import storage
from table_catalog import search_extracted_tables, query_catalogued_table_data

app = FastAPI(
    title="PDF 多模态提取入仓服务",
    description="基于 cloudmineru + PostgreSQL(pgvector+JSONB) + MinIO 的 PDF 多模态提取 Pipeline",
    version="1.0.0",
)

# 默认允许开发环境跨域；生产环境可用 CORS_ALLOW_ORIGINS 设置逗号分隔的域名白名单。
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _process_uploaded_pdf(tmp_path: str, source_file_name: str) -> None:
    """在响应上传请求后执行完整处理，并在结束时清理临时文件。"""
    try:
        process_pdf(tmp_path, source_file_name=source_file_name)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/upload")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """上传一个 PDF，立即返回任务 ID，并在后台完成提取。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    # 保存上传文件到临时目录
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        file_id = record_file_start(file.filename, content)
    except Exception:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    background_tasks.add_task(_process_uploaded_pdf, tmp_path, file.filename)
    return JSONResponse(
        status_code=202,
        content={
            "file_id": file_id,
            "status": "pending",
            "status_url": f"/status/{file_id}",
        },
    )


@app.get("/status/{file_id}")
async def get_status(file_id: str):
    """查询某个 PDF 文件的处理状态与结果汇总"""
    status = get_file_status(file_id)
    if status is None:
        raise HTTPException(status_code=404, detail="file_id 不存在")

    blocks = get_file_blocks(file_id)
    return JSONResponse(content={
        "file": status,
        "blocks_trace": blocks,
    })


@app.get("/search")
async def search_text(
    q: str = Query(..., description="搜索查询文本"),
    file_id: Optional[str] = Query(None, description="限定文件 ID"),
    top_k: int = Query(10, ge=1, le=50),
):
    """
    向量语义检索：使用查询文本的 Embedding 在 pgvector 中搜索最相似的文本块。
    需要 Embedding 模型可用。
    """
    from text_pipeline import _get_embedding_model

    model = _get_embedding_model()
    # 生成查询向量
    query_vector = model.encode([q], normalize_embeddings=True)[0].tolist()

    # 搜索
    if file_id:
        index_name = f"pdf_{file_id}_text"
        results = storage.vector.search(index_name, query_vector, top_k)
    else:
        # 跨文件搜索暂不支持
        results = []

    return JSONResponse(content={"results": results, "query": q})


@app.get("/policy-search")
async def policy_search(
    q: str = Query(..., min_length=1, description="办事问题或制度条款查询"),
    top_k: int = Query(10, ge=1, le=20, description="返回条款数，范围 1 到 20"),
):
    """跨制度检索可回查条款，始终返回原文证据而非生成式答案。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="检索问题不能为空")

    from policy.retrieval import (
        PolicyClauseIndexNotReadyError,
        PolicyClauseRetrievalServiceError,
        build_policy_search_response,
        search_indexed_policy_clauses,
    )

    try:
        candidates = search_indexed_policy_clauses(query, top_k)
    except PolicyClauseIndexNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PolicyClauseRetrievalServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(content=build_policy_search_response(query, candidates))


@app.get("/policy-workflow-graph/{run_id}")
async def get_policy_workflow_graph(run_id: str):
    """返回指定流程抽取运行的已审核办事流程图谱及其条款证据。"""
    from policy.workflow_graph import build_policy_workflow_graph

    try:
        return JSONResponse(content=build_policy_workflow_graph(run_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/file/{file_id}/tables")
async def list_tables(file_id: str):
    """列出某个 PDF 文件的所有已提取表格（含存储位置）"""
    blocks = get_file_blocks(file_id)
    tables = [
        b for b in blocks
        if b.get("block_type") == "table"
        and b.get("storage_target") != "pending_review"
    ]
    return JSONResponse(content={"tables": tables})


@app.get("/tables/search")
async def search_tables(
    q: str = Query(..., min_length=1, description="表格编号、表名或原始 PDF 文件名"),
    file_id: Optional[str] = Query(None, description="限定文件 ID"),
    limit: int = Query(10, ge=1, le=50, description="返回表格数量"),
    row_limit: int = Query(100, ge=1, le=500, description="每张表返回的最大行数"),
):
    """搜索表格目录，并返回前端可直接展示的列定义与数据行。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="搜索关键词不能为空")
    try:
        results = search_extracted_tables(
            query=query,
            file_id=file_id,
            limit=limit,
            row_limit=row_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(content={"query": query, "results": results})


@app.get("/tables/{file_id}/{block_id}/data")
async def get_table_data(
    file_id: str,
    block_id: str,
    filters: Optional[str] = Query(
        None,
        description='JSON 条件数组，例如 [{"column":"项目","operator":"contains","value":"Rcpy"}]',
    ),
    limit: int = Query(100, ge=1, le=500, description="每页最大行数"),
    offset: int = Query(0, ge=0, le=10000, description="从第几行开始返回"),
):
    """单独返回一张表格的列定义和分页行数据，不混入文本检索结果。"""
    try:
        parsed_filters = [] if filters is None else json.loads(filters)
        if not isinstance(parsed_filters, list):
            raise ValueError("filters 必须是 JSON 数组")
        result = query_catalogued_table_data(
            file_id=file_id,
            block_id=block_id,
            filters=parsed_filters,
            limit=limit,
            offset=offset,
        )
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="filters 必须是合法 JSON") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="表格不存在或尚未入库")
    return JSONResponse(content=result)


@app.get("/file/{file_id}/text")
async def get_text_chunks(
    file_id: str,
    limit: int = Query(50, ge=1, le=200),
):
    """获取某个 PDF 文件中已存储的文本块（前 N 条）"""
    blocks = get_file_blocks(file_id)
    text_blocks = [
        b for b in blocks if b.get("block_type") == "text"
    ]
    return JSONResponse(content={"text_blocks": text_blocks[:limit]})


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}

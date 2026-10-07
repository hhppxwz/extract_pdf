"""
FastAPI REST 接口
提供 PDF 上传、状态查询、检索等功能。
"""
import os
import re
import tempfile
import json
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from models import ProcessingStatus
from config import app_config
from pipeline import process_pdf
from metadata_service import get_file_status, get_file_blocks, record_file_start
from storage_adapter import storage
from table_catalog import search_extracted_tables, query_catalogued_table_data

@asynccontextmanager
async def lifespan(application: FastAPI):
    from policy.warmup import warmup_policy_qa
    # 等待预热结束后接受请求，避免第一个用户承担模型及索引初始化耗时。
    application.state.policy_qa_warmup = await run_in_threadpool(warmup_policy_qa)
    yield


app = FastAPI(
    title="PDF 多模态提取入仓服务",
    description="基于 cloudmineru + PostgreSQL(pgvector+JSONB) + MinIO 的 PDF 多模态提取 Pipeline",
    version="1.0.0",
    lifespan=lifespan,
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
    """上传 PDF 或 Word，立即返回任务 ID，并在后台完成提取。"""
    from pathlib import Path

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".doc", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 PDF、DOC 和 DOCX 文件")
    if suffix in {".doc", ".docx"} and app_config.parser.parser_backend != "cloudmineru":
        raise HTTPException(status_code=400, detail="DOC/DOCX 仅支持 cloudmineru 解析后端")

    # 保存上传文件到临时目录
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
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
    as_of: Optional[str] = Query(None, description="可选适用日期，格式 YYYY-MM-DD"),
):
    """跨制度检索可回查条款，始终返回原文证据而非生成式答案。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="检索问题不能为空")
    if not isinstance(as_of, str):
        as_of = None
    if as_of is not None:
        from datetime import date
        try:
            as_of = date.fromisoformat(as_of).isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="as_of 必须是 YYYY-MM-DD") from exc

    from policy.retrieval import (
        PolicyClauseIndexNotReadyError,
        PolicyClauseRetrievalServiceError,
        build_policy_search_response,
        search_indexed_policy_clauses,
    )

    try:
        candidates = search_indexed_policy_clauses(query, top_k, as_of=as_of)
    except PolicyClauseIndexNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PolicyClauseRetrievalServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    warnings = list(candidates[0].get("temporal_warnings") or []) if candidates else []
    return JSONResponse(content=build_policy_search_response(query, candidates, as_of, warnings))


@app.post("/policy-answer")
async def policy_answer(
    question: str = Form(..., min_length=1, description="制度咨询或合规判断问题"),
    as_of: Optional[str] = Form(
        None,
        description="可选适用日期，格式 YYYY-MM-DD",
        json_schema_extra={"format": "date"},
    ),
):
    """接收 Swagger 表单，并基于多份制度条款证据生成带原文引用的初步回答。"""
    from policy.answering import answer_policy_question
    from policy.retrieval import PolicyClauseIndexNotReadyError, PolicyClauseRetrievalServiceError

    try:
        if as_of and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) is None:
            raise ValueError("as_of 必须是有效的 YYYY-MM-DD 日期")
        normalized_as_of = date.fromisoformat(as_of).isoformat() if as_of else None
        result = answer_policy_question(question, normalized_as_of)
    except PolicyClauseIndexNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PolicyClauseRetrievalServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(content=result)


@app.get("/policy-qa", response_class=HTMLResponse)
async def policy_qa_page():
    """提供无需前端构建工具的制度问答页面。"""
    from policy.answer_ui import render_policy_qa_page
    return HTMLResponse(render_policy_qa_page())


@app.get("/", include_in_schema=False)
async def policy_home():
    """将首页引导至制度问答页面。"""
    return RedirectResponse("/policy-qa")


@app.get("/api/policies")
async def list_policy_catalog(
    q: str = Query("", max_length=200),
    status: str = Query(""),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """列出已入库制度，支持关键词、效力状态和分页。"""
    if status not in {"", "current", "invalid", "unknown"}:
        raise HTTPException(status_code=422, detail="status 必须是 current、invalid 或 unknown")
    from policy.web_catalog import list_policies

    return list_policies(q, status, limit, offset)


@app.get("/api/policies/{policy_id}")
async def get_policy_catalog_detail(policy_id: str):
    """读取制度当前结构版本的条款全文。"""
    from policy.web_catalog import get_policy_detail

    result = get_policy_detail(policy_id)
    if result is None:
        raise HTTPException(status_code=404, detail="制度不存在")
    return result


class ConversationTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    as_of: Optional[date] = None


class MessageFeedback(BaseModel):
    rating: str


def _browser_token(request: Request) -> str:
    return request.cookies.get("policy_browser", "")


@app.post("/api/conversations", status_code=201)
async def create_policy_conversation(request: Request):
    """为当前匿名浏览器创建持久问答会话。"""
    from policy.web_conversations import create_conversation, new_browser_token

    token = _browser_token(request) or new_browser_token()
    response = JSONResponse(create_conversation(token), status_code=201)
    response.set_cookie(
        "policy_browser", token, httponly=True, secure=request.url.scheme == "https",
        samesite="lax", max_age=60 * 60 * 24 * 180,
    )
    return response


@app.get("/api/conversations")
async def list_policy_conversations(request: Request):
    from policy.web_conversations import list_conversations

    return {"items": list_conversations(_browser_token(request))}


@app.get("/api/conversations/{conversation_id}")
async def get_policy_conversation(request: Request, conversation_id: str):
    from policy.web_conversations import get_conversation

    result = get_conversation(_browser_token(request), conversation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return result


@app.patch("/api/conversations/{conversation_id}")
async def rename_policy_conversation(request: Request, conversation_id: str, payload: ConversationTitle):
    from policy.web_conversations import rename_conversation

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="会话名称不能为空")
    if not rename_conversation(_browser_token(request), conversation_id, title):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"conversation_id": conversation_id, "title": title}


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def delete_policy_conversation(request: Request, conversation_id: str):
    from policy.web_conversations import delete_conversation

    if not delete_conversation(_browser_token(request), conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")


@app.post("/api/conversations/{conversation_id}/messages", status_code=201)
async def ask_in_policy_conversation(
    request: Request, conversation_id: str, payload: ConversationQuestion,
):
    """生成回答并将问题、回答及服务端引用一起写入会话。"""
    from policy.answering import answer_policy_question
    from policy.retrieval import PolicyClauseIndexNotReadyError, PolicyClauseRetrievalServiceError
    from policy.web_conversations import get_conversation, save_message

    token = _browser_token(request)
    if get_conversation(token, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="问题不能为空")
    try:
        result = answer_policy_question(question, payload.as_of.isoformat() if payload.as_of else None)
    except PolicyClauseIndexNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PolicyClauseRetrievalServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    message = save_message(token, conversation_id, question, result)
    if message is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return message


@app.post("/api/messages/{message_id}/feedback")
async def feedback_policy_message(request: Request, message_id: str, payload: MessageFeedback):
    from policy.web_conversations import save_feedback

    if payload.rating not in {"helpful", "unhelpful"}:
        raise HTTPException(status_code=422, detail="rating 必须是 helpful 或 unhelpful")
    if not save_feedback(_browser_token(request), message_id, payload.rating):
        raise HTTPException(status_code=404, detail="消息不存在")
    return {"message_id": message_id, "rating": payload.rating}


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

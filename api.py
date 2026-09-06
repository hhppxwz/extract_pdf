"""
FastAPI REST 接口
提供 PDF 上传、状态查询、检索等功能。
"""
import os
import tempfile
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.responses import JSONResponse

from models import ProcessingStatus
from pipeline import process_pdf
from metadata_service import get_file_status, get_file_blocks
from storage_adapter import storage

app = FastAPI(
    title="PDF 多模态提取入仓服务",
    description="基于 cloudmineru + PostgreSQL(pgvector+JSONB) + MinIO 的 PDF 多模态提取 Pipeline",
    version="1.0.0",
)


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """上传一个 PDF 文件，触发完整提取 Pipeline，返回 file_id"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")

    # 保存上传文件到临时目录
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = process_pdf(tmp_path)
    finally:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return JSONResponse(content=result.model_dump())


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

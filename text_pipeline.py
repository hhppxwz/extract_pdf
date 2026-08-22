"""
文本处理管线：分块 → Embedding → 存入 pgvector 向量索引
"""
import uuid
from typing import Optional

from config import app_config
from models import ContentBlock, TextChunk, BlockType
from storage_adapter import storage
import logging
# 懒加载 Embedding 模型，避免未安装时阻塞导入
_embedding_model: Optional[object] = None



def _get_embedding_model():
    """懒加载 sentence-transformers 模型"""
    global _embedding_model
    if _embedding_model is None and not app_config.mock_mode:
        from sentence_transformers import SentenceTransformer
        try:
            # 先尝试仅本地加载
            _embedding_model=SentenceTransformer(
            app_config.embedding.model_name,
            device=app_config.embedding.device,
            local_files_only=True,
            )
            print(f"成功从本地加载模型: {app_config.embedding.model_name}")
            return _embedding_model
        except Exception as e:
            print(f"本地未找到模型 {app_config.embedding.model_name}，尝试在线下载...")
            _embedding_model = SentenceTransformer(
                app_config.embedding.model_name,
                device=app_config.embedding.device,
            )
            print(f"成功下载模型: {app_config.embedding.model_name}")
            return _embedding_model


def chunk_text(
    text: str, file_id: str, block_id: str, page_num: int = 0
) -> list[TextChunk]:
    """
    将文本按段落切分为固定大小的块。
    策略：优先按段落边界切分，单段过长则强制按 chunk_size 截断。
    """
    chunk_size = app_config.embedding.chunk_size
    chunk_overlap = app_config.embedding.chunk_overlap

    # 按段落切分（双换行或单换行）
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[TextChunk] = []
    current_chunk = ""
    chunk_index = 0

    for para in paragraphs:
        # 如果加入当前段落后不超过 chunk_size，合并
        if len(current_chunk) + len(para) + 1 <= chunk_size:
            current_chunk = (current_chunk + "\n" + para).strip() if current_chunk else para
        else:
            # 保存当前块
            if current_chunk:
                chunks.append(_make_chunk(
                    current_chunk, file_id, block_id, page_num, chunk_index
                ))
                chunk_index += 1
            # 新段落作为新块的开始（如果段落本身超长，强制截断）
            if len(para) > chunk_size:
                # 超长段落强制按 chunk_size 截断，带 overlap
                start = 0
                while start < len(para):
                    sub = para[start:start + chunk_size]
                    chunks.append(_make_chunk(sub, file_id, block_id, page_num, chunk_index))
                    chunk_index += 1
                    start += chunk_size - chunk_overlap
                current_chunk = ""
            else:
                current_chunk = para

    # 最后一个块
    if current_chunk:
        chunks.append(_make_chunk(current_chunk, file_id, block_id, page_num, chunk_index))

    return chunks


def _make_chunk(
    text: str, file_id: str, block_id: str, page_num: int, chunk_index: int
) -> TextChunk:
    """构造 TextChunk 对象"""
    return TextChunk(
        chunk_id=f"{block_id}_chunk_{chunk_index}",
        file_id=file_id,
        block_id=block_id,
        page_num=page_num,
        text=text,
        chunk_index=chunk_index,
        token_count=len(text),  # 中文字符数近似 token 数
    )


def embed_chunks(chunks: list[TextChunk]) -> list[TextChunk]:
    """
    对文本块批量生成 Embedding 向量。
    mock 模式下返回零向量占位。
    """
    if not chunks:
        return chunks

    if app_config.mock_mode:
        # mock 模式：返回 128 维零向量
        for chunk in chunks:
            chunk.embedding = [0.0] * 128
        return chunks

    model = _get_embedding_model()
    if model is None:
        raise RuntimeError("Embedding 模型未加载，请检查 sentence-transformers 安装")

    texts = [c.text for c in chunks]
    embeddings = model.encode(texts, normalize_embeddings=True).tolist()

    for chunk, emb in zip(chunks, embeddings):
        chunk.embedding = emb

    return chunks


def store_chunks(chunks: list[TextChunk]) -> int:
    """将文本块及其向量写入 pgvector"""
    if not chunks or not any(c.embedding for c in chunks):
        return 0

    dim = len(chunks[0].embedding) if chunks[0].embedding else 128
    index_name = f"pdf_{chunks[0].file_id}_text"

    # 确保向量索引存在
    storage.vector.create_index(index_name, dim)

    vectors = [c.embedding for c in chunks]
    metadata = [
        {
            "file_id": c.file_id,
            "block_id": c.block_id,
            "chunk_id": c.chunk_id,
            "page_num": c.page_num,
            "text": c.text,
        }
        for c in chunks
    ]

    return storage.vector.insert_vectors(index_name, vectors, metadata)


def process_text_blocks(
    blocks: list[ContentBlock], file_id: str
) -> tuple[int, list[TextChunk]]:
    """
    文本块处理主入口：筛选文本块 → 分块 → Embedding → 存储
    返回 (存入的块数, TextChunk列表)
    """
    text_blocks = [b for b in blocks if b.type == BlockType.TEXT and b.content.strip()]
    if not text_blocks:
        return 0, []

    all_chunks: list[TextChunk] = []
    for block in text_blocks:
        chunks = chunk_text(block.content, file_id, block.block_id, block.page_num)
        all_chunks.extend(chunks)

    if not all_chunks:
        return 0, []

    # Embedding
    all_chunks = embed_chunks(all_chunks)

    # 存储
    stored = store_chunks(all_chunks)

    return stored, all_chunks

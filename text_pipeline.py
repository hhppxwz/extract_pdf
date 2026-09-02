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

import re

# ============================================================
# 针对不同文档类型的重组逻辑
# ============================================================

def _merge_by_heading(text_blocks: list) -> list:
    """
    学术论文：按章节标题合并
    判断标准（按优先级）：
    1. MinerU 返回的 text_level == 2（二级标题）
    2. 正则匹配（作为兜底，应对 MinerU 漏标的情况）
    3. 排除过长文本（>80字，避免把正文段落误判为标题）
    """
    # ===== 写入处理前完整内容 =====
    with open("before.txt", "w", encoding="utf-8") as f:
        f.write(f"===== 处理前原始块 (共 {len(text_blocks)} 个) =====\n\n")
        for idx, block in enumerate(text_blocks):
            f.write(f"【块 {idx + 1}】页码: {block.page_num}\n")
            f.write(f"内容:\n{block.content}\n")
            f.write(f"原始json内容:\n{block.raw}\n")
            f.write("-" * 80 + "\n\n")

    heading_pattern = re.compile(
        r'^(Abstract|Introduction|Method|Experiment|Conclusion|References|'
        r'附录|第[一二三四五六七八九十]+章|'
        r'\d+\.?\s+[A-Za-z])',  # 数字 + 可选点 + 空格 + 字母
        re.I
    )
    sorted_blocks = sorted(text_blocks, key=lambda x: x.page_num)
    merged = []
    if not sorted_blocks:
        return merged


    current = {"text": "", "page_num": sorted_blocks[0].page_num}

    for block in sorted_blocks:
        text = block.content.strip()
        if not text:
            continue


        # ===== 核心判断逻辑（三个标准） =====
        # 标准 1：类型是 text（已满足，因为传入的就是 text_blocks）
        #            且 text_level == 2（MinerU 的二级标题标记）
        text_level = block.raw.get('text_level', 0)  # 如果没有该属性，默认 0
        is_heading_by_level = (text_level == 2)

        # 标准 2：正则匹配
        is_heading_by_regex = bool(heading_pattern.match(text))

        # 标准 3：长度不过长（真正的标题一般不会超过 80 个字）
        is_too_long = len(text) > 80

        # 最终判定：符合 level 或 正则，且不能太长
        is_heading = (is_heading_by_level or is_heading_by_regex) and not is_too_long
        # ========================================

        if is_heading:
            # 遇到标题：保存上一个章节
            if current["text"]:
                merged.append(current)
            # 开启新章节（标题作为新 chunk 的开头）
            current = {"text": text, "page_num": block.page_num}
        else:
            # 普通段落：追加到当前章节
            if current["text"]:
                current["text"] += "\n" + text
            else:
                current["text"] = text
                current["page_num"] = block.page_num

    # 保存最后一个章节
    if current["text"]:
        merged.append(current)

    # ===== 写入处理后完整内容 =====
    with open("after.txt", "w", encoding="utf-8") as f:
        f.write(f"===== 处理后合并单元 (共 {len(merged)} 个) =====\n\n")
        for idx, chunk in enumerate(merged):
            f.write(f"【章节 {idx + 1}】起始页码: {chunk['page_num']}\n")
            f.write(f"内容:\n{chunk['text']}\n")
            f.write("=" * 80 + "\n\n")
    return merged



def _merge_by_article(text_blocks: list) -> list:
    """
    政策制度：按 "第X条" 合并
    """
    article_pattern = re.compile(r'^第[一二三四五六七八九十百]+条')
    sorted_blocks = sorted(text_blocks, key=lambda x: x.page_num)
    merged = []
    current = {"text": "", "page_num": sorted_blocks[0].page_num if sorted_blocks else 0}

    for block in sorted_blocks:
        text = block.content.strip()
        if not text:
            continue
        if article_pattern.match(text):
            if current["text"]:
                merged.append(current)
            current = {"text": text, "page_num": block.page_num}
        else:
            if current["text"]:
                current["text"] += "\n" + text
            else:
                current["text"] = text
                current["page_num"] = block.page_num

    if current["text"]:
        merged.append(current)
    return merged


def _merge_by_page(text_blocks: list) -> list:
    """
    行政表单：按页码合并（不跨页），每一页作为一个独立的逻辑单元
    """
    from collections import defaultdict
    page_map = defaultdict(list)
    for block in text_blocks:
        page_map[block.page_num].append(block.content.strip())

    merged = []
    for page_num, texts in sorted(page_map.items()):
        merged.append({
            "text": "\n".join([t for t in texts if t]),
            "page_num": page_num
        })
    return merged

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
    blocks: list[ContentBlock], file_id: str,doc_type: str = "other"
) -> tuple[int, list[TextChunk]]:
    """
    文本块处理主入口：筛选文本块 → 分块 → Embedding → 存储
    返回 (存入的块数, TextChunk列表)
    """
    text_blocks = [b for b in blocks if b.type == BlockType.TEXT
                   and b.content.strip()
                   and b.raw.get('type') not in ('header', 'footer','page_number','page_footnote') #过滤页眉页脚
                   ]
    if not text_blocks:
        return 0, []
    # ========== 【新增逻辑】根据文档类型进行重组 ==========
    if doc_type == "academic_paper":
        merged_units = _merge_by_heading(text_blocks)
        print(f"[重组] 学术论文: {len(text_blocks)} 个原始块 → {len(merged_units)} 个章节单元")
    elif doc_type == "policy_regulation":
        merged_units = _merge_by_article(text_blocks)
        print(f"[重组] 政策制度: {len(text_blocks)} 个原始块 → {len(merged_units)} 个条款单元")
    elif doc_type == "admin_form":
        merged_units = _merge_by_page(text_blocks)
        print(f"[重组] 行政表单: {len(text_blocks)} 个原始块 → {len(merged_units)} 个页面单元")
    else:
        # 其他类型：不做合并，每个原始块独立处理（保持你原有的行为）
        merged_units = [{"text": b.content, "page_num": b.page_num} for b in text_blocks]
        print(f"[重组] 其他类型: 不合并，保留 {len(merged_units)} 个独立块")
    # ====================================================

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

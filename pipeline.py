"""
主流程编排模块
串联：解析 → 分类 → 文本管线 → 图片管线 → 表格管线 → 溯源记录
"""
import time
import datetime
import traceback

from typing import Optional

from config import app_config
from document_classfier import classify_document
from metadata import extract_document_metadata
from models import ContentBlock, ProcessingResult, ProcessingStatus, BlockType

from pdf_parser import get_parser_client
from parsers import PDFParser




from classifier import classify_blocks
from text_pipeline import process_text_blocks
from image_pipeline import process_image_blocks
from table_pipeline import process_table_blocks
from metadata_service import (
    record_file_start, record_file_done, record_file_failed,
    record_block_storage,
)
from storage_adapter import storage

import hashlib
'''
def _convert_parsed_data_to_blocks(
        parsed_data: dict,
        file_path: str,
        file_id: str = ""
) -> list[ContentBlock]:
    """
    将 parse 返回的原始数据转换为 ContentBlock 列表
    """
    import re
    import base64

    text = parsed_data.get("text")
    images_info = parsed_data.get("images_info", [])
    content_list_data = parsed_data.get("content_list_data")
    content_list_v2_data = parsed_data.get("content_list_v2_data")

    print("[DEBUG] text 长度:", len(text) if text else 0)
    print("[DEBUG] 图片数量:", len(images_info))

    print("\n--- content_list_data ---")
    if content_list_data is None:
        print("  (None)")
    elif isinstance(content_list_data, list):
        print(f"  条目数: {len(content_list_data)}")
    else:
        print(f"  非列表类型: {type(content_list_data)}")
        print(f"  内容: {content_list_data}")

    print("\n--- content_list_v2_data ---")
    if content_list_v2_data is None:
        print("  (None)")
    elif isinstance(content_list_v2_data, list):
        print(f"  条目数: {len(content_list_v2_data)}")
    else:
        print(f"  非列表类型: {type(content_list_v2_data)}")
        print(f"  内容: {content_list_v2_data}")
    print("========================================\n")
    # 优先使用 content_list_data，否则使用 content_list_v2_data
    content_list = content_list_data or content_list_v2_data
    if not content_list:
        # 如果没有结构化数据，回退到完整 Markdown
        if text:
            return [
                ContentBlock(
                    block_id=f"blk_{hashlib.md5(file_path.encode()).hexdigest()[:12]}",
                    type=BlockType.TEXT,
                    page_num=0,
                    content=text,
                )
            ]
        raise RuntimeError("未找到有效内容")
    print("开始处理")
    # 构建图片文件名到数据的映射
    img_map = {img["image_name"]: img["raw_bytes"] for img in images_info}

    blocks = []
    for idx, item in enumerate(content_list):
        # 提取公共字段
        item_type = item.get("type", "text")
        # 注意：你的数据使用 page_idx 而非 page_num
        page_num = item.get("page_idx", 0)
        bbox = item.get("bbox")

        if item_type == "text":
            content = item.get("content", "")
            block = ContentBlock(
                block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_text'.encode()).hexdigest()[:12]}",
                type=BlockType.TEXT,
                page_num=page_num,
                bbox=bbox,
                content=content,
            )
            blocks.append(block)

        elif item_type == "table":
            # 优先从 table_body 获取 HTML 表格
            table_html = item.get("table_body", "")
            # 如果 table_body 为空，尝试 content
            if not table_html:
                content = item.get("content", "")
                # 检查是否包含表格结构
                if "<table" in content or ("|" in content and "---" in content):
                    table_html = content

            if table_html:
                # 有表格结构 → 归类为 TABLE
                print("处理为表格")
                block = ContentBlock(
                    block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_table'.encode()).hexdigest()[:12]}",
                    type=BlockType.TABLE,
                    page_num=page_num,
                    bbox=bbox,
                    content=table_html,
                    table_html=table_html,
                )
                blocks.append(block)
            else:
                # 没有表格结构 → 尝试匹配图片（降级）
                img_data = None
                img_path = item.get("img_path")
                if img_path and img_path in img_map:
                    img_data = img_map[img_path]
                # 如果按路径没找到，尝试按顺序匹配
                if img_data is None and idx < len(images_info):
                    img_data = images_info[idx].get("data")
                if img_data:
                    img_b64 = base64.b64encode(img_data).decode('utf-8')
                    # 猜测图片格式，默认 png
                    ext = "png"
                    if img_path and '.' in img_path:
                        ext = img_path.split('.')[-1].lower()
                    block = ContentBlock(
                        block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_img'.encode()).hexdigest()[:12]}",
                        type=BlockType.IMAGE,
                        page_num=page_num,
                        bbox=bbox,
                        content=f"data:image/{ext};base64,{img_b64}",
                    )
                    blocks.append(block)
                else:
                    # 既无表格也无图片 → 降级为文本
                    block = ContentBlock(
                        block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_text'.encode()).hexdigest()[:12]}",
                        type=BlockType.TEXT,
                        page_num=page_num,
                        bbox=bbox,
                        content=item.get("content", "") or f"[无法识别的表格条目]",
                    )
                    blocks.append(block)

        elif item_type == "image":
            # 图片块
            img_data = None
            # 尝试从 img_path 匹配
            img_path = item.get("img_path")
            if img_path and img_path in img_map:
                img_data = img_map[img_path]
            # 如果没找到，按顺序匹配
            if img_data is None and idx < len(images_info):
                img_data = images_info[idx].get("data")
            if img_data:
                img_b64 = base64.b64encode(img_data).decode('utf-8')
                ext = "png"
                if img_path and '.' in img_path:
                    ext = img_path.split('.')[-1].lower()
                block = ContentBlock(
                    block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_img'.encode()).hexdigest()[:12]}",
                    type=BlockType.IMAGE,
                    page_num=page_num,
                    bbox=bbox,
                    content=f"data:image/{ext};base64,{img_b64}",
                )
                blocks.append(block)
            else:
                # 无图片数据，降级为文本（内容可能包含图片描述）
                content = item.get("content", "") or f"[图片: {item.get('type')}]"
                block = ContentBlock(
                    block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_text'.encode()).hexdigest()[:12]}",
                    type=BlockType.TEXT,
                    page_num=page_num,
                    bbox=bbox,
                    content=content,
                )
                blocks.append(block)

        else:
            # 未知类型，作为文本处理
            content = item.get("content", "") or f"[未知类型: {item_type}]"
            block = ContentBlock(
                block_id=f"blk_{hashlib.md5(f'{page_num}_{idx}_other'.encode()).hexdigest()[:12]}",
                type=BlockType.TEXT,
                page_num=page_num,
                bbox=bbox,
                content=content,
            )
            blocks.append(block)

    return blocks
    '''
def process_pdf(file_path: str, page_count: int = 0) -> ProcessingResult:
    t_start = time.time()
    errors: list[str] = []
    file_id = ""
    has_critical_error = False
    final_status = ProcessingStatus.DONE


    try:
        # ---- Step 1: 读取文件，上传到 MinIO ----
        print(f"{datetime.datetime.now()} 开始读取文件上传至MinIO")
        print(f"file_id: {file_id}")
        print(f"file_path: {file_path}")

        with open(file_path, "rb") as f:
            file_data = f.read()
        file_name = file_path.replace("\\", "/").split("/")[-1]

        # ---- Step 2: 记录文件开始处理 ----
        file_id = record_file_start(file_name, file_data, page_count)

        # 上传原始 PDF 到 MinIO；抽取验证阶段可以通过环境变量暂时关闭。
        if app_config.minio.enabled:
            minio_key = f"pdf/{file_id}.pdf"
            try:
                storage.object.upload(
                    app_config.minio.bucket_pdf,
                    minio_key,
                    file_data,
                    content_type="application/pdf",
                )
            except Exception:
                errors.append(f"MinIO 上传失败:  {traceback.format_exc()}")
                has_critical_error = True
        else:
            print("已跳过 MinIO 原始文件上传（MINIO_ENABLED=false）")

        # ---- Step 3: cloudmineru 解析 ----
        #临时注释测试  parser = get_parser_client()
        parser = PDFParser()  # 自动根据 config 选择后端
        print(f"{datetime.datetime.now()} 开始解析pdf")


        try:
            # 返回原始数据
            blocks = parser.parse(file_path, page_count)
            print(f"{datetime.datetime.now()} 解析完成，共 {len(blocks)} 个块")


        except Exception as e:
            '''
            record_file_failed(file_id, f"cloudmineru 解析失败: {e}")
            return ProcessingResult(
                file_id=file_id,
                status=ProcessingStatus.FAILED,
                errors=[f"cloudmineru 解析失败: {e}"],
            )'''

            traceback.print_exc()  # ← 打印完整堆栈
            # 然后向上抛出或记录错误
            raise

        # 填充 file_id
        for b in blocks:
            b.file_id = file_id

        total_blocks = len(blocks)

        #判断类型：学术期刊/规章制度/……
        classification = {"doc_type": "other", "confidence": 0.0}
        try:
            classification = classify_document(file_path, file_name)
        except Exception as e:
            errors.append(f"{datetime.datetime.now()} 分类pdf失败: {e}")

        doc_type = classification["doc_type"]
        doc_conf = classification["confidence"]
        print(f"分类结果{classification}")

        #-----元数据抽取--------------
        # ... 在 process_pdf 中 ...
        metadata = {}
        try:
            metadata = extract_document_metadata(blocks, doc_type)
            print(f"{datetime.datetime.now()}  提取metadata为{metadata}")
        except Exception as e:
            errors.append(f"matedata获取失败: {traceback.format_exc()}")

        # 规章制度单独保存文档和条款结构。此阶段只使用解析块和确定性规则，
        # 实体、关系抽取由独立命令执行，避免大模型失败影响 PDF 基础处理。
        if doc_type == "policy_regulation":
            try:
                from policy_pipeline import structure_policy_document

                structure_result = structure_policy_document(
                    file_id=file_id,
                    file_name=file_name,
                    blocks=blocks,
                    metadata=metadata,
                )
                print(
                    f"{datetime.datetime.now()} 制度结构化完成："
                    f"{structure_result['clause_count']} 条，"
                    f"质量 {structure_result['parse_quality']:.3f}"
                )
            except Exception as e:
                errors.append(f"制度条款结构化失败: {traceback.format_exc()}")



        # ---- Step 4: 分类所有表格块 ----
        print(f"{datetime.datetime.now()}  开始分类表格块")
        classify_blocks(blocks)
        print(f"{datetime.datetime.now()}  分类后长度{len(blocks)}")


        # ---- Step 5: 文本管线 ----
        text_stored = 0
        print(f"{datetime.datetime.now()}  开始文本管线")
        try:
            text_stored, _ = process_text_blocks(blocks, file_id,doc_type)
        except Exception as e:
            errors.append(f"文本管线失败: {traceback.format_exc()}")
            has_critical_error = True

        print(f"{datetime.datetime.now()}  文本管线结束")
        # ---- Step 6: 图片管线 ----
        print(f"{datetime.datetime.now()}  开始图片管线")
        images_stored = 0
        try:
            images_stored, _ = process_image_blocks(blocks, file_id)
        except Exception as e:
            errors.append(f"图片管线失败: {traceback.format_exc()}")

        print(f"{datetime.datetime.now()}  图片管线结束")

        # ---- Step 7: 表格管线 ----
        print(f"{datetime.datetime.now()}  开始表格管线")
        dt_stored = 0
        form_stored = 0
        uncertain_count = 0
        try:
            dt_stored, form_stored, uncertain_count = process_table_blocks(blocks, file_id)
        except Exception as e:
            errors.append(f"表格管线失败: {traceback.format_exc()}")

        print(f"{datetime.datetime.now()}  表格管线结束")
        # ---- Step 8: 溯源记录 ----
        for b in blocks:
            try:
                if b.type == BlockType.TEXT and b.content.strip():
                    record_block_storage(
                        file_id, b, "pg_vector",
                        f"pdf_{file_id}_text"
                    )
                elif b.type == BlockType.IMAGE and b.image_url:
                    record_block_storage(
                        file_id, b, "minio",
                        b.image_url or ""
                    )
                elif b.type == BlockType.TABLE:
                    if b.table_category and b.table_category.value != "uncertain":
                        target = "pg_relational" if b.table_category.value == "data_table" else "pg_jsonb"
                        loc = f"pdf_{file_id}_tbl_{b.block_id}" if target == "pg_relational" else "pdf_forms"
                        record_block_storage(file_id, b, target, loc)
                    else:
                        record_block_storage(file_id, b, "pending_review", "")
            except Exception as e:
                errors.append(f"存储溯源记录失败 (block {b.block_id}): {e}")
                has_critical_error = True

        # ---- 完成 ----
        # 最终状态
        if has_critical_error:
            final_status = ProcessingStatus.FAILED
            record_file_failed(file_id, "; ".join(errors))
        else:
            final_status = ProcessingStatus.DONE
            record_file_done(file_id)

        return ProcessingResult(
            file_id=file_id,
            status=final_status,
            total_blocks=total_blocks,
            text_chunks_stored=text_stored,
            images_stored=images_stored,
            data_tables_stored=dt_stored,
            forms_stored=form_stored,
            uncertain_tables=uncertain_count,
            errors=errors,
            duration_seconds=round(time.time() - t_start, 2),
        )

    except Exception as e:
        duration = round(time.time() - t_start, 2)
        if file_id:
            record_file_failed(file_id, str(e))
        return ProcessingResult(
            file_id=file_id,
            status=ProcessingStatus.FAILED,
            errors=errors + [f"Pipeline 整体失败: {traceback.format_exc()}"],
            duration_seconds=duration,
        )

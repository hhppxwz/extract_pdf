"""
图片处理管线：提取 → 过滤 → 分类 → 存入 MinIO
"""
import io
import uuid
import base64
from typing import Optional

import httpx
from PIL import Image

from config import app_config
from models import ContentBlock, BlockType, ImageCategory
from storage_adapter import storage

# 图片最小尺寸阈值（像素），小于此值的图标类直接过滤
MIN_IMAGE_SIZE = 100


def _download_image(url_or_b64: str) -> Optional[bytes]:
    """从 URL 或 base64 字符串获取图片二进制数据"""
    if url_or_b64.startswith("http://") or url_or_b64.startswith("https://"):
        try:
            resp = httpx.get(url_or_b64, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None
    # 尝试 base64 解码
    # cloudmineru 可能返回 data:image/png;base64,... 格式
    if url_or_b64.startswith("data:"):
        try:
            header, encoded = url_or_b64.split(",", 1)
            return base64.b64decode(encoded)
        except Exception:
            return None
    # 纯 base64
    try:
        return base64.b64decode(url_or_b64)
    except Exception:
        return None


def _get_image_size(data: bytes) -> tuple[int, int]:
    """获取图片尺寸 (宽, 高)"""
    try:
        img = Image.open(io.BytesIO(data))
        return img.size
    except Exception:
        return (0, 0)


def _classify_image(image_data: bytes, size: tuple[int, int]) -> ImageCategory:
    """
    图片分类：基于尺寸启发式规则。
    后续可接入视觉 LLM 进行更精细的分类。
    """
    w, h = size
    # 极小图片视为图标 → 过滤掉
    if w < MIN_IMAGE_SIZE and h < MIN_IMAGE_SIZE:
        return ImageCategory.ICON

    # 宽高比极大或极小可能是印章/签名条
    aspect = max(w, h) / max(min(w, h), 1)
    if aspect > 5:
        return ImageCategory.SEAL

    # 图表通常有固定比例，此处简化：中等以上尺寸且较方正视为图表或照片
    if min(w, h) >= 300:
        return ImageCategory.CHART if 0.8 < aspect < 1.25 else ImageCategory.PHOTO

    return ImageCategory.PHOTO


def process_image_block(block: ContentBlock, file_id: str) -> Optional[ContentBlock]:
    """
    处理单个图片块：
    1. 下载/解码图片数据
    2. 过滤过小的图标
    3. 上传到 MinIO
    4. 写元数据到 PG
    返回更新后的 ContentBlock（含 image_url 和 image_category）
    """
    if block.type != BlockType.IMAGE:
        return None

    image_data = _download_image(block.content)
    if image_data is None or len(image_data) == 0:
        return None

    size = _get_image_size(image_data)
    block.image_size = size

    category = _classify_image(image_data, size)
    block.image_category = category

    # 图标类过滤，不上传
    if category == ImageCategory.ICON:
        return block

    # 上传到 MinIO
    bucket = app_config.minio.bucket_images
    ext = _guess_extension(image_data)
    key = f"{file_id}/{block.block_id}.{ext}"

    try:
        url = storage.object.upload(bucket, key, image_data, content_type=f"image/{ext}")
        block.image_url = url
    except Exception as e:
        # MinIO 不可用时记录错误但不阻塞
        block.image_url = f"upload_failed: {e}"

    return block


def _guess_extension(data: bytes) -> str:
    """根据文件头魔数推断图片格式"""
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "png"


def process_image_blocks(
    blocks: list[ContentBlock], file_id: str
) -> tuple[int, list[ContentBlock]]:
    """
    图片块处理主入口
    返回 (成功存储的图片数, 处理后的图片块列表)
    """
    image_blocks = [b for b in blocks if b.type == BlockType.IMAGE]
    if not image_blocks:
        return 0, []

    stored = 0
    processed: list[ContentBlock] = []
    for block in image_blocks:
        b = block.model_copy()
        result = process_image_block(b, file_id)
        if result and result.image_url and not result.image_url.startswith("upload_failed"):
            stored += 1
        if result:
            processed.append(result)

    return stored, processed

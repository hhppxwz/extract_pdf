"""
数据模型定义
涵盖 PDF 解析结果、表格结构、处理状态等核心数据结构。
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from datetime import datetime
from pydantic import BaseModel, Field


# ============================================================
# 内容块类型
# ============================================================

class BlockType(str, Enum):
    """content_block 的类型枚举"""
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    FORMULA = "formula"


class TableCategory(str, Enum):
    """表格分类结果"""
    DATA_TABLE = "data_table"     # 存数据的表格 → 转关系
    FORM = "form"                 # 填信息的表单 → 转 JSON
    UNCERTAIN = "uncertain"       # 无法确定 → 人工复核


class ImageCategory(str, Enum):
    """图片分类结果"""
    CHART = "chart"       # 图表/统计图
    PHOTO = "photo"       # 照片/插图
    SEAL = "seal"         # 印章/签名
    ICON = "icon"         # 图标/装饰（可过滤）


class ProcessingStatus(str, Enum):
    """文件处理状态"""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


# ============================================================
# cloudmineru 响应模型
# ============================================================

class MinerUBlock(BaseModel):
    """cloudmineru 返回的单个内容块"""
    type: BlockType
    bbox: list[float] = Field(default_factory=list)  # [x0, y0, x1, y1]
    page_num: int = 0
    content: str = ""        # 文本内容 或 表格 HTML 或 图片 URL/base64


class MinerUPage(BaseModel):
    """PDF 单页信息"""
    page_num: int
    width: float = 0.0
    height: float = 0.0
    blocks: list[MinerUBlock] = Field(default_factory=list)


class MinerUResponse(BaseModel):
    """cloudmineru 解析结果"""
    code: int = 0
    message: str = ""
    pages: list[MinerUPage] = Field(default_factory=list)


class MinerUTaskStatus(BaseModel):
    """cloudmineru 任务状态"""
    task_id: str
    status: str  # "pending" | "processing" | "done" | "failed"
    progress: float = 0.0  # 0～100
    result: Optional[Any] = None  # 允许任何类型（字符串、字典等）
    error: Optional[str] = None
    markdown_url: Optional[str] = None  # Agent API 用
    zip_url: Optional[str] = None  # v4 专用：full_zip_url


# ============================================================
# 内部标准化内容块
# ============================================================

class ContentBlock(BaseModel):
    """系统内部统一的内容块表示"""
    block_id: str = ""                    # 唯一标识
    file_id: str = ""                     # 来源 PDF 标识
    type: BlockType
    page_num: int = 0
    bbox: list[float] = Field(default_factory=list)
    content: str = ""                     # 原始内容
    # 表格专用字段
    table_category: Optional[TableCategory] = None
    table_score: Optional[float] = None   # 分类评分
    table_html: Optional[str] = None      # 表格 HTML 表示
    # 图片专用字段
    image_category: Optional[ImageCategory] = None
    image_url: Optional[str] = None       # MinIO 中的访问 URL
    image_size: Optional[tuple[int, int]] = None  # (w, h)


# ============================================================
# 表格结构模型
# ============================================================

class TableCell(BaseModel):
    """单个表格单元格"""
    row: int
    col: int
    text: str = ""
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


class TableStructure(BaseModel):
    """解析后的表格结构"""
    headers: list[str] = Field(default_factory=list)     # 列名
    rows: list[list[str]] = Field(default_factory=list)  # 数据行（二维数组）
    col_types: list[str] = Field(default_factory=list)   # 列类型推断结果
    has_aggregation: bool = False  # 是否含合计行
    row_count: int = 0
    col_count: int = 0


# ============================================================
# 文本块与 Embedding
# ============================================================

class TextChunk(BaseModel):
    """文本分块"""
    chunk_id: str = ""
    file_id: str = ""
    block_id: str = ""
    page_num: int = 0
    text: str = ""
    chunk_index: int = 0
    token_count: int = 0
    embedding: Optional[list[float]] = None  # 向量


# ============================================================
# 处理状态与溯源
# ============================================================

class FileRecord(BaseModel):
    """PDF 文件处理记录"""
    file_id: str
    file_name: str
    file_hash: str = ""         # SHA256
    file_size: int = 0
    page_count: int = 0
    status: ProcessingStatus = ProcessingStatus.PENDING
    minio_key: str = ""         # MinIO 中原始 PDF 的存储 key
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    error_message: str = ""


class BlockStorageRecord(BaseModel):
    """内容块存储溯源记录"""
    id: int = 0
    file_id: str
    block_id: str
    block_type: BlockType
    page_num: int = 0
    storage_target: str = ""    # 存储后端标识
    storage_location: str = ""  # 具体位置：表名 / bucket key / 索引名
    created_at: datetime = Field(default_factory=datetime.now)


class ProcessingResult(BaseModel):
    """单个 PDF 的处理结果汇总"""
    file_id: str
    status: ProcessingStatus
    total_blocks: int = 0
    text_chunks_stored: int = 0
    images_stored: int = 0
    data_tables_stored: int = 0
    forms_stored: int = 0
    uncertain_tables: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0

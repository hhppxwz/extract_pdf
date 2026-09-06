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


class BatchStatus(str, Enum):
    """批任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class BatchItemStatus(str, Enum):
    """批任务中的单个文件状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class ErrorKind(str, Enum):
    """处理失败类型，用于决定是否自动重试"""
    TRANSIENT = "transient"
    PERMANENT = "permanent"


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
    err_code: Optional[int] = None  #  错误码
    err_msg: Optional[str] = None #错误信息


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
    # 论文管线需要
    chunk_level: Optional[str] = None      # abstract / section / paragraph / reference
    section_title: Optional[str] = None     # 所属章节标题
    chunk_index: Optional[int] = None       # 段落序号

    # KG 管线需要
    entities: Optional[list[dict]] = None   # 抽取的实体
    relations: Optional[list[dict]] = None  # 抽取的关系

    # 溯源需要
    doc_type: Optional[str] = None          # 文档类型标签
    doc_type_confidence: Optional[float] = None

    # ===== 新增：存储 MinerU 原始 JSON =====
    raw: Optional[dict] = None  # 或者用 Any，如果你存的是复杂结构
    # =====================================


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
    error_kind: Optional[ErrorKind] = None


class ProcessingVersion(BaseModel):
    """一次批处理使用的代码和模型版本快照"""
    parser_version: str
    embedding_model: str
    llm_model: str
    pipeline_version: str = ""


# ============================================================
# 制度条款与知识抽取模型
# ============================================================

class PolicyValidityStatus(str, Enum):
    """制度效力状态。未人工核验的制度统一为 unknown。"""
    CURRENT = "current"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class PolicyStructureStatus(str, Enum):
    """制度文档结构化状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PolicyExtractionStatus(str, Enum):
    """实体关系抽取运行状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class PolicyExtractionItemStatus(str, Enum):
    """单条款实体关系抽取状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class PolicyEntityType(str, Enum):
    """首期制度实体类型白名单。"""
    MATTER = "matter"
    DEPARTMENT = "department"
    AUDIENCE = "audience"
    MATERIAL = "material"
    AMOUNT = "amount"
    DEADLINE = "deadline"
    APPROVAL_ACTION = "approval_action"


class PolicyRelationType(str, Enum):
    """首期制度关系类型白名单。"""
    APPLIES_TO = "applies_to"
    HANDLED_BY = "handled_by"
    REQUIRES_MATERIAL = "requires_material"
    HAS_AMOUNT = "has_amount"
    HAS_DEADLINE = "has_deadline"
    REQUIRES_APPROVAL = "requires_approval"
    CITES = "cites"
    BASED_ON = "based_on"
    REVISES = "revises"
    ABOLISHES = "abolishes"
    REPLACES = "replaces"


class PolicyReviewStatus(str, Enum):
    """抽取结果或审核项的处理状态。"""
    AUTO_APPROVED = "auto_approved"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PolicyCandidateKind(str, Enum):
    """人工审核候选的来源类型。"""
    ENTITY = "entity"
    RELATION = "relation"


class PolicyAnnotationDecision(str, Enum):
    """人工对候选结果作出的标注结论。"""
    APPROVED = "approved"
    REJECTED = "rejected"
    CORRECTED = "corrected"
    ADDED = "added"


class PolicyDocument(BaseModel):
    """制度文件结构化记录。"""
    policy_id: str
    file_id: str
    file_name: str = ""
    title: str = ""
    doc_number: str = ""
    issuing_department: str = ""
    issue_date: Optional[str] = None
    effective_date: Optional[str] = None
    expiry_date: Optional[str] = None
    validity_status: PolicyValidityStatus = PolicyValidityStatus.UNKNOWN
    version: str = ""
    original_pdf_url: str = ""
    parse_quality: float = 0.0
    structure_status: PolicyStructureStatus = PolicyStructureStatus.PENDING
    structure_version: str = ""


class PolicyClause(BaseModel):
    """制度条款及其在原 PDF 中的定位信息。"""
    clause_id: str
    policy_id: str
    parent_clause_id: Optional[str] = None
    level: str = "article"
    chapter_path: list[str] = Field(default_factory=list)
    article_no: str = ""
    paragraph_no: str = ""
    item_no: str = ""
    raw_text: str = ""
    search_text: str = ""
    page_start: int = 0
    page_end: int = 0
    bboxes: list[list[float]] = Field(default_factory=list)
    confidence: float = 0.0
    content_hash: str = ""
    sequence_no: int = 0
    structure_version: str = ""
    is_active: bool = True


class PolicyEntity(BaseModel):
    """条款中抽取出的实体及其证据。"""
    entity_id: str
    run_id: str
    clause_id: str
    entity_type: PolicyEntityType
    name: str
    raw_text: str = ""
    normalized_value: dict[str, Any] = Field(default_factory=dict)
    evidence_text: str = ""
    confidence: float = 0.0
    review_status: PolicyReviewStatus = PolicyReviewStatus.PENDING


class PolicyRelation(BaseModel):
    """条款实体之间或制度之间的关系。"""
    relation_id: str
    run_id: str
    clause_id: str
    relation_type: PolicyRelationType
    subject_entity_id: Optional[str] = None
    object_entity_id: Optional[str] = None
    target_policy_id: Optional[str] = None
    target_text: str = ""
    evidence_text: str = ""
    confidence: float = 0.0
    review_status: PolicyReviewStatus = PolicyReviewStatus.PENDING


class ReviewItem(BaseModel):
    """需要人工确认的制度结构或抽取问题。"""
    review_id: str
    run_id: Optional[str] = None
    policy_id: Optional[str] = None
    clause_id: Optional[str] = None
    entity_id: Optional[str] = None
    relation_id: Optional[str] = None
    issue_type: str
    description: str = ""
    evidence_text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    status: PolicyReviewStatus = PolicyReviewStatus.PENDING
    reviewer: str = ""
    review_note: str = ""


class PolicyManualAnnotation(BaseModel):
    """人工审核结果，保留模型候选并记录可训练的金标准标签。"""
    annotation_id: str
    run_id: str
    clause_id: str
    candidate_kind: PolicyCandidateKind
    candidate_id: Optional[str] = None
    decision: PolicyAnnotationDecision
    label_data: dict[str, Any] = Field(default_factory=dict)
    evidence_start: Optional[int] = None
    evidence_end: Optional[int] = None
    reviewer: str = ""
    review_note: str = ""
    guideline_version: str = "manual-annotation-v1"


class PolicyExtractionRun(BaseModel):
    """一次可恢复、可版本化的实体关系抽取运行。"""
    run_id: str
    batch_id: str
    status: PolicyExtractionStatus = PolicyExtractionStatus.PENDING
    parser_version: str = ""
    embedding_model: str = ""
    llm_model: str = ""
    pipeline_version: str = ""
    prompt_version: str = ""
    rule_version: str = ""
    schema_version: str = ""
    total_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

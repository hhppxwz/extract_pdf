"""
应用配置模块
所有配置项通过环境变量读取。PostgreSQL + pgvector + JSONB 统一多模存储。
支持 mock_mode 用于测试环境，不连接真实后端服务。
"""
import os
from dataclasses import dataclass, field

@dataclass
class ParserConfig:
    parser_backend: str = "cloudmineru"  # "cloudmineru" | "pymupdf"
    pymupdf_extract_images: bool = True
    pymupdf_extract_tables: bool = True


@dataclass
class CloudMinerUConfig:
    """cloudmineru API 配置"""
    api_url: str = field(default_factory=lambda: os.getenv(
        "CLOUDMINERU_API_URL", "https://mineru.net/api/v1/agent"
    ))


    # Batch 精准解析 API 地址
    batch_api_url: str = field(default_factory=lambda: os.getenv(
        "CLOUDMINERU_BATCH_API_URL", "https://mineru.net/api/v4/file-urls/batch"
    ))
    batch_api_key: str = field(default_factory=lambda: os.getenv(
        "CLOUDMINERU_API_KEY", "sk-zSMCeLQcu3lkgokAJJVie6tz9rvcITN5NPk49Y0qP4PwwAZa"
    ))
    timeout_per_page: int = 3
    poll_interval: float = 2.0
    mode: str = "v4"         #可选：v4或agent   默认v4即batch


@dataclass
class LLMConfig:
    """LLM API 配置（OpenAI 兼容接口）"""
    api_url: str = field(default_factory=lambda: os.getenv(
        "LLM_API_URL", "https://api.openai.com/v1"
    ))
    api_key: str = field(default_factory=lambda: os.getenv(
        "LLM_API_KEY", ""
    ))
    model: str = field(default_factory=lambda: os.getenv(
        "LLM_MODEL", "gpt-4o"
    ))
    confidence_threshold: float = 0.7


@dataclass
class PostgresConfig:
    """PostgreSQL + pgvector 连接配置"""
    host: str = field(default_factory=lambda: os.getenv(
        "PG_HOST", "127.0.0.1"
    ))
    port: int = field(default_factory=lambda: int(os.getenv(
        "PG_PORT", "5432"
    )))
    user: str = field(default_factory=lambda: os.getenv(
        "PG_USER", "postgres"
    ))
    password: str = field(default_factory=lambda: os.getenv(
        "PG_PASSWORD", "POSTGRES18"
    ))
    database: str = field(default_factory=lambda: os.getenv(
        "PG_DATABASE", "pdf_warehouse"
    ))
    pool_size: int = 5
    pool_recycle: int = 3600


@dataclass
class MinIOConfig:
    """MinIO 对象存储配置"""
    endpoint: str = field(default_factory=lambda: os.getenv(
        "MINIO_ENDPOINT", "127.0.0.1:9000"
    ))
    access_key: str = field(default_factory=lambda: os.getenv(
        "MINIO_ACCESS_KEY", "minioadmin"
    ))
    secret_key: str = field(default_factory=lambda: os.getenv(
        "MINIO_SECRET_KEY", "minioadmin"
    ))
    bucket_pdf: str = field(default_factory=lambda: os.getenv(
        "MINIO_BUCKET_PDF", "pdf-storage"
    ))
    bucket_images: str = field(default_factory=lambda: os.getenv(
        "MINIO_BUCKET_IMAGES", "pdf-images"
    ))
    secure: bool = False


@dataclass
class EmbeddingConfig:
    """文本 Embedding 模型配置"""
    model_name: str = field(default_factory=lambda: os.getenv(
        "EMBEDDING_MODEL", "BAAI/bge-m3"  #模型名称
    ))
    device: str = field(default_factory=lambda: os.getenv(
        "EMBEDDING_DEVICE", "cpu"
    ))
    chunk_size: int = 512
    chunk_overlap: int = 50


@dataclass
class AppConfig:
    """应用总配置"""
    parser: ParserConfig = field(default_factory=ParserConfig)

    cloudmineru: CloudMinerUConfig = field(default_factory=CloudMinerUConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    minio: MinIOConfig = field(default_factory=MinIOConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    mock_mode: bool = field(default_factory=lambda: os.getenv(
        "MOCK_MODE", "false"
    ).lower() == "true")


    column_naming_style: str = "original"  # 'original' 或 'normalized'
    # original: 完全保留原始列名（中文），查询需用双引号
    # normalized: 中文列名规范化为英文标识符col_1/col_2，原始列名存入 COMMENT 和元数据表


# 全局单例配置
app_config = AppConfig()

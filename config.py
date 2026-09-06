"""
应用配置模块
所有配置项通过环境变量读取。PostgreSQL + pgvector + JSONB 统一多模存储。
所有服务均通过真实后端配置连接。
"""
import os
from dataclasses import dataclass, field

from pathlib import Path
from dotenv import load_dotenv

# 1. 基于本文件所在目录，绝对路径加载 .env（不依赖当前工作目录）
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)  # 如果 .env 不存在，不会报错，但后续取变量会取到 None

# 2. 定义“强制获取环境变量”的函数（没有默认值）
def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        raise EnvironmentError(
            f"必需的环境变量 '{key}' 未设置！"
            f"请确保 .env 文件存在于 {env_path} 并包含该变量！"
        )
    return value

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
    batch_api_key = get_required_env("CLOUDMINERU_API_KEY")


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

    host = get_required_env("PG_HOST")
    port = int(get_required_env("PG_PORT"))  # 先拿字符串再转 int
    user = get_required_env("PG_USER")
    password = get_required_env("PG_PASSWORD")
    database = get_required_env("PG_DATABASE")

    pool_size: int = 5
    pool_recycle: int = 3600


@dataclass
class MinIOConfig:
    """MinIO 对象存储配置"""
    enabled: bool = field(default_factory=lambda: os.getenv(
        "MINIO_ENABLED", "true"
    ).strip().lower() in {"1", "true", "yes", "on"})
    endpoint =get_required_env("MINIO_ENDPOINT")
    access_key = get_required_env("MINIO_ACCESS_KEY")
    secret_key = get_required_env("MINIO_SECRET_KEY")
    bucket_pdf =get_required_env("MINIO_BUCKET_PDF")
    bucket_images =get_required_env("MINIO_BUCKET_IMAGES")

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
    column_naming_style: str = "original"  # 'original' 或 'normalized'
    # original: 完全保留原始列名（中文），查询需用双引号
    # normalized: 中文列名规范化为英文标识符col_1/col_2，原始列名存入 COMMENT 和元数据表


# 全局单例配置
app_config = AppConfig()

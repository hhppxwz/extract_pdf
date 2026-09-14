"""
统一存储适配层
封装 PostgreSQL + pgvector + JSONB（关系/向量/文档） 和 MinIO（二进制对象）。
统一连接 PostgreSQL、pgvector、JSONB 和 MinIO 真实后端。
"""
import json
import hashlib
import re
import uuid
from contextlib import contextmanager
from contextlib import contextmanager
from abc import ABC, abstractmethod
from typing import Optional, Any, Sequence
from dataclasses import dataclass, field

from config import app_config


# ============================================================
# 抽象接口
# ============================================================

class RelationalStorage(ABC):
    """关系表存储接口"""

    @abstractmethod
    def create_table(self, table_name: str, columns: list[tuple[str, str]]) -> None:
        """创建表：columns = [(列名, 类型), ...]"""
        ...

    @abstractmethod
    def insert_rows(
        self, table_name: str, columns: list[str], rows: list[list[Any]]
    ) -> int:
        """批量插入行，返回插入行数"""
        ...

    @abstractmethod
    def query(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: Optional[Sequence[Any]] = None,
    ) -> list[dict[str, Any]]:
        """查询表中数据"""
        ...

    @abstractmethod
    def query_for_update(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: Optional[Sequence[Any]] = None,
    ) -> list[dict[str, Any]]:
        """在当前事务内查询并锁定命中的行。"""
        ...

    @abstractmethod
    def transaction(self):
        """提供关系存储的事务上下文。"""
        ...

    @abstractmethod
    def update_rows(
        self,
        table_name: str,
        values: dict[str, Any],
        where: str,
        params: Optional[Sequence[Any]] = None,
    ) -> int:
        """按参数化条件更新数据，返回更新行数"""
        ...

    @abstractmethod
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        """执行参数化 SQL 或幂等 DDL"""
        ...


class VectorStorage(ABC):
    """向量存储接口"""

    @abstractmethod
    def create_index(self, index_name: str, dim: int) -> None:
        """创建向量索引"""
        ...

    @abstractmethod
    def insert_vectors(
        self,
        index_name: str,
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> int:
        """批量插入向量及元数据，返回插入数"""
        ...

    @abstractmethod
    def search(
        self, index_name: str, query_vector: list[float], top_k: int = 10
    ) -> list[dict[str, Any]]:
        """向量相似度检索，返回 top_k 条结果（含 metadata 和 score）"""
        ...

    @abstractmethod
    def delete_vectors_by_metadata(
        self, index_name: str, metadata_key: str, metadata_value: str
    ) -> int:
        """按 metadata 的单个字段删除向量，返回删除数量。"""
        ...

    @abstractmethod
    def count_vectors(self, index_name: str) -> int:
        """返回一个向量索引中的记录数量。"""
        ...


class DocumentStorage(ABC):
    """JSON 文档存储接口"""

    @abstractmethod
    def insert_document(
        self, collection: str, document: dict[str, Any], doc_id: str = ""
    ) -> str:
        """插入一个 JSON 文档，返回 doc_id"""
        ...

    @abstractmethod
    def find_documents(
        self, collection: str, filter_expr: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        """查询文档列表"""
        ...


class ObjectStorage(ABC):
    """二进制对象存储接口"""

    @abstractmethod
    def upload(self, bucket: str, key: str, data: bytes, content_type: str = "") -> str:
        """上传对象，返回访问 URL 或路径"""
        ...

    @abstractmethod
    def download(self, bucket: str, key: str) -> bytes:
        """下载对象"""
        ...

    @abstractmethod
    def ensure_bucket(self, bucket: str) -> None:
        """确保 bucket 存在"""
        ...


# ============================================================
# PostgreSQL + pgvector 实现
# ============================================================

class PgStorageAdapter(RelationalStorage, VectorStorage, DocumentStorage):
    """
    PostgreSQL 统一适配器。
    前提：数据库中已安装 pgvector 扩展（CREATE EXTENSION vector;）。
    关系表用标准 SQL，向量用 pgvector 的 vector 类型，文档用 JSONB。
    """

    def __init__(self):
        self._conn = None
        self._pg_config = app_config.postgres

    @property
    def conn(self):
        if self._conn is None:


            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(
                host=self._pg_config.host,
                port=self._pg_config.port,
                user=self._pg_config.user,
                password=self._pg_config.password,
                dbname=self._pg_config.database,
            )
            self._conn.autocommit = True
        return self._conn

    # --- 关系表 ---

    def create_table(self, table_name: str, columns: list[tuple[str, str]]) -> None:
        col_defs = ", ".join(f'"{col}" {dtype}' for col, dtype in columns)
        sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs});'
        with self.conn.cursor() as cur:
            cur.execute(sql)

    def drop_table(self, table_name: str) -> None:
        sql = f'DROP TABLE IF EXISTS "{table_name}";'
        with self.conn.cursor() as cur:
            cur.execute(sql)

    def insert_rows(
        self, table_name: str, columns: list[str], rows: list[list[Any]]
    ) -> int:
        if not rows:
            return 0
        placeholders = ", ".join(["%s"] * len(columns))
        col_names = ", ".join(f'"{c}"' for c in columns)
        sql = f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)
        return len(rows)

    def query(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: Optional[Sequence[Any]] = None,
    ) -> list[dict[str, Any]]:
        sql = f'SELECT * FROM "{table_name}"'
        if where:
            sql += f" WHERE {where}"
        sql += f" LIMIT {limit}"
        import psycopg2.extras
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, tuple(params or ()))
            return [dict(row) for row in cur.fetchall()]

    def query_for_update(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: Optional[Sequence[Any]] = None,
    ) -> list[dict[str, Any]]:
        """在当前事务内锁定查询结果，供并发审核等读改写场景使用。"""
        sql = f'SELECT * FROM "{table_name}"'
        if where:
            sql += f" WHERE {where}"
        sql += f" LIMIT {limit} FOR UPDATE"
        import psycopg2.extras
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, tuple(params or ()))
            return [dict(row) for row in cur.fetchall()]

    @contextmanager
    def transaction(self):
        """临时关闭自动提交，在异常时回滚并恢复原连接配置。"""
        connection = self.conn
        original_autocommit = connection.autocommit
        connection.autocommit = False
        try:
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.autocommit = original_autocommit

    def update_rows(
        self,
        table_name: str,
        values: dict[str, Any],
        where: str,
        params: Optional[Sequence[Any]] = None,
    ) -> int:
        """更新内部固定表，条件值通过参数传入，避免 SQL 注入。"""
        if not values:
            return 0
        set_sql = ", ".join(f'"{column}" = %s' for column in values)
        sql = f'UPDATE "{table_name}" SET {set_sql} WHERE {where}'
        sql_params = tuple(values.values()) + tuple(params or ())
        with self.conn.cursor() as cur:
            cur.execute(sql, sql_params)
            return cur.rowcount

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        """执行参数化 SQL，并提交当前语句。"""
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))

    # --- 向量（pgvector）---

    @staticmethod
    def _validate_vector_index_name(index_name: str) -> str:
        """限制向量表名为内部标识符，避免动态表名被注入。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", index_name or ""):
            raise ValueError(f"非法向量索引名: {index_name}")
        return index_name

    def create_index(self, index_name: str, dim: int) -> None:
        """创建向量表：id, embedding(vector(dim)), metadata(jsonb)"""
        index_name = self._validate_vector_index_name(index_name)
        if dim < 1:
            raise ValueError("向量维度必须大于 0")
        sql = f"""
        CREATE TABLE IF NOT EXISTS "{index_name}" (
            id SERIAL PRIMARY KEY,
            embedding vector({dim}),
            metadata JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
        with self.conn.cursor() as cur:
            cur.execute(sql)
        # 建 IVFFlat 索引加速检索
        try:
            idx_sql = f"""
            CREATE INDEX IF NOT EXISTS "{index_name}_embedding_idx"
            ON "{index_name}" USING ivfflat (embedding vector_cosine_ops);
            """
            with self.conn.cursor() as cur:
                cur.execute(idx_sql)
        except Exception:
            pass  # 索引创建失败不阻塞

    def insert_vectors(
        self,
        index_name: str,
        vectors: list[list[float]],
        metadata: list[dict[str, Any]],
    ) -> int:
        if not vectors:
            return 0
        index_name = self._validate_vector_index_name(index_name)
        if len(vectors) != len(metadata):
            raise ValueError("向量数量必须与 metadata 数量一致")
        sql = f'INSERT INTO "{index_name}" (embedding, metadata) VALUES (%s::vector, %s)'
        with self.conn.cursor() as cur:
            import psycopg2.extras
            # 将向量转成 pgvector 兼容格式
            data = [
                (json.dumps(v), json.dumps(m, ensure_ascii=False))
                for v, m in zip(vectors, metadata)
            ]
            psycopg2.extras.execute_batch(cur, sql, data)
        return len(vectors)

    def search(
        self, index_name: str, query_vector: list[float], top_k: int = 10
    ) -> list[dict[str, Any]]:
        index_name = self._validate_vector_index_name(index_name)
        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        sql = f"""
        SELECT id, metadata, 1 - (embedding <=> %s::vector) AS similarity
        FROM "{index_name}"
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """
        import psycopg2.extras
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            vec_str = json.dumps(query_vector)
            cur.execute(sql, (vec_str, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]

    def delete_vectors_by_metadata(
        self, index_name: str, metadata_key: str, metadata_value: str
    ) -> int:
        """仅删除指定制度的旧向量，避免重建时影响其他制度。"""
        index_name = self._validate_vector_index_name(index_name)
        if not metadata_key:
            raise ValueError("metadata 字段名不能为空")
        sql = f'DELETE FROM "{index_name}" WHERE metadata ->> %s = %s'
        with self.conn.cursor() as cur:
            cur.execute(sql, (metadata_key, metadata_value))
            return cur.rowcount

    def count_vectors(self, index_name: str) -> int:
        """查询索引记录数，用于区分空索引和正常检索。"""
        index_name = self._validate_vector_index_name(index_name)
        with self.conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{index_name}"')
            row = cur.fetchone()
        return int(row[0] if row else 0)

    # --- JSON 文档 ---

    def insert_document(
        self, collection: str, document: dict[str, Any], doc_id: str = ""
    ) -> str:
        if not doc_id:
            doc_id = hashlib.md5(
                json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]

        # 确保文档表存在
        self.create_table(collection, [
            ("id", "VARCHAR(64) PRIMARY KEY"),
            ("doc", "JSONB"),
            ("created_at", "TIMESTAMP DEFAULT NOW()"),
        ])

        sql = f"""
        INSERT INTO "{collection}" (id, doc)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc;
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (doc_id, json.dumps(document, ensure_ascii=False)))
        return doc_id

    def find_documents(
        self, collection: str, filter_expr: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = f'SELECT id, doc, created_at FROM "{collection}"'
        if filter_expr:
            sql += f" WHERE {filter_expr}"
        sql += f" LIMIT {limit}"
        with self.conn.cursor() as cur:
            import psycopg2.extras
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(sql)
            except Exception:
                return []
            return [dict(row) for row in cur.fetchall()]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ============================================================
# MinIO 实现
# ============================================================

class MinIOStorageAdapter(ObjectStorage):
    """MinIO 对象存储适配器"""

    def __init__(self):
        self._client = None
        self._config = app_config.minio

    @property
    def client(self):
        if self._client is None:
            from minio import Minio
            self._client = Minio(
                endpoint=self._config.endpoint,
                access_key=self._config.access_key,
                secret_key=self._config.secret_key,
                secure=self._config.secure,
            )
        return self._client

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def upload(self, bucket: str, key: str, data: bytes, content_type: str = "") -> str:
        import io
        self.ensure_bucket(bucket)
        self.client.put_object(
            bucket_name=bucket,
            object_name=key,
            data=io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        protocol = "https" if self._config.secure else "http"
        return f"{protocol}://{self._config.endpoint}/{bucket}/{key}"

    def download(self, bucket: str, key: str) -> bytes:
        response = self.client.get_object(bucket, key)
        return response.read()


# ============================================================
# 统一的 StorageFacade：对外暴露简化接口
# ============================================================

class StorageFacade:
    """
    统一存储门面。
    统一使用真实后端实现。
    """

    def __init__(self):
        self.relational: RelationalStorage = PgStorageAdapter()
        self.vector: VectorStorage = self.relational  # type: ignore
        self.document: DocumentStorage = self.relational  # type: ignore
        self.object: ObjectStorage = MinIOStorageAdapter()

    def close(self):
        if hasattr(self.relational, "close"):
            self.relational.close()


# 全局单例
storage = StorageFacade()

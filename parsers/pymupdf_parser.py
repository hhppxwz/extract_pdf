# parsers/pymupdf_parser.py
import fitz
import base64
import uuid
import hashlib
from typing import List, Optional, Dict, Any
from models import ContentBlock, BlockType, MinerUTaskStatus
from parsers.base import ParserStrategy
from config import app_config


class PyMuPDFParser(ParserStrategy):
    """基于 PyMuPDF 的本地 PDF 解析器"""

    def __init__(
            self,
            extract_images: bool = True,
            extract_tables: bool = True,
    ):
        self.extract_images = extract_images
        self.extract_tables = extract_tables
        self._result_cache: Dict[str, List[ContentBlock]] = {}

    def submit(self, file_path: str) -> str:
        """本地解析立即执行，返回 task_id"""
        task_id = f"local_{uuid.uuid4().hex[:12]}"
        blocks = self._extract(file_path)
        self._result_cache[task_id] = blocks
        return task_id

    def get_status(self, task_id: str) -> MinerUTaskStatus:
        blocks = self._result_cache.get(task_id)
        if blocks is not None:
            return MinerUTaskStatus(
                task_id=task_id,
                status="done",
                progress=100.0,
                result=blocks,
            )
        return MinerUTaskStatus(
            task_id=task_id,
            status="processing",
            progress=50.0,
        )

    def poll_until_done(
            self,
            task_id: str,
            timeout_seconds: Optional[float] = None
    ) -> MinerUTaskStatus:
        # 本地解析已经在 submit 时完成，直接返回
        return self.get_status(task_id)

    def parse(self, file_path: str, page_count: int = 0) -> List[ContentBlock]:
        """同步解析，直接返回 ContentBlock 列表"""
        return self._extract(file_path)

    def _extract(self, file_path: str) -> List[ContentBlock]:
        """核心提取逻辑"""
        blocks = []
        doc = fitz.open(file_path)

        for page_num, page in enumerate(doc):
            # 1. 提取文本
            text = page.get_text()

            if text.strip():
                blocks.append(ContentBlock(
                    block_id=f"p{page_num}_text_{hashlib.md5(text.encode()).hexdigest()[:8]}",
                    type=BlockType.TEXT,
                    page_num=page_num,
                    content=text,
                ))

            # 2. 提取图片
            if self.extract_images:
                image_list = page.get_images()
                for img_idx, img in enumerate(image_list):
                    try:
                        xref = img[0]
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n - pix.alpha < 4:  # 仅处理非 CMYK 图片
                            img_data = pix.tobytes("png")
                            b64 = base64.b64encode(img_data).decode()
                            blocks.append(ContentBlock(
                                block_id=f"p{page_num}_img_{img_idx}",
                                type=BlockType.IMAGE,
                                page_num=page_num,
                                content=f"data:image/png;base64,{b64}",
                            ))
                        pix = None
                    except Exception as e:
                        continue

            # 3. 提取表格（PyMuPDF 1.23+ 支持 find_tables）
            if self.extract_tables and hasattr(page, 'find_tables'):
                try:
                    tables = page.find_tables()
                    for tbl_idx, table in enumerate(tables):
                        blocks.append(ContentBlock(
                            block_id=f"p{page_num}_table_{tbl_idx}",
                            type=BlockType.TABLE,
                            page_num=page_num,
                            content=table.to_html() if hasattr(table, 'to_html') else str(table),
                            table_html=table.to_html() if hasattr(table, 'to_html') else None,
                        ))
                except Exception:
                    pass

        doc.close()
        return blocks
# parsers/dispatcher.py
from typing import Optional, List
from parsers.base import ParserStrategy
from parsers.cloud_mineru import CloudMineruParser
from parsers.pymupdf_parser import PyMuPDFParser
from config import app_config
from models import ContentBlock


class PDFParser:
    """
    统一解析调度器
    根据配置或参数选择具体的解析后端
    """

    def __init__(
            self,
            backend: Optional[str] = None,
    ):
        """
        Args:
            backend: 'cloudmineru' | 'pymupdf'，默认从 config 读取
        """
        self.backend = backend or app_config.parser.parser_backend
        self._strategy: Optional[ParserStrategy] = None

    @property
    def strategy(self) -> ParserStrategy:
        if self._strategy is None:
            if self.backend == "pymupdf":
                self._strategy = PyMuPDFParser(
                    extract_images=app_config.parser.pymupdf_extract_images,
                    extract_tables=app_config.parser.pymupdf_extract_tables,
                )
            else:  # cloudmineru
                self._strategy = CloudMineruParser(mode=app_config.cloudmineru.mode)
        return self._strategy

    def parse(self, file_path: str, page_count: int = 0) -> List[ContentBlock]:
        """同步解析 PDF"""
        return self.strategy.parse(file_path, page_count)

    def submit(self, file_path: str) -> str:
        """提交异步任务"""
        return self.strategy.submit(file_path)

    def get_status(self, task_id: str):
        """查询任务状态"""
        return self.strategy.get_status(task_id)

    def poll_until_done(self, task_id: str, timeout_seconds: Optional[float] = None):
        """轮询直到完成"""
        return self.strategy.poll_until_done(task_id, timeout_seconds)

    def close(self):
        if hasattr(self._strategy, 'close'):
            self._strategy.close()

# parsers/base.py
from abc import ABC, abstractmethod
from typing import Optional, List
from models import ContentBlock, MinerUTaskStatus


class ParserStrategy(ABC):
    """PDF解析器策略抽象基类"""

    @abstractmethod
    def parse(self, file_path: str, page_count: int = 0):
        """同步解析，直接返回结果（兼容你现有的 parse 签名）"""
        pass

    @abstractmethod
    def submit(self, file_path: str) -> str:
        """提交任务，返回 task_id"""
        pass

    @abstractmethod
    def get_status(self, task_id: str) -> MinerUTaskStatus:
        """查询任务状态"""
        pass

    @abstractmethod
    def poll_until_done(self, task_id: str, timeout_seconds: Optional[float] = None) -> MinerUTaskStatus:
        """轮询直到完成"""
        pass
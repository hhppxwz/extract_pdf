"""在服务接受请求前初始化问答使用的本地模型和关键词索引。"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def warmup_policy_qa() -> dict:
    """预热只访问本地模型和数据库，不调用问答或重排大模型接口。"""
    from policy.retrieval import _encode_policy_clause_texts, _get_bm25_index_snapshot

    report = {}
    for stage, label, operation in (
        ('embedding', '向量模型', lambda: _encode_policy_clause_texts(['制度问答预热'])),
        ('bm25', 'BM25 索引', _get_bm25_index_snapshot),
    ):
        start = time.perf_counter()
        try:
            value = operation()
            report[stage] = {'status': 'ready', 'seconds': round(time.perf_counter() - start, 4)}
            if stage == 'bm25':
                report[stage]['document_count'] = len(value[1])
            logger.info('制度问答%s预热完成，耗时 %.3f 秒', label, report[stage]['seconds'])
        except Exception as exc:
            # 各阶段独立预热；失败时由现有首次检索路径重新尝试初始化。
            report[stage] = {'status': 'failed', 'seconds': round(time.perf_counter() - start, 4),
                             'error': str(exc)}
            logger.warning('制度问答%s预热失败，后续查询将重试：%s', label, exc)
    return report

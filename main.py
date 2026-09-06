"""
PDF 多模态提取入仓 —— 启动入口
使用方式：
    python main.py                              # 启动 FastAPI 服务
    python main.py --process <pdf>              # 使用默认配置处理
    python main.py --process <pdf> --backend pymupdf   # 使用本地 PyMuPDF
"""
import sys
import os
import argparse

from pathlib import Path
import fitz
import datetime
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

# 确保工作目录为项目根目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def run_api():
    """启动 FastAPI 服务"""
    import uvicorn
    from api import app
    print("=" * 60)
    print("PDF 多模态提取入仓服务")
    print("=" * 60)
    print("启动 FastAPI 服务于 http://127.0.0.1:8000")
    print("API 文档: http://127.0.0.1:8000/docs")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000)


def run_process(pdf_path: str, backend: str = None):
    """命令行处理单个 PDF"""
    # 如果指定了 backend，修改全局 config
    if backend:
        from config import app_config
        app_config.parser.parser_backend = backend
        print(f"使用解析后端: {backend}")

    from pipeline import process_pdf

    print(f"{datetime.datetime.now()} 正在处理: {pdf_path}")

    with fitz.open(pdf_path) as doc:
        page_count = len(doc)
    print(f"共有: {page_count}页")

    result = process_pdf(pdf_path,page_count)

    print(f"\n处理结果:")
    print(f"  文件 ID:    {result.file_id}")
    print(f"  状态:       {result.status.value}")
    print(f"  总块数:     {result.total_blocks}")
    print(f"  文本块:     {result.text_chunks_stored}")
    print(f"  图片:       {result.images_stored}")
    print(f"  数据表:     {result.data_tables_stored}")
    print(f"  表单:       {result.forms_stored}")
    print(f"  待审核表:   {result.uncertain_tables}")
    print(f"  耗时:       {result.duration_seconds}s")
    if result.errors:
        print(f"  错误:")
        for e in result.errors:
            print(f"    - {e}")


def _print_batch_summary(status: dict) -> None:
    """打印批次和文件任务统计。"""
    batch = status["batch"]
    print(f"\n批次 ID: {batch['batch_id']}")
    print(f"批次状态: {batch.get('status')}")
    print(f"总文件数: {batch.get('total_count', 0)}")
    print(f"成功: {batch.get('succeeded_count', 0)}")
    print(f"跳过: {batch.get('skipped_count', 0)}")
    print(f"待重试: {batch.get('retryable_failed_count', 0)}")
    print(f"永久失败: {batch.get('permanent_failed_count', 0)}")


def run_process_dir(source_dir: str, backend: str = None) -> None:
    """创建并运行目录批次。"""
    if backend:
        from config import app_config
        app_config.parser.parser_backend = backend
        print(f"使用解析后端: {backend}")

    from batch_processor import create_batch_job, run_batch

    batch_id = create_batch_job(source_dir)
    print(f"已创建批次: {batch_id}")
    status = run_batch(batch_id)
    _print_batch_summary(status)


def run_resume_batch(batch_id: str) -> None:
    """恢复指定批次。"""
    from batch_processor import run_batch

    status = run_batch(batch_id, resume=True)
    _print_batch_summary(status)


def run_batch_status(batch_id: str) -> None:
    """只查看批次状态，不执行文件。"""
    from batch_processor import get_batch_status

    _print_batch_summary(get_batch_status(batch_id))


def _print_policy_extraction_summary(status: dict) -> None:
    """打印制度实体关系抽取运行统计。"""
    run = status["run"]
    print(f"\n抽取运行 ID: {run['run_id']}")
    print(f"抽取状态: {run.get('status')}")
    print(f"总条款数: {run.get('total_count', 0)}")
    print(f"成功: {run.get('succeeded_count', 0)}")
    print(f"失败: {run.get('failed_count', 0)}")
    print(f"当前版本: {'是' if run.get('is_current') else '否'}")


def run_extract_policy_batch(batch_id: str, limit: int | None = None) -> None:
    """创建一个新的制度实体关系抽取版本并执行。"""
    from policy_extractor import create_and_run_policy_extraction

    _print_policy_extraction_summary(
        create_and_run_policy_extraction(batch_id, limit=limit)
    )


def run_resume_policy_extraction(run_id: str) -> None:
    """恢复指定的制度实体关系抽取运行。"""
    from policy_extractor import run_policy_extraction

    _print_policy_extraction_summary(run_policy_extraction(run_id, resume=True))


def run_policy_extraction_status(run_id: str) -> None:
    """查看制度实体关系抽取状态，不执行抽取。"""
    from policy_extractor import get_policy_extraction_status

    _print_policy_extraction_summary(get_policy_extraction_status(run_id))


def run_policy_review(run_id: str, reviewer: str, limit: int) -> None:
    """在终端逐项人工审核制度实体和关系候选。"""
    from policy_reviewer import run_interactive_policy_review

    summary = run_interactive_policy_review(run_id, reviewer=reviewer, limit=limit)
    print("\n本次人工审核统计:")
    print(f"通过: {summary['approved']}")
    print(f"拒绝: {summary['rejected']}")
    print(f"修正: {summary['corrected']}")
    print(f"补充: {summary['added']}")
    print(f"跳过: {summary['skipped']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="PDF 多模态提取入仓工具")
    parser.add_argument(
        "--process",
        type=str,
        help="处理单个 PDF 文件的路径"
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["cloudmineru", "pymupdf"],
        default=None,
        help="指定解析后端：cloudmineru或pymupdf，默认从 config 读取"
    )
    parser.add_argument(
        "--process-dir",
        type=str,
        help="递归创建并处理目录中的全部 PDF"
    )
    parser.add_argument(
        "--resume-batch",
        type=str,
        help="恢复指定批次，只处理未完成或到期重试的文件"
    )
    parser.add_argument(
        "--batch-status",
        type=str,
        help="查看指定批次状态，不执行文件"
    )
    parser.add_argument(
        "--extract-policy-batch",
        type=str,
        help="为指定 PDF 批次创建并执行制度实体关系抽取"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="仅抽取前 N 条实质性条款；只可与 --extract-policy-batch 一起使用"
    )
    parser.add_argument(
        "--resume-policy-extraction",
        type=str,
        help="恢复指定的制度实体关系抽取运行"
    )
    parser.add_argument(
        "--policy-extraction-status",
        type=str,
        help="查看指定制度实体关系抽取运行状态"
    )
    parser.add_argument(
        "--review-policy-run",
        type=str,
        help="在终端逐项人工审核指定抽取运行的实体和关系候选"
    )
    parser.add_argument(
        "--reviewer",
        type=str,
        help="人工审核人的姓名或账号；只可与 --review-policy-run 一起使用"
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="本次最多显示多少条待审核候选，默认 20；只可与 --review-policy-run 一起使用"
    )

    args = parser.parse_args()

    selected_modes = [
        bool(args.process),
        bool(args.process_dir),
        bool(args.resume_batch),
        bool(args.batch_status),
        bool(args.extract_policy_batch),
        bool(args.resume_policy_extraction),
        bool(args.policy_extraction_status),
        bool(args.review_policy_run),
    ]
    if sum(selected_modes) > 1:
        parser.error("处理、批次和制度抽取参数只能选择一个")
    if args.limit is not None and not args.extract_policy_batch:
        parser.error("--limit 只能与 --extract-policy-batch 一起使用")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于等于 1")
    if args.reviewer and not args.review_policy_run:
        parser.error("--reviewer 只能与 --review-policy-run 一起使用")
    if args.review_policy_run and not args.reviewer:
        parser.error("--review-policy-run 必须提供 --reviewer")
    if args.review_limit < 1:
        parser.error("--review-limit 必须大于等于 1")

    if args.process_dir:
        run_process_dir(args.process_dir, backend=args.backend)
    elif args.resume_batch:
        run_resume_batch(args.resume_batch)
    elif args.batch_status:
        run_batch_status(args.batch_status)
    elif args.extract_policy_batch:
        run_extract_policy_batch(args.extract_policy_batch, limit=args.limit)
    elif args.resume_policy_extraction:
        run_resume_policy_extraction(args.resume_policy_extraction)
    elif args.policy_extraction_status:
        run_policy_extraction_status(args.policy_extraction_status)
    elif args.review_policy_run:
        run_policy_review(args.review_policy_run, args.reviewer, args.review_limit)
    elif args.process:
        run_process(args.process, backend=args.backend)
    else:
        run_api()

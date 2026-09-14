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
from typing import Sequence

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
    from policy.clause_export import export_batch_clause_structure

    batch_id = create_batch_job(source_dir)
    print(f"已创建批次: {batch_id}")
    status = run_batch(batch_id)
    _print_batch_summary(status)
    output_path = Path(source_dir).resolve() / f"{batch_id}_条款重组结果.json"
    exported_path = export_batch_clause_structure(batch_id, output_path)
    print(f"已导出条款重组结果: {exported_path}")


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
    from policy.extraction import create_and_run_policy_extraction
    print(f"{datetime.datetime.now()} 开始抽取关系")

    _print_policy_extraction_summary(
        create_and_run_policy_extraction(batch_id, limit=limit)
    )
    print(f"{datetime.datetime.now()} 结束抽取")


def run_resume_policy_extraction(run_id: str) -> None:
    """恢复指定的制度实体关系抽取运行。"""
    from policy.extraction import run_policy_extraction

    _print_policy_extraction_summary(run_policy_extraction(run_id, resume=True))


def run_policy_extraction_status(run_id: str) -> None:
    """查看制度实体关系抽取状态，不执行抽取。"""
    from policy.extraction import get_policy_extraction_status

    _print_policy_extraction_summary(get_policy_extraction_status(run_id))


def _print_policy_clause_index_summary(status: dict) -> None:
    """打印跨制度条款索引运行统计和失败原因。"""
    run = status.get("run", status)
    print(f"\n条款索引运行 ID: {run['run_id']}")
    print(f"索引状态: {run.get('status')}")
    print(f"总制度数: {run.get('total_count', 0)}")
    print(f"成功: {run.get('succeeded_count', 0)}")
    print(f"跳过: {run.get('skipped_count', 0)}")
    print(f"失败: {run.get('failed_count', 0)}")
    for item in status.get("items", []):
        if item.get("status") == "failed":
            print(f"  失败制度 {item.get('policy_id')}: {item.get('last_error')}")


def run_rebuild_policy_clause_index(batch_id: str) -> None:
    """为一个已结构化批次创建并执行全局条款索引重建。"""
    from policy.retrieval import create_and_run_policy_clause_index

    _print_policy_clause_index_summary(create_and_run_policy_clause_index(batch_id))


def run_resume_policy_clause_index(run_id: str) -> None:
    """只恢复失败的制度条款索引任务。"""
    from policy.retrieval import run_policy_clause_index

    _print_policy_clause_index_summary(run_policy_clause_index(run_id, resume=True))


def run_policy_clause_index_status(run_id: str) -> None:
    """查看条款索引重建进度和逐制度失败原因。"""
    from policy.retrieval import get_policy_clause_index_status

    _print_policy_clause_index_summary(get_policy_clause_index_status(run_id))


def run_policy_review(run_id: str, reviewer: str, limit: int) -> None:
    """在终端逐项人工审核制度实体和关系候选。"""
    from policy.reviewer import run_interactive_policy_review

    summary = run_interactive_policy_review(run_id, reviewer=reviewer, limit=limit)
    print("\n本次人工审核统计:")
    print(f"通过: {summary['approved']}")
    print(f"拒绝: {summary['rejected']}")
    print(f"修正: {summary['corrected']}")
    print(f"补充: {summary['added']}")
    print(f"跳过: {summary['skipped']}")


def run_extract_policy_abolition_relations(batch_id: str) -> None:
    """抽取一个批次中需要人工确认的制度废止关系。"""
    from policy.abolition import extract_batch_abolition_relations

    summary = extract_batch_abolition_relations(batch_id)
    print("\n废止关系抽取统计:")
    for key, label in (("policies", "制度"), ("clauses", "条款"), ("candidates", "候选"), ("resolved", "已解析"), ("unresolved", "待解析")):
        print(f"{label}: {summary[key]}")


def run_policy_abolition_relation_status(batch_id: str) -> None:
    """显示一个批次的废止关系审核和解析状态。"""
    from policy.abolition import get_batch_abolition_relation_status

    summary = get_batch_abolition_relation_status(batch_id)
    print("\n废止关系状态:")
    for key, label in (("total", "总数"), ("pending", "待审核"), ("approved", "已通过"), ("rejected", "已拒绝"), ("resolved", "已解析"), ("unresolved", "待解析")):
        print(f"{label}: {summary[key]}")


def run_policy_abolition_review(batch_id: str, reviewer: str, limit: int) -> None:
    """在终端审核一个批次中待处理的制度废止候选。"""
    from policy.abolition import run_interactive_abolition_review

    summary = run_interactive_abolition_review(batch_id, reviewer, limit)
    print("\n本次废止关系审核统计:")
    print(f"通过: {summary['approved']}")
    print(f"拒绝: {summary['rejected']}")
    print(f"跳过: {summary['skipped']}")


def _print_quality_outputs(title: str, paths: dict[str, Path]) -> None:
    """输出质检工具生成的文件路径。"""
    print(f"\n{title}")
    for name, path in paths.items():
        print(f"  {name}: {path}")


def run_export_policy_clause_review(
    batch_id: str,
    output_dir: str,
    clauses_per_policy: int,
) -> None:
    """导出一个批次的条款人工抽检表。"""
    from policy.quality import export_clause_review

    _print_quality_outputs(
        "已导出条款抽检表：",
        export_clause_review(batch_id, output_dir, clauses_per_policy),
    )


def _print_policy_process_summary(status: dict) -> None:
    """打印流程条款分流运行统计。"""
    run = status.get("run", status)
    print(f"\n流程判定运行 ID: {run['run_id']}")
    print(f"运行状态: {run.get('status')}")
    print(f"总条款数: {run.get('total_count', 0)}")
    print(f"流程条款: {run.get('process_count', 0)}")
    print(f"非流程条款: {run.get('non_process_count', 0)}")
    print(f"待人工确认: {run.get('pending_count', 0)}")
    print(f"失败: {run.get('failed_count', 0)}")


def run_classify_policy_process_batch(
    batch_id: str,
    selected_domains: Sequence[str] | None = None,
) -> None:
    """创建并运行一个制度批次的流程条款分流。"""
    from policy.process_runner import create_and_run_policy_process_classification

    _print_policy_process_summary(
        create_and_run_policy_process_classification(batch_id, selected_domains=selected_domains)
    )


def run_resume_policy_process(run_id: str) -> None:
    """恢复指定流程判定运行中的失败条款。"""
    from policy.process_runner import run_policy_process_classification

    _print_policy_process_summary(run_policy_process_classification(run_id, resume=True))


def run_policy_process_status(run_id: str) -> None:
    """查看流程判定运行状态，不执行分流。"""
    from policy.process_runner import get_policy_process_status

    _print_policy_process_summary(get_policy_process_status(run_id))


def run_extract_policy_process_run(process_run_id: str, limit: int | None = None) -> None:
    """只从流程判定运行中已确认的条款执行实体关系抽取。"""
    from policy.extraction import create_and_run_process_policy_extraction

    _print_policy_extraction_summary(
        create_and_run_process_policy_extraction(process_run_id, limit=limit)
    )


def run_export_policy_graph_review(run_id: str, output_dir: str) -> None:
    """导出一次抽取运行的图谱候选人工抽检表。"""
    from policy.quality import export_graph_review

    _print_quality_outputs(
        "已导出图谱抽检表：",
        export_graph_review(run_id, output_dir),
    )


def run_export_policy_process_review(process_run_id: str, output_dir: str) -> None:
    """导出流程条款分流的人工审核表。"""
    from policy.quality import export_process_review

    _print_quality_outputs(
        "已导出流程条款抽检表：",
        export_process_review(process_run_id, output_dir),
    )


def run_export_policy_process_graph_review(run_id: str, output_dir: str) -> None:
    """导出仅限流程条款来源的图谱人工审核表。"""
    from policy.quality import export_process_graph_review

    _print_quality_outputs(
        "已导出流程图谱抽检表：",
        export_process_graph_review(run_id, output_dir),
    )


def run_export_policy_workflow_graph(run_id: str, output_path: str) -> None:
    """导出指定流程抽取运行的已审核办事流程图谱 JSON。"""
    from policy.workflow_graph import export_policy_workflow_graph

    path = export_policy_workflow_graph(run_id, output_path)
    print(f"已导出办事流程图谱：{path}")


def run_report_policy_quality(output_dir: str) -> None:
    """读取已回填的审核表并生成质量报告。"""
    from policy.quality import write_quality_report

    _print_quality_outputs(
        "已生成质量报告：",
        write_quality_report(output_dir),
    )


def print_core_help() -> None:
    """打印面向首次使用者的制度图谱核心操作指南。"""
    print(
        """
制度文件知识图谱：三步上手

目标：把一批规章制度 PDF 导入系统，再抽取每条制度中的实体和关系。

第 1 步：导入 PDF（命令结束后记下输出的 batch_id）
  python main.py --process-dir "D:\\policies"

第 2 步：抽取实体和关系（把上一步的 batch_id 填入；首次建议限制条款数）
  python main.py --extract-policy-batch batch_xxxxxxxxxxxxxxxx --limit 10
  命令结束后记下输出的 policy_run_id。

第 3 步：审核并导出结果
  python main.py --review-policy-run policy_run_xxxxxxxxxxxxxxxx --reviewer 你的姓名
  python main.py --export-policy-graph-review policy_run_xxxxxxxxxxxxxxxx --quality-output "D:\\policy_quality"

补充：处理单个 PDF 可用 --process "D:\\policies\\示例.pdf"。
办事流程图谱、断点恢复、质量报告和条款检索属于进阶功能，请运行：
  python main.py --help-all
        """.strip()
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="PDF 多模态提取入仓工具（运行 --help 查看核心操作）",
        add_help=False,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="显示 PDF 导入和实体关系抽取的三步指南",
    )
    parser.add_argument(
        "--help-all",
        action="help",
        help="显示全部命令和进阶功能",
    )

    pdf_group = parser.add_argument_group("PDF 导入")
    entity_graph_group = parser.add_argument_group("实体和关系抽取")
    workflow_group = parser.add_argument_group("办事流程图谱（进阶）")
    recovery_group = parser.add_argument_group("状态、恢复与条款检索（进阶）")
    quality_group = parser.add_argument_group("抽检与质量报告（进阶）")

    pdf_group.add_argument(
        "--process",
        type=str,
        help="处理单个 PDF 文件的路径"
    )
    pdf_group.add_argument(
        "--backend",
        type=str,
        choices=["cloudmineru", "pymupdf"],
        default=None,
        help="指定解析后端：cloudmineru或pymupdf，默认从 config 读取"
    )
    pdf_group.add_argument(
        "--process-dir",
        type=str,
        help="递归创建并处理目录中的全部 PDF"
    )
    recovery_group.add_argument(
        "--resume-batch",
        type=str,
        help="恢复指定批次，只处理未完成或到期重试的文件"
    )
    recovery_group.add_argument(
        "--batch-status",
        type=str,
        help="查看指定批次状态，不执行文件"
    )
    entity_graph_group.add_argument(
        "--extract-policy-batch",
        type=str,
        help="为指定 PDF 批次创建并执行制度实体关系抽取"
    )
    entity_graph_group.add_argument(
        "--limit",
        type=int,
        help="仅抽取前 N 条实质性条款；可与两种实体关系抽取命令一起使用"
    )
    recovery_group.add_argument(
        "--resume-policy-extraction",
        type=str,
        help="恢复指定的制度实体关系抽取运行"
    )
    recovery_group.add_argument(
        "--policy-extraction-status",
        type=str,
        help="查看指定制度实体关系抽取运行状态"
    )
    recovery_group.add_argument(
        "--rebuild-policy-clause-index",
        type=str,
        help="为指定已结构化 PDF 批次建立跨制度条款索引",
    )
    recovery_group.add_argument(
        "--resume-policy-clause-index",
        type=str,
        help="恢复指定条款索引运行中的失败制度",
    )
    recovery_group.add_argument(
        "--policy-clause-index-status",
        type=str,
        help="查看指定跨制度条款索引运行状态",
    )
    entity_graph_group.add_argument(
        "--review-policy-run",
        type=str,
        help="在终端逐项人工审核指定抽取运行的实体和关系候选"
    )
    entity_graph_group.add_argument(
        "--extract-policy-abolition-relations",
        type=str,
        help="从指定已结构化 PDF 批次抽取待审核的制度废止关系",
    )
    recovery_group.add_argument(
        "--policy-abolition-relation-status",
        type=str,
        help="查看指定批次的制度废止关系审核状态",
    )
    entity_graph_group.add_argument(
        "--review-policy-abolition-relations",
        type=str,
        help="在终端审核指定批次的制度废止关系候选",
    )
    entity_graph_group.add_argument(
        "--abolition-reviewer",
        type=str,
        help="废止关系审核人的姓名或账号；只可与 --review-policy-abolition-relations 一起使用",
    )
    entity_graph_group.add_argument(
        "--abolition-review-limit",
        type=int,
        default=None,
        help="本次最多显示多少条待审核废止关系，默认 20；只可与 --review-policy-abolition-relations 一起使用",
    )
    entity_graph_group.add_argument(
        "--reviewer",
        type=str,
        help="人工审核人的姓名或账号；只可与 --review-policy-run 一起使用"
    )
    entity_graph_group.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="本次最多显示多少条待审核候选，默认 20；只可与 --review-policy-run 一起使用"
    )
    workflow_group.add_argument(
        "--classify-policy-process-batch",
        type=str,
        help="为指定 PDF 批次创建并执行流程条款分流",
    )
    workflow_group.add_argument(
        "--policy-domains",
        type=str,
        help="流程分流的事项域代码，逗号分隔；省略则使用目录中全部启用事项域",
    )
    workflow_group.add_argument(
        "--resume-policy-process",
        type=str,
        help="恢复指定流程条款分流运行中的失败条款",
    )
    workflow_group.add_argument(
        "--policy-process-status",
        type=str,
        help="查看指定流程条款分流运行状态",
    )
    workflow_group.add_argument(
        "--extract-policy-process-run",
        type=str,
        help="只从指定流程判定运行的已确认条款抽取实体和关系",
    )
    quality_group.add_argument(
        "--export-policy-clause-review",
        type=str,
        help="导出指定批次的制度条款人工抽检 CSV"
    )
    entity_graph_group.add_argument(
        "--export-policy-graph-review",
        type=str,
        help="导出指定抽取运行的实体关系人工抽检 CSV"
    )
    workflow_group.add_argument(
        "--export-policy-process-review",
        type=str,
        help="导出指定流程判定运行的流程条款人工抽检 CSV",
    )
    workflow_group.add_argument(
        "--export-policy-process-graph-review",
        type=str,
        help="导出指定流程图谱抽取运行的实体关系人工抽检 CSV",
    )
    workflow_group.add_argument(
        "--export-policy-workflow-graph",
        type=str,
        help="导出指定流程抽取运行的已审核办事流程图谱 JSON",
    )
    workflow_group.add_argument(
        "--workflow-graph-output",
        type=str,
        help="办事流程图谱 JSON 的输出文件；只可与 --export-policy-workflow-graph 一起使用",
    )
    quality_group.add_argument(
        "--report-policy-quality",
        type=str,
        help="读取指定目录中回填的抽检 CSV，生成质量报告"
    )
    quality_group.add_argument(
        "--quality-output",
        type=str,
        help="条款或图谱抽检 CSV 的输出目录"
    )
    quality_group.add_argument(
        "--clauses-per-policy",
        type=int,
        default=10,
        help="每份制度均匀抽取的条款数，默认 10；只可与 --export-policy-clause-review 一起使用"
    )

    args = parser.parse_args()

    if args.help:
        print_core_help()
        raise SystemExit(0)

    selected_modes = [
        bool(args.process),
        bool(args.process_dir),
        bool(args.resume_batch),
        bool(args.batch_status),
        bool(args.extract_policy_batch),
        bool(args.classify_policy_process_batch),
        bool(args.resume_policy_process),
        bool(args.policy_process_status),
        bool(args.extract_policy_process_run),
        bool(args.resume_policy_extraction),
        bool(args.policy_extraction_status),
        bool(args.rebuild_policy_clause_index),
        bool(args.resume_policy_clause_index),
        bool(args.policy_clause_index_status),
        bool(args.review_policy_run),
        bool(args.extract_policy_abolition_relations),
        bool(args.policy_abolition_relation_status),
        bool(args.review_policy_abolition_relations),
        bool(args.export_policy_clause_review),
        bool(args.export_policy_graph_review),
        bool(args.export_policy_process_review),
        bool(args.export_policy_process_graph_review),
        bool(args.export_policy_workflow_graph),
        bool(args.report_policy_quality),
    ]
    if sum(selected_modes) > 1:
        parser.error("处理、批次和制度抽取参数只能选择一个")
    if args.limit is not None and not (args.extract_policy_batch or args.extract_policy_process_run):
        parser.error("--limit 只能与制度实体关系抽取参数一起使用")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于等于 1")
    if args.policy_domains is not None and not args.classify_policy_process_batch:
        parser.error("--policy-domains 只能与 --classify-policy-process-batch 一起使用")
    selected_domains = None
    if args.policy_domains is not None:
        selected_domains = [item.strip() for item in args.policy_domains.split(",") if item.strip()]
        if not selected_domains:
            parser.error("--policy-domains 至少需要一个事项域代码")
    if args.workflow_graph_output and not args.export_policy_workflow_graph:
        parser.error("--workflow-graph-output 只能与 --export-policy-workflow-graph 一起使用")
    if args.export_policy_workflow_graph and not args.workflow_graph_output:
        parser.error("--export-policy-workflow-graph 必须提供 --workflow-graph-output")
    if args.reviewer and not args.review_policy_run:
        parser.error("--reviewer 只能与 --review-policy-run 一起使用")
    if args.review_policy_run and not args.reviewer:
        parser.error("--review-policy-run 必须提供 --reviewer")
    if args.abolition_reviewer and not args.review_policy_abolition_relations:
        parser.error("--abolition-reviewer 只能与 --review-policy-abolition-relations 一起使用")
    if args.review_policy_abolition_relations and not str(args.abolition_reviewer or "").strip():
        parser.error("--review-policy-abolition-relations 必须提供非空 --abolition-reviewer")
    if args.abolition_review_limit is not None:
        if args.abolition_review_limit < 1:
            parser.error("--abolition-review-limit 必须大于等于 1")
        if not args.review_policy_abolition_relations:
            parser.error("--abolition-review-limit 只能与 --review-policy-abolition-relations 一起使用")
    if args.review_limit < 1:
        parser.error("--review-limit 必须大于等于 1")
    if args.clauses_per_policy < 1:
        parser.error("--clauses-per-policy 必须大于等于 1")
    if args.quality_output and not (
        args.export_policy_clause_review
        or args.export_policy_graph_review
        or args.export_policy_process_review
        or args.export_policy_process_graph_review
    ):
        parser.error("--quality-output 只能与质检表导出参数一起使用")
    if args.clauses_per_policy != 10 and not args.export_policy_clause_review:
        parser.error("--clauses-per-policy 只能与 --export-policy-clause-review 一起使用")
    if (
        args.export_policy_clause_review
        or args.export_policy_graph_review
        or args.export_policy_process_review
        or args.export_policy_process_graph_review
    ) and not args.quality_output:
        parser.error("导出质检表必须提供 --quality-output")

    if args.process_dir:
        run_process_dir(args.process_dir, backend=args.backend)
    elif args.resume_batch:
        run_resume_batch(args.resume_batch)
    elif args.batch_status:
        run_batch_status(args.batch_status)
    elif args.extract_policy_batch:
        run_extract_policy_batch(args.extract_policy_batch, limit=args.limit)
    elif args.classify_policy_process_batch:
        run_classify_policy_process_batch(
            args.classify_policy_process_batch,
            selected_domains=selected_domains,
        )
    elif args.resume_policy_process:
        run_resume_policy_process(args.resume_policy_process)
    elif args.policy_process_status:
        run_policy_process_status(args.policy_process_status)
    elif args.extract_policy_process_run:
        run_extract_policy_process_run(args.extract_policy_process_run, limit=args.limit)
    elif args.resume_policy_extraction:
        run_resume_policy_extraction(args.resume_policy_extraction)
    elif args.policy_extraction_status:
        run_policy_extraction_status(args.policy_extraction_status)
    elif args.rebuild_policy_clause_index:
        run_rebuild_policy_clause_index(args.rebuild_policy_clause_index)
    elif args.resume_policy_clause_index:
        run_resume_policy_clause_index(args.resume_policy_clause_index)
    elif args.policy_clause_index_status:
        run_policy_clause_index_status(args.policy_clause_index_status)
    elif args.review_policy_run:
        run_policy_review(args.review_policy_run, args.reviewer, args.review_limit)
    elif args.extract_policy_abolition_relations:
        run_extract_policy_abolition_relations(args.extract_policy_abolition_relations)
    elif args.policy_abolition_relation_status:
        run_policy_abolition_relation_status(args.policy_abolition_relation_status)
    elif args.review_policy_abolition_relations:
        run_policy_abolition_review(
            args.review_policy_abolition_relations,
            args.abolition_reviewer,
            args.abolition_review_limit or 20,
        )
    elif args.export_policy_clause_review:
        run_export_policy_clause_review(
            args.export_policy_clause_review,
            args.quality_output,
            args.clauses_per_policy,
        )
    elif args.export_policy_graph_review:
        run_export_policy_graph_review(
            args.export_policy_graph_review,
            args.quality_output,
        )
    elif args.export_policy_process_review:
        run_export_policy_process_review(
            args.export_policy_process_review,
            args.quality_output,
        )
    elif args.export_policy_process_graph_review:
        run_export_policy_process_graph_review(
            args.export_policy_process_graph_review,
            args.quality_output,
        )
    elif args.export_policy_workflow_graph:
        run_export_policy_workflow_graph(
            args.export_policy_workflow_graph,
            args.workflow_graph_output,
        )
    elif args.report_policy_quality:
        run_report_policy_quality(args.report_policy_quality)
    elif args.process:
        run_process(args.process, backend=args.backend)
    else:
        run_api()

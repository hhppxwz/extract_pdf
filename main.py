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
import fitz
import datetime

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

    args = parser.parse_args()

    if args.process:
        run_process(args.process, backend=args.backend)
    else:
        run_api()
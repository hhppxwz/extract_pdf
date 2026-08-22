"""
PDF 多模态提取入仓 —— 启动入口
使用方式：
    python main.py                    # 启动 FastAPI 服务
    python main.py --process <pdf>    # 命令行处理单个 PDF
"""
import sys
import os

# 确保工作目录为项目根目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def run_api():
    """启动 FastAPI 服务"""
    import uvicorn
    from api import app
    print("=" * 60)
    print("PDF 多模态提取入仓服务")
    print("=" * 60)
    print(f"Mock 模式: {os.getenv('MOCK_MODE', 'false')}")
    print("启动 FastAPI 服务于 http://127.0.0.1:8000")
    print("API 文档: http://127.0.0.1:8000/docs")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000)


def run_process(pdf_path: str):
    """命令行处理单个 PDF"""
    from pipeline import process_pdf
    print(f"正在处理: {pdf_path}")
    result = process_pdf(pdf_path)
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
    if len(sys.argv) > 1 and sys.argv[1] == "--process":
        if len(sys.argv) < 3:
            print("用法: python main.py --process <pdf文件路径>")
            sys.exit(1)
        run_process(sys.argv[2])
    else:
        run_api()

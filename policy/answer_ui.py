"""制度问答页面资源拼装。"""
from pathlib import Path


_ASSET_DIR = Path(__file__).parent / "web_ui"


def render_policy_qa_page() -> str:
    """返回无需前端构建工具即可使用的制度问答页面。"""
    template = (_ASSET_DIR / "answer.html").read_text(encoding="utf-8")
    style = (_ASSET_DIR / "answer.css").read_text(encoding="utf-8")
    script = (_ASSET_DIR / "clause_tree.js").read_text(encoding="utf-8") + "\n" + (
        _ASSET_DIR / "answer.js").read_text(encoding="utf-8")
    return template.replace("{{STYLE}}", style).replace("{{SCRIPT}}", script)

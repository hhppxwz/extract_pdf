# parsers/cloud_mineru.py
import time
import hashlib
import uuid
import logging
import httpx
import io
import zipfile
import re
import json as json_lib
import os
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from models import BlockType, ContentBlock, MinerUTaskStatus
from config import app_config, CloudMinerUConfig
from parsers.base import ParserStrategy

logger = logging.getLogger(__name__)


class CloudMineruParser(ParserStrategy):
    """
    MinerU API 统一客户端
    支持 mode='agent' 和 mode='v4'（精准解析）
    继承 ParserStrategy 接口
    """

    def __init__(
            self,
            config: Optional[CloudMinerUConfig] = None,
            mode: Optional[str] = 'v4',  # 'agent' 或 'v4'
    ):
        self.config = config or app_config.cloudmineru
        self.mode = mode or getattr(self.config, "mode", "agent")
        self._client: Optional[httpx.Client] = None

        if self.mode == "v4":
            self.base_url = self.config.batch_api_url.rstrip("/")
        else:
            self.base_url = self.config.api_url.rstrip("/")

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {}
            if self.mode == "v4" and self.config.batch_api_key:
                headers["Authorization"] = f"Bearer {self.config.batch_api_key}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(120.0),
            )
        return self._client

    # ========== 提交任务 ==========
    def submit(self, file_path: str) -> str:
        """提交 PDF 文件，返回 task_id"""
        if self.mode == "v4":
            return self._submit_v4(file_path)
        else:
            return self._submit_agent(file_path)

    def _submit_agent(self, file_path: str) -> str:
        """Agent API 两步提交"""
        file_name = Path(file_path).name
        init_data = {
            "file_name": file_name,
            "language": "ch",
            "enable_table": True,
            "is_ocr": False,
            "enable_formula": True,
        }
        resp = self.client.post("/parse/file", json=init_data)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"初始化上传失败: {result.get('msg')}")
        data_field = result.get("data", {})
        task_id = data_field.get("task_id")
        file_url = data_field.get("file_url")
        if not task_id or not file_url:
            raise RuntimeError(f"获取上传信息失败: {result}")
        with open(file_path, "rb") as f:
            file_content = f.read()
        resp_upload = httpx.put(file_url, content=file_content, timeout=60.0)
        resp_upload.raise_for_status()
        return task_id

    def _submit_v4(self, file_path: str) -> str:
        """v4 API 批量上传方式"""
        file_name = Path(file_path).name
        batch_data = {
            "files": [{"name": file_name}],
            "model_version": "vlm",
        }
        resp = self.client.post("", json=batch_data)
        resp.raise_for_status()
        data = resp.json()
        batch_data = data.get("data", {})
        file_urls = batch_data.get("file_urls", [])
        if not file_urls:
            raise RuntimeError(f"获取上传链接失败: {data}")
        file_url = file_urls[0]
        with open(file_path, "rb") as f:
            file_content = f.read()
        resp_upload = httpx.put(file_url, content=file_content, timeout=120.0)
        resp_upload.raise_for_status()
        batch_id = batch_data.get("batch_id")
        if not batch_id:
            raise RuntimeError(f"未返回 batch_id: {data}")
        return batch_id

    # ========== 状态查询 ==========
    def get_status(self, task_id: str) -> MinerUTaskStatus:
        """查询任务状态"""
        if self.mode == "v4":
            return self._get_status_v4(task_id)
        else:
            return self._get_status_agent(task_id)

    def _get_status_agent(self, task_id: str) -> MinerUTaskStatus:
        resp = self.client.get(f"/parse/{task_id}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"agent查询状态失败: {data.get('msg')}")
        task_data = data.get("data", {})
        status=task_data.get("state")
        if status == "done":
            return MinerUTaskStatus(
                task_id=task_data.get("task_id"),
                status=task_data.get("state"),
                progress=task_data.get("progress", 0.0),
                markdown_url=task_data.get("markdown_url"),
                #result=task_data.get("result"),
            )
        elif status == 'failed':
            return MinerUTaskStatus(
                task_id=task_data.get("task_id"),
                status=task_data.get("state"),
                progress=task_data.get("progress", 0.0),
                err_code=task_data.get("err_code"),
                err_msg=task_data.get("err_msg"),
            )
        else:
            return MinerUTaskStatus(
                task_id=task_data.get("task_id"),
                status=task_data.get("state"),
                progress=task_data.get("progress", 0.0),
            )




    def _get_status_v4(self, task_id: str) -> MinerUTaskStatus:
        resp = self.client.get(f"https://mineru.net/api/v4/extract-results/batch/{task_id}")

        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"v4 查询状态失败: {data.get('msg')}")
        batch_data = data.get("data", {})
        results = batch_data.get("extract_result", [])
        if not results:
            return MinerUTaskStatus(
                task_id=task_id,
                status="processing",
                progress=0.0,
                result=None,
                error="",
                zip_url=None,
            )
        if isinstance(results, list):
            #print("正常返回列表")
            first = results[0] if results else {}
        else:
            #print("并非列表！！！")
            first = results or {}


        state = first.get("state", "unknown")
        err_msg = first.get("err_msg", "")
        full_zip_url = first.get("full_zip_url")
        if state == "done":
            progress = 100.0
        elif state in ("running", "processing"):
            progress = 50.0
        else:
            progress = 0.0
        return MinerUTaskStatus(
            task_id=task_id,
            status=state,
            progress=progress,
            result=None,
            error=err_msg,
            markdown_url=None,
            zip_url=full_zip_url,
        )

    # ========== 轮询==========
    def poll_until_done(
            self,
            task_id: str,
            timeout_seconds: Optional[float] = None
    ) -> MinerUTaskStatus:
        start = time.time()
        while True:
            status = self.get_status(task_id)
            if status.status in ("done", "success", "failed"):
                return status
            elapsed = time.time() - start
            if timeout_seconds and elapsed > timeout_seconds:
                raise TimeoutError(f"任务 {task_id} 超时")
            time.sleep(self.config.poll_interval)

    # ========== Mojibake 检测与修复 ==========
    @staticmethod
    def _is_mojibake(text: str) -> bool:
        exact_patterns = [
            r'â\x88[\x80-\x9f]',
            r'â\x80[\x90-\xbf]',
            r'Â[\x80-\xbf]',
            r'â\x81[\x80-\xbf]',
        ]
        count = sum(len(re.findall(p, text)) for p in exact_patterns)
        if count >= 3:
            return True
        if len(text) > 100:
            a_circumflex = text.count('â')
            a_cap = text.count('Â')
            total = len(text)
            if (a_circumflex + a_cap) / total > 0.005:
                return True
        return False

    @staticmethod
    def _looks_better(fixed: str, original: str) -> bool:
        """比较修复后是否看起来更好（简单启发式）"""
        return fixed.count('�') < original.count('�')

    # ========== 清洗函数 ==========
    def clean_html_tags(self, text):
        if isinstance(text, str):
            return re.sub(r'</?(sup|sub)>', '', text, flags=re.IGNORECASE)
        return text

    def clean_json_data(self, obj, skip_keys={'url', 'doi', 'link', 'id', 'ee'}):
        if isinstance(obj, dict):
            new_dict = {}
            for key, value in obj.items():
                if key.lower() in skip_keys:
                    new_dict[key] = value
                else:
                    new_dict[key] = self.clean_json_data(value, skip_keys)
            return new_dict
        elif isinstance(obj, list):
            return [self.clean_json_data(item, skip_keys) for item in obj]
        elif isinstance(obj, str):
            return self.clean_html_tags(obj)
        else:
            return obj

    # ========== ZIP 下载与解析 ==========
    def _download_and_extract_markdown(self, zip_url: str) -> Tuple[str, List[Dict], Any, Any]:
        """下载 zip 包，解压后读取 markdown 文件和图片"""
        print(f"[CloudMinerU] 下载结果zip: {zip_url}")
        logger.debug(f"[CloudMinerU] 下载结果zip: {zip_url}")
        zip_resp = httpx.get(zip_url, timeout=60.0)
        zip_resp.raise_for_status()

        # DEBUG 保存（调试逻辑）
        debug_save_zip = os.environ.get("CLOUDMINERU_SAVE_ZIP", "0") == "1"
        if debug_save_zip:
            debug_zip_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_zips")
            os.makedirs(debug_zip_dir, exist_ok=True)
            zip_hash = hashlib.md5(zip_url.encode()).hexdigest()[:12]
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf_debug:
                md_files = [f for f in zf_debug.namelist() if f.endswith(".md")]
                zip_name = os.path.basename(md_files[0].replace(".md", "")) if md_files else zip_hash
            debug_zip_path = os.path.join(debug_zip_dir, f"{zip_hash}_{zip_name}.zip")
            with open(debug_zip_path, "wb") as f:
                f.write(zip_resp.content)
            logger.debug(f"[CloudMinerU] DEBUG: 保存原始zip到 {debug_zip_path}")

        content_list_data = None
        content_list_v2_data = None

        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            all_names = zf.namelist()
            logger.debug(f"[CloudMinerU] zip内文件列表: {all_names}")

            # 读取 Markdown
            md_files = [f for f in all_names if f.endswith(".md")]
            if not md_files:
                text_files = [f for f in all_names if f.endswith((".txt", ".markdown"))]
                if text_files:
                    md_files = text_files
                else:
                    raise RuntimeError(f"zip中未找到.md文件: {all_names}")

            raw_bytes = None
            for candidate in ["markdown.md", "content.md", "result.md"]:
                if candidate in md_files:
                    raw_bytes = zf.read(candidate)
                    break
            if raw_bytes is None:
                raw_bytes = zf.read(md_files[0])

            text = raw_bytes.decode("utf-8", errors="replace")
            is_mojibake = self._is_mojibake(text)
            if is_mojibake:
                try:
                    fixed = text.encode("latin-1").decode("utf-8", errors="replace")
                    if self._looks_better(fixed, text):
                        text = fixed
                        logger.debug("[CloudMinerU] Mojibake 修复成功")
                except Exception as e:
                    logger.warning(f"[CloudMinerU] Mojibake 修复失败: {e}")

            # 清理 HTML 标签
            text = re.sub(r'</?(sup|sub)>', '', text, flags=re.IGNORECASE)

            # 读取 content_list.json
            cl_files = [n for n in all_names if n.endswith("content_list.json") and "result" not in n]
            cl_v2_files = [n for n in all_names if n.endswith("content_list_v2.json") and "result" not in n]

            for cl_file in cl_files:
                try:
                    cl_bytes = zf.read(cl_file)
                    content_list_data = json_lib.loads(cl_bytes.decode("utf-8", errors="replace"))
                    content_list_data = self.clean_json_data(content_list_data)
                    break
                except Exception as e:
                    logger.warning(f"[CloudMinerU] {cl_file} 解析失败: {e}")

            for cl_v2_file in cl_v2_files:
                try:
                    cl_v2_bytes = zf.read(cl_v2_file)
                    content_list_v2_data = json_lib.loads(cl_v2_bytes.decode("utf-8", errors="replace"))
                    content_list_v2_data = self.clean_json_data(content_list_v2_data)
                    break
                except Exception as e:
                    logger.warning(f"[CloudMinerU] {cl_v2_file} 解析失败: {e}")

            # 构建 image_meta_map
            image_meta_map = {}
            all_blocks_for_img = []
            if content_list_data:
                if isinstance(content_list_data, list):
                    if content_list_data and isinstance(content_list_data[0], list):
                        for page_blocks in content_list_data:
                            all_blocks_for_img.extend(page_blocks)
                    else:
                        all_blocks_for_img = content_list_data
            if not all_blocks_for_img and content_list_v2_data:
                if isinstance(content_list_v2_data, list):
                    if content_list_v2_data and isinstance(content_list_v2_data[0], list):
                        for page_blocks in content_list_v2_data:
                            all_blocks_for_img.extend(page_blocks)
                    else:
                        all_blocks_for_img = content_list_v2_data

            for block in all_blocks_for_img:
                img_path = block.get("img_path", "")
                if not img_path:
                    continue
                btype = block.get("type", "")
                if btype == "chart":
                    cap_field = "chart_caption"
                elif btype == "table":
                    cap_field = "table_caption"
                else:
                    cap_field = "image_caption"
                cap = block.get(cap_field)
                if isinstance(cap, list):
                    caption_text = " ".join(c for c in cap if c and c.strip()) if cap else ""
                else:
                    caption_text = str(cap or "")
                page_idx = block.get("page_idx", 0)
                image_meta_map[img_path] = {
                    "page_num": page_idx + 1,
                    "caption": caption_text.strip(),
                    "type": btype,
                }

            # 提取图片
            images_info = []
            image_names_in_zip = [
                n for n in all_names
                if (n.startswith("images/") or "/images/" in n)
                   and n.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"))
            ]
            for img_name in image_names_in_zip:
                meta = image_meta_map.get(img_name, {})
                caption = meta.get("caption", "")
                btype = meta.get("type", "unknown")
                try:
                    raw_bytes = zf.read(img_name)
                    images_info.append({
                        "image_name": img_name,
                        "raw_bytes": raw_bytes,
                        "page_num": meta.get("page_num", 0),
                        "caption": caption,
                        "type": btype,
                    })
                except Exception as e:
                    logger.warning(f"[CloudMinerU] 读取图片 {img_name} 失败: {e}")

            logger.info(f"[CloudMinerU] 保留所有图片: {len(images_info)} 张")
            return text, images_info, content_list_data, content_list_v2_data


    # ========== 主解析入口（返回 List[ContentBlock]） ==========
    def parse(self, file_path: str, page_count: int = 0) -> List[ContentBlock]:
        """
        同步解析 PDF，返回 ContentBlock 列表
        这是实现 ParserStrategy 接口的核心方法
        """
        task_id = self.submit(file_path)
        timeout = max(60, page_count * self.config.timeout_per_page) if page_count else 300
        try:
            status = self.poll_until_done(task_id, timeout_seconds=timeout)
        except RuntimeError as e:
            print(f"捕获到异常: {e}")
            # 或者打印完整堆栈
            import traceback
            traceback.print_exc()

        #if status.status in ("failed", "error"):
         #   raise RuntimeError(f"解析失败: {getattr(status, 'error', '未知错误')}")

        blocks = []

        if self.mode == "v4":
            print("当前为v4 batch模式")
            if hasattr(status, "zip_url") and status.zip_url:
                markdown, images_info, content_list_data, content_list_v2_data = \
                    self._download_and_extract_markdown(status.zip_url)

                for item in content_list_data:
                    item_type = item.get("type", "text")

                    # 1. 提取纯文本内容（不同字段名兼容）
                    content = item.get("text") or ""

                    # 2. 映射 BlockType
                    if item_type == "table":
                        block_type = BlockType.TABLE
                        # 表格内容优先取 table_body，否则取 content
                        if item.get("table_body"):
                            content = item.get("table_body")
                    elif item_type in ("image", "figure", "chart"):
                        block_type = BlockType.IMAGE
                    else:
                        block_type = BlockType.TEXT

                    # 3. 构造 ContentBlock，挂载 raw
                    block = ContentBlock(
                        block_id=f"blk_{hashlib.md5(f'{file_path}_{item.get("page_idx", 0)}_{item_type}'.encode()).hexdigest()[:12]}",
                        type=block_type,
                        page_num=item.get("page_idx", 0),
                        content=content,
                        metadata={
                            "bbox": item.get("bbox"),
                            "page_idx": item.get("page_idx"),
                        },
                    )
                    # ===== 【核心】挂载原始 JSON =====
                    block.raw = item
                    # ================================

                    blocks.append(block)


            else:
                raise RuntimeError("zip_url错误")
        else:
            # agent 模式
            print("当前为agent模式")

            if page_count>20:
                raise RuntimeError("pdf总页数超过agent最大限制20页，请使用v4")

            if status.err_code != None:
                raise RuntimeError(f"agent未解析成功"
                                   f"错误码：{status.err_code} "
                                   f"错误信息：{status.err_msg}")



            print(f"返回md链接：{status.markdown_url}")
            resp_md = httpx.get(status.markdown_url, timeout=30.0)
            resp_md.raise_for_status()
            md_content = resp_md.text
            blocks.append(ContentBlock(
                block_id=f"blk_{hashlib.md5(file_path.encode()).hexdigest()[:12]}",
                type=BlockType.TEXT,
                page_num=0,
                content=md_content,
            ))

        return blocks

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


# ========== Mock 客户端 ==========
class MockCloudMineruParser(CloudMineruParser):
    """mock 客户端，返回模拟的解析结果，用于测试"""

    def __init__(self, config: Optional[CloudMinerUConfig] = None):
        super().__init__(config=config, mode="agent")

    def submit(self, file_path: str) -> str:
        return f"mock_task_{uuid.uuid4().hex[:8]}"

    def get_status(self, task_id: str) -> MinerUTaskStatus:
        return MinerUTaskStatus(
            task_id=task_id,
            status="processing",
            progress=50.0,
        )

    def parse(self, file_path: str, page_count: int = 0) -> List[ContentBlock]:
        return [
            ContentBlock(
                block_id="blk_mock_text_001",
                type=BlockType.TEXT,
                page_num=0,
                content="这是一段模拟的 PDF 文本内容，用于测试文本分块和 Embedding 流程。",
            ),
            ContentBlock(
                block_id="blk_mock_dtable_001",
                type=BlockType.TABLE,
                page_num=1,
                content="学号 姓名 成绩\n001 张三 85\n002 李四 92\n合计 177\n",
                table_html="<table><tr><td>学号</td><td>姓名</td><td>成绩</td></tr>..."
            ),
            ContentBlock(
                block_id="blk_mock_img_001",
                type=BlockType.IMAGE,
                page_num=0,
                content="https://example.com/mock_image.png",
            ),
        ]
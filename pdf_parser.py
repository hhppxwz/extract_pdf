import time
import hashlib
import uuid
from typing import Optional, Dict, Any ,Tuple,List
from pathlib import Path
import logging
import httpx
import io
import zipfile
import re

from config import app_config, CloudMinerUConfig
from models import (
    BlockType,
    ContentBlock,
    MinerUBlock,
    MinerUPage,
    MinerUResponse,
    MinerUTaskStatus,
)
logger = logging.getLogger(__name__)

"""
在这里将pdf解析
可选：
cloudmineru  batch 模式（当前主力，返回 content_list.json 最丰富）
cloudmineru  agent 模式（轻量场景）
PyMuPDF （离线场景、简单 PDF）
"""
class CloudMinerUClient:
    """
    MinerU API 统一客户端
    支持 mode='agent' (v1) 和 mode='v4' (精准解析)
    """

    def __init__(
        self,
        config: Optional[CloudMinerUConfig] = None,
        mode: Optional[str] = 'v4',  # 'agent' 或 'v4'
    ):
        self.config = config or app_config.cloudmineru
        self.mode = mode or getattr(self.config, "mode", "agent")  # 从配置读取或默认agent
        self._client: Optional[httpx.Client] = None

        # 根据 mode 设置基础URL（若配置中未区分，可在此补全）
        if self.mode == "v4":
            self.base_url = self.config.batch_api_url.rstrip("/")  # 如 "https://mineru.net/api/v4"
        else:  # agent
            self.base_url = self.config.api_url.rstrip("/")  # 如 "https://mineru.net/api/v1/agent"

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {}
            # v4 需要 Authorization，agent 不需要
            if self.mode == "v4" and self.config.batch_api_key:
                headers["Authorization"] = f"Bearer {self.config.batch_api_key}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(120.0),  # 适当延长超时
            )
        return self._client

    # -------------------- 提交任务 --------------------
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
        """v4 API 批量上传方式（适用于本地文件）"""
        file_name = Path(file_path).name

        # 步骤1：申请上传链接
        batch_data = {
            "files": [{"name": file_name}],
            "model_version": "vlm",
        }
        resp = self.client.post("", json=batch_data)
        resp.raise_for_status()
        data = resp.json()

        # 解析响应
        batch_data = data.get("data", {})
        file_urls = batch_data.get("file_urls", [])
        if not file_urls:
            raise RuntimeError(f"获取上传链接失败: {data}")

        file_url = file_urls[0]

        # 步骤2：用 PUT 上传文件
        with open(file_path, "rb") as f:
            file_content = f.read()
        resp_upload = httpx.put(file_url, content=file_content, timeout=120.0)
        resp_upload.raise_for_status()

        # 步骤3：获取 task_id（从 batch_id 轮询结果）
        batch_id = batch_data.get("batch_id")
        if not batch_id:
            raise RuntimeError(f"未返回 batch_id: {data}")

        # 注意：这里需要轮询 /extract-results/batch/{batch_id} 获取每个文件的 task_id
        # 简化处理：直接用 batch_id 作为标识
        return batch_id

    # -------------------- 查询状态 --------------------
    def get_status(self, task_id: str) -> MinerUTaskStatus:
        """查询任务状态"""
        if self.mode == "v4":
            return self._get_status_v4(task_id)
        else:
            return self._get_status_agent(task_id)

    def _get_status_agent(self, task_id: str) -> MinerUTaskStatus:
        resp = self.client.get(f"/parse/{task_id}")  # 或 "/parse/file?task_id=..."
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"查询状态失败: {data.get('msg')}")
        task_data = data.get("data", {})
        return MinerUTaskStatus(
            task_id=task_data.get("task_id"),
            status=task_data.get("state"),      # agent 用 state
            progress=task_data.get("progress", 0.0),
            markdown_url=task_data.get("markdown_url"),  # agent 特有
            result=task_data.get("result"),
        )

    def _get_status_v4(self, task_id: str) -> MinerUTaskStatus:
        resp = self.client.get(f"https://mineru.net/api/v4/extract-results/batch/{task_id}")
        resp.raise_for_status()
        data = resp.json()
        print("[DEBUG] v4 get_status response:", data)

        if data.get("code") != 0:
            raise RuntimeError(f"v4 查询失败: {data.get('msg')}")

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

        first = results[0]
        state = first.get("state", "unknown")
        err_msg = first.get("err_msg", "")
        full_zip_url = first.get("full_zip_url")

        # 估算进度
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
            result=None,  # 暂不设置，由 parse 阶段处理
            error=err_msg,
            markdown_url=None,
            zip_url=full_zip_url,  # 存放 ZIP 链接
        )
    # -------------------- 轮询 --------------------
    def poll_until_done(
        self, task_id: str, timeout_seconds: Optional[float] = None
    ) -> MinerUTaskStatus:
        start = time.time()
        while True:
            status = self.get_status(task_id)
            # 完成状态：done, success, failed
            if status.status in ("done", "success", "failed"):
                return status
            elapsed = time.time() - start
            if timeout_seconds and elapsed > timeout_seconds:
                raise TimeoutError(f"任务 {task_id} 超时")
            time.sleep(self.config.poll_interval)

    @staticmethod
    def _is_mojibake(text: str) -> bool:
        """
        检测是否为 UTF-8 被当 Latin-1 的 Mojibake 文本。

        典型症状：
          ∗ (U+2217) → â\x88\x97   (0xE2 0x88 0x97 被 Latin-1 读为 â ‡ —)
          † (U+2020) → â\x80\xa0   (0xE2 0x80 0xA0)
          — (U+2014) → â\x80\x94   (0xE2 0x80 0x94)
          ² (U+00B2) → Â²           (0xC2 0xB2)

        检测策略：查找典型的三字节 UTF-8 Mojibake 序列。
        """
        # 策略1：精确模式匹配（最可靠）
        exact_patterns = [
            r'â\x88[\x80-\x9f]',  # ∗†‡§ 等 (E2 88 XX)
            r'â\x80[\x90-\xbf]',  # —'"' 等 (E2 80 XX)
            r'Â[\x80-\xbf]',  # ²³¹º° 等 (C2 XX)
            r'â\x81[\x80-\xbf]',  # 其他常用符号 (E2 81 XX)
        ]
        count = sum(len(re.findall(p, text)) for p in exact_patterns)
        if count >= 3:
            return True

        # 策略2：统计 â 和 Â 出现频率（Mojibake 文本中这两个字符异常多）
        if len(text) > 100:
            a_circumflex = text.count('â')
            a_cap = text.count('Â')
            total = len(text)
            # 如果 â 或 Â 出现频率 > 0.5%，大概率是 Mojibake
            if (a_circumflex + a_cap) / total > 0.005:
                logger.debug(
                    f"[CloudMinerU] Mojibake检测(策略2): â={a_circumflex}, Â={a_cap}, 频率={(a_circumflex + a_cap) / total:.4f}")
                return True

        return False
    # ============================================================
    # 定义清洗函数 用于去除json文件中多余的html标签 sup/sub
    # ============================================================
    def clean_html_tags(self,text):
        if isinstance(text, str):
            return re.sub(r'</?(sup|sub)>', '', text, flags=re.IGNORECASE)
        return text

    def clean_json_data(self,obj, skip_keys={'url', 'doi', 'link', 'id', 'ee'}):
        """递归清洗 JSON 中的所有字符串，跳过 URL/DOI 等关键字段"""
        if isinstance(obj, dict):
            new_dict = {}
            for key, value in obj.items():
                # 如果字段名在跳过列表中，直接保留原值
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

    def _download_and_extract_markdown(self, zip_url: str) -> Tuple[str, List[Dict], Any, Any]:
        """
        下载 zip 包，解压后读取 markdown 文件和图片。

        Returns:
            Tuple[str, List[Dict], Any, Any]:
                - markdown 文本
                - 图片信息列表，每个 dict 包含:
                    image_name: zip里的文件名（如 images/image_0.png）
                    raw_bytes: 图片原始字节
                    page_num: 页码
                    caption: caption（如果有）
                - content_list_data: content_list.json 数据
                - content_list_v2_data: content_list_v2.json 数据
        """
        import json as json_lib
        import os

        logger.debug(f"[CloudMinerU] 下载结果zip: {zip_url}")
        zip_resp = httpx.get(zip_url, timeout=60.0)
        zip_resp.raise_for_status()

        # DEBUG: 保存原始zip到本地
        import os
        debug_save_zip = os.environ.get("CLOUDMINERU_SAVE_ZIP", "0") == "1"
        if debug_save_zip:
            debug_zip_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_zips")
            os.makedirs(debug_zip_dir, exist_ok=True)
            import hashlib
            zip_hash = hashlib.md5(zip_url.encode()).hexdigest()[:12]
            # 从zip内文件名反推 paper_id（取第一个md文件名去掉.md后缀）
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf_debug:
                md_files = [f for f in zf_debug.namelist() if f.endswith(".md")]
                zip_name = os.path.basename(md_files[0].replace(".md", "")) if md_files else zip_hash
            debug_zip_path = os.path.join(debug_zip_dir, f"{zip_hash}_{zip_name}.zip")
            with open(debug_zip_path, "wb") as f:
                f.write(zip_resp.content)
            logger.debug(f"[CloudMinerU] DEBUG: 保存原始zip到 {debug_zip_path}")

        # 保存 content_list.json 数据，供后续解析使用
        content_list_data = None
        content_list_v2_data = None

        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            all_names = zf.namelist()
            logger.debug(f"[CloudMinerU] zip内文件列表: {all_names}")

            # ── 1. 读取 Markdown ──────────────────────────────────────────
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

            # 编码修复
            text = raw_bytes.decode("utf-8", errors="replace")
            is_mojibake = self._is_mojibake(text)
            logger.debug(f"[CloudMinerU] Mojibake检测: {is_mojibake}, â={text.count('â')}, Â={text.count('Â')}")
            if is_mojibake:
                try:
                    fixed = text.encode("latin-1").decode("utf-8", errors="replace")
                    if self._looks_better(fixed, text):
                        text = fixed
                        logger.debug("[CloudMinerU] Mojibake 修复成功")
                except Exception as e:
                    logger.warning(f"[CloudMinerU] Mojibake 修复失败: {e}")
            # 清理<sub>和<sup>标签
            # 清理前，打印前 300 个字符看看
            print(f"[CloudMinerU] 清理前文本长度: {len(text)}")
            print(f"[预览-清理前]: {text[:300]}...\n")  # 只取前300字
            print(f"[CloudMinerU] 清理<sub>和<sup>标签前文本长度: {len(text)}")

            text = re.sub(r'</?(sup|sub)>', '', text, flags=re.IGNORECASE)

            # 清理后，再次打印前 300 个字符做对比
            print(f"[CloudMinerU] 清理后文本长度: {len(text)}")
            print(f"[预览-清理后]: {text[:300]}...")

            # ── LaTeX 残留清理 ─────────────────────────────────────────────
            #text = self._clean_latex_residue(text)

            # ── 2. 读取 content_list.json（扁平格式，推荐）───────────────
            # 同时读取 content_list_v2.json（按页分组格式，备用）
            cl_files = [n for n in all_names if n.endswith("content_list.json") and "result" not in n]
            cl_v2_files = [n for n in all_names if n.endswith("content_list_v2.json") and "result" not in n]

            for cl_file in cl_files:
                try:
                    cl_bytes = zf.read(cl_file)
                    content_list_data = json_lib.loads(cl_bytes.decode("utf-8", errors="replace"))
                    logger.debug(
                        f"[CloudMinerU] 读取 {cl_file}, {len(content_list_data) if isinstance(content_list_data, list) else '?'} 个block")
                    print(f"清洗前类型: {type(content_list_data)}")
                    print(f"清洗前内容预览: {str(content_list_data)[:200]}")
                    content_list_data = self.clean_json_data(content_list_data)
                    if isinstance(content_list_data, dict):
                        print(f"✅ 清洗完成，标题: {content_list_data.get('title', '')[:100]}")
                    elif isinstance(content_list_data, list) and len(content_list_data) > 0:
                        print(f"✅ 清洗完成，第一条标题: {content_list_data[0].get('title', '')[:100]}")
                    print(f"清洗后类型: {type(content_list_data)}")
                    print(f"清洗后内容预览: {str(content_list_data)[:200]}")

                    logger.debug(
                        f"[CloudMinerU] 读取 {cl_file}, {len(content_list_data) if isinstance(content_list_data, list) else '?'} 个block")
                    break  # 只取第一个
                except Exception as e:
                    logger.warning(f"[CloudMinerU] {cl_file} 解析失败: {e}")

            for cl_v2_file in cl_v2_files:
                try:
                    cl_v2_bytes = zf.read(cl_v2_file)
                    content_list_v2_data = json_lib.loads(cl_v2_bytes.decode("utf-8", errors="replace"))
                    content_list_v2_data = self.clean_json_data(content_list_v2_data)
                    logger.debug(
                        f"[CloudMinerU] 读取 {cl_v2_file}, {len(content_list_v2_data) if isinstance(content_list_v2_data, list) else '?'} 个block")

                    logger.debug(f"[CloudMinerU] 读取 {cl_v2_file}")
                    break
                except Exception as e:
                    logger.warning(f"[CloudMinerU] {cl_v2_file} 解析失败: {e}")

            # ── 3. 从 content_list.json / v2 读取图片 caption 和页码 ──
            # 统一展平为列表：[{type, img_path, image_caption, table_caption, page_idx}, ...]
            image_meta_map = {}  # img_path → {page_num, caption, type}
            all_blocks_for_img = []

            # 优先用 content_list.json（扁平格式）
            if content_list_data:
                if isinstance(content_list_data, list):
                    if content_list_data and isinstance(content_list_data[0], list):
                        # 按页分组格式，展平
                        for page_blocks in content_list_data:
                            all_blocks_for_img.extend(page_blocks)
                    else:
                        all_blocks_for_img = content_list_data

            # 备用：content_list_v2.json
            if not all_blocks_for_img and content_list_v2_data:
                if isinstance(content_list_v2_data, list):
                    if content_list_v2_data and isinstance(content_list_v2_data[0], list):
                        for page_blocks in content_list_v2_data:
                            all_blocks_for_img.extend(page_blocks)
                    else:
                        all_blocks_for_img = content_list_v2_data

            # 构建 image_meta_map
            for block in all_blocks_for_img:
                img_path = block.get("img_path", "")
                if not img_path:
                    continue
                btype = block.get("type", "")
                # caption 字段：chart 用 chart_caption，table 用 table_caption，其他用 image_caption
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
                    "page_num": page_idx + 1,  # 转成1-based页码
                    "caption": caption_text.strip(),
                    "type": btype,
                }
            logger.info(
                f"[CloudMinerU] image_meta_map: {len(image_meta_map)} 个, 有caption: {sum(1 for v in image_meta_map.values() if v['caption'])}")

            # ── 4. 提取图片文件（保留所有图片，不论是否有 caption）─────────────────
            images_info = []
            image_names_in_zip = [n for n in all_names
                                  if (n.startswith("images/") or "/images/" in n)
                                  and n.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"))]

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
                    logger.debug(
                        f"[CloudMinerU] 保留图片: {img_name}, type={btype}, caption: {caption[:50] if caption else '(无caption)'}")
                except Exception as e:
                    logger.warning(f"[CloudMinerU] 读取图片 {img_name} 失败: {e}")

            logger.info(
                f"[CloudMinerU] 保留所有图片: {len(images_info)} 张（有caption: {sum(1 for i in images_info if i['caption'])}, 无caption: {sum(1 for i in images_info if not i['caption'])}）")
            return text, images_info, content_list_data, content_list_v2_data

    # -------------------- 主解析入口 --------------------
    def parse(self, file_path: str, page_count: int = 0) -> list[ContentBlock]:
        """同步解析 PDF，返回 ContentBlock 列表"""
        task_id = self.submit(file_path)
        timeout = max(60, page_count * self.config.timeout_per_page) if page_count else 300
        status = self.poll_until_done(task_id, timeout_seconds=timeout)

        if status.status in ("failed", "error"):
            raise RuntimeError(f"解析失败: {getattr(status, 'error', '未知错误')}")

        # 根据不同 mode 提取内容
        if self.mode == "v4":
            # ===== 新增：优先处理 v4 返回的 ZIP 链接 =====
            if hasattr(status, "zip_url") and status.zip_url:
                # 调用新函数下载并提取 ZIP 中的所有内容
                markdown, images_info, content_list_data, content_list_v2_data = self._download_and_extract_markdown(
                    status.zip_url)
                return {
                    "markdown": markdown,
                    "images_info": images_info,
                    "content_list_data": content_list_data,
                    "content_list_v2_data": content_list_v2_data,
                    "api_result": status,
                }
            else:
                raise RuntimeError("zip_url错误")





        else:  # agent
            # agent 通过 markdown_url 下载内容
            if not getattr(status, "markdown_url", None):
                raise RuntimeError("agent 未返回 markdown_url")
            resp_md = httpx.get(status.markdown_url, timeout=30.0)
            resp_md.raise_for_status()
            md_content = resp_md.text
            # 将整个 Markdown 作为一个文本块
            return [
                ContentBlock(
                    block_id=f"blk_{hashlib.md5(file_path.encode()).hexdigest()[:12]}",
                    type=BlockType.TEXT,
                    page_num=0,
                    content=md_content,
                )
            ]

    # -------------------- 内部转换工具 --------------------
    def _convert_to_content_blocks(self, result: MinerUResponse) -> list[ContentBlock]:
        blocks = []
        for page in result.pages:
            for raw_block in page.blocks:
                block = ContentBlock(
                    block_id=self._generate_block_id(raw_block, page.page_num),
                    file_id="",
                    type=raw_block.type,
                    page_num=page.page_num,
                    bbox=raw_block.bbox,
                    content=raw_block.content,
                )
                blocks.append(block)
        return blocks

    def _generate_block_id(self, raw_block: MinerUBlock, page_num: int) -> str:
        seed = f"{page_num}:{raw_block.type}:{raw_block.bbox}:{raw_block.content[:100]}"
        hash_hex = hashlib.md5(seed.encode()).hexdigest()[:12]
        return f"blk_{hash_hex}"

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

# ============================================================
# 工厂函数
# ============================================================

def get_parser_client() -> CloudMinerUClient:
    """创建真实的 MinerU 客户端"""
    return CloudMinerUClient()

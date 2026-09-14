import base64, requests
from pathlib import Path
import fitz  # pymupdf
import time
from PIL import Image
API = "http://llm.whu.edu.cn/v1/chat/completions"
KEY = "sk-biAvogg0doSAn1PkXS53LA"
MODEL = "mineru2.5-1.2b"

def pdf_pages_to_b64(pdf_path, dpi=60, max_pages=20):  # dpi从72降到50
    doc = fitz.open(pdf_path)
    imgs = []
    for i in range(min(len(doc), max_pages)):
        pix = doc[i].get_pixmap(dpi=dpi)
        imgs.append(base64.b64encode(pix.tobytes("jpeg")).decode())  # 去掉 quality 参数
    return imgs

def parse_pdf(pdf_path):
    pages = pdf_pages_to_b64(pdf_path)

    # 只测试前两页，交换顺序
    test_order = [1, 0]  # 先发第二页（索引1），再发第一页（索引0）
    for idx in test_order:
        page_b64 = pages[idx]
        page_num = idx + 1
        print(f"Testing page {page_num} ...")

        content = [{"type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{page_b64}"}}]
        content.append({"type": "text",
                        "text": "Parse this document page. Output in MinerU format."})

        r = requests.post(API, headers={"Authorization": f"Bearer {KEY}"}, json={
            "model": MODEL,
            "messages": [{"role": "user", "content": content}],
        }, timeout=300)
        print(f"  status: {r.status_code}")
        if r.status_code != 200:
            print(f"  error: {r.text[:200]}")
        else:
            print(f"  success: {r.json()['choices'][0]['message']['content'][:100]}...")
        time.sleep(5)  # 等待5秒


print(parse_pdf("test_pdf/PARROT-ABenchmark for Evaluating LLMs in Cross-System SQL Translation.pdf"))
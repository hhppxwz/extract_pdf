import requests
from urllib.parse import urlsplit

API = "http://llm.whu.edu.cn/v1/chat/completions"
KEY = "sk-biAvogg0doSAn1PkXS53LA"
u = urlsplit(API)
base_url = f"{u.scheme}://{u.netloc}"

r = requests.get(
    f"{base_url}/openapi.json",
    headers={"Authorization": f"Bearer {KEY}"},
    timeout=10,
)

print(r.status_code)
if r.ok:
    for path in r.json().get("paths", {}):
        print(path)
else:
    print(r.text[:500])
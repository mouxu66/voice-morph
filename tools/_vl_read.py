"""一次性探针：把图丢给本地视觉模型，看它读到什么。

不是产品代码。2026-10-25 用来对比 Qwen3-VL-4B 与 Ornith-1.5-9B(+mmproj)。

Ornith 是**推理模型**：不限制思考预算时，900 token 会全部被 reasoning 吃光，
content 返回空字符串（第一次跑就踩了）。所以这里默认 enable_thinking=false。
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import requests

URL = "http://127.0.0.1:8082/v1/chat/completions"


def ask(
    img: Path,
    question: str,
    max_tokens: int = 900,
    timeout: int = 300,
    think: bool = False,
) -> str:
    b64 = base64.b64encode(img.read_bytes()).decode()
    body = {
        "model": "local",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }
    t0 = time.time()
    r = requests.post(URL, json=body, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    dt = time.time() - t0
    msg = j["choices"][0]["message"]
    txt = (msg.get("content") or "").strip()
    reason = (msg.get("reasoning_content") or "").strip()
    usage = j.get("usage") or {}
    print(
        f"--- {dt:.1f}s | completion {usage.get('completion_tokens')}"
        f" | content {len(txt)}字 | reasoning {len(reason)}字 ---"
    )
    if not txt and reason:
        print("[只有 reasoning，没有 content] 前 200 字：", reason[:200])
    return txt


def main() -> int:
    img = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\变声\outputs\eval-3864\shots\guide.png")
    q = sys.argv[2] if len(sys.argv) > 2 else "这张图是什么？有哪些栏目？逐行把表格读出来。"
    mx = int(sys.argv[3]) if len(sys.argv) > 3 else 1600
    print("图:", img, img.stat().st_size // 1024, "KB")
    print(ask(img, q, max_tokens=mx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

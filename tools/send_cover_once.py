"""一次性：把切好的翻唱成品按序批量发进当前打开的微信聊天窗口。

用法：
    .venv/Scripts/python.exe tools/send_cover_once.py cover_c1.wav cover_c2.wav ...

仅直调 wechat_voice._send_batch（与 send_text 的多条分支同一内核），
不做任何切分 —— 入参必须已经是 ≤MAX_CHUNK_S 的块。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import wechat_voice as wv  # noqa: E402


def main() -> int:
    wavs = []
    for name in sys.argv[1:]:
        p = Path(name)
        if not p.is_absolute():
            p = wv.OUTPUTS_DIR / p.name if hasattr(wv, "OUTPUTS_DIR") else ROOT / "outputs" / p.name
        if not p.exists():
            print(f"[SKIP] 不存在: {p}")
            return 1
        wavs.append(p)
    if not wavs:
        print("用法: send_cover_once.py <wav1> [wav2 ...]")
        return 1
    print(f"[SEND] 批量发送 {len(wavs)} 块 → 当前打开的微信聊天窗口")
    res = wv._send_batch(wavs)
    print(json.dumps(res, ensure_ascii=False, default=str)[:4000])
    return 0 if res.get("outcome") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

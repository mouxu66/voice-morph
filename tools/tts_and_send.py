"""TTS 合成 → 微信自动发送（一次性真机验证 / 后续可复用）

用法：
    .venv/Scripts/python.exe tools/tts_and_send.py                 # 合成并发送
    .venv/Scripts/python.exe tools/tts_and_send.py --no-send       # 只合成，不发送
    .venv/Scripts/python.exe tools/tts_and_send.py --text "内容"   # 指定文案
    .venv/Scripts/python.exe tools/tts_and_send.py --ref 路径.wav  # 指定音色参考

产物：outputs/tts_<ts>.wav（合成）+ 发送历史 outputs/wechat_send_history.json
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import wechat_voice as wv  # noqa: E402
from config import OUTPUTS_DIR  # noqa: E402

DEFAULT_TEXT = ("你好，这是变声工坊的自动发送测试。"
                "文字转语音、虚拟声卡、微信语音消息录制，整条链路已经打通。")
DEFAULT_REF = str(ROOT / "media" / "voicebank" / "kangaroo" / "reference.wav")


def synth(text: str, ref: str) -> Path:
    import qwen3_tts
    t0 = time.time()
    print(f"[TTS] 合成中：{text}")
    print(f"[TTS] 音色参考：{ref}")
    data = qwen3_tts.tts(text=text, ref_audio=ref, language="Chinese")
    out = OUTPUTS_DIR / f"tts_{int(time.time() * 1000)}.wav"
    out.write_bytes(data)
    dur = wv._wav_duration(out)
    print(f"[TTS] 完成 {out.name}（{dur:.2f}s，{len(data) / 1024:.0f}KB，耗时 {time.time() - t0:.1f}s）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--wav", default="", help="已有 wav 文件名（跳过合成直接发）")
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args()

    if a.wav:
        wav = Path(a.wav)
        if not wav.is_absolute():
            wav = OUTPUTS_DIR / wav.name
        print(f"[TTS] 跳过合成，使用已有音频 {wav.name}（{wv._wav_duration(wav):.2f}s）")
    else:
        wav = synth(a.text, a.ref)

    if a.no_send:
        print("[SEND] --no-send，未发送")
        return 0

    t0 = time.time()
    res = wv._do_send(wv.SendVoiceReq(wav=wav.name))
    if hasattr(res, "status_code") and getattr(res, "status_code", 200) != 200:
        print(f"[SEND] 失败 HTTP {res.status_code}: {res.body.decode('utf-8', 'ignore')}")
        return 1
    print(f"\n[SEND] outcome={res.get('outcome')}  耗时 {time.time() - t0:.1f}s  "
          f"声卡还原={res.get('restored')}")
    for i, s in enumerate(res.get("steps", []), 1):
        print(f"       {i}. {s}")
    return 0 if res.get("outcome") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

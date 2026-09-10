"""TTS 合成 → 微信自动发送（一次性真机验证 / 后续可复用）

用法：
    .venv/Scripts/python.exe tools/tts_and_send.py                 # 合成并发送
    .venv/Scripts/python.exe tools/tts_and_send.py --no-send       # 只合成，不发送
    .venv/Scripts/python.exe tools/tts_and_send.py --text "内容"   # 指定文案
    .venv/Scripts/python.exe tools/tts_and_send.py --ref 路径.wav  # 指定音色参考
    .venv/Scripts/python.exe tools/tts_and_send.py --voice kangaroo_v2   # 指定 RVC 音色
    .venv/Scripts/python.exe tools/tts_and_send.py --no-rvc        # 跳过 RVC（纯 TTS 克隆，音色会不像）

链路：文字 → TTS（管"怎么说"）→ **RVC（管"谁在说"）** → 微信语音

⚠️ RVC 这一步是袋鼠音色的**唯一来源**：TTS 零样本克隆复现不了袋鼠音色
   （2026-08-31 用户 A/B 亲耳判定），必须靠 RVC 换音色。跳过它就是普通播音腔。

产物：outputs/tts_<ts>.wav（合成）+ outputs/tts_<ts>_<voice>.wav（换声后）
      + 发送历史 outputs/wechat_send_history.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
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
DEFAULT_VOICE = "kangaroo_v2"

RVC_ROOT = Path(os.environ.get("VM_RVC_ROOT", "D:/RVC"))
RVC_VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
INFER_PY = ROOT / "m2_server" / "offline_vc_infer.py"


def rvc_convert(wav: Path, voice: str, pitch: int = 0, index_rate: float = 0.5) -> Path:
    """TTS 产物 → RVC 换成目标音色，返回新 wav 路径。

    在 D:/RVC/.venv 子进程里跑（与主服务 torch 隔离）。
    """
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC 环境: {RVC_VENV_PY}")
    pth = RVC_ROOT / "logs" / voice / f"{voice}.pth"
    if not pth.exists():
        pth = RVC_ROOT / "assets" / "weights" / f"{voice}.pth"
    if not pth.exists():
        raise RuntimeError(f"找不到音色模型 {voice}.pth（logs/ 与 assets/weights/ 都没有）")
    index = next(iter(sorted((RVC_ROOT / "logs" / voice).glob("added_*.index"))), None)
    out = OUTPUTS_DIR / f"{wav.stem}_{voice}.wav"

    cmd = [str(RVC_VENV_PY), str(INFER_PY),
           "--pth", str(pth),
           "--index", str(index) if index else "",
           "--input", str(wav), "--output", str(out),
           "--pitch", str(pitch), "--index-rate", str(index_rate)]
    print(f"[RVC] 换声 → {voice}（index={'有' if index else '无'}，rate={index_rate}）")
    t0 = time.time()
    # cwd 必须是 RVC 根：offline_vc_infer 在 load_vc() 之前就 `from infer.audio import ...`，
    # 不 chdir 过去会直接 ModuleNotFoundError（与 m2_server/offline_vc.py 保持一致）
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace", cwd=str(RVC_ROOT))
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(f"RVC 失败: {err[-1][:300] if err else r.returncode}")
    print(f"[RVC] 完成 {out.name}（{wv._wav_duration(out):.2f}s，耗时 {time.time() - t0:.1f}s）")
    return out


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
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="RVC 音色实验名（默认袋鼠 kangaroo_v2）")
    ap.add_argument("--pitch", type=int, default=0, help="变调半音数")
    ap.add_argument("--index-rate", type=float, default=0.5, help="特征检索权重，越高越像但可能不稳")
    ap.add_argument("--no-rvc", action="store_true", help="跳过 RVC（音色会明显不像）")
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args()

    if a.wav:
        wav = Path(a.wav)
        if not wav.is_absolute():
            wav = OUTPUTS_DIR / wav.name
        print(f"[TTS] 跳过合成，使用已有音频 {wav.name}（{wv._wav_duration(wav):.2f}s）")
    else:
        wav = synth(a.text, a.ref)
        if not a.no_rvc:
            wav = rvc_convert(wav, a.voice, a.pitch, a.index_rate)

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

"""RVC 文件级换声：任意 wav → 目标音色 wav（子进程跑 offline_vc_infer.py）。

抽出来是因为两处都要用：
  - `wechat_voice.send_text`（微信一键发送：TTS → RVC → 发送）
  - `tools/tts_and_send.py`（命令行真机验证）

推理在 `D:/RVC/.venv` 子进程里跑，与主服务 torch 隔离（和 offline_vc.py 同一套路）。

⚠️ 袋鼠音色的唯一来源就是这一步：TTS 零样本克隆复现不了袋鼠音色
   （2026-08-31 用户 A/B 亲耳判定），别指望 `ref_audio` 能顶替 RVC。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from config import OUTPUTS_DIR, RVC_ROOT

RVC_VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
INFER_PY = Path(__file__).resolve().parent / "offline_vc_infer.py"


class RvcError(RuntimeError):
    pass


def resolve_model(voice: str) -> tuple[Path, Path | None]:
    """找音色的 .pth 与 added_*.index（index 可以没有，有则音色更像）。"""
    log_dir = RVC_ROOT / "logs" / voice
    pth = log_dir / f"{voice}.pth"
    if not pth.exists():
        pth = RVC_ROOT / "assets" / "weights" / f"{voice}.pth"
    if not pth.exists():
        raise RvcError(f"找不到音色模型 {voice}.pth（logs/ 与 assets/weights/ 都没有）")
    index = next(iter(sorted(log_dir.glob("added_*.index"))), None)
    return pth, index


def resolve_rvc_voice(voice_id: str) -> str | None:
    """TTS 音色 id（voicebank/<id>，如 `kangaroo`）→ 可推理的 RVC 实验名（logs/ 下）。

    两套命名不一样：voicebank 用 `kangaroo`，RVC 实验是 `kangaroo_v2` / `kangaroo_v2_40k`。
    按现役主力优先的顺序试，都找不到返回 None（调用方据此决定"不换声"还是报错）。
    """
    if not voice_id:
        return None
    # 覆盖两种实际命名：kangaroo → kangaroo_v2 / kangaroo_v2_40k
    for cand in (f"{voice_id}_v2", f"{voice_id}_v2_40k", f"{voice_id}_40k", voice_id):
        try:
            resolve_model(cand)
            return cand
        except RvcError:
            continue
    return None


def rvc_convert(wav: Path, voice: str, pitch: int = 0, index_rate: float = 0.5) -> Path:
    """把 wav 换成 voice 的音色，产物写进 outputs/<原名>_<voice>.wav，返回其路径。"""
    if not RVC_VENV_PY.exists():
        raise RvcError(f"找不到 RVC 运行环境: {RVC_VENV_PY}")
    src = Path(wav)
    if not src.is_absolute():
        src = OUTPUTS_DIR / src.name
    if not src.exists():
        raise RvcError(f"找不到待转换音频: {src}")

    pth, index = resolve_model(voice)
    out = OUTPUTS_DIR / f"{src.stem}_{voice}.wav"
    cmd = [str(RVC_VENV_PY), str(INFER_PY),
           "--pth", str(pth),
           "--index", str(index) if index else "",
           "--input", str(src), "--output", str(out),
           "--pitch", str(pitch), "--index-rate", str(index_rate)]
    # cwd 必须是 RVC 根：offline_vc_infer 在 load_vc() 之前就 `from infer.audio import ...`
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       encoding="utf-8", errors="replace", cwd=str(RVC_ROOT))
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RvcError("RVC 换声失败: " + " | ".join(tail)[-400:])
    return out


def rvc_convert_logged(wav: Path, voice: str, pitch: int = 0, index_rate: float = 0.5,
                       log=print) -> Path:
    """带进度日志的版本（命令行/桌宠用）。"""
    pth, index = resolve_model(voice)
    log(f"[RVC] 换声 → {voice}（index={'有' if index else '无'}，rate={index_rate}）")
    t0 = time.time()
    out = rvc_convert(wav, voice, pitch, index_rate)
    try:
        import soundfile as sf
        d, sr = sf.read(str(out))
        dur = len(d) / sr
    except Exception:
        dur = -1
    log(f"[RVC] 完成 {out.name}（{dur:.2f}s，耗时 {time.time() - t0:.1f}s）")
    return out

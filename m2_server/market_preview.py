# -*- coding: utf-8 -*-
"""市场音色安装后自动试听（A2）。

流程：固定一句中文 → 用本机任一 voicebank 参考音色经 TTS 合成源句（缓存到
outputs/market/_preview_src.wav）→ 走 RVC 离线推理（offline_vc_infer.py 子进程，
与离线变声同链路）→ outputs/market/<id>_preview.wav，供市场卡片与音色库共用播放器。

状态模型：每个音色一个 sidecar（outputs/market/<id>_preview.json）
    ready      试听已生成（wav 存在）
    generating 生成中
    failed     生成失败（error 带原因）
    skipped    GPU 被占用/未加载，等前端手动重试（不静默排队，避免长任务堆积）
    missing    从未生成

并发与资源：
    - 单进程 _inflight 去重，同一音色同时只跑一个生成任务
    - 实时变声 / 级联变声 / 离线变声任一在跑 → 标记 skipped（它们正在占用 RVC GPU 环境）
    - TTS 源句失败（无可用 voicebank 等）→ failed 可读原因，不只返「生成失败」
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import config as cfg
from runtime import VOICEBANK

# 固定试听句：约 4～5 秒，清晰中性，覆盖多种音色
PREVIEW_TEXT = "安装完成，这是一段自动生成的试听，请听听这个新音色的声音品质。"

MARKET_DIR = cfg.OUTPUTS_DIR / "market"
MARKET_DIR.mkdir(parents=True, exist_ok=True)

INFER_PY = Path(__file__).resolve().parent / "offline_vc_infer.py"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"

_lock = threading.Lock()
_inflight: set[str] = set()


# ---------------- 状态 ----------------


def _sidecar(voice_id: str) -> Path:
    return MARKET_DIR / f"{voice_id}_preview.json"


def _out_wav(voice_id: str) -> Path:
    return MARKET_DIR / f"{voice_id}_preview.wav"


def preview_url(voice_id: str) -> str:
    return f"/api/media/outputs/market/{voice_id}_preview.wav"


def _read_sidecar(voice_id: str) -> dict:
    try:
        return json.loads(_sidecar(voice_id).read_text(encoding="utf-8"))
    except Exception:
        return {}


def status(voice_id: str) -> dict:
    """试听状态：{status, url, error}。wav 存在优先判 ready（sidecar 可脏）。"""
    wav = _out_wav(voice_id)
    if wav.exists():
        return {"status": "ready", "url": preview_url(voice_id), "error": ""}
    sc = _read_sidecar(voice_id)
    return {"status": sc.get("status") or "missing", "url": "", "error": str(sc.get("error") or "")}


def _mark(voice_id: str, st: str, error: str = ""):
    _sidecar(voice_id).write_text(json.dumps({
        "status": st, "error": error,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), encoding="utf-8")


# ---------------- 源句缓存 ----------------


def _src_wav() -> Path:
    return MARKET_DIR / "_preview_src.wav"


def _ensure_source() -> Path:
    """返回试听源句 wav；无则用本机任一 voicebank 参考音色 TTS 合成并缓存。

    抛 RuntimeError 时带可读原因（无 voicebank / TTS 失败）。
    """
    cached = _src_wav()
    if cached.exists():
        return cached
    refs = sorted((p for p in VOICEBANK.iterdir() if p.is_dir() and (p / "reference.wav").exists()),
                  key=lambda p: p.name) if VOICEBANK.is_dir() else []
    if not refs:
        raise RuntimeError("本机没有可用于合成试听源句的参考音色（音色库为空），请先自建一个音色")
    vb = refs[0]
    from qwen3_tts import tts as qwen_tts
    bytes_out = qwen_tts(text=PREVIEW_TEXT, ref_audio=str(vb / "reference.wav"),
                         ref_text="", language="Chinese", voice_id=vb.name)
    tmp = _src_wav().with_suffix(".wav.tmp")
    tmp.write_bytes(bytes_out)
    tmp.replace(cached)
    return cached


# ---------------- 生成 ----------------


def _gpu_busy() -> str:
    """RVC GPU 环境是否被占用；返回占用说明，空闲返回空串。"""
    try:
        from rvc_live import _live_proc_alive
        if _live_proc_alive():
            return "实时变声正在运行"
    except Exception:
        pass
    try:
        from cascade import _cascade_alive
        if _cascade_alive():
            return "级联变声正在运行"
    except Exception:
        pass
    try:
        from offline_vc import OFFLINEVC_STATE
        if OFFLINEVC_STATE.get("running"):
            return "离线变声任务正在运行"
    except Exception:
        pass
    return ""


def _find_index(voice_id: str) -> str:
    idx = next(iter((cfg.RVC_ROOT / "logs" / voice_id).glob("added_*.index")), None)
    return str(idx) if idx else ""


def _find_pth(voice_id: str) -> Path:
    w = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
    if w.exists():
        return w
    l = cfg.RVC_ROOT / "logs" / voice_id / f"{voice_id}.pth"
    return l if l.exists() else Path("")


def generate(voice_id: str) -> dict:
    """触发试听生成（后台线程）。同一音色去重；GPU 忙 → skipped 不排队。

    立即返回当前状态；真实生成在 daemon 线程里推进并落 sidecar。
    """
    st = status(voice_id)
    if st["status"] == "ready":
        return st
    with _lock:
        if voice_id in _inflight:
            return {"status": "generating", "url": "", "error": ""}
        _inflight.add(voice_id)
    threading.Thread(target=_worker, args=(voice_id,), daemon=True).start()
    return {"status": "generating", "url": "", "error": ""}


def try_auto_preview(voice_id: str):
    """安装收尾自动触发（fire-and-forget，任何异常不外抛，不阻塞安装）。"""
    try:
        generate(voice_id)
    except Exception:
        pass


def _worker(voice_id: str):
    try:
        _do_generate(voice_id)
    finally:
        with _lock:
            _inflight.discard(voice_id)


def _do_generate(voice_id: str):
    if status(voice_id)["status"] == "ready":
        return
    # GPU 忙是瞬时状态 → 优先标 skipped（前端可重试），比 failed 更友好
    busy = _gpu_busy()
    if busy:
        _mark(voice_id, "skipped", f"{busy}，可稍后手动重试生成试听")
        return
    if not RVC_VENV_PY.exists():
        _mark(voice_id, "failed", "RVC 运行环境缺失，无法生成试听（请先安装/配置 RVC 整合包）")
        return
    pth = _find_pth(voice_id)
    if not pth.exists():
        _mark(voice_id, "failed", f"音色 {voice_id} 没有可推理的 RVC 模型")
        return
    _mark(voice_id, "generating")
    try:
        src = _ensure_source()
    except Exception as e:  # noqa: BLE001
        _mark(voice_id, "failed", f"试听源句合成失败：{e}")
        return
    out = _out_wav(voice_id)
    cmd = [str(RVC_VENV_PY), str(INFER_PY),
           "--pth", str(pth),
           "--index", _find_index(voice_id),
           "--input", str(src), "--output", str(out),
           "--pitch", "0", "--index-rate", "0.3"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace", cwd=str(cfg.RVC_ROOT))
        if r.returncode != 0 or not out.exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(" | ".join(tail)[-300:] or "无错误输出")
        _mark(voice_id, "ready")
    except Exception as e:  # noqa: BLE001
        _mark(voice_id, "failed", f"RVC 推理失败：{e}")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
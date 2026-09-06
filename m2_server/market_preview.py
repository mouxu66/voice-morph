# -*- coding: utf-8 -*-
"""市场音色安装后自动试听（A2）。

流程：从本机 voicebank 参考音截中段 ~5s 真人声当源句（缓存到
outputs/market/_preview_src.wav）→ 走 RVC 离线推理（offline_vc_infer.py 子进程，
与离线变声同链路）→ outputs/market/<id>_preview.wav，供市场卡片与音色库共用播放器。

源句为何不用 TTS（2026-09-06 懒羊羊试听静音根因）：本机 Qwen3-TTS 对袋鼠等
真实锚点的零样本克隆本就不可靠（会吐 1s 纯静音/乱码，见 A/B 试听结论），
静音源句经 RVC 转换后仍是静音。RVC 是 voice-to-voice，直接用真人参考声
当源句最稳，也最符合"试听音色品质"的目的。

状态模型：每个音色一个 sidecar（outputs/market/<id>_preview.json）
    ready      试听已生成（wav 存在且非静音）
    generating 生成中
    failed     生成失败（error 带原因）
    skipped    GPU 被占用/未加载，等前端手动重试（不静默排队，避免长任务堆积）
    missing    从未生成

并发与资源：
    - 单进程 _inflight 去重，同一音色同时只跑一个生成任务
    - 实时变声 / 级联变声 / 离线变声任一在跑 → 标记 skipped（它们正在占用 RVC GPU 环境）
    - 源句/输出做 RMS 静音校验，静音按 failed 处理（绝不把无声 wav 标成 ready）
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import config as cfg
from runtime import VOICEBANK

# 静音判定阈值：正常语音 RMS 远大于此；数字静音/近静音均视为无声
_MIN_RMS = 1e-3

# 固定试听句：约 4～5 秒，清晰中性，覆盖多种音色（仅文档用途；源句已改用真人声）
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
    """试听状态：{status, url, error}。wav 存在且非静音才判 ready（防坏文件假 ready）。"""
    wav = _out_wav(voice_id)
    if wav.exists() and _audible(wav):
        return {"status": "ready", "url": preview_url(voice_id), "error": ""}
    if wav.exists():
        return {"status": "failed", "url": "", "error": "试听文件为静音，请重新生成"}
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


def _audible(path: Path) -> bool:
    """wav 是否有声（RMS 高于静音阈值）；读不了/全静音都算无声。"""
    try:
        x, _sr = sf.read(str(path))
        if x.ndim > 1:
            x = x.mean(axis=1)
        if x.size == 0:
            return False
        return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2))) > _MIN_RMS
    except Exception:
        return False


def _extract_ref_segment(ref: Path, out: Path, want_s: float = 5.0) -> None:
    """从参考音截中段 ~want_s 秒当试听源句（真人声；中段避开开头静音/呼吸声）。"""
    x, sr = sf.read(str(ref))
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.asarray(x, dtype=np.float32)
    trim = max(1, len(x) // 10)               # 丢头尾各 10%
    core = x[trim:-trim] if len(x) > 2 * trim else x
    want = int(want_s * sr)
    if len(core) <= want:
        seg = core
    else:                                      # 取中段
        start = (len(core) - want) // 2
        seg = core[start:start + want]
    peak = float(np.max(np.abs(seg))) if seg.size else 0.0
    if peak > 0:
        seg = seg * (0.7 / peak)               # 归一到约 -3dB，避免源句过轻
    tmp = out.with_name(out.stem + "_tmp.wav")   # 保持 .wav 扩展名，soundfile 靠它识别格式
    sf.write(str(tmp), seg, sr)
    tmp.replace(out)


def _ensure_source() -> Path:
    """返回试听源句 wav：缓存有效直接用；否则从 voicebank 参考音截真人声。

    抛 RuntimeError 时带可读原因（无 voicebank / 参考音无声 / 截取失败）。
    """
    cached = _src_wav()
    if cached.exists() and _audible(cached):
        return cached
    refs = sorted((p for p in VOICEBANK.iterdir() if p.is_dir() and (p / "reference.wav").exists()),
                  key=lambda p: p.name) if VOICEBANK.is_dir() else []
    if not refs:
        raise RuntimeError("本机没有可用于截取试听源句的参考音色（音色库为空），请先自建一个音色")
    for vb in refs:                            # 第一个参考音全静音时顺延下一个
        ref = vb / "reference.wav"
        if not _audible(ref):
            continue
        try:
            _extract_ref_segment(ref, cached)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"从参考音截取源句失败：{e}")
        if _audible(cached):
            return cached
    raise RuntimeError("所有参考音均为静音，无法截取试听源句，请检查音色库")


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
    # pitch=+12：试听源句是真人参考声（音区低），目标多为卡通/女声等高音区音色，
    # 不移调时 f0 跨度大易出电音（2026-09-06 懒羊羊实测 C/D 组对比后定稿）；
    # index-rate=0.75：加大向目标音色检索的贴力度，压源音色残留。
    cmd = [str(RVC_VENV_PY), str(INFER_PY),
           "--pth", str(pth),
           "--index", _find_index(voice_id),
           "--input", str(src), "--output", str(out),
           "--pitch", "12", "--index-rate", "0.75"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace", cwd=str(cfg.RVC_ROOT))
        if r.returncode != 0 or not out.exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(" | ".join(tail)[-300:] or "无错误输出")
        if not _audible(out):
            out.unlink(missing_ok=True)
            raise RuntimeError("输出为纯静音（源句或模型异常），请重试或换源音色")
        _mark(voice_id, "ready")
    except Exception as e:  # noqa: BLE001
        _mark(voice_id, "failed", f"RVC 推理失败：{e}")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
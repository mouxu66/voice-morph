# -*- coding: utf-8 -*-
"""市场音色安装后自动试听（A2）。

流程：用内置干净中文人声源句（assets/preview_source.wav，魔搭官方模型的中文
示例语音，非袋鼠音色）→ 走 RVC 离线推理（offline_vc_infer.py 子进程，
与离线变声同链路）→ outputs/market/<id>_preview.wav，供市场卡片与音色库共用播放器。

源句为何用固定干净人声（2026-09-07 修复"试听全是袋鼠味"根因）：此前源句从本机
voicebank 参考音（唯一音色=袋鼠）截取，RVC 是 voice-to-voice，源句音色会残留在
输出里，导致每个音色试听都带袋鼠腔。改用与任何目标音色无源关系的中性中文人声后，
输出只呈现目标音色本身。

源句为何不用 TTS（2026-09-06 懒羊羊试听静音根因）：本机 Qwen3-TTS 对袋鼠等
真实锚点的零样本克隆本就不可靠（会吐 1s 纯静音/乱码，见 A/B 试听结论），
且 /tts 接口强制要求参考音频，无纯合成路径。RVC 是 voice-to-voice，
直接用固定真人声当源句最稳，也最符合"试听音色品质"的目的。

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

# 固定试听句：约 4～5 秒，清晰中性，覆盖多种音色（仅文档用途；源句已改用内置干净人声）
PREVIEW_TEXT = "安装完成，这是一段自动生成的试听，请听听这个新音色的声音品质。"

# 内置干净源句：魔搭官方模型（damo/speech_campplus_sv_zh-cn_16k-common）examples 里的
# 中文示例语音，16k 单声道 ~5s，与任何市场音色无源关系 —— 试听只呈现目标音色本身
BUILTIN_SRC = Path(__file__).resolve().parent / "assets" / "preview_source.wav"

# RVC 推理参数（可调）：
#   pitch=+12 原为低音区真人源句（袋鼠参考音）设计，目标多为卡通/女声高音区音色，
#   不移调时 f0 跨度大易出电音（2026-09-06 懒羊羊实测 C/D 组对比后定稿）；
#   源句换成中性人声后可微调（女声目标若显尖/电音可降到 +8~+10）。
#   index-rate=0.75：加大向目标音色检索的贴力度，压源音色残留。
_PITCH = 12
_INDEX_RATE = 0.75

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
    """试听状态：{status, url, error}。wav 存在且非静音才判 ready（防坏文件假 ready）。

    额外一层：源句指纹不匹配（换过源句 / 指纹机制之前的旧缓存）时判 missing，
    让前端自动重新生成——否则换了源句用户听到的还是旧音色的老音频。
    """
    wav = _out_wav(voice_id)
    if wav.exists() and _audible(wav):
        if _stale(voice_id):
            return {"status": "missing", "url": "", "error": ""}
        return {"status": "ready", "url": preview_url(voice_id), "error": ""}
    if wav.exists():
        return {"status": "failed", "url": "", "error": "试听文件为静音，请重新生成"}
    sc = _read_sidecar(voice_id)
    return {"status": sc.get("status") or "missing", "url": "", "error": str(sc.get("error") or "")}


def _mark(voice_id: str, st: str, error: str = "", src_fp: str | None = None):
    # 未显式传指纹时保留原值：failed/skipped/generating 不该抹掉已有指纹
    fp = src_fp if src_fp is not None else str(_read_sidecar(voice_id).get("src_fp") or "")
    _sidecar(voice_id).write_text(json.dumps({
        "status": st, "error": error, "src_fp": fp,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), encoding="utf-8")


# ---------------- 源句缓存 ----------------


def _src_wav() -> Path:
    return MARKET_DIR / "_preview_src.wav"


def _source_fingerprint(src: Path) -> str:
    """源句指纹（路径 + 大小 + mtime）：源句一换就变，用于让旧试听缓存自动失效。

    为什么需要（2026-09-07）：RVC 是 voice-to-voice，源句音色会残留进输出，
    换源句后旧试听就不再代表当前效果（此前换内置干净源句后，旧缓存仍带袋鼠腔，
    而无任何失效机制，用户听到的始终是老音频）。
    只 stat 不解码音频，前端轮询状态也不会有额外开销。
    """
    try:
        st = src.stat()
        return f"{src}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return f"{src}|missing"


def _current_source_path() -> Path | None:
    """当前会使用的源句路径（只判存在、不解码音频）；还没生成过则返回 None。"""
    if BUILTIN_SRC.exists():
        return BUILTIN_SRC
    cached = _src_wav()
    return cached if cached.exists() else None


def _stale(voice_id: str) -> bool:
    """试听是否过期：无指纹（指纹机制之前的旧缓存）或源句指纹变了 → 需重新生成。"""
    fp = str(_read_sidecar(voice_id).get("src_fp") or "")
    if not fp:
        return True                      # 旧缓存无法确认源句，一律重生成
    cur = _current_source_path()
    if cur is None:
        return False                     # 源句还没就绪，不因此判过期（避免反复重试）
    return fp != _source_fingerprint(cur)


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
    """返回试听源句 wav：内置干净人声优先；否则从 voicebank 参考音截真人声。

    内置源句 BUILTIN_SRC 与任何目标音色无源关系，避免 voice-to-voice 残留源音色
    （修复"所有试听都带袋鼠味"）；缺失/无声时退回旧逻辑（voicebank 参考音）。
    抛 RuntimeError 时带可读原因（无 voicebank / 参考音无声 / 截取失败）。
    """
    if BUILTIN_SRC.exists() and _audible(BUILTIN_SRC):
        return BUILTIN_SRC
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


def _find_pth(voice_id: str):
    """返回可推理的 RVC 权重路径；未安装返回 None（勿返回 Path("")：Windows 上
    空 Path==curdir，exists() 为 True，会让调用方误判为有模型）。"""
    w = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
    if w.exists():
        return w
    l = cfg.RVC_ROOT / "logs" / voice_id / f"{voice_id}.pth"
    return l if l.exists() else None


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
    if not pth or not pth.exists():
        # Windows 上 Path("") == "." 且 exists() 为 True，_find_pth 必须返回 None
        # 而不是空 Path，否则"未安装"的音色会带空路径去 torch.load(".") →
        # 报误导性的 PermissionError: '.'。
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
           "--pitch", str(_PITCH), "--index-rate", str(_INDEX_RATE)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace", cwd=str(cfg.RVC_ROOT))
        if r.returncode != 0 or not out.exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(" | ".join(tail)[-300:] or "无错误输出")
        if not _audible(out):
            out.unlink(missing_ok=True)
            raise RuntimeError("输出为纯静音（源句或模型异常），请重试或换源音色")
        # 记下本次使用的源句指纹：下次源句一换，这个缓存就自动失效并重生成
        _mark(voice_id, "ready", src_fp=_source_fingerprint(src))
    except Exception as e:  # noqa: BLE001
        _mark(voice_id, "failed", f"RVC 推理失败：{e}")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
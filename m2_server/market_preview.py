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

import contextlib
import json
import subprocess
import threading
import time
from pathlib import Path

import config as cfg
import numpy as np
import soundfile as sf
from runtime import VOICEBANK

# 静音判定阈值：正常语音 RMS 远大于此；数字静音/近静音均视为无声
_MIN_RMS = 1e-3

# GPU 忙自动补生成延时（秒）：skipped 后后台等这么久再试一次，仍忙则维持 skipped
_BACKOFF_S = 20

# 输出质量关阈值（A2 健壮性增强，2026-09-10）：防"有声但废"的破音/截断/NaN 漏过
_QUALITY_MIN_DUR = 0.5  # 试听短于此（秒）→ 视为截断/异常
_QUALITY_MAX_DUR = 60.0  # 试听长于此（秒）→ 异常
_QUALITY_CLIP_RATIO = 0.02  # 峰值>0.995 的样本占比超此 → 削顶破音

# 源句台词 —— 仅文档用途，改这个常量**不会**改变试听音频。
# 试听是 RVC voice-to-voice：听到的内容由 assets/preview_source.wav 决定，与文本无关。
# 要换台词就换源句音频：python tools/make_preview_source.py --text "新台词"
PREVIEW_TEXT = "大家好，这是我的新声音，你觉得怎么样？"

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
    _sidecar(voice_id).write_text(
        json.dumps(
            {
                "status": st,
                "error": error,
                "src_fp": fp,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


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
        return True  # 旧缓存无法确认源句，一律重生成
    cur = _current_source_path()
    if cur is None:
        return False  # 源句还没就绪，不因此判过期（避免反复重试）
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


def _quality_ok(path: Path) -> tuple[bool, str]:
    """试听输出质量关（A2 健壮性，2026-09-10）：在 _audible 的纯响度之外，再挡掉
    破音/削顶/截断/NaN——这些"有声但废"的情况 _audible 查不出来，会漏成假 ready。

    只查可客观判定的异常；音色像不像、好不好听是主观项，无法自动检测，交给人耳。
    返回 (是否合格, 不合格原因)；合格时原因为空串。
    """
    try:
        x, sr = sf.read(str(path))
    except Exception as e:  # noqa: BLE001
        return False, f"试听文件读取失败：{e}"
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return False, "空音频"
    if not np.all(np.isfinite(x)):
        return False, "输出含 NaN/Inf（模型崩溃）"
    dur = x.size / sr
    if dur < _QUALITY_MIN_DUR:
        return False, f"试听过短（{dur:.2f}s，疑似截断）"
    if dur > _QUALITY_MAX_DUR:
        return False, f"试听过长（{dur:.1f}s，异常）"
    peak = float(np.max(np.abs(x)))
    if peak > 1.0:
        return False, "输出削顶/爆音（峰值>1.0）"
    clip_ratio = float(np.mean(np.abs(x) > 0.995))
    if clip_ratio > _QUALITY_CLIP_RATIO:
        return False, f"输出存在明显削顶（破音，占比{clip_ratio:.1%}）"
    if float(np.sqrt(np.mean(x**2))) <= _MIN_RMS:
        return False, "静音（RMS 过低）"
    return True, ""


def _extract_ref_segment(ref: Path, out: Path, want_s: float = 5.0) -> None:
    """从参考音截中段 ~want_s 秒当试听源句（真人声；中段避开开头静音/呼吸声）。"""
    x, sr = sf.read(str(ref))
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.asarray(x, dtype=np.float32)
    trim = max(1, len(x) // 10)  # 丢头尾各 10%
    core = x[trim:-trim] if len(x) > 2 * trim else x
    want = int(want_s * sr)
    if len(core) <= want:
        seg = core
    else:  # 取中段
        start = (len(core) - want) // 2
        seg = core[start : start + want]
    peak = float(np.max(np.abs(seg))) if seg.size else 0.0
    if peak > 0:
        seg = seg * (0.7 / peak)  # 归一到约 -3dB，避免源句过轻
    tmp = out.with_name(out.stem + "_tmp.wav")  # 保持 .wav 扩展名，soundfile 靠它识别格式
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
    refs = (
        sorted(
            (p for p in VOICEBANK.iterdir() if p.is_dir() and (p / "reference.wav").exists()),
            key=lambda p: p.name,
        )
        if VOICEBANK.is_dir()
        else []
    )
    if not refs:
        raise RuntimeError("本机没有可用于截取试听源句的参考音色（音色库为空），请先自建一个音色")
    for vb in refs:  # 第一个参考音全静音时顺延下一个
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
    try:
        from runtime import gpu_holder_reason

        reason = gpu_holder_reason()
        if reason:
            return reason
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
    log = cfg.RVC_ROOT / "logs" / voice_id / f"{voice_id}.pth"
    return log if log.exists() else None


def generate(voice_id: str, download: dict | None = None) -> dict:
    """触发试听生成（后台线程）。同一音色去重；GPU 忙 → skipped 不排队。

    download 提供直链且音色未安装时 → 先把权重下载到市场缓存再转换
    （与安装共用同一份暂存文件，之后一键安装直接复用、免二次下载）。
    立即返回当前状态；真实生成在 daemon 线程里推进并落 sidecar。
    """
    st = status(voice_id)
    if st["status"] == "ready":
        return st
    with _lock:
        if voice_id in _inflight:
            return {"status": "generating", "url": "", "error": ""}
        _inflight.add(voice_id)
    if download and _find_pth(voice_id) is None:
        threading.Thread(target=_worker_pre, args=(voice_id, dict(download)), daemon=True).start()
    else:
        threading.Thread(target=_worker, args=(voice_id,), daemon=True).start()
    return {"status": "generating", "url": "", "error": ""}


def try_auto_preview(voice_id: str):
    """安装收尾自动触发（fire-and-forget，任何异常不外抛，不阻塞安装）。"""
    with contextlib.suppress(Exception):
        generate(voice_id)


def _worker(voice_id: str):
    try:
        _do_generate(voice_id)
        # C（2026-09-10）：GPU 忙标 skipped 后，后台等一会再自动试一次。
        # 安装收尾自动触发的试听无人盯着，自愈比等用户手动重试更稳；仍忙则维持 skipped。
        _maybe_backoff(voice_id, None, None)
    finally:
        with _lock:
            _inflight.discard(voice_id)


def _worker_pre(voice_id: str, download: dict):
    """未安装音色的试听线程：先确保权重落地（必要时下载），再用暂存权重转换。"""
    try:
        try:
            pth = _ensure_staged(voice_id, download)
        except Exception as e:  # noqa: BLE001
            _mark(voice_id, "failed", f"试听模型下载失败：{e}")
            return
        _do_generate(voice_id, pth_override=pth, index_override="")
        # C：与已安装路径一致，GPU 忙标 skipped 后延时自动补生成一次
        _maybe_backoff(voice_id, pth, "")
    finally:
        with _lock:
            _inflight.discard(voice_id)


def _maybe_backoff(voice_id: str, pth_override: Path | None, index_override: str | None):
    """C（2026-09-10）：刚被标 skipped（GPU 忙）的音色，后台等一会再自动试一次；
    仍忙或已被其他路径置 ready 则不动，维持 skipped 交前端手动重试。

    由 _worker / _worker_pre 在 _do_generate 返回后调用——此时 inflight 仍被调用方
    持有，本函数内不再重复 aquire，避免与用户手动重试/安装重触发竞争。
    """
    if status(voice_id)["status"] != "skipped":
        return
    time.sleep(_BACKOFF_S)
    if status(voice_id)["status"] == "ready" or _gpu_busy():
        return
    _do_generate(voice_id, pth_override=pth_override, index_override=index_override)


def _ensure_staged(voice_id: str, download: dict) -> Path:
    """未安装音色的试听前置：确保权重已落在市场下载缓存（与安装共用同一份）。

    - 缓存里已有有效权重 → 直接复用（后续一键安装时 DownloadManager 的幂等
      分支看到同一文件也会跳过下载，等于试听白嫖了安装的下载量）。
    - 缺失 → 用 DownloadManager 起一个 preview_<id> 任务下载（白名单/断点
      续传/信号量限流与安装同链路，进度同样进下载托盘），轮询到 done。
    """
    from market_download import _torch_header_ok, get_manager

    mgr = get_manager()
    staged = mgr.download_dir / f"{voice_id}.pth"
    if staged.exists() and _torch_header_ok(staged):
        return staged
    name = f"preview_{voice_id}"
    mgr.start(
        name=name,
        url=download.get("url") or "",
        mirror_url=download.get("mirror_url"),
        sha256=download.get("sha256"),
        filename=f"{voice_id}.pth",
    )
    deadline = time.time() + 3600
    while time.time() < deadline:
        st = mgr.task_status(name)
        s = st.get("status")
        if s == "done":
            break
        if s in ("failed", "cancelled", "interrupted"):
            raise RuntimeError(str(st.get("error") or s))
        time.sleep(0.5)
    else:
        raise RuntimeError("下载超时")
    if not staged.exists() or not _torch_header_ok(staged):
        raise RuntimeError("下载产物不是有效的 PyTorch 存档")
    return staged


def _do_generate(
    voice_id: str, pth_override: Path | None = None, index_override: str | None = None
):
    """执行一次试听转换并落 sidecar。

    pth_override/index_override：未安装音色用市场暂存权重转换时由 _worker_pre
    传入（暂存权重无 index，index_override="" 显式跳过检索）；
    缺省时走已安装路径（voicebank/logs 下找权重与 index）。
    """
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
    pth = pth_override if pth_override is not None else _find_pth(voice_id)
    if not pth or not Path(pth).exists():
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
    index = index_override if index_override is not None else _find_index(voice_id)
    cmd = [
        str(RVC_VENV_PY),
        str(INFER_PY),
        "--pth",
        str(pth),
        "--index",
        index,
        "--input",
        str(src),
        "--output",
        str(out),
        "--pitch",
        str(_PITCH),
        "--index-rate",
        str(_INDEX_RATE),
    ]
    # B（2026-09-10）：瞬态失败自愈——RVC 子进程偶发 CUDA/OOM 崩溃，重试一次再下定论
    r = None
    last_err = "无错误输出"
    for attempt in range(2):
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
                encoding="utf-8",
                errors="replace",
                cwd=str(cfg.RVC_ROOT),
            )
            if r.returncode == 0 and out.exists():
                break
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            last_err = "RVC 推理失败：" + (" | ".join(tail)[-300:] or "无错误输出")
        except Exception as e:  # noqa: BLE001
            last_err = f"RVC 推理异常：{e}"
        if attempt < 1:
            time.sleep(1.5)
    if r is None or r.returncode != 0 or not out.exists():
        with contextlib.suppress(Exception):
            out.unlink(missing_ok=True)
        _mark(voice_id, "failed", last_err)
        return
    # A（2026-09-10）：输出质量关——防"有声但废"（破音/削顶/截断/NaN）漏过纯响度检查
    ok, why = _quality_ok(out)
    if not ok:
        with contextlib.suppress(Exception):
            out.unlink(missing_ok=True)
        _mark(voice_id, "failed", f"试听质量不合格：{why}")
        return
    # 记下本次使用的源句指纹：下次源句一换，这个缓存就自动失效并重生成
    _mark(voice_id, "ready", src_fp=_source_fingerprint(src))

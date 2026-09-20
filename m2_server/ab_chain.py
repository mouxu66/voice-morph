"""多链路 A/B 高频链路评测（A4）：同一段输入 × {RVC / Seed-VC / Qwen3} + 客观分。

决策痛点：同一段输入到底走哪条链路更像目标音色？长期靠人耳挑（袋鼠音色 A/B
试了 8 轮）。这里一次性并排跑完三条链路，并用两把客观"尺子"打分，与主观听感同屏：

    secs  音色像度：输出 vs 目标音色参考音的 CAM++ 声纹余弦相似度（0~1，越高越像）
    nats  自然度：NatScore 模型评分（local 权重 models/natscore/final.pt，whisper-small
          编码器 + 偏好头；值越高越自然，可负数）

链路（三者串行共用 GPU，避免显存竞争）：
    - RVC     : 走 offline_vc_infer.py 子进程（与离线变声同链路），保音高
    - Seed-VC : 走 seed_vc.run_conversion 子进程（零样本表达力换音色）
    - Qwen3   : 文本输入的 TTS 克隆（无 text 时跳过并提示）

单次完整跑（RVC + Seed-VC + 打分）约 1~3 分钟；与 /ab/run 一样是同步端点，
FastAPI 会把它放进线程池执行，浏览器 fetch 保持连接等待即可。
"""

import contextlib
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config as cfg
from common import MAX_UPLOAD_BYTES, find_ffmpeg, voice_ref
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from runtime import OUT
from rvc_common import ensure_infer_pth

router = APIRouter(prefix="/api")

INFER_PY = Path(__file__).resolve().parent / "offline_vc_infer.py"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
NATSCORE_CKPT = cfg.ROOT / "models" / "natscore" / "final.pt"

_chain_lock = threading.Lock()  # 全局单飞：一次只跑一个链路对比
_NATS = None  # NatScore 单例（lazy，加载 whisper-small + final.pt）
_NATS_LOCK = threading.Lock()

LOG = logging.getLogger(__name__)


def _live_running() -> bool:
    """实时变声是否在运行。延迟 import：`rvc_live` 属可关插件 sound.rvc-live，
    模块级引用会让「关掉」失效并把试音间拴在它的加载成败上。
    失败按未运行处理 —— 互斥放行。"""
    try:
        from rvc_live import _live_proc_alive
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[ab_chain] 无法确认实时变声状态（%s），按未运行处理", exc)
        return False
    return bool(_live_proc_alive())


# ---------------- 客观分 ----------------


def _nats_scorer():
    """NatScore 本地打分器单例（懒加载；whisper-small 已缓存则离线可用）。"""
    global _NATS
    if _NATS is None:
        with _NATS_LOCK:
            if _NATS is None:
                import sys

                tools = cfg.ROOT / "tools"
                if str(tools) not in sys.path:
                    sys.path.insert(0, str(tools))
                # 先查权重再 import：natscore_local 会拉起 torch，权重缺失时
                # 若先 import 就会抛裸 ModuleNotFoundError，与「给可读原因」相悳
                # （CI 瘦环境无 torch，test_nats_scorer_missing_ckpt_raises 曾因此红）
                if not NATSCORE_CKPT.exists():
                    raise RuntimeError(f"NatScore 权重缺失：{NATSCORE_CKPT}")
                try:
                    from natscore_local import load_local
                except ImportError as exc:  # 多为缺 torch
                    raise RuntimeError(
                        f"NatScore 依赖缺失（需 torch 等）：{exc}；" f"完整依赖见 requirements.txt"
                    ) from exc
                _NATS = load_local(str(NATSCORE_CKPT))
    return _NATS


def _secs(wav: Path, target_ref: Path) -> float:
    """音色像度：CAM++ 声纹余弦相似度（speaker_sep 单例，模型首次加载较慢）。"""
    import numpy as np
    import speaker_sep

    emb = speaker_sep._sv_embed(speaker_sep._read16k(wav))
    emb_ref = speaker_sep._sv_embed(speaker_sep._read16k(target_ref))
    return float(np.dot(emb, emb_ref) / (np.linalg.norm(emb) * np.linalg.norm(emb_ref) + 1e-9))


def _score_metrics(wav: Path, target_ref: Path) -> dict:
    """一把客观尺子一次出齐：secs（像度）+ nats（自然度）+ duration_s。"""
    import soundfile as sf

    secs = _secs(wav, target_ref)
    nats = float(_nats_scorer().score(str(wav)))
    d, sr = sf.read(str(wav))
    return {"secs": round(secs, 3), "nats": round(nats, 3), "duration_s": round(len(d) / sr, 1)}


# ---------------- 三条链路 ----------------


def _preprocess16k(src: Path, dst: Path) -> None:
    cmd = [
        find_ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-af",
        "aresample=16000",
        "-ac",
        "1",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 预处理失败: {r.stderr.strip()[:1500]}")


# RVC 子进程输出里的噪声行：torch 的弃用警告会连带打印源码片段和 caret 行，
# 直接取末尾几行会把真正的异常挤掉（见 _fail_tail 的注释）。
_NOISE_RE = re.compile(
    r"(Warning|warn\(|deprecated|^[\s\^|]+$|^\s*$|UserWarning|FutureWarning|FutureWarning)"
)


def _fail_tail(stderr: str, stdout: str, n: int = 4) -> str:
    """从 RVC 子进程输出里挑出「真正的错误行」。

    为什么需要挑（2026-09-18 试音间实测）：offline_vc_infer.py 启动时 torch 会打一条
    weight_norm 弃用 FutureWarning（两行：警告本身 + 源码片段），此时直接取最后 3 行，
    报出来的就是那条警告 —— 用户看到「RVC 推理失败: FutureWarning … WeightNorm.apply」，
    完全不知道真因是 cuDNN 崩了。优先取含 Error/error/Traceback 的行；
    退而求其次才去掉警告与源码片段取末尾。
    """
    lines = [
        line_str.rstrip() for line_str in (stderr or "").splitlines() + (stdout or "").splitlines()
    ]
    lines = [line_str for line_str in lines if line_str.strip()]
    hits = [
        line_str
        for line_str in lines
        if ("Error" in line_str or "error" in line_str or "Traceback" in line_str)
    ]
    if hits:
        return " | ".join(hits[-n:])[-400:]
    clean = [line_str for line_str in lines if not _NOISE_RE.search(line_str)]
    return " | ".join((clean or lines)[-n:])[-400:]


def _rvc_link(
    voice_id: str,
    src16k: Path,
    out_wav: Path,
    pth: "Path | None" = None,
    index: "str | None" = None,
    pitch: int = 0,
    index_rate: float = 0.5,
) -> None:
    """RVC 整段推理（与离线变声同链路：D:\\RVC\\.venv 子进程）。

    pth / index 覆盖：试音间对「未安装但已在市场下载缓存里」的音色用暂存权重推理
    （暂存权重无配套 index，传 index="" 显式跳过特征检索，与市场试听同一策略）。
    不传时保持原行为：从本机已就绪音色解析权重与 index。
    """
    if pth is None:
        pth = ensure_infer_pth(voice_id)
        if pth is None:
            raise RuntimeError(f"音色 [{voice_id}] 没有可推理的 RVC 模型（未训练）")
    if index is None:
        idx = next(iter((cfg.RVC_ROOT / "logs" / voice_id).glob("added_*.index")), None)
        index = str(idx) if idx else ""
    cmd = [
        str(RVC_VENV_PY),
        str(INFER_PY),
        "--pth",
        str(pth),
        "--index",
        index,
        "--input",
        str(src16k),
        "--output",
        str(out_wav),
        "--pitch",
        str(pitch),
        "--index-rate",
        str(index_rate),
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,
        encoding="utf-8",
        errors="replace",
        cwd=str(cfg.RVC_ROOT),
    )
    if r.returncode != 0 or not out_wav.exists():
        raise RuntimeError("RVC 推理失败: " + _fail_tail(r.stderr or "", r.stdout or ""))


def _seedvc_link(voice_id: str, src16k: Path, out_wav: Path) -> None:
    """Seed-VC 零样本变声（run_conversion 子进程，与 /seedvc 同链路）。"""
    ref, _ = voice_ref(voice_id)
    from seed_vc import run_conversion

    tmp_dir = OUT / f"ab_chain_seedvc_{int(time.time() * 1000)}"
    produced = run_conversion(src16k, ref, tmp_dir)
    shutil.move(str(produced), str(out_wav))
    with contextlib.suppress(Exception):
        tmp_dir.rmdir()


def _qwen3_link(voice_id: str, text: str, out_wav: Path) -> None:
    """Qwen3-TTS 文本克隆（与 /tts 同链路；ref_text 置空走 x-vector 声纹模式）。"""
    ref, _ = voice_ref(voice_id)
    from qwen3_tts import tts as qwen_tts

    out_wav.write_bytes(qwen_tts(text, ref_audio=str(ref), ref_text="", voice_id=voice_id))


# ---------------- API ----------------


@router.post("/ab/chain")
async def ab_chain(
    file: UploadFile = File(...),
    voice_id: str = Form(""),
    text: str = Form(""),
):
    """同一段输入 × 三条链路并排试听 + 客观分（secs / nats）。

    任一条链路失败不拖垮整体：该链路返回 status=failed + 可读原因，其余照常。
    """
    if not voice_id:
        raise HTTPException(status_code=400, detail="请先选择目标音色")
    if not _chain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有链路对比任务在跑，请稍候")
    try:
        if _live_running():
            raise HTTPException(
                status_code=409, detail="实时变声正在运行，请先停止（避免争抢显卡）"
            )
        from cascade import _cascade_alive

        if _cascade_alive():
            raise HTTPException(
                status_code=409, detail="级联变声正在运行，请先停止（避免争抢显卡）"
            )
        from offline_vc import OFFLINEVC_STATE

        if OFFLINEVC_STATE.get("running"):
            raise HTTPException(status_code=409, detail="离线变声任务正在运行，请先等待完成")
        from runtime import gpu_holder_reason

        _holder = gpu_holder_reason()
        if _holder:
            raise HTTPException(status_code=409, detail=f"{_holder}，请先等它结束（避免争抢显卡）")

        # 目标参考音频合法性/存在性校验（三条链路 + 打分共用）
        ref, _ = voice_ref(voice_id)

        stamp = int(time.time() * 1000)
        raw_path = OUT / f"ab_chain_src_{stamp}{Path(file.filename or 'a.wav').suffix or '.wav'}"
        if (file.size or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
            )
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
            )
        raw_path.write_bytes(raw)

        src16k = OUT / f"ab_chain_in_{stamp}.wav"
        text = text.strip()
        try:
            _preprocess16k(raw_path, src16k)

            links = {
                "rvc": _run_one("rvc", lambda out: _rvc_link(voice_id, src16k, out), stamp),
                "seed_vc": _run_one(
                    "seed_vc", lambda out: _seedvc_link(voice_id, src16k, out), stamp
                ),
            }
            if text:
                links["qwen3"] = _run_one(
                    "qwen3", lambda out: _qwen3_link(voice_id, text, out), stamp
                )
            else:
                links["qwen3"] = {
                    "status": "skipped",
                    "url": "",
                    "error": "未提供文本，Qwen3（TTS 链路）不参与对比",
                    "metrics": None,
                }

            # 客观分：只对有产物的链路打分（打分器首次加载较慢，放最后统一跑）
            for name in ("rvc", "seed_vc", "qwen3"):
                link = links[name]
                if link["status"] == "done":
                    try:
                        link["metrics"] = _score_metrics(link["_wav"], ref)
                    except Exception as e:  # noqa: BLE001 —— 打分失败不拖垮对比结果
                        link["metrics"] = None
                        link["error"] = (
                            link["error"] + "；" if link["error"] else ""
                        ) + f"客观分失败：{e}"
                    finally:
                        link.pop("_wav", None)

            return {"ok": True, "target_voice_id": voice_id, "text": text, "chains": links}
        finally:
            for p in (raw_path, src16k):
                with contextlib.suppress(Exception):
                    p.unlink(missing_ok=True)
    finally:
        _chain_lock.release()


def _run_one(tag: str, func, stamp: int) -> dict:
    """跑单条链路到 out wav；成功返回 done+url，失败返回 failed+error（不抛）。"""
    out_wav = OUT / f"ab_chain_{tag}_{stamp}.wav"
    try:
        func(out_wav)
        if not out_wav.exists():
            raise RuntimeError("链路未产出音频")
        return {
            "status": "done",
            "url": f"/api/media/outputs/{out_wav.name}",
            "error": "",
            "metrics": None,
            "_wav": out_wav,
        }
    except Exception as e:  # noqa: BLE001 —— 单链路失败不拖垮整体
        with contextlib.suppress(Exception):
            out_wav.unlink(missing_ok=True)
        return {"status": "failed", "url": "", "error": str(e)[:300], "metrics": None}

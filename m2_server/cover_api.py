"""扒歌换声（翻唱）：整首歌 → 分离人声/伴奏 → 换音色 → 合回伴奏 → 导出成品。

接口：
    POST /api/cover/fetch   粘一条音频直链 → 下到会话目录（只下载，可先试听）
    POST /api/cover/run     multipart 上传歌曲（或给下好的 src_name）+ 音色/变调，提交后台任务
    GET  /api/cover/status  轮询进度与结果

歌曲来源两条路（都在会话目录，都"跑完即删"）：
    · 上传文件 —— 老路径，行为不变；
    · 粘直链（`url_fetch`）—— 下下来先试听（`/cover/fetch`），确认是这首歌再跑。

为什么是"三步现成件拼起来"而不是新模型：
    demucs 分离（`htdemucs --two-stems` 出 vocals + no_vocals）、RVC 换声
    （`offline_vc_infer.py`，与离线变声同一套）、ffmpeg 合回。三步都是本项目
    已经在跑的东西，这里只是把它们串成一条歌用的链路。

★ 关键性质（决定了这个功能是"天然可用"而不是"要额外调教"）：
    RVC 是 **voice-to-voice** —— 它只替换音色，**读 F0 曲线 + 节奏 + 时长**。
    所以原唱的转音、颤音、换气、音准、节奏全部原样保留。用户想要的"技巧展示"
    是 RVC 的天然行为，不需要额外实现什么。

三个必须做对的细节（每个错了都不会报错，只是成品不对）：
    1. **必须用 `--two-stems vocals` 拿伴奏**。只要人声不拿伴奏，最后就合不回去。
    2. **变调是歌的命门**。RVC 默认 pitch=0，而翻唱常见需求是升/降八度
       （男女声互转）。本模块提供 `pitch`，并额外做**自动建议**
       （比对人声中位基频与目标音色参考音高）。
    3. **分离产物必须落在会话目录**（`session_out.derived_dir`），
       否则"退出即删"会在第一次跑翻唱时漏一堆 wav 在 outputs 根
       （`rvc_convert` 已经踩过一次，见其注释）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import config as cfg
import session_out
import url_fetch
from common import MAX_UPLOAD_BYTES, find_ffmpeg
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from rvc_common import ensure_infer_pth

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: demucs 模型。`htdemucs` 是既有的（pipeline.py 同款，首次运行会下 ~300MB）；
#: `htdemucs_ft` 分离质量更好但慢 4 倍 —— 翻唱是"整首歌"，慢 4 倍不可接受，故不换。
DEMUCS_MODEL = "htdemucs"

#: 分离产物根目录（每次现读 cfg，不许早绑定 —— §8.36/§8.37）。
#: 注意与 m1_workshop 的 `media/demucs_out` **不同目录**：练库的分离产物是长期资产，
#: 翻唱的分离产物是会话垃圾，退出即删。
SEPARATE_SUBDIR = "cover_sep"

COVER_STATE: dict = {
    "running": False,
    "status": "idle",  # idle | running | done | error
    "step": "",  # separate | convert | mix（给前端显示到哪一步了）
    "message": "",
    "percent": 0.0,
    "voice_id": "",
    "url": "",
    "duration_s": 0.0,
    "pitch": 0,
    "error": "",
}
_cover_lock = threading.Lock()


def _gpu_guard() -> str:
    """返回"不能开工"的原因；空闲返回空串。

    翻唱要跑 demucs + RVC，两件都吃 GPU。与离线变声同一套互斥口径，
    但**不因实时变声/级联运行就直接拒绝** —— 翻唱动辄几分钟，用户可能正
    在实时变声里测试。所以这里只提示，真正的裁决交给 `gpu_holder_reason`
    （专指"训练"这类长任务）与 `torch` 自己的 OOM。
    """
    from runtime import gpu_holder_reason

    return gpu_holder_reason()


def _separate_dir() -> Path:
    d = cfg.OUTPUTS_DIR / session_out.DIRNAME / SEPARATE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def separate_song(src: Path, stamp: int) -> tuple[Path, Path]:
    """把整首歌拆成 (人声, 伴奏)。返回 (vocals.wav, instrumental.wav)。

    demucs 输出形如 `<out>/<model>/<stem>/vocals.wav` 与 `no_vocals.wav`。
    `--two-stems vocals` 只出这两轨 —— 正好是我们需要的全部，不多算鼓/贝斯。
    """
    out_dir = _separate_dir() / f"sep_{stamp}"
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        DEMUCS_MODEL,
        "--two-stems",
        "vocals",
        "-o",
        str(out_dir),
        str(src),
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
        encoding="utf-8",
        errors="replace",
    )
    stem_dir = out_dir / DEMUCS_MODEL / src.stem
    vocals, accomp = stem_dir / "vocals.wav", stem_dir / "no_vocals.wav"
    if r.returncode != 0 or not vocals.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(
            "人声分离失败：首次运行需联网下载 demucs 模型（约 300MB）"
            + ("｜" + " | ".join(tail)[-400:] if tail else "")
        )
    if not accomp.exists():
        # 分离成功但伴奏缺失：多半是 demucs 版本差异。宁可报错也不静默只换人声 ——
        # 那样用户拿到的是一首没有伴奏的歌，比失败更让人困惑。
        raise RuntimeError(f"分离产物缺少伴奏轨（预期 {accomp.name}），无法合回")
    return vocals, accomp


def mix_back(vocals: Path, accomp: Path, out: Path, gains: tuple[float, float] = (1.0, 1.0)) -> Path:
    """把换好声的人声与伴奏合成一条立体声 wav。

    `gains` = (人声增益, 伴奏增益)。默认不动（1.0/1.0）—— 但**翻唱链路实际传进来的
    是 `auto_vocal_gain` 的实测配平值**（见 `_cover_worker` ⑤），用户显式关掉
    自动配平时才是这里这个 1.0 默认。
    """
    vg, ag = gains
    cmd = [
        find_ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(vocals),
        "-i",
        str(accomp),
        "-filter_complex",
        f"[0:a]volume={vg}[v];[1:a]volume={ag}[a];[v][a]amix=inputs=2:duration=longest:normalize=0",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("合成失败: " + " | ".join(tail)[-400:])
    return out


# ---- 人声/伴奏自动配平（2026-09-26）----
# 用户两轮真机听感都是"人声太小，听不出音色"（0.5 版、0.85 版同反馈）：
# RVC 换声输出的电平普遍低于 demucs 分出来的伴奏。与其让用户对着增益滑块
# 盲猜，不如把两轨的实测 RMS 量出来直接配平 —— 所以做成默认开启。

#: 自动配平目标：人声比伴奏高多少 dB。~2.5dB 是"人声清楚、但不压伴奏"的
#: 常用起点（KTV 里"跟唱"的听感）；再高就开始盖过鼓点/贝斯了。
AUTO_VOCAL_LEAD_DB = 2.5

#: 增益夹紧范围。测量会被纯伴奏段/爆音段带偏，夹住防止人声被推飞或压没。
AUTO_GAIN_LIMITS = (0.5, 3.0)


def rms_db(path: Path) -> float | None:
    """整轨 RMS 电平（dBFS）。读不出 / 全静音返回 None —— 调用方按"不调"处理。

    为什么用 RMS 而不是峰值：听感响度跟 RMS 走；峰值只防削波，不描述"响"。
    """
    try:
        import numpy as np
        import soundfile as sf

        d, _ = sf.read(str(path))
        if d.size == 0:
            return None
        rms = float(np.sqrt(np.mean(np.asarray(d, dtype="float64") ** 2)))
        if rms <= 1e-9:
            return None
        return 20.0 * math.log10(rms)
    except Exception:  # noqa: BLE001 —— 量不出电平不该炸掉整条翻唱链路
        return None


def auto_vocal_gain(
    vocal_db: float | None,
    accomp_db: float | None,
    target_lead_db: float = AUTO_VOCAL_LEAD_DB,
    limits: tuple[float, float] = AUTO_GAIN_LIMITS,
) -> float:
    """按实测电平差算人声增益，使人声比伴奏高 `target_lead_db`。

    任一轨测不出（None）→ 返回 1.0：**量不出来时最安全的是不调**，
    而不是拍一个"大概行"的数。结果夹在 `limits` 内并保留两位小数
    （ffmpeg 的 volume= 滤镜值太长没有意义，还不好在日志里对账）。
    """
    if vocal_db is None or accomp_db is None:
        return 1.0
    need_db = target_lead_db - (vocal_db - accomp_db)
    gain = 10.0 ** (need_db / 20.0)
    lo, hi = limits
    return round(min(max(gain, lo), hi), 2)


def _pitch_suggest(vocals: Path, voice_id: str) -> int:
    """自动变调建议（半音）。

    做法：量人声的中位基频，量目标音色参考音的中位基频，算两者差多少个半音，
    四舍五入到最近的**整数半音**。

    为什么用中位而不是均值：唱歌有大量高音（尤其副歌），均值会被拉高，
    而"这个人的音域中心在哪"才是该对齐的量。
    """
    try:
        import librosa
        import numpy as np

        ref = cfg.MEDIA_DIR / "voicebank" / voice_id / "reference.wav"
        if not ref.exists():
            return 0
        f0_src = _median_f0(vocals, np, librosa)
        f0_ref = _median_f0(ref, np, librosa)
        if not f0_src or not f0_ref:
            return 0
        semitones = 12 * float(np.log2(f0_ref / f0_src))
        return int(round(semitones))
    except Exception as e:  # noqa: BLE001 —— 建议值算不出来时"不调"是最安全的默认
        LOG.warning("[cover] 变调建议计算失败（按 0 处理）: %s", e)
        return 0


def _median_f0(path: Path, np, librosa) -> float | None:
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    if y.size == 0:
        return None
    f0 = librosa.yin(y, fmin=65, fmax=1000, sr=sr)
    f0 = f0[np.isfinite(f0)]
    f0 = f0[f0 > 65]
    if f0.size < 10:
        return None
    return float(np.median(f0))


class CoverFetchReq(BaseModel):
    """粘直链请求体（只有 url —— 音色/变调那些参数都在 `/cover/run` 上）。"""

    url: str


def _duration_of(path: Path) -> float:
    """读时长（只解析文件头）。读不出来给 0 —— 试听那一行少个数字而已。"""
    try:
        import soundfile as sf

        return round(float(sf.info(str(path)).duration), 1)
    except Exception:  # noqa: BLE001 —— 时长只是展示，读不到不该影响下载结果
        return 0.0


@router.post("/cover/fetch")
async def cover_fetch(req: CoverFetchReq):
    """把一条音频直链下到会话目录，返回可立即试听的地址（**不跑链路**）。

    为什么分成两步、而不是"粘了就直接跑完"：翻唱要几分钟，而"下错歌"是最常见的
    失误（版本不对 / 下到的是别人的翻唱 / 纯伴奏）。先花几秒试听，比等三分钟划算。

    护栏全在 `url_fetch` 里（拒内网、逐跳校验重定向、限大小、拒网页），这里只负责
    把阻塞的下载丢进线程池，别卡住事件循环。
    """
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, url_fetch.fetch_to_session, req.url)
    except url_fetch.FetchError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "name": info["name"],
        "url": info["url"],
        "bytes": info["bytes"],
        "duration_s": _duration_of(Path(info["path"])),
    }


async def _resolve_source(file: UploadFile | None, src_name: str, stamp: int) -> Path:
    """定"这首歌从哪来"：会话里已下好的（`src_name`）或刚上传的（`file`）。

    两条路都落在**会话目录**：上传的写完就在那，下好的本来就在那 —— 后面
    `_cover_worker` 的 finally 一律删掉它（跑完即删，与"即用即删"同一口径）。

    刻意在拿状态锁**之前**调用：源不对时不该把 `COVER_STATE` 置成 running，
    否则前端会一直转圈等一个根本不会开始的任务。
    """
    if src_name:
        found = session_out.find(src_name)
        if found is None or not session_out.is_session(found):
            raise HTTPException(
                status_code=400,
                detail="这个源文件已经不在会话里了（退出应用会清空），请重新下载或改用选择文件",
            )
        return found
    if file is None:
        raise HTTPException(status_code=400, detail="请先选择歌曲文件，或粘贴一条音频直链")
    limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大：>{limit_mb}MB")
    dst = session_out.new_path(
        f"cover_src_{stamp}", Path(file.filename or "song.wav").suffix or ".wav"
    )
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大：>{limit_mb}MB")
    dst.write_bytes(raw)
    return dst


@router.post("/cover/run")
async def cover_run(
    file: UploadFile | None = File(None),
    src_name: str = Form(""),
    voice_id: str = Form(""),
    pitch: int = Form(0),
    index_rate: float = Form(0.5),
    vocal_gain: float = Form(1.0),
    accomp_gain: float = Form(1.0),
    auto_pitch: bool = Form(False),
    auto_gain: bool = Form(True),
):
    """提交翻唱任务。pitch 为半音数（升八度 +12，降八度 -12）。

    auto_pitch=True 时忽略传入的 pitch，改由 `_pitch_suggest` 算 —— 用户不知道
    该调多少半音是常态，"让程序建议"比"给个滑块让他猜"合格得多。

    auto_gain=True（默认）时忽略传入的 vocal_gain/accomp_gain，改由实测 RMS
    自动配平（见 `auto_vocal_gain`）—— 同理：用户听出"人声太小"时根本不知道
    该补多少倍，量出来直接配比滑块靠谱。旧前端不传这个字段 → 走默认 True。

    歌曲来源二选一：`file`（上传）或 `src_name`（`/cover/fetch` 下好的会话文件名）。
    """
    stamp = int(time.time() * 1000)
    raw_path = await _resolve_source(file, src_name, stamp)  # 先定源，再动状态
    with _cover_lock:
        if COVER_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有翻唱任务在跑，请稍候")
        if not voice_id:
            raise HTTPException(status_code=400, detail="请先选择音色")
        pth = ensure_infer_pth(voice_id)
        if pth is None:
            raise HTTPException(
                status_code=404,
                detail=f"音色 [{voice_id}] 没有可推理的 RVC 模型，先到实时变声页训练",
            )
        if not (cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe").exists():
            raise HTTPException(status_code=500, detail="RVC 运行环境缺失")
        holder = _gpu_guard()
        if holder:
            raise HTTPException(status_code=409, detail=f"{holder}，请先等它结束（避免争抢显卡）")

        COVER_STATE.update(
            running=True,
            status="running",
            step="separate",
            message="正在分离人声与伴奏…",
            percent=5.0,
            voice_id=voice_id,
            url="",
            duration_s=0.0,
            pitch=pitch,
            error="",
        )

    threading.Thread(
        target=_cover_worker,
        args=(raw_path, voice_id, pth, pitch, index_rate, vocal_gain, accomp_gain, auto_pitch, auto_gain, stamp),
        daemon=True,
    ).start()
    return {"ok": True, "voice_id": voice_id}


def _cover_worker(
    raw_path: Path,
    voice_id: str,
    pth: Path,
    pitch: int,
    index_rate: float,
    vocal_gain: float,
    accomp_gain: float,
    auto_pitch: bool,
    auto_gain: bool,
    stamp: int,
):
    import soundfile as sf

    in_path = session_out.new_path(f"cover_in_{stamp}")
    voice_path = session_out.new_path(f"cover_voice_{stamp}")
    out_path = session_out.new_path(f"cover_{stamp}")
    vocals = accomp = None
    try:
        COVER_STATE.update(running=True, status="running", step="separate",
                           message="正在分离人声与伴奏…", percent=8.0)
        # ① 统一转 wav（demucs 只吃音频；上传的多半是 mp3/m4a/flac）
        cmd = [
            find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(raw_path),
            "-ar", "44100", "-ac", "2", str(in_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not in_path.exists():
            raise RuntimeError(f"音频解码失败: {(r.stderr or '').strip()[:600]}")

        # ② 分离
        vocals, accomp = separate_song(in_path, stamp)
        COVER_STATE.update(step="convert", percent=30.0,
                           message="人声已分离，正在换音色…")

        # ③ 变调（自动建议 or 用户指定）
        use_pitch = pitch
        if auto_pitch:
            COVER_STATE.update(percent=33.0, message="正在分析音域、计算变调…")
            use_pitch = _pitch_suggest(vocals, voice_id)
        COVER_STATE.update(pitch=use_pitch)

        # ④ RVC 换声。沿用 rvc_convert 的常驻 worker（第二次起模型有缓存），
        #    它按 `session_out.derived_dir(src)` 决定产物落哪 —— 输入在会话目录，
        #    产物就也在会话目录，退出即删能收干净。
        import rvc_convert

        COVER_STATE.update(step="convert", percent=40.0,
                           message=f"正在换音色…（{voice_id}，变调 {use_pitch:+d} 半音）")
        rvc_out = rvc_convert.rvc_convert(vocals, voice_id, pitch=use_pitch, index_rate=index_rate)
        COVER_STATE.update(step="mix", percent=85.0, message="正在与伴奏合成…")

        # ⑤ 合回伴奏。auto_gain=True（默认）时按实测 RMS 自动配平人声：
        #    RVC 换声输出电平普遍低于 demucs 伴奏（用户 0.5/0.85 两版都反馈"人声太小"），
        #    量出来直接配，比让用户盲猜滑块靠谱；量不出电平则原样合成，不乱动。
        if auto_gain:
            vg = auto_vocal_gain(rms_db(rvc_out), rms_db(accomp))
            gains = (vg, 1.0)
        else:
            gains = (vocal_gain, accomp_gain)
        mix_back(rvc_out, accomp, voice_path, gains=gains)

        # ⑥ 输出：搬到会话目录下带正式名字的文件，避免成品是个 `cover_时间戳/stem_voice` 这种怪名
        out_path.write_bytes(voice_path.read_bytes())
        d, sr = sf.read(str(out_path))
        duration_s = round(len(d) / sr, 1)
        gain_note = f"（人声 ×{gains[0]:.2f}）" if gains != (1.0, 1.0) else ""
        COVER_STATE.update(
            running=False,
            status="done",
            step="",
            message=f"翻唱完成{gain_note}",
            percent=100.0,
            url=f"/api/media/outputs/{session_out.rel_url(out_path.name)}",
            duration_s=duration_s,
            error="",
        )
    except Exception as e:
        COVER_STATE.update(running=False, status="error", step="", message="", error=str(e))
    finally:
        # 中间产物全部清掉，只留成品。分离目录整个删（一首歌的分离结果没有复用价值，
        # 而它往往是最大的几个文件）。rvc_out 是 rvc_convert 写的，一并收走。
        with contextlib.suppress(Exception):
            raw_path.unlink(missing_ok=True)
        for p in (in_path, voice_path):
            with contextlib.suppress(Exception):
                p.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            import shutil

            shutil.rmtree(_separate_dir() / f"sep_{stamp}", ignore_errors=True)


@router.get("/cover/status")
def cover_status():
    return COVER_STATE


@router.post("/cover/pitch_suggest")
async def cover_pitch_suggest(
    file: UploadFile | None = File(None),
    src_name: str = Form(""),
    voice_id: str = Form(""),
):
    """只算变调建议（不跑完整链路）。前端选好歌 + 音色后调它填默认值。

    只分离人声再量基频 —— 比不分离准得多（伴奏的贝斯/鼓会污染基频估计）。
    约 20~40 秒，期间前端显示"正在分析音域"。

    `src_name` 是 `/cover/fetch` 下好的会话源：**这份不删** —— 用户分析完还要
    用它开跑（上传的那份用完即删，与 `/cover/run` 同口径）。
    """
    if not (cfg.MEDIA_DIR / "voicebank" / voice_id / "reference.wav").exists():
        raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 缺少参考音，无法给建议")
    stamp = int(time.time() * 1000)
    if src_name:
        found = session_out.find(src_name)
        if found is None or not session_out.is_session(found):
            raise HTTPException(
                status_code=400,
                detail="这个源文件已经不在会话里了（退出应用会清空），请重新下载或改用选择文件",
            )
        raw_path, keep_src = found, True
    else:
        if file is None:
            raise HTTPException(status_code=400, detail="请先选择歌曲文件，或粘贴一条音频直链")
        if (file.size or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"文件过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
            )
        raw_path = session_out.new_path(
            f"cps_{stamp}", Path(file.filename or "s.wav").suffix or ".wav"
        )
        raw_path.write_bytes(await file.read())
        keep_src = False
    step = None
    try:
        step = session_out.new_path(f"cps_in_{stamp}")
        subprocess.run(
            [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(raw_path),
             "-ar", "44100", "-ac", "2", str(step)],
            capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
        )
        vocals, _ = separate_song(step, stamp)
        return {"ok": True, "pitch": _pitch_suggest(vocals, voice_id)}
    finally:
        for leftover in (None if keep_src else raw_path, step):
            with contextlib.suppress(Exception):
                if leftover is not None:
                    leftover.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            import shutil

            shutil.rmtree(_separate_dir() / f"sep_{stamp}", ignore_errors=True)

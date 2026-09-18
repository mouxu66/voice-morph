# -*- coding: utf-8 -*-
"""试音间（Audition Room）：同一个声音，一次试完一批音色。

产品语义：一段试音音频（或一句文字）当"考题"，一次挂上多个音色，串行跑完并排听
+ 客观分；想立刻听到效果就用手动轮换的实时试音。

三条路径与复用（不复制任何引擎代码）：
    - 批量并排（离线）: 本模块串行调 ab_chain._rvc_link
                        —— 与 /offlinevc、市场试听同一条 RVC 子进程链路
    - 文字试音        : 本模块逐音色调 qwen3_tts.tts —— 与 /tts、/ab/run 同链路
    - 实时轮换（单件）: 前端直接调 rvc_live（/rvc/live/start?exp_name=&monitor=），
                        本模块只负责提供环境态势（/audition/env）与互斥保护
    客观分（secs 音色像度 / nats 自然度）复用 ab_chain 已实现的打分器。

命名坑（务必记住）：voicebank id ≠ RVC 实验名（`kangaroo` ↔ `kangaroo_v2`）。
音色在**音频路径**下用 RVC 实验名 / 市场 voice_id；**文字路径**需要那套 voicebank
id（TTS 克隆要参考音），靠 rvc_convert.rvc_voice_candidates 反向求交。

资源纪律（8GB 卡上两个 RVC 推理进程必然 OOM）：
    - 批量任务启动时 hold_gpu("audition", ...) 占独占位；离线变声 / 市场试听 /
      链路对比都会查 runtime.gpu_holder_reason() 并回 409，不再各查各的
    - 实时 / 级联 / 离线任一在跑 → 本模块直接 409，不排队（排队会让用户以为卡死）
    - 任务可取消：取消后不再开下一个音色（否则选 10 个就锁死显卡十几分钟）

缓存：产物文件名由 (模式|源|文本|pitch|index_rate) 的哈希决定 —— 同样的参数再点
一次直接复用已有 wav，不重跑推理（批量试音每个 1~3 分钟，重复跑很贵）。
"""
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

import config as cfg
from ab_chain import _preprocess16k, _rvc_link
from common import MAX_UPLOAD_BYTES, is_valid_voice_id
from market_manifest import find_manifest_item
from market_preview import BUILTIN_SRC, _ensure_staged, _find_index, _find_pth
from rvc_common import ensure_infer_pth
from rvc_convert import rvc_voice_candidates
from runtime import (API_PREFIX, OUT, gpu_holder_reason, hold_gpu, release_gpu)

router = APIRouter(prefix=API_PREFIX)

AUDITION_DIR = OUT / "audition"
AUDITION_DIR.mkdir(parents=True, exist_ok=True)

# 客观打分脚本（一次性子进程：不污染后端进程的 CUDA 上下文，见该文件顶部注释）
SCORE_PY = Path(__file__).resolve().parent / "audition_score.py"
SCORE_TIMEOUT = 300

_SRC_RE = re.compile(r"^src_[A-Za-z0-9_]+$")

# 源音频时长约束：太短 RVC 拿不到有效基频；太长则 N 个音色串行会等到天荒地老
MIN_SOURCE_S = 1.0
MAX_SOURCE_S = 60.0

# 源音频留存上限（超出先清最旧的；16k 单声道 60s 约 1.9MB，不是空间问题，
# 是别让列表变成垃圾场）。清理时跳过当前任务正在用的那个。
SRC_KEEP = 20
SRC_HARD_CAP = 40

# 任务锁：全局单飞（一次只跑一个批量试音）
_TASK_LOCK = threading.Lock()
_CANCEL = threading.Event()

AUDITION_STATE: dict = {
    "task_id": "",
    "running": False,        # 只表示「推理阶段」在跑；打分阶段另有 scoring
    "status": "idle",        # idle | running | done | cancelled | error
    "mode": "",              # audio | text
    "total": 0,
    "finished": 0,
    "current": "",
    "current_name": "",
    "message": "",
    "error": "",
    "source_name": "",
    "text": "",
    "results": [],           # 逐个追加，前端轮询即可看到结果陆续出来
    "scoring": False,        # 结果都出来后，附加的客观分还在后台算
    "score_finished": 0,
    "score_total": 0,
}
_STATE_LOCK = threading.Lock()


# ---------------- 展示名 / 参考音 ----------------


def _display_name(voice_id: str) -> str:
    """音色中文名：市场清单 > logs/<id>/source.json > meta.json > 目录名。

    与市场卡片、音色库同一优先级顺序（中文名只在市场侧维护），保证同一个音色
    在三个页面叫同一个名字。
    """
    item = find_manifest_item(voice_id)
    if item and item.get("name"):
        return str(item["name"])
    for fname, key in (("source.json", "display_name"), ("meta.json", "display_name")):
        f = cfg.RVC_ROOT / "logs" / voice_id / fname
        if not f.exists():
            continue
        try:
            name = json.loads(f.read_text(encoding="utf-8")).get(key)
            if name:
                return str(name)
        except Exception:
            continue
    return voice_id


def _voicebank_for(rvc_id: str) -> str | None:
    """RVC 实验名 / 市场 voice_id → 对应 voicebank 音色 id（无参考音时返回 None）。

    文字合成（TTS 克隆）必须要参考音，而参考音只存在于 media/voicebank/<id>/。
    市场 RVC 权重没有 voicebank 目录（has_reference=false）→ 文字路径对它不成立，
    调用方据此给出「这个音色只能换音色、不能输字」的明确原因。
    """
    bank = cfg.MEDIA_DIR / "voicebank"
    if not bank.is_dir():
        return None
    if (bank / rvc_id / "reference.wav").exists():
        return rvc_id
    for d in sorted(bank.iterdir()):
        if not d.is_dir() or not (d / "reference.wav").exists():
            continue
        if rvc_id in rvc_voice_candidates(d.name):
            return d.name
    return None


def _ref_for(rvc_id: str) -> Path | None:
    """目标音色的参考音（算 secs 音色像度用）；没有则 None。"""
    vb = _voicebank_for(rvc_id)
    if not vb:
        return None
    ref = cfg.MEDIA_DIR / "voicebank" / vb / "reference.wav"
    return ref if ref.exists() else None


# ---------------- 权重解析 ----------------


def _resolve_weight(voice_id: str) -> tuple["Path | None", str, str]:
    """解析音色的可推理权重 → (pth, index, 错误原因)。

    顺序刻意从"最便宜"到"最贵"：
        1. 本机已就绪（已安装市场音色 / 自训产物）—— 纯 stat
        2. 只有训练检查点 G_*.pth —— 走提取（较慢，幂等缓存）
        3. 市场清单里有 —— 下到下载缓存再用（与市场试听共用同一份缓存，
           之后真去安装时 DownloadManager 看到同一文件会跳过下载，等于试音白赚下载量）
    """
    pth = _find_pth(voice_id)
    if pth:
        return Path(pth), _find_index(voice_id), ""
    pth = ensure_infer_pth(voice_id)
    if pth:
        return Path(pth), _find_index(voice_id), ""
    item = find_manifest_item(voice_id)
    if not item:
        return None, "", f"音色 [{voice_id}] 没有可推理的模型，也不在音色市场清单里"
    dl = item.get("download")
    if not dl:
        return None, "", f"市场音色 [{voice_id}] 缺少下载直链，无法自动获取"
    try:
        staged = _ensure_staged(voice_id, dl)
    except Exception as e:  # noqa: BLE001
        return None, "", f"音色 [{voice_id}] 权重下载失败：{e}"
    # 暂存权重没有配套 index → 显式跳过特征检索（与市场试听同一策略）
    return Path(staged), "", ""


# ---------------- 客观分 ----------------


def _score_batch(jobs: list[dict]) -> dict:
    """一次子进程算完整批客观分 → {key: {secs, nats, score_error}}。

    为什么**必须**走子进程：打分器（CAM++ + whisper-small）在后端主进程里加载会占住
    那个 CUDA 上下文，之后每个音色的 RVC 推理子进程就会 `cuDNN CUDNN_STATUS_EXECUTION_FAILED`
    （实测「第一个成功、第二个起全失败」）。详见 audition_score.py 顶部注释。
    子进程跑完即退出、显存全还，且整批只加载一次模型（逐个起进程要多等 N 倍加载时间）。

    失败绝不抛：打分是附加信息，不能因为它把试音结果变成失败。
    """
    if not jobs:
        return {}
    stamp = int(time.time() * 1000)
    jobs_p = AUDITION_DIR / f".score_jobs_{stamp}.json"
    out_p = AUDITION_DIR / f".score_out_{stamp}.json"
    try:
        jobs_p.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(SCORE_PY), "--jobs", str(jobs_p), "--out", str(out_p)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=SCORE_TIMEOUT, encoding="utf-8", errors="replace",
                           cwd=str(cfg.ROOT))
        if out_p.exists():
            data = json.loads(out_p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "__error__" not in data:
                return data
            return {j["key"]: {"secs": None, "nats": None,
                               "score_error": str(data.get("__error__") or "打分失败")}
                    for j in jobs}
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return {j["key"]: {"secs": None, "nats": None,
                           "score_error": "打分进程未产出结果：" + (" | ".join(tail)[-300:] or "无输出")}
                for j in jobs}
    except subprocess.TimeoutExpired:
        return {j["key"]: {"secs": None, "nats": None,
                           "score_error": f"打分超时（>{SCORE_TIMEOUT}s），已跳过"}
                for j in jobs}
    except Exception as e:  # noqa: BLE001
        return {j["key"]: {"secs": None, "nats": None, "score_error": f"打分失败：{e}"}
                for j in jobs}
    finally:
        for p in (jobs_p, out_p):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _duration_s(out: Path) -> "float | None":
    """产物时长（只读文件头，不解码），顺带当作"产物是否完好"的廉价校验。"""
    try:
        import soundfile as sf
        info = sf.info(str(out))
        return round(info.frames / info.samplerate, 1) if info.samplerate else None
    except Exception:
        return None


# ---------------- 单件试音 ----------------


def _cache_path(voice_id: str, mode: str, src: Path | None, text: str,
                pitch: int, index_rate: float) -> Path:
    """产物路径 = 参数哈希。同样参数重复点不会重跑推理（结果直接复用）。"""
    key = f"{mode}|{src.name if src else ''}|{text}|{pitch}|{index_rate}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return AUDITION_DIR / f"aud_{voice_id}_{h}.wav"


def _try_one(voice_id: str, mode: str, src: Path | None, text: str,
             pitch: int, index_rate: float) -> tuple[Path, bool]:
    """试音一个音色 → (产物路径, 是否命中已有缓存)。异常带可读原因抛出。"""
    out = _cache_path(voice_id, mode, src, text, pitch, index_rate)
    if out.exists() and out.stat().st_size > 1024:
        return out, True

    if mode == "text":
        vb = _voicebank_for(voice_id)
        if not vb:
            raise RuntimeError(
                f"音色 [{_display_name(voice_id)}] 没有参考音，无法用文字合成"
                f"（市场 RVC 权重只能换音色；要输字请先训练一个音色）")
        ref = cfg.MEDIA_DIR / "voicebank" / vb / "reference.wav"
        from qwen3_tts import tts as qwen_tts
        data = qwen_tts(text, ref_audio=str(ref), ref_text="", voice_id=vb)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return out, False

    if src is None:
        raise RuntimeError("缺少源音频")
    pth, index, err = _resolve_weight(voice_id)
    if pth is None:
        raise RuntimeError(err)
    _rvc_link(voice_id, src, out, pth=pth, index=index,
              pitch=pitch, index_rate=index_rate)
    return out, False


# ---------------- 状态更新 ----------------


def _mark(**kw) -> None:
    with _STATE_LOCK:
        AUDITION_STATE.update(kw)


def _snapshot() -> dict:
    with _STATE_LOCK:
        return json.loads(json.dumps(AUDITION_STATE, ensure_ascii=False))


def _set_result(entry: dict) -> None:
    """按 voice_id 覆盖式写入结果（同一任务里一个音色只出现一次）。"""
    with _STATE_LOCK:
        for i, r in enumerate(AUDITION_STATE["results"]):
            if r.get("voice_id") == entry["voice_id"]:
                AUDITION_STATE["results"][i] = entry
                return
        AUDITION_STATE["results"].append(entry)


# ---------------- 任务线程 ----------------


def _same_task(task_id: str) -> bool:
    """打分线程期间用户可能已经开了下一轮试音 —— 那时旧结果绝不能写回去。

    评分在独占位释放之后异步跑（见 _score_pass），所以"跑着分数、又点了开始"
    是完全可能的。没有这道闸，第一轮的分数会盖进第二轮的结果列表，
    表现为"刚跑的两个音色显示的是上一轮的分"。
    """
    with _STATE_LOCK:
        return str(AUDITION_STATE.get("task_id") or "") == task_id


def _score_pass(task_id: str) -> None:
    """阶段二：结果都出来之后，再统一算客观分（异步，不挡住结果展示）。

    刻意与推理分开（不是图省事）：打分器一加载就占住本进程的 CUDA 上下文，
    紧接着的 RVC 子进程就会 cuDNN 报错。所以顺序必须是「所有推理 → 再打分」，
    且打分完成后独占位早已释放，用户可以马上去开实时变声。
    """
    try:
        snap = _snapshot()
        pending = [r for r in snap["results"]
                   if r.get("status") == "done" and r.get("url")]
        if not pending:
            return
        _mark(scoring=True, score_finished=0, score_total=len(pending))
        jobs = [{"key": r["voice_id"],
                 "wav": str(AUDITION_DIR / Path(r["url"]).name),
                 "ref": str(_ref_for(r["voice_id"]) or "")}
                for r in pending]
        scores = _score_batch(jobs)
        for r in pending:
            if not _same_task(task_id):
                return          # 已经换了一轮任务，这批分作废
            s = scores.get(r["voice_id"]) or {}
            entry = dict(r)
            entry["secs"] = s.get("secs")
            entry["nats"] = s.get("nats")
            entry["score_error"] = s.get("score_error") or ""
            _set_result(entry)
            with _STATE_LOCK:
                AUDITION_STATE["score_finished"] = AUDITION_STATE.get("score_finished", 0) + 1
    except Exception:  # noqa: BLE001
        pass          # 打分是附加信息，任何异常都不该影响已经出来的结果
    finally:
        if _same_task(task_id):
            _mark(scoring=False)


def _worker(task_id: str, voice_ids: list[str], mode: str,
            src: Path | None, text: str, pitch: int, index_rate: float,
            score: bool = True) -> None:
    try:
        # 阶段一：只做推理。这里绝不允许加载任何打分模型 —— 一旦占了本进程的
        # CUDA 上下文，后面的 RVC 子进程会 cuDNN 崩（见 audition_score.py）。
        for idx, vid in enumerate(voice_ids):
            if _CANCEL.is_set():
                _mark(status="cancelled", current="", current_name="",
                      message=f"已取消（完成 {idx}/{len(voice_ids)}）")
                break
            name = _display_name(vid)
            _mark(current=vid, current_name=name,
                  message=f"正在试音「{name}」（{idx + 1}/{len(voice_ids)}）…")
            entry = {"voice_id": vid, "display_name": name, "status": "running",
                     "url": "", "secs": None, "nats": None, "duration_s": None,
                     "error": "", "score_error": "", "from_cache": False}
            _set_result(entry)
            try:
                out, cached = _try_one(vid, mode, src, text, pitch, index_rate)
                entry["status"] = "done"
                entry["url"] = f"/api/media/outputs/audition/{out.name}"
                entry["from_cache"] = cached
                entry["duration_s"] = _duration_s(out)
                try:
                    from history import register as history_register
                    history_register("trial", vid, out.name, entry["url"],
                                     entry.get("duration_s") or 0.0,
                                     input_text=text or "",
                                     params={"mode": mode, "pitch": pitch,
                                             "index_rate": index_rate,
                                             "source": src.name if src else ""})
                except Exception:
                    pass          # 登记失败不影响试音结果
            except Exception as e:  # noqa: BLE001
                entry["status"] = "failed"
                entry["error"] = str(e)
            _set_result(entry)
            with _STATE_LOCK:
                AUDITION_STATE["finished"] = idx + 1
        else:
            _mark(status="done", current="", current_name="",
                  message=f"试音完成（{len(voice_ids)} 个）")
    except Exception as e:  # noqa: BLE001
        _mark(status="error", error=str(e), message="试音中断")
    finally:
        # 先释放独占位与锁，再决定要不要打分：用户不该为了几个附加分数
        # 而等着开不了实时变声。
        _mark(running=False)
        _CANCEL.clear()
        release_gpu("audition")
        _TASK_LOCK.release()
    if score and _snapshot()["status"] == "done":
        threading.Thread(target=_score_pass, args=(task_id,), daemon=True).start()


# ---------------- API ----------------


@router.get("/audition/env")
def audition_env():
    """试音前的环境态势：谁在占 GPU、显存余量、TTS 引擎是否在线。

    前端据此禁用按钮并给出原因 —— 而不是让用户点下去收一串 409。
    """
    live_running = cascade_running = offline_running = False
    live_exp = ""
    try:
        from rvc_live import _live_proc_alive, _state
        live_running = bool(_live_proc_alive())
        live_exp = str((_state.get("train") or {}).get("exp") or "")
    except Exception:
        pass
    try:
        from cascade import _cascade_alive
        cascade_running = bool(_cascade_alive())
    except Exception:
        pass
    try:
        from offline_vc import OFFLINEVC_STATE
        offline_running = bool(OFFLINEVC_STATE.get("running"))
    except Exception:
        pass
    tts_worker = False
    try:
        from qwen3_tts import worker_alive
        tts_worker = bool(worker_alive())
    except Exception:
        pass

    total = used = None
    try:
        from rvc_live import _gpu_snapshot
        snap = _gpu_snapshot()
        total, used = snap.get("gpu_total_mb"), snap.get("gpu_used_mb")
    except Exception:
        pass
    min_free = 2048
    try:
        from rvc_live import MIN_LIVE_FREE_VRAM_MB
        min_free = int(MIN_LIVE_FREE_VRAM_MB)
    except Exception:
        pass
    free = (total - used) if (total and used is not None) else None

    holder = gpu_holder_reason()
    if not holder:
        for running, why in ((live_running, "实时变声正在运行"),
                             (cascade_running, "级联变声正在运行"),
                             (offline_running, "离线变声任务正在运行")):
            if running:
                holder = why
                break
    low_vram = free is not None and free < min_free
    return {
        "live_running": live_running, "live_exp": live_exp,
        "cascade_running": cascade_running, "offline_running": offline_running,
        "tts_worker": tts_worker,
        "gpu_total_mb": total, "gpu_used_mb": used, "gpu_free_mb": free,
        "min_free_vram_mb": min_free, "low_vram": low_vram,
        "busy_reason": holder,
        # 离线批量能否开跑（实时/级联/离线在跑或显存不足都不行）
        "batch_ready": not holder and not low_vram,
        # 文字路径还要求 TTS 引擎在线（离线批量不要求，它走 RVC）
        "text_ready": not holder and not low_vram,
    }


@router.get("/audition/sources")
def audition_sources():
    """试音间里已备好的源音频（最近在前），供前端复用/展示。"""
    import soundfile as sf
    items = []
    files = sorted(AUDITION_DIR.glob("src_*.wav"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for p in files[:SRC_KEEP]:
        try:
            info = sf.info(str(p))
            dur = round(info.frames / info.samplerate, 1) if info.samplerate else 0.0
        except Exception:
            dur = 0.0
        items.append({"source_id": p.stem, "url": f"/api/media/outputs/audition/{p.name}",
                      "duration_s": dur, "builtin": p.stem == "src_builtin",
                      "created_at": int(p.stat().st_mtime)})
    return {"sources": items}


def _active_source_id() -> str:
    """当前任务正在使用的源音频 id（无任务时返回空串）。

    注意别拿 `AUDITION_STATE["current"]` 当它 —— current 是**音色** id，
    源音频在 `source_name`（如 `src_1737...wav`）。用错了的后果是：
    保护形同虚设，清理/删除可能把正在被读取的源文件干掉，
    批量任务跑到一半报"源音频不存在"。
    """
    with _STATE_LOCK:
        name = str(AUDITION_STATE.get("source_name") or "")
        running = bool(AUDITION_STATE.get("running"))
    if not name or not running:
        return ""
    return name[:-4] if name.endswith(".wav") else name


def _prune_sources(keep_id: str = "") -> None:
    """源音频超过硬上限时清掉最旧的（跳过正在使用的那个）。"""
    files = sorted(AUDITION_DIR.glob("src_*.wav"), key=lambda p: p.stat().st_mtime)
    if len(files) <= SRC_HARD_CAP:
        return
    protected = {keep_id, _active_source_id(), "src_builtin"}
    for p in files[:len(files) - SRC_KEEP]:
        if p.stem in protected:
            continue
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _store_source(raw: bytes, suffix: str) -> dict:
    """落地源音频：先转 16k 单声道 wav（同一段试音素材喂给所有音色，只转一次），
    再校验时长，返回 source_id 与时长。"""
    stamp = int(time.time() * 1000)
    src_id = f"src_{stamp}"
    raw_path = AUDITION_DIR / f"{src_id}_raw{suffix or '.wav'}"
    out_path = AUDITION_DIR / f"{src_id}.wav"
    raw_path.write_bytes(raw)
    try:
        _preprocess16k(raw_path, out_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail=f"音频无法识别（支持 wav/mp3/m4a/ogg/webm 等）：{e}")
    finally:
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        import soundfile as sf
        info = sf.info(str(out_path))
        dur = info.frames / info.samplerate if info.samplerate else 0.0
    except Exception:
        dur = 0.0
    if dur < MIN_SOURCE_S:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400,
                            detail=f"音频太短（{dur:.1f}s），至少要说满 {MIN_SOURCE_S:.0f} 秒")
    if dur > MAX_SOURCE_S:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400,
                            detail=f"音频太长（{dur:.0f}s），试音间请用 {MAX_SOURCE_S:.0f} 秒以内的片段"
                                   f"（每个音色要单独跑一遍，源太长会等到很久）")
    _prune_sources(keep_id=src_id)
    return {"source_id": src_id, "url": f"/api/media/outputs/audition/{out_path.name}",
            "duration_s": round(dur, 1)}


@router.post("/audition/source")
async def audition_source(file: UploadFile = File(...)):
    """上传/录音落成试音音频（同一段素材会被所有选中音色共用）。"""
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    if not raw:
        raise HTTPException(status_code=400, detail="上传内容为空")
    suffix = Path(file.filename or "a.wav").suffix.lower() or ".wav"
    return _store_source(raw, suffix)


@router.post("/audition/source/builtin")
def audition_source_builtin():
    """用内置中性中文人声当源音频（不想录音、或手上没有麦克风时的一键试音）。

    这段素材与任何目标音色没有源关系（市场试听用的同一份），所以输出只呈现
    目标音色本身，不会因为"源句自带袋鼠腔"而污染所有试音结果。
    """
    if not BUILTIN_SRC.exists():
        raise HTTPException(status_code=404, detail=f"内置源句缺失：{BUILTIN_SRC}")
    dst = AUDITION_DIR / "src_builtin.wav"
    if not dst.exists():
        try:
            _preprocess16k(BUILTIN_SRC, dst)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"内置源句转码失败：{e}")
    try:
        import soundfile as sf
        info = sf.info(str(dst))
        dur = round(info.frames / info.samplerate, 1) if info.samplerate else 0.0
    except Exception:
        dur = 0.0
    return {"source_id": dst.stem, "url": f"/api/media/outputs/audition/{dst.name}",
            "duration_s": dur, "builtin": True}


class TryRequest(BaseModel):
    voice_ids: list[str]
    source_id: str = ""
    text: str = ""
    pitch: int = 0
    index_rate: float = 0.5
    # 客观分（音色像度 + 自然度）默认算；关掉能省一次打分器加载（8GB 卡上更稳）
    score: bool = True


@router.post("/audition/try")
def audition_try(req: TryRequest):
    """开一个批量试音任务（后台串行执行，前端轮询 /audition/task 看结果陆续出来）。

    音频模式：source_id + voice_ids[]（一个声音试 N 个音色）
    文字模式：text + voice_ids[]（同一句话各音色各合成一遍；仅带参考音的音色可用）
    """
    voice_ids: list[str] = []
    for v in req.voice_ids:
        v = (v or "").strip()
        if v and v not in voice_ids:       # 去重但保序：用户多选时不该重复跑
            voice_ids.append(v)
    if not voice_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个音色")
    bad = [v for v in voice_ids if not is_valid_voice_id(v)]
    if bad:
        raise HTTPException(status_code=400, detail=f"音色 ID 非法：{bad[0]}")

    text = (req.text or "").strip()
    mode = "text" if (text and not req.source_id) else "audio"
    src: Path | None = None
    if mode == "audio":
        sid = (req.source_id or "").strip()
        if not _SRC_RE.match(sid):
            raise HTTPException(status_code=400, detail="source_id 非法")
        src = AUDITION_DIR / f"{sid}.wav"
        if not src.exists():
            raise HTTPException(status_code=404, detail=f"源音频 {sid} 不存在，请重新录制或上传")
    else:
        if not text:
            raise HTTPException(status_code=400, detail="请提供要合成的话")
        if len(text) > 200:
            raise HTTPException(status_code=400, detail="文字过长（上限 200 字）")

    if not 0.0 <= req.index_rate <= 1.0:
        raise HTTPException(status_code=400, detail="index_rate 需在 0~1 之间")
    if not -24 <= req.pitch <= 24:
        raise HTTPException(status_code=400, detail="pitch 需在 -24~+24 半音之间")

    busy = gpu_holder_reason()
    if busy:
        raise HTTPException(status_code=409, detail=f"{busy}，请先等它结束（避免争抢显卡）")
    try:
        from rvc_live import _live_proc_alive
        if _live_proc_alive():
            raise HTTPException(status_code=409,
                                detail="实时变声正在运行，请先停止再批量试音（避免争抢显卡）")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        from cascade import _cascade_alive
        if _cascade_alive():
            raise HTTPException(status_code=409,
                                detail="级联变声正在运行，请先停止再批量试音（避免争抢显卡）")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        from offline_vc import OFFLINEVC_STATE
        if OFFLINEVC_STATE.get("running"):
            raise HTTPException(status_code=409, detail="离线变声任务正在运行，请先等它完成")
    except HTTPException:
        raise
    except Exception:
        pass

    if not _TASK_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有试音任务在跑，请先等它结束或取消")
    if not hold_gpu("audition", "试音间正在批量试音"):
        _TASK_LOCK.release()
        raise HTTPException(status_code=409, detail="GPU 已被其他任务占用，请稍后再试")

    _CANCEL.clear()
    task_id = f"aud_{int(time.time() * 1000)}"
    _mark(task_id=task_id, running=True, status="running", mode=mode,
          total=len(voice_ids), finished=0, current="", current_name="",
          message="排队中…", error="", results=[],
          scoring=False, score_finished=0, score_total=0,
          source_name=src.name if src else "", text=text)
    threading.Thread(target=_worker, daemon=True,
                     args=(task_id, voice_ids, mode, src, text,
                           int(req.pitch), float(req.index_rate), bool(req.score))).start()
    return {"ok": True, "task_id": task_id, "total": len(voice_ids), "mode": mode,
            "score": bool(req.score)}


@router.get("/audition/task")
def audition_task():
    """当前试音任务状态与已出结果（逐件追加，可边跑边听）。"""
    return _snapshot()


@router.post("/audition/cancel")
def audition_cancel():
    """取消批量试音：当前这一件跑完就停，不再开下一个。"""
    if not _TASK_LOCK.locked() and not _snapshot().get("running"):
        return {"ok": True, "cancelled": False, "message": "当前没有试音任务"}
    _CANCEL.set()
    _mark(message="正在取消（等当前这件跑完）…")
    return {"ok": True, "cancelled": True}


@router.delete("/audition/source/{source_id}")
def audition_source_delete(source_id: str):
    """删掉一个不再需要的源音频（内置源句与使用中的源不允许删）。"""
    if not _SRC_RE.match(source_id):
        raise HTTPException(status_code=400, detail="source_id 非法")
    if source_id == "src_builtin":
        raise HTTPException(status_code=400, detail="内置源句不可删除")
    if source_id == _active_source_id():
        raise HTTPException(status_code=409, detail="该源音频正在被当前试音任务使用")
    p = AUDITION_DIR / f"{source_id}.wav"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"源音频 {source_id} 不存在")
    try:
        p.unlink()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")
    return {"ok": True, "source_id": source_id}


@router.get("/audition/history")
def audition_history(limit: int = 50):
    """试音间产出的历史（复用作品库，kind=trial），供试音间内快速回看。"""
    limit = max(1, min(int(limit), 100))
    try:
        from history import query as history_query
        return history_query(kind="trial", limit=limit)
    except Exception:
        return {"items": [], "total": 0, "limit": limit, "offset": 0}

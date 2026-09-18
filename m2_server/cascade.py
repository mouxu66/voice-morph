"""级联变声 router：录音 → ASR → TTS → 虚拟声卡（文字中转，消除口音）。

链路与 RVC 实时变声共用同一出口（CABLE Input → CABLE Output），微信等应用
无需任何改动，两种方案可无缝切换。核心差异：RVC 保留源说话人发音特征，
级联走文字中转，输出只含目标音色。

接口：
    POST /api/cascade/start   body {ref_audio?, ref_text?, chunk_max_s?, mode?}
    POST /api/cascade/stop
    GET  /api/cascade/status

子进程 cascade_stream.py 跑在 RVC venv（唯一有 sounddevice 的环境），
状态写 outputs/cascade_state.json，本模块读取合并后返回前端。

与 RVC 实时互斥（坑 #7）：两者抢 GPU（8GB）且都要占 CABLE，
start 前双向检查，任一在跑都返回 409。

声卡还原三层保障（坑 #6，与 rvc_live 同一套机制与备份文件）：
    1. 守护线程在子进程退出后自动 restore
    2. restore 失败走 reset 兜底
    3. 服务器启动时 rvc_live._auto_clean 检查残留备份并还原（共用备份文件）
"""
import json
import os
import subprocess
import threading
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config as cfg
import prosody_relay
from rvc_common import (ensure_infer_pth, find_index, _find_pids_by_cmdline, _kill_pids)
from rvc_live import _audio, _reset_audio, _thread_env

logger = logging.getLogger(__name__)

ROOT = cfg.ROOT
STREAM_PY = Path(__file__).resolve().parent / "cascade_stream.py"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
STATE_FILE = cfg.OUTPUTS_DIR / "cascade_state.json"
RUN_LOG = cfg.OUTPUTS_DIR / "cascade_run.log"
# 兜底参考音（经 config 走 VM_DEFAULT_REF，不再写死作者本机的音色文件）
DEFAULT_REF = cfg.DEFAULT_REF_AUDIO

# 播放端（渲染端）关键词，`|` 分隔多候选：中文 Windows 下 MME 枚举成
# 「扬声器 (VB-Audio Virtual Cable)」（驱动名命中），英文下是
# `CABLE Input (VB-Audio Virtual C`（截断到 31 字符，端点词命中）。
# 2026-09-19 事故：写死单个 "CABLE Input"，名字一漂移级联就启动即退出（桌宠报"启动失败"）。
# 四处默认值必须一致，见 cascade_stream.py 同处注释与 tests/test_cable_keyword_consistency.py。
OUTPUT_DEVICE = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "VB-Audio Virtual Cable|CABLE Input")

_NO_WINDOW = 0x08000000

_STATE = {
    "running": False, "pid": None, "audio_switched": False,
    "error": "", "started_at": "",
}
_warming = False
_pid_cache: dict = {"ts": None, "pids": []}


def _find_cascade_pids() -> list[int]:
    """按命令行找 cascade_stream.py 进程（覆盖服务重启后内存 pid 丢失）。

    带 1s 缓存：status 每 3s 被前端轮询，每次都起 PowerShell 进程太重。
    """
    now = time.time()
    if _pid_cache["ts"] is not None and now - _pid_cache["ts"] < 1.0:
        return _pid_cache["pids"]
    pids = _find_pids_by_cmdline("cascade_stream")
    _pid_cache.update(ts=now, pids=pids)
    return pids


def _cascade_alive() -> bool:
    return bool(_find_cascade_pids())


def _worker_health(timeout: float = 2.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/health",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except Exception as e:
        logger.debug("[cascade] 健康检查端口 8001 不可达（worker 未起）: %s", e)
        return False


def _warm_worker():
    """后台预热 8001（懒启动要加载 4GB 模型，约 26~35s，坑 #3）。"""
    global _warming
    if _warming:
        return
    _warming = True
    try:
        from qwen3_tts import ensure_worker
        ensure_worker()
    except Exception as e:
        _STATE["error"] = f"worker 预热失败: {e}"
        print(f"[cascade] {_STATE['error']}", flush=True)
    finally:
        _warming = False


def _read_child_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("[cascade] 读取子进程状态文件失败（返回空）: %s", e)
        return {}


class CascadeStartReq(BaseModel):
    voice_id: str | None = None   # 音色库 ID（media/voicebank/<id>/reference.wav）
    ref_audio: str | None = None  # 显式参考音频路径（优先级低于 voice_id）
    ref_text: str = ""
    chunk_max_s: float | None = None
    silence_ms: int | None = None
    prime_s: float | None = None  # 播放预缓冲秒数（越大越抗抖动、首块越晚出声）
    mode: str = "stream"  # stream=按句流式 | whole=等整段说完（低打断感、高延迟）
    rvc_voice: str | None = None  # 末尾接 RVC 的音色 ID（空=TTS 直出，音色靠克隆）


router = APIRouter(prefix="/api")


@router.post("/cascade/start")
def cascade_start(req: CascadeStartReq | None = None):
    body = req or CascadeStartReq()
    if _cascade_alive():
        return JSONResponse({"ok": True, "already_running": True, "pid": _STATE["pid"]})
    from rvc_live import _live_proc_alive
    if _live_proc_alive():
        raise HTTPException(status_code=409,
                            detail="实时变声正在运行，请先停止（两者抢 GPU 且都占 CABLE）")
    ref_audio = body.ref_audio
    if body.voice_id:
        ref = cfg.MEDIA_DIR / "voicebank" / body.voice_id / "reference.wav"
        if not ref.exists():
            raise HTTPException(status_code=404,
                                detail=f"音色 [{body.voice_id}] 不存在或没有参考音频")
        ref_audio = str(ref)

    # 末尾接 RVC 时，TTS 参考音优先跟目标音色同源（用户选的「语气=目标音色腔调」）：
    # 有该音色的参考音频就用，没有（市场下载的多数如此）则回退内置默认，
    # 此时腔调来自默认参考音、音色仍由 RVC 决定。
    rvc_pth = rvc_index = ""
    if body.rvc_voice:
        pth = ensure_infer_pth(body.rvc_voice)
        if pth is None:
            raise HTTPException(status_code=404,
                                detail=f"音色 [{body.rvc_voice}] 没有可推理的 RVC 模型，先到实时变声页训练")
        rvc_pth = str(pth)
        idx = find_index(body.rvc_voice)
        if idx:
            rvc_index = str(idx)
        if not body.ref_audio:
            ref_audio = prosody_relay.resolve_ref_audio(body.rvc_voice)
    if not ref_audio:
        ref_audio = str(DEFAULT_REF)
    if not Path(ref_audio).exists():
        raise HTTPException(status_code=404, detail=f"参考音频不存在: {ref_audio}")
    if not RVC_VENV_PY.exists():
        raise HTTPException(status_code=500, detail=f"RVC venv 缺失: {RVC_VENV_PY}")
    if not STREAM_PY.exists():
        raise HTTPException(status_code=500, detail=f"子进程脚本缺失: {STREAM_PY}")

    # 坑 #3：worker 懒启动，未就绪时先返回 warming 让前端展示「模型加载中」，
    # 前台轮询 status 等 worker_ready 后再次 start（第二次走完整启动）
    if not _worker_health():
        threading.Thread(target=_warm_worker, daemon=True).start()
        return JSONResponse({
            "ok": True, "warming": True,
            "hint": "TTS 模型加载中（首次约 30s），完成后会自动就绪，请稍候重试",
        })

    chunk_max_s = body.chunk_max_s
    if chunk_max_s is None:
        chunk_max_s = 60.0 if body.mode == "whole" else 6.0

    # 切声卡：录音默认 → CABLE Output（首次自动备份原设备；还原三层保障见模块注释）
    try:
        _audio("apply")
        _STATE["audio_switched"] = True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切换虚拟声卡失败: {e}")

    # 状态文件与输出目录显式对齐本服务的 cfg.OUTPUTS_DIR：
    # 安装版 VM_OUTPUTS_DIR 指向数据目录（D:\变声\outputs），子进程按 __file__
    # 推导会落到 resources/backend/outputs，两边对不上导致 status 永远空
    cmd = [str(RVC_VENV_PY), str(STREAM_PY),
           "--ref-audio", ref_audio, "--ref-text", body.ref_text,
           "--chunk-max-s", str(chunk_max_s),
           "--state-path", str(STATE_FILE),
           "--out-dir", str(cfg.OUTPUTS_DIR)]
    if body.silence_ms:
        cmd += ["--silence-ms", str(body.silence_ms)]
    if body.prime_s is not None:
        cmd += ["--prime-s", str(body.prime_s)]
    if rvc_pth:
        cmd += ["--rvc-pth", rvc_pth, "--rvc-index", rvc_index]

    # 存档本次启动参数：全局热键一键重启时复用（否则只能用默认音色）
    try:
        (cfg.OUTPUTS_DIR / "cascade_last_start.json").write_text(
            json.dumps({
                "voice_id": body.voice_id, "ref_audio": ref_audio,
                "ref_text": body.ref_text, "chunk_max_s": chunk_max_s,
                "silence_ms": body.silence_ms, "prime_s": body.prime_s,
                "mode": body.mode, "rvc_voice": body.rvc_voice,
            }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("[cascade] 写入启动参数快照失败（可忽略）: %s", e)
    logf = open(RUN_LOG, "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf,
                                stderr=subprocess.STDOUT,
                                creationflags=_NO_WINDOW,
                                # 限线程：级联链路里 RVC + whisper + TTS 三段同样会
                                # 各自按逻辑核数开线程池，不限制会瞬时拉满整机 CPU。
                                env={**os.environ, **_thread_env()})
        _pid_cache["ts"] = None
    except Exception as e:
        logf.close()
        _STATE.update(running=False, pid=None, audio_switched=False)
        ok, detail = _reset_audio()
        msg = f"拉起级联子进程失败: {e}"
        if not ok:
            msg += f"；自动还原声卡也失败({detail})，请点「一键恢复音频」"
        raise HTTPException(status_code=500, detail=msg)

    # 秒退检测：设备解析/worker 等待都在启动路径上，给 6s 窗口
    deadline = time.time() + 6
    while time.time() < deadline:
        if proc.poll() is not None:
            logf.close()
            _STATE.update(running=False, pid=None, audio_switched=False)
            ok, detail = _reset_audio()
            msg = "级联子进程启动即退出（日志见 outputs/cascade_run.log）"
            if not ok:
                msg += f"；自动还原声卡失败({detail})，请点「一键恢复音频」"
            raise HTTPException(status_code=500, detail=msg)
        time.sleep(1)
    logf.close()

    _STATE.update(running=True, pid=proc.pid, audio_switched=True, error="",
                  started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    threading.Thread(target=_cascade_waiter, args=(proc,), daemon=True).start()
    return JSONResponse({
        "ok": True, "pid": proc.pid, "output_device": OUTPUT_DEVICE,
        "chunk_max_s": chunk_max_s, "mode": body.mode,
        "hint": "已把系统录音设备切到 CABLE Output；对着麦克风说话即可，"
                "说完一句约 1.5~2s 后播出目标音色。点「停止」自动还原声卡",
    })


def _cascade_waiter(proc: subprocess.Popen):
    proc.wait()
    error = ""
    try:
        _audio("restore")
    except Exception as e:
        error = f"自动还原声卡失败: {e}"
        print(f"[cascade_waiter] {error}，尝试 reset 兜底", flush=True)
        ok, detail = _reset_audio()
        if not ok:
            error = f"自动还原声卡失败: {e}；reset 兜底也失败: {detail}"
            print(f"[cascade_waiter] {error}", flush=True)
        else:
            error = ""
    _STATE.update(running=False, pid=None, audio_switched=False, error=error)


@router.post("/cascade/stop")
def cascade_stop():
    targets = set(_find_cascade_pids())
    if not targets and _STATE["pid"]:
        targets.add(_STATE["pid"])
    _kill_pids(list(targets), "stop")
    _pid_cache["ts"] = None
    _STATE.update(running=False, pid=None, audio_switched=False)
    try:
        _audio("restore")
        _STATE["error"] = ""
        return JSONResponse({"ok": True, "restored": True})
    except Exception as e:
        ok, detail = _reset_audio()
        if ok:
            _STATE["error"] = ""
            return JSONResponse({"ok": True, "restored": True,
                                 "note": f"restore 失败({e})，已用 reset 兜底恢复"})
        _STATE["error"] = f"还原声卡失败: {e}；reset 兜底失败: {detail}"
        return JSONResponse({"ok": False, "error": _STATE["error"],
                             "fallback_failed": True})


@router.get("/cascade/last-start")
def cascade_last_start():
    """上次启动参数（供全局热键等外部调用方复用）。"""
    try:
        return json.loads(
            (cfg.OUTPUTS_DIR / "cascade_last_start.json").read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("[cascade] 读取最近一次启动参数失败（返回空）: %s", e)
        return {}


@router.get("/cascade/status")
def cascade_status():
    alive = _cascade_alive()
    child = _read_child_state()
    stage = child.get("stage") if alive else "idle"
    # 子进程死后残留的状态文件不能继续谎报 running/playing
    if not alive and stage not in ("idle",):
        stage = "idle"
    return {
        "ok": True,
        "running": alive,
        "pid": _STATE["pid"] if alive else None,
        "stage": stage,
        "worker_ready": _worker_health(),
        "warming": _warming,
        "audio_switched": _STATE["audio_switched"],
        "last_error": _STATE.get("error", ""),
        # 子进程上报的细粒度状态
        "last_text": child.get("last_text", ""),
        "last_asr_s": child.get("last_asr_s", 0.0),
        "last_tts_s": child.get("last_tts_s", 0.0),
        # 分阶段耗时统计（最近 30 块 avg/p95，供前端定位瓶颈）
        "avg_asr_s": child.get("avg_asr_s", 0.0),
        "p95_asr_s": child.get("p95_asr_s", 0.0),
        "avg_tts_s": child.get("avg_tts_s", 0.0),
        "p95_tts_s": child.get("p95_tts_s", 0.0),
        # 末尾接 RVC（音色归属）：rvc_voice 为空=未启用
        "rvc_voice": child.get("rvc_voice", ""),
        "rvc_error": child.get("rvc_error", ""),
        "last_rvc_s": child.get("last_rvc_s", 0.0),
        "last_audio_s": child.get("last_audio_s", 0.0),
        "last_fast": child.get("last_fast"),
        "chunks": child.get("chunks", 0),
        "dropped": child.get("dropped", 0),
        "avg_latency_s": child.get("avg_latency_s", 0.0),
        "last_latency_s": child.get("last_latency_s", 0.0),
        "queued_s": child.get("queued_s", 0.0),
        "input_device": child.get("input_device", ""),
        "output_device": child.get("output_device", OUTPUT_DEVICE),
        "child_error": child.get("error", ""),
        "updated_at": child.get("updated_at", ""),
    }

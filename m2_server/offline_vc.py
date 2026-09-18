"""离线变声工作台：录音/音频文件 → （可选降噪）→ RVC 离线整段推理 → 导出 48k wav。

接口：
    POST /api/offlinevc/run    multipart 上传音频 + 指定音色/变调/降噪，提交后台转换
    GET  /api/offlinevc/status 轮询进度与结果

设计：
    - 推理在 D:\\RVC\\.venv 子进程里跑 offline_vc_infer.py（与主服务 torch 隔离，
      与 rvc_live 实时变声同一环境）；上传音频（webm/wav/…）先经 ffmpeg 统一转
      16k 单声道 wav，可选降噪（P2-5：优先 DeepFilterNet 模型增强，不可用回退 afftdn）。
    - 同一时刻只允许一个转换任务，且实时变声运行中会拒绝（避免抢 GPU）。
"""
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

import config as cfg
from common import MAX_UPLOAD_BYTES, find_ffmpeg
from rvc_common import ensure_infer_pth
from rvc_live import _live_proc_alive

OUT = cfg.OUTPUTS_DIR
OUT.mkdir(exist_ok=True)
INFER_PY = Path(__file__).resolve().parent / "offline_vc_infer.py"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"

router = APIRouter(prefix="/api")

OFFLINEVC_STATE: dict = {
    "running": False,
    "status": "idle",        # idle | running | done | error
    "message": "",
    "voice_id": "",
    "url": "",
    "duration_s": 0.0,
    "error": "",
}
_ovc_lock = threading.Lock()


@router.post("/offlinevc/run")
async def offlinevc_run(
    file: UploadFile = File(...),
    voice_id: str = Form(""),
    pitch: int = Form(0),
    index_rate: float = Form(0.5),
    denoise: bool = Form(False),
    post_seedvc: bool = Form(False),
    enhance_level: str = Form("standard"),
    prosody: str = Form("keep"),
):
    """提交离线变声任务。pitch 为半音数（男转女 +12，女转男 -12）。

    prosody 语气来源：
        keep  = 保留源音频的抑扬顿挫（RVC 默认行为，含说话人口音与口头禅）
        relay = 重铸语气：先 ASR 转文字，再用目标音色的参考音重新合成，
                然后才送进 RVC —— 口音/口头禅/犹豫被清掉，音色仍由 RVC 决定
    """
    if prosody not in ("keep", "relay"):
        raise HTTPException(status_code=400, detail=f"prosody 参数非法: {prosody}")
    with _ovc_lock:
        if OFFLINEVC_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有转换任务在跑，请稍候")
        if not voice_id:
            raise HTTPException(status_code=400, detail="请先选择音色")
        pth = ensure_infer_pth(voice_id)
        if pth is None:
            raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 没有可推理的 RVC 模型，先到实时变声页训练")
        if not RVC_VENV_PY.exists():
            raise HTTPException(status_code=500, detail="RVC 运行环境缺失")
        if _live_proc_alive():
            raise HTTPException(status_code=409, detail="实时变声正在运行，请先停止后再离线转换（避免争抢显卡）")
        from cascade import _cascade_alive
        if _cascade_alive():
            raise HTTPException(status_code=409, detail="级联变声正在运行，请先停止后再离线转换（避免争抢显卡）")
        from runtime import gpu_holder_reason
        _holder = gpu_holder_reason()
        if _holder:
            raise HTTPException(status_code=409, detail=f"{_holder}，请先等它结束（避免争抢显卡）")

        OFFLINEVC_STATE.update(
            running=True, status="running", message="已提交",
            voice_id=voice_id, url="", duration_s=0.0, error="",
        )

    stamp = int(time.time() * 1000)
    raw_path = OUT / f"ovc_src_{stamp}{Path(file.filename or 'a.wav').suffix or '.wav'}"
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换")
    raw_path.write_bytes(raw)
    threading.Thread(
        target=_ovc_worker,
        args=(raw_path, voice_id, pth, pitch, index_rate, denoise, post_seedvc, stamp),
        kwargs={"enhance_level": enhance_level, "prosody": prosody},
        daemon=True,
    ).start()
    return {"ok": True, "voice_id": voice_id, "prosody": prosody}


def _ovc_worker(raw_path: Path, voice_id: str, pth: Path,
                pitch: int, index_rate: float, denoise: bool,
                post_seedvc: bool, stamp: int, enhance_level: str = "standard",
                prosody: str = "keep"):
    import soundfile as sf

    in_path = OUT / f"ovc_in_{stamp}.wav"
    relay_path = OUT / f"ovc_relay_{stamp}.wav"
    out_path = OUT / f"offlinevc_{stamp}.wav"
    index = next(iter((cfg.RVC_ROOT / "logs" / voice_id).glob("added_*.index")), None)
    try:
        OFFLINEVC_STATE.update(message="音频预处理中…")
        # 统一转 16k 单声道 wav
        cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(raw_path),
               "-af", "aresample=16000", "-ac", "1", str(in_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not in_path.exists():
            raise RuntimeError(f"ffmpeg 预处理失败: {r.stderr.strip()[:1500]}")
        # denoise：优先 DeepFilterNet 模型增强（P2-5），模型不可用回退 afftdn。
        # enhance_level 三档（弱人声保护，P2-5 验收发现）：light=最多压6dB /
        # standard=12dB / strong=不限（原行为）。afftdn 回退同样分档。
        if denoise:
            try:
                from audio_enhance import enhance_file, resolve_atten_lim
                tmp = OUT / f"ovc_enh_{stamp}.wav"
                enhance_file(in_path, tmp, atten_lim_db=resolve_atten_lim(enhance_level))
                tmp.replace(in_path)
            except Exception:
                # nf 越低压得越狠：light 保留更多弱声
                nf = {"light": -15, "standard": -25, "strong": -35}.get(enhance_level, -25)
                af = f"afftdn=nf={nf},"
                cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(raw_path),
                       "-af", af + "aresample=16000", "-ac", "1", str(in_path)]
                subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # 语气重铸：ASR 转文字 → 用目标音色的参考音重新合成，再交给 RVC。
        # 放在降噪之后（ASR 吃干净音频更准）、RVC 之前（RVC 只管换音色）。
        if prosody == "relay":
            OFFLINEVC_STATE.update(message="语气重铸中…（转文字 → 重新合成，约 10~30 秒）")
            from prosody_relay import relay
            relay(in_path, voice_id, relay_path).replace(in_path)

        OFFLINEVC_STATE.update(message="RVC 推理中…（整段单次推理，几十秒到几分钟）")
        cmd = [str(RVC_VENV_PY), str(INFER_PY),
               "--pth", str(pth),
               "--index", str(index) if index else "",
               "--input", str(in_path), "--output", str(out_path),
               "--pitch", str(pitch), "--index-rate", str(index_rate)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace", cwd=str(cfg.RVC_ROOT))
        if r.returncode != 0 or not out_path.exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError("RVC 推理失败: " + " | ".join(tail)[-400:])

        # 可选后处理：Seed-VC convert-style 把 RVC 压平的韵律/情绪补回来
        # （RVC 只保音高，表达力弱；Seed-VC V2 零样本按 reference 重塑语气）
        if post_seedvc:
            ref = cfg.MEDIA_DIR / "voicebank" / voice_id / "reference.wav"
            if not ref.exists():
                raise RuntimeError(f"音色 [{voice_id}] 缺少 reference.wav，无法做 Seed-VC 情绪补偿")
            OFFLINEVC_STATE.update(message="Seed-VC 情绪/韵律补偿中…（约 1 分钟）")
            from seed_vc import run_conversion
            tmp_dir = OUT / f"ovc_seedvc_{stamp}"
            produced = run_conversion(out_path, ref, tmp_dir, convert_style=True)
            import shutil
            shutil.move(str(produced), str(out_path))
            try:
                tmp_dir.rmdir()
            except Exception:
                pass

        d, sr = sf.read(str(out_path))
        duration_s = round(len(d) / sr, 1)
        from history import register as history_register
        history_register("offlinevc", voice_id, out_path.name,
                         f"/api/media/outputs/{out_path.name}", duration_s,
                         params={"pitch": pitch, "index_rate": index_rate,
                                 "denoise": denoise, "post_seedvc": post_seedvc,
                                 "enhance_level": enhance_level, "prosody": prosody})
        OFFLINEVC_STATE.update(
            running=False, status="done", message="完成",
            url=f"/api/media/outputs/{out_path.name}",
            duration_s=duration_s, error="",
        )
    except Exception as e:
        OFFLINEVC_STATE.update(running=False, status="error", message="",
                               error=str(e))
    finally:
        for p in (raw_path, in_path, relay_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


@router.get("/offlinevc/status")
def offlinevc_status():
    return OFFLINEVC_STATE


@router.post("/offlinevc/pitch_suggest")
async def offlinevc_pitch_suggest(
    file: UploadFile = File(...),
    voice_id: str = Form(""),
):
    """自动音高建议：分析输入音频中位基频，对照目标音色参考音高算建议变调。

    前端在选好音频+音色后自动调用，把建议值默认填进变调滑块。
    纯 CPU 分析（librosa.pyin），不占显卡，与转换任务互不影响。
    """
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    stamp = int(time.time() * 1000)
    raw_path = OUT / f"pitch_src_{stamp}{Path(file.filename or 'a.wav').suffix or '.wav'}"
    raw_path.write_bytes(await file.read())
    try:
        from pitch_advice import full_suggestion
        return await run_in_threadpool(full_suggestion, raw_path, voice_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"音高分析失败: {e}")
    finally:
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:
            pass

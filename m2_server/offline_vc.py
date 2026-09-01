"""离线变声工作台：录音/音频文件 → （可选降噪）→ RVC 离线整段推理 → 导出 48k wav。

接口：
    POST /api/offlinevc/run    multipart 上传音频 + 指定音色/变调/降噪，提交后台转换
    GET  /api/offlinevc/status 轮询进度与结果

设计：
    - 推理在 D:\\RVC\\.venv 子进程里跑 offline_vc_infer.py（与主服务 torch 隔离，
      与 rvc_live 实时变声同一环境）；上传音频（webm/wav/…）先经 ffmpeg 统一转
      16k 单声道 wav，可选 afftdn 降噪。
    - 同一时刻只允许一个转换任务，且实时变声运行中会拒绝（避免抢 GPU）。
"""
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import config as cfg
from common import MAX_UPLOAD_BYTES
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
):
    """提交离线变声任务。pitch 为半音数（男转女 +12，女转男 -12）。"""
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
        args=(raw_path, voice_id, pth, pitch, index_rate, denoise, stamp),
        daemon=True,
    ).start()
    return {"ok": True, "voice_id": voice_id}


def _ovc_worker(raw_path: Path, voice_id: str, pth: Path,
                pitch: int, index_rate: float, denoise: bool, stamp: int):
    import soundfile as sf

    in_path = OUT / f"ovc_in_{stamp}.wav"
    out_path = OUT / f"offlinevc_{stamp}.wav"
    index = next(iter((cfg.RVC_ROOT / "logs" / voice_id).glob("added_*.index")), None)
    try:
        OFFLINEVC_STATE.update(message="音频预处理中…")
        # 统一转 16k 单声道 wav；denoise 时加 afftdn 降噪（nf 越低压得越狠）
        af = "afftdn=nf=-25," if denoise else ""
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_path),
               "-af", af + "aresample=16000", "-ac", "1", str(in_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not in_path.exists():
            raise RuntimeError(f"ffmpeg 预处理失败: {r.stderr.strip()[:1500]}")

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

        d, sr = sf.read(str(out_path))
        duration_s = round(len(d) / sr, 1)
        from history import register as history_register
        history_register("offlinevc", voice_id, out_path.name,
                         f"/api/media/outputs/{out_path.name}", duration_s,
                         params={"pitch": pitch, "index_rate": index_rate,
                                 "denoise": denoise})
        OFFLINEVC_STATE.update(
            running=False, status="done", message="完成",
            url=f"/api/media/outputs/{out_path.name}",
            duration_s=duration_s, error="",
        )
    except Exception as e:
        OFFLINEVC_STATE.update(running=False, status="error", message="",
                               error=str(e))
    finally:
        for p in (raw_path, in_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


@router.get("/offlinevc/status")
def offlinevc_status():
    return OFFLINEVC_STATE

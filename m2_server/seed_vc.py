# -*- coding: utf-8 -*-
"""Seed-VC 高表现力离线变声：录音/音频文件 → （可选降噪）→ Seed-VC V2 零样本变声 → 导出 wav。

与 offline_vc.py（RVC 实时音色路径）并列：RVC 保音高但易压平韵律；Seed-VC V2 在换音色
的同时保留（V1）/ 转换（--convert-style）源音频的语气、节奏、情绪——补 RVC 缺的「表达力」。

接口：
    POST /api/seedvc/run      上传源音频 + 目标参考（voicebank 音色 或 上传参考音）+ 表达力旋钮，后台转换
    GET  /api/seedvc/status   轮询进度与结果

设计：
    - 推理在 seed_vc_repo/ 下用主 .venv 子进程跑 inference_v2.py（与生产 torch 同一环境，
      权重已缓存在 seed_vc_repo/checkpoints 与 HF 缓存，无需重下）。
    - 目标音色二选一：target_voice_id（复用 voicebank 的 reference.wav，零样本无需训练）
      或上传 target 参考音频。
    - 同一时刻只允许一个转换任务，且实时变声/级联运行中会拒绝（避免抢 GPU）。
"""
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import config as cfg
from common import MAX_UPLOAD_BYTES, find_ffmpeg, voice_ref
from rvc_live import _live_proc_alive
from cascade import _cascade_alive

OUT = cfg.OUTPUTS_DIR
OUT.mkdir(exist_ok=True)

SEEDVC_REPO = cfg.ROOT / "seed_vc_repo"
SEEDVC_INFER = SEEDVC_REPO / "inference_v2.py"
SEEDVC_VENV_PY = cfg.ROOT / ".venv" / "Scripts" / "python.exe"

# 走国内 HF 镜像下载/加载权重（首次已缓存，后续直接用）
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

# 目标音色 → 微调 run 目录（不存在该目录或目录里无 CFM_*.pth 时静默回落零样本）。
# 2026-09-05：kangaroo 用 73 条自录切片（video_260828_110637 + video_260828_105338）微调 CFM 100 步，
# 像度 CAM++ 0.71 → 0.806、漏源更低、F0 表达力更强（对照 experiments/seedvc_ft_eval.py）。
SEEDVC_FT_RUNS: dict[str, Path] = {
    "kangaroo": SEEDVC_REPO / "runs" / "kangaroo_ft_100",
}
SEEDVC_FT_MAX_RUNS = 3  # 自定义微调最多支持 N 个音色，避免误填膨胀


def _ft_ckpt(voice_id: str) -> Path | None:
    """查目标音色对应的最新微调 CFM 检查点；无则返回 None（零样本）。"""
    run_dir = SEEDVC_FT_RUNS.get(voice_id)
    if not run_dir or not run_dir.is_dir():
        return None
    ckpts = sorted(run_dir.glob("CFM_*.pth"))
    return ckpts[-1] if ckpts else None

router = APIRouter(prefix="/api")

SEEDVC_STATE: dict = {
    "running": False,
    "status": "idle",        # idle | running | done | error
    "message": "",
    "target": "",
    "url": "",
    "duration_s": 0.0,
    "error": "",
}
_seedvc_lock = threading.Lock()


@router.post("/seedvc/run")
async def seedvc_run(
    file: UploadFile = File(...),
    target: UploadFile = File(None),
    target_voice_id: str = Form(""),
    convert_style: bool = Form(False),
    similarity_cfg_rate: float = Form(0.5),
    top_p: float = Form(0.9),
    temperature: float = Form(1.0),
    diffusion_steps: int = Form(10),
    length_adjust: float = Form(1.0),
    denoise: bool = Form(False),
):
    """提交 Seed-VC 表达力变声任务。

    target_voice_id 与上传 target 参考音频二选一；convert_style=True 开启情绪/口音转换。
    """
    with _seedvc_lock:
        if SEEDVC_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有转换任务在跑，请稍候")
        if not target_voice_id and (target is None or not target.filename):
            raise HTTPException(status_code=400, detail="请选择 voicebank 音色或上传目标参考音频")
        if not SEEDVC_VENV_PY.exists():
            raise HTTPException(status_code=500, detail="Seed-VC 运行环境缺失（主 .venv）")
        if not SEEDVC_INFER.exists():
            raise HTTPException(status_code=500, detail="Seed-VC 推理脚本缺失（seed_vc_repo/inference_v2.py）")
        if _live_proc_alive():
            raise HTTPException(status_code=409, detail="实时变声正在运行，请先停止后再转换（避免争抢显卡）")
        if _cascade_alive():
            raise HTTPException(status_code=409, detail="级联变声正在运行，请先停止后再转换（避免争抢显卡）")

        # 解析目标参考音频
        target_label = target_voice_id or "uploaded"
        if target_voice_id:
            ref_path, _ = voice_ref(target_voice_id)   # 合法性/存在性校验，缺失抛 400/404
        else:
            ref_path = None  # 上传态在 worker 内落盘

        SEEDVC_STATE.update(
            running=True, status="running", message="已提交",
            target=target_label, url="", duration_s=0.0, error="",
        )

    stamp = int(time.time() * 1000)
    raw_path = OUT / f"seedvc_src_{stamp}{Path(file.filename or 'a.wav').suffix or '.wav'}"
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换")
    raw_path.write_bytes(raw)

    threading.Thread(
        target=_seedvc_worker,
        args=(raw_path, target, ref_path, target_label, convert_style,
              similarity_cfg_rate, top_p, temperature, diffusion_steps, length_adjust, denoise, stamp),
        daemon=True,
    ).start()
    return {"ok": True, "target": target_label}


def _preprocess(src: Path, dst: Path, denoise: bool) -> None:
    """统一转 16k 单声道 wav；denoise 时加 afftdn 降噪。"""
    af = "afftdn=nf=-25," if denoise else ""
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-af", af + "aresample=16000", "-ac", "1", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 预处理失败: {r.stderr.strip()[:1500]}")


def run_conversion(in_src: Path, in_tgt: Path, out_dir: Path, *,
                   convert_style: bool = False,
                   similarity_cfg_rate: float = 0.5,
                   top_p: float = 0.9,
                   temperature: float = 1.0,
                   diffusion_steps: int = 10,
                   length_adjust: float = 1.0,
                   cfm_checkpoint_path: Path | None = None) -> Path:
    """跑一次 Seed-VC V2 子进程，返回生成的 wav 路径（调用方负责搬移/改名）。

    供本模块 /seedvc 与 offline_vc（RVC 后处理补情绪，post_seedvc）复用。
    权重已缓存在 seed_vc_repo/checkpoints 与 HF 缓存，单次约几十秒。
    cfm_checkpoint_path 非空时用自定义微调 CFM 权重（替代零样本底模）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(SEEDVC_VENV_PY), str(SEEDVC_INFER),
           "--source", str(in_src),
           "--target", str(in_tgt),
           "--output", str(out_dir),
           "--diffusion-steps", str(diffusion_steps),
           "--convert-style", "true" if convert_style else "false",
           "--similarity-cfg-rate", str(similarity_cfg_rate),
           "--top-p", str(top_p),
           "--temperature", str(temperature),
           "--length-adjust", str(length_adjust)]
    if cfm_checkpoint_path is not None:
        cmd += ["--cfm-checkpoint-path", str(cfm_checkpoint_path)]
    env = dict(os.environ)
    env["HF_ENDPOINT"] = HF_ENDPOINT
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       encoding="utf-8", errors="replace", cwd=str(SEEDVC_REPO), env=env)
    wavs = list(out_dir.glob("*.wav"))
    if r.returncode != 0 or not wavs:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError("Seed-VC 推理失败: " + " | ".join(tail)[-500:])
    return wavs[0]


def _seedvc_worker(raw_path: Path, target: UploadFile, ref_path: Path | None,
                   target_label: str, convert_style: bool,
                   similarity_cfg_rate: float, top_p: float, temperature: float,
                   diffusion_steps: int, length_adjust: float, denoise: bool, stamp: int,
                   cfm_checkpoint_path: Path | None = None):
    import soundfile as sf

    in_src = OUT / f"seedvc_in_src_{stamp}.wav"
    in_tgt = OUT / f"seedvc_in_tgt_{stamp}.wav"
    out_dir = OUT / f"seedvc_tmp_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = OUT / f"seedvc_{stamp}.wav"
    try:
        SEEDVC_STATE.update(message="音频预处理中…")
        _preprocess(raw_path, in_src, denoise)

        # 目标参考：voicebank 直接复用；上传则落盘后预处理
        if ref_path is not None and ref_path.exists():
            tgt_for_vc = ref_path
        else:
            tgt_raw = OUT / f"seedvc_tgt_{stamp}{Path(target.filename or 'a.wav').suffix or '.wav'}"
            tgt_raw.write_bytes(target.file.read())
            _preprocess(tgt_raw, in_tgt, False)
            tgt_for_vc = in_tgt

        SEEDVC_STATE.update(message="Seed-VC 推理中…（权重已缓存，约几十秒）")
        produced = run_conversion(in_src, tgt_for_vc, out_dir,
                                  convert_style=convert_style,
                                  similarity_cfg_rate=similarity_cfg_rate,
                                  top_p=top_p, temperature=temperature,
                                  diffusion_steps=diffusion_steps,
                                  length_adjust=length_adjust,
                                  cfm_checkpoint_path=cfm_checkpoint_path)
        import shutil
        shutil.move(str(produced), str(final_path))

        d, sr = sf.read(str(final_path))
        duration_s = round(len(d) / sr, 1)
        from history import register as history_register
        history_register("seedvc", target_label, final_path.name,
                         f"/api/media/outputs/{final_path.name}", duration_s,
                         params={"convert_style": convert_style,
                                 "similarity_cfg_rate": similarity_cfg_rate,
                                 "top_p": top_p, "temperature": temperature,
                                 "diffusion_steps": diffusion_steps,
                                 "length_adjust": length_adjust, "denoise": denoise})
        SEEDVC_STATE.update(
            running=False, status="done", message="完成",
            url=f"/api/media/outputs/{final_path.name}",
            duration_s=duration_s, error="",
        )
    except Exception as e:
        SEEDVC_STATE.update(running=False, status="error", message="", error=str(e))
    finally:
        for p in (raw_path, in_src, in_tgt):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            out_dir.rmdir()
        except Exception:
            pass


@router.get("/seedvc/status")
def seedvc_status():
    return SEEDVC_STATE

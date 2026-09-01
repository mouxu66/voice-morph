# -*- coding: utf-8 -*-
"""音色微调工坊：录音上传 → 切片转写 → Qwen3-TTS 少样本微调 → 试听 → 入音色库。

流程（对应前端四步）：
  1. POST /api/ft/upload          上传录音(webm/m4a/wav) + voice_id，后台处理：
     ffmpeg 转 24k 单声道 → 静音切句(≤12s) → worker /transcribe 逐条转写
     → 产 media/ft/<id>/train_raw.jsonl + status.json
  2. GET  /api/ft/status          处理进度 / 切片数 / 总时长 / 转写抽查
  3. POST /api/ft/train           prepare_data(tokenizer 提 codes) → sft_8gb.py 训练
     GET  /api/ft/train_status    阶段/loss/显存峰值/日志尾（进程级，重启可追踪）
  4. POST /api/ft/audition        x-vector 与微调模型 A/B 试听
     POST /api/ft/publish          微调模型入音色库（voicebank/<id>/ft_model），
     之后 /tts 对该音色自动走微调合成（见 qwen3_tts.tts 的 meta 分流）

设计约定：
  - 训练在 venv312 子进程跑（GPU 独占），与 worker 推理不同时进行
  - 全部落盘 status.json / train_run.log，服务重启可续读状态
  - 锚点参考音频自动选「最长切片」，保证 speaker 嵌入质量
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

import config as cfg
from common import is_valid_voice_id

router = APIRouter(prefix="/api")

ROOT = Path(__file__).resolve().parent.parent
FT_DIR = cfg.MEDIA_DIR / "ft"
VOICEBANK = cfg.MEDIA_DIR / "voicebank"
OUTPUTS = cfg.OUTPUTS_DIR
VENV_PY = cfg.TTS_VENV_PY
FT_REPO = ROOT / "tts_trial" / "Qwen3-TTS" / "finetuning"
PREPARE_PY = FT_REPO / "prepare_data.py"
TOKENIZER_DIR = cfg.QWEN_TOKENIZER_DIR
SFT_PY = ROOT / "tts_trial" / "sft_8gb.py"
WORKER = "http://127.0.0.1:8001"

# 切句参数（24k 单声道）
FRAME_MS = 20
SILENCE_DB_OFF = -38.0     # 低于峰值这么多的帧视为静音（自适应后再放宽）
MIN_SEG_S = 2.0            # 太短的句子丢弃
MAX_SEG_S = 12.0           # 训练样本上限
MIN_GAP_S = 0.35           # 静音多久算句间停顿

CREATE_NEW_CONSOLE = 0x00000010

# 训练进程注册表 {voice_id: {pid, running, rc}}（内存态；日志才是权威进度源）
_TRAIN: dict[str, dict] = {}


def _vdir(voice_id: str) -> Path:
    if not is_valid_voice_id(voice_id):
        raise HTTPException(400, "voice_id 非法")
    return FT_DIR / voice_id


def _status(voice_id: str) -> dict:
    p = _vdir(voice_id) / "status.json"
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"stage": "new", "voice_id": voice_id}


def _set_status(voice_id: str, **kw):
    d = _vdir(voice_id)
    d.mkdir(parents=True, exist_ok=True)
    st = _status(voice_id)
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (d / "status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    return st


def _worker_post(path: str, payload: dict, timeout: int = 120):
    from qwen3_tts import post
    return json.loads(post(path, payload, timeout=timeout))


def _worker_post_bytes(path: str, payload: dict, timeout: int = 300) -> bytes:
    from qwen3_tts import post
    return post(path, payload, timeout=timeout)


# ---------------- 切句 ----------------

def _split_on_silence(x: np.ndarray, sr: int) -> list[tuple[int, int]]:
    """按 RMS 静音切句；阈值取(噪声底+12dB, 固定-38dB)较松者，适配不同麦克风增益。"""
    frame = int(sr * FRAME_MS / 1000)
    n = len(x) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    noise_floor = float(np.percentile(db, 10))
    thr = max(noise_floor + 12.0, SILENCE_DB_OFF)
    quiet = db < thr

    min_gap = int(MIN_GAP_S * 1000 / FRAME_MS)
    max_len = int(MAX_SEG_S * 1000 / FRAME_MS)
    min_seg = int(MIN_SEG_S * 1000 / FRAME_MS)

    segs: list[tuple[int, int]] = []
    start = None
    quiet_run = 0
    for i in range(n):
        if quiet[i]:
            quiet_run += 1
            if start is not None and quiet_run >= min_gap:
                segs.append((start, i - quiet_run + 1))
                start = None
        else:
            quiet_run = 0
            if start is None:
                start = i
            elif i - start >= max_len:  # 超长句硬切
                segs.append((start, i))
                start = i
    if start is not None:
        segs.append((start, n))
    # 收尾：丢太短、回收静音边距
    out = []
    for a, b in segs:
        if b - a >= min_seg:
            pad = int(0.08 * 1000 / FRAME_MS)
            out.append((max(0, (a - pad)) * frame, min(n, (b + pad)) * frame))
    return out


def _process(voice_id: str, raw_path: Path):
    """后台：解码→切句→转写→train_raw.jsonl。全程写 status.json。"""
    clips_dir = _vdir(voice_id) / "clips"
    try:
        clips_dir.mkdir(parents=True, exist_ok=True)
        wav24 = _vdir(voice_id) / "full_24k.wav"
        _set_status(voice_id, stage="processing", message="转码 24kHz 单声道…")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(raw_path),
             "-ar", "24000", "-ac", "1", str(wav24)],
            check=True, capture_output=True)

        _set_status(voice_id, message="静音切句…")
        x, sr = sf.read(str(wav24), dtype="float32")
        dur_total = len(x) / sr
        segs = _split_on_silence(x, sr)
        clips = []
        for i, (a, b) in enumerate(segs, 1):
            p = clips_dir / f"seg_{i:03d}.wav"
            sf.write(str(p), x[a:b], sr)
            clips.append(p)
        if not clips:
            raise RuntimeError("没有切出有效语句（录音太短或全程静音）")

        _set_status(voice_id, message=f"转写 {len(clips)} 条切片…", clips=len(clips))
        rows, transcripts, done = [], [], 0
        for p in clips:
            r = _worker_post("/transcribe", {"path": str(p).replace("\\", "/")})
            done += 1
            if r.get("error") or not (r.get("text") or "").strip():
                continue
            rows.append({"audio": str(p).replace("\\", "/"), "text": r["text"].strip()})
            transcripts.append({"name": p.name, "text": r["text"].strip(),
                                "quality": r.get("quality")})
            _set_status(voice_id, transcribed=done)

        if len(rows) < 8:
            raise RuntimeError(f"有效转写样本只有 {len(rows)} 条（需≥8），请重录或改善环境噪音")

        # 锚点 = 最长切片（speaker 嵌入最稳）
        anchor = max(rows, key=lambda r: len(sf.read(r["audio"])[0]))
        for r in rows:
            r["ref_audio"] = anchor["audio"]
        raw_jsonl = _vdir(voice_id) / "train_raw.jsonl"
        raw_jsonl.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")

        total_speech = sum(len(sf.read(r["audio"])[0]) / sr for r in rows)
        _set_status(voice_id, stage="ready", message="处理完成，可开始训练",
                    clips=len(rows), duration_s=round(dur_total, 1),
                    speech_s=round(total_speech, 1),
                    anchor=os.path.basename(anchor["audio"]),
                    transcripts=transcripts[:8])
    except Exception as exc:
        _set_status(voice_id, stage="error", error=str(exc))


@router.post("/ft/upload")
async def ft_upload(voice_id: str = Form(...), file: UploadFile = File(...)):
    d = _vdir(voice_id)
    raw = d / f"raw{Path(file.filename or 'rec.webm').suffix.lower()}"
    d.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(await file.read())
    _set_status(voice_id, stage="processing", message="已接收录音", error="")
    threading.Thread(target=_process, args=(voice_id, raw), daemon=True).start()
    return {"ok": True, "voice_id": voice_id}


@router.get("/ft/list")
def ft_list():
    items = []
    if FT_DIR.exists():
        for d in sorted(FT_DIR.iterdir()):
            if d.is_dir() and (d / "status.json").exists():
                st = json.loads((d / "status.json").read_text("utf-8"))
                items.append({"voice_id": d.name, **st})
    return {"items": items}


@router.get("/ft/status")
def ft_status(voice_id: str):
    st = _status(voice_id)
    tr = _TRAIN.get(voice_id)
    if tr:
        st["train"] = {k: tr.get(k) for k in ("pid", "running", "rc")}
    return st


# ---------------- 训练 ----------------

def _decode(line: bytes) -> str:
    """子进程输出优先 utf-8（PYTHONIOENCODING），cmd.exe 等外部输出回退 gbk。"""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return line.decode("gbk", errors="replace")


def _tee_run(cmd: list[str], log_path: Path, cwd: Path | None = None) -> int:
    """运行子进程，stdout/stderr 逐行 tee 到 log（训练/预处理共用）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with open(log_path, "a", encoding="utf-8") as f:
        for line in iter(proc.stdout.readline, b""):
            f.write(_decode(line))
            f.flush()
    return proc.wait()


def _find_train_pid(voice_id: str) -> int | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'sft_8gb' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15)
        pids = [int(l) for l in out.stdout.split() if l.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _latest_ckpt(voice_id: str) -> Path | None:
    """sft 输出布局：ft_output/<voice_id>/checkpoint-epoch-N。"""
    root = ROOT / "tts_trial" / "ft_output" / voice_id
    cks = sorted(root.glob("checkpoint-epoch-*"), key=lambda p: int(p.name.rsplit("-", 1)[-1]))
    return cks[-1] if cks else None


def _train_job(voice_id: str, epochs: int):
    d = _vdir(voice_id)
    log = d / "train_run.log"
    log.write_text(
        f"=== FT TRAIN {voice_id} {time.strftime('%F %T')} epochs={epochs} ===\n", "utf-8")
    try:
        # 1) tokenizer 提 codes（阻塞，~30s）
        _set_status(voice_id, stage="training", message="提取音频 codes(tokenizer)…")
        rc = _tee_run(
            [str(VENV_PY), "-u", str(PREPARE_PY), "--device", "cuda:0",
             "--tokenizer_model_path", str(TOKENIZER_DIR),
             "--input_jsonl", str(d / "train_raw.jsonl"),
             "--output_jsonl", str(d / "train_codes.jsonl")],
            log, cwd=str(FT_REPO))
        if rc != 0:
            raise RuntimeError(f"prepare_data 退出码 {rc}，详见 train_run.log")

        # 2) sft 训练（detached，训练器自己 tee 到同一日志）
        anchor = _status(voice_id).get("anchor", "")
        anchor_path = str(d / "clips" / anchor).replace("\\", "/") if anchor else ""
        cmd = [str(VENV_PY), "-u", str(SFT_PY),
               "--train_jsonl", str(d / "train_codes.jsonl"),
               "--num_epochs", str(epochs),
               "--speaker_name", voice_id,
               "--output_model_path", str(ROOT / "tts_trial" / "ft_output" / voice_id)]
        if anchor_path:
            cmd += ["--anchor_ref", anchor_path]
        _set_status(voice_id, stage="training", message="训练中…")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _TRAIN[voice_id] = {"pid": proc.pid, "running": True, "rc": None}

        def _pump():
            with open(log, "a", encoding="utf-8") as f:
                for line in iter(proc.stdout.readline, b""):
                    f.write(_decode(line))
                    f.flush()
        threading.Thread(target=_pump, daemon=True).start()

        def _wait():
            rc = proc.wait()
            _TRAIN[voice_id] = {"pid": proc.pid, "running": False, "rc": rc}
            st = _status(voice_id)
            if st.get("stage") == "training":
                ck = _latest_ckpt(voice_id)
                if rc == 0 and ck:
                    _set_status(voice_id, stage="trained", message="训练完成，可试听/入库",
                                checkpoint=str(ck), error="")
                else:
                    _set_status(voice_id, stage="error",
                                error=f"训练退出码 {rc}，详见 ft 状态页日志")
        threading.Thread(target=_wait, daemon=True).start()
    except Exception as exc:
        _TRAIN[voice_id] = {"pid": None, "running": False, "rc": -1}
        _set_status(voice_id, stage="error", error=str(exc))


@router.post("/ft/train")
def ft_train(voice_id: str, epochs: int = 12):
    st = _status(voice_id)
    if st.get("stage") not in ("ready", "trained", "error"):
        raise HTTPException(400, f"当前状态 {st.get('stage')} 不可启动训练")
    if not (st.get("clips") or 0) >= 8:
        raise HTTPException(400, "样本不足（需≥8 条切片），请重新录音")
    tr = _TRAIN.get(voice_id) or {}
    if tr.get("running") or _find_train_pid(voice_id):
        raise HTTPException(400, "该音色已在训练中")
    threading.Thread(target=_train_job, args=(voice_id, epochs), daemon=True).start()
    return {"ok": True, "voice_id": voice_id, "epochs": epochs}


@router.get("/ft/train_status")
def ft_train_status(voice_id: str):
    d = _vdir(voice_id)
    st = _status(voice_id)
    tr = _TRAIN.get(voice_id) or {}
    log = d / "train_run.log"
    res = {"stage": st.get("stage"), "message": st.get("message", ""),
           "error": st.get("error", ""),
           "running": bool(tr.get("running")) or _find_train_pid(voice_id) is not None,
           "rc": tr.get("rc"), "checkpoint": st.get("checkpoint"),
           "log_tail": [], "loss": None, "epoch": None, "epochs": None,
           "vram_peak": None}
    if not log.exists():
        return res
    lines = log.read_text("utf-8", errors="replace").splitlines()
    res["log_tail"] = lines[-14:]
    for ln in reversed(lines):
        m = re.search(r"\[epoch (\d+) step (\d+)\] loss=([\d.]+).*峰值显存=([\d.]+)", ln)
        if m:
            res["epoch"], res["loss"], res["vram_peak"] = int(m.group(1)), float(m.group(3)), float(m.group(4))
            break
    m = re.search(r"epochs=(\d+)", lines[0]) if lines else None
    if m:
        res["epochs"] = int(m.group(1))
    if any("[DONE]" in l for l in lines[-5:]):
        res["done"] = True
    return res


# ---------------- 试听 / 入库 ----------------

def _published_model(voice_id: str) -> Path:
    p = VOICEBANK / voice_id / "ft_model"
    if not p.exists():
        ck = _latest_ckpt(voice_id)
        if not ck:
            raise HTTPException(404, "该音色还没有训练完成的模型")
        return ck
    return p


@router.post("/ft/audition")
def ft_audition(voice_id: str, text: str):
    """A/B：同一句话分别用 微调模型 / x-vector 声纹 合成，返回 outputs URL。"""
    if not text.strip():
        raise HTTPException(400, "text 不能为空")
    ck = _published_model(voice_id)
    ref = VOICEBANK / voice_id / "reference.wav"
    out = {}
    wav = _worker_post_bytes("/tts_speaker", {
        "model_dir": str(ck).replace("\\", "/"), "speaker": voice_id, "text": text})
    p1 = OUTPUTS / f"ft_aud_{voice_id}_tuned.wav"
    p1.write_bytes(wav)
    out["tuned_url"] = f"/media/outputs/{p1.name}"
    if ref.exists():
        wav2 = _worker_post_bytes("/tts", {
            "ref_audio": str(ref).replace("\\", "/"), "ref_text": "", "text": text})
        p2 = OUTPUTS / f"ft_aud_{voice_id}_xvec.wav"
        p2.write_bytes(wav2)
        out["xvec_url"] = f"/media/outputs/{p2.name}"
    return {"ok": True, **out}


@router.post("/ft/publish")
def ft_publish(voice_id: str, display_name: str = ""):
    """把最新 checkpoint 硬链接进音色库并写 meta（/tts 自动分流到微调合成）。"""
    st = _status(voice_id)
    ck = _latest_ckpt(voice_id)
    if not ck:
        raise HTTPException(404, "没有可入库的 checkpoint")
    vd = VOICEBANK / voice_id
    (vd / "ft_model").mkdir(parents=True, exist_ok=True)
    for f in ck.rglob("*"):
        rel = f.relative_to(ck)
        t = vd / "ft_model" / rel
        if f.is_dir():
            t.mkdir(parents=True, exist_ok=True)
            continue
        if t.exists():
            continue
        try:
            os.link(f, t)
        except OSError:
            shutil.copy2(f, t)
    anchor = st.get("anchor")
    if anchor:
        shutil.copy2(d_dir := _vdir(voice_id) / "clips" / anchor, vd / "reference.wav")
    meta = {"display_name": display_name or f"{voice_id}（微调）",
            "kind": "finetuned", "speaker": voice_id,
            "model_dir": str(vd / "ft_model").replace("\\", "/"),
            "published_at": time.strftime("%F %T")}
    (vd / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    _set_status(voice_id, stage="published", message="已入库")
    return {"ok": True, "voice_id": voice_id, "model_dir": meta["model_dir"]}


@router.delete("/ft/{voice_id}")
def ft_delete(voice_id: str):
    d = _vdir(voice_id)
    if not d.exists():
        raise HTTPException(404, "不存在")
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}

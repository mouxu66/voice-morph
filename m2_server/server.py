"""M2 转换服务（FastAPI · 常驻服务）

启动：
    python m2_server/server.py
    默认监听 8000 端口。

接口：
    GET  /health              健康检查
    GET  /voices              列出音色库
    GET  /raw_videos          列出素材视频
    POST /pipeline/run        跑 M1 流水线（提取→分离→切片）
    GET  /clips               列出切片片段
    POST /voicebank           用勾选片段生成音色档案
    DELETE /voicebank/{id}    删除音色
    GET  /media/...           静态音频访问（clips / outputs / voicebank）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from rvc_live import router as rvc_live_router

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
VOICEBANK = ROOT / "media" / "voicebank"
CLIPS_DIR = ROOT / "media" / "clips"
RAW_DIR = ROOT / "media" / "raw_videos"
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
VOICEBANK.mkdir(parents=True, exist_ok=True)

# 统一挂载 /api 前缀，开发(走 vite proxy)与生产(直连)共用同一套路径
API_PREFIX = "/api"

# 流水线运行状态（供前端轮询分步进度/取消）
PIPELINE_STATE = {
    "running": False,
    "status": "idle",        # idle | running | done | cancelled | error
    "step": "",
    "message": "",
    "percent": 0,
    "clips": 0,
    "error": "",
}
_pipeline_cancel = threading.Event()
_pipeline_lock = threading.Lock()

# 音色 ID 白名单：仅允许字母/数字/下划线/连字符，杜绝路径穿越（如 .. 或 /）
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid_voice_id(voice_id: str) -> bool:
    return bool(voice_id) and bool(_VOICE_ID_RE.match(voice_id))

app = FastAPI(title="变声 · M2 转换服务", version="0.1.0")

# CORS：允许本地前端(5173 dev)与打包后的 file:// 页面访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rvc_live_router)


@app.get(API_PREFIX + "/health")
def health():
    import torch
    return {"status": "ok", "cuda": torch.cuda.is_available()}


# ---------------- 音色库 ----------------

@app.get(API_PREFIX + "/voices")
def list_voices():
    import json
    from pydub import AudioSegment
    voices = []
    for d in VOICEBANK.iterdir():
        ref = d / "reference.wav"
        if d.is_dir() and ref.exists():
            # 显示名优先取 meta.json 的 display_name（如「美团袋鼠」），否则回退 ID
            display = d.name
            meta_path = d / "meta.json"
            if meta_path.exists():
                try:
                    display = str(json.loads(meta_path.read_text("utf-8")).get("display_name") or d.name)
                except Exception:
                    pass
            voices.append({
                "id": d.name,
                "display_name": display,
                "reference": ref.name,
                "duration_s": round(len(AudioSegment.from_wav(str(ref))) / 1000, 1),
            })
    return {"voices": voices}


@app.delete(API_PREFIX + "/voicebank/{voice_id}")
async def delete_voice(voice_id: str):
    if not is_valid_voice_id(voice_id):
        raise HTTPException(400, "音色 ID 非法")
    d = VOICEBANK / voice_id
    if not d.exists():
        raise HTTPException(404, f"音色 [{voice_id}] 不存在")
    # rmtree 是阻塞 IO，丢进线程池避免卡住事件循环
    await run_in_threadpool(shutil.rmtree, d, True)
    return {"ok": True}


@app.post(API_PREFIX + "/voicebank")
async def create_voicebank(voice_id: str, request: Request):
    """用勾选的片段生成参考音频。body: clips=name1&clips=name2..."""
    import io
    from pydub import AudioSegment

    form = await request.form()
    clips = form.getlist("clips")
    if not clips:
        raise HTTPException(400, "请至少提供一个片段名")
    if not is_valid_voice_id(voice_id):
        raise HTTPException(400, "音色 ID 非法")

    out_dir = VOICEBANK / voice_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "reference.wav"

    merged = AudioSegment.silent(duration=300)
    for name in clips:
        p = CLIPS_DIR / f"{name}.wav"
        if not p.exists():
            raise HTTPException(404, f"片段不存在: {name}")
        seg = AudioSegment.from_wav(str(p))
        merged += seg + AudioSegment.silent(duration=300)

    merged = merged.set_channels(1).set_frame_rate(22050)
    merged.export(str(out), format="wav")

    # 记录片段清单（供 demo_convert 按片段提取音色向量）
    (out_dir / "clips.txt").write_text("\n".join(clips), encoding="utf-8")
    # 删除旧的音色向量缓存，下次自动重新提取
    (out_dir / "reference_se.npy").unlink(missing_ok=True)

    return {"ok": True, "voice_id": voice_id, "duration_s": round(len(merged) / 1000, 1)}


# ---------------- M1 素材流水线 ----------------

@app.get(API_PREFIX + "/raw_videos")
def list_raw_videos():
    videos = []
    for f in RAW_DIR.iterdir():
        if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi"):
            videos.append({"name": f.name, "size_mb": round(f.stat().st_size / 1e6, 1)})
    return {"videos": videos}


def _load_pipeline_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pipeline", ROOT / "m1_workshop" / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _update_pipeline(**kw):
    with _pipeline_lock:
        PIPELINE_STATE.update(kw)


_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi"}


@app.post(API_PREFIX + "/upload/video")
async def upload_video(file: UploadFile = File(...)):
    """拖拽/选择上传视频素材到 media/raw_videos/"""
    if not file.filename:
        raise HTTPException(400, "未提供文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _VIDEO_SUFFIXES:
        raise HTTPException(400, f"仅支持视频格式：{', '.join(sorted(_VIDEO_SUFFIXES))}")
    dest = RAW_DIR / Path(file.filename).name
    if dest.exists():
        raise HTTPException(409, f"同名文件已存在：{file.filename}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    dest.write_bytes(data)
    return {"ok": True, "name": file.filename, "size_mb": round(len(data) / 1e6, 1)}


class OpenFolderRequest(BaseModel):
    kind: str  # raw_videos | clips | outputs | voicebank


@app.post(API_PREFIX + "/open/folder")
def open_folder(req: OpenFolderRequest):
    """在系统文件管理器中打开指定目录（仅本地桌面使用）"""
    dirs = {
        "raw_videos": RAW_DIR,
        "clips": CLIPS_DIR,
        "outputs": OUT,
        "voicebank": VOICEBANK,
    }
    d = dirs.get(req.kind)
    if d is None:
        raise HTTPException(400, "无效的目录类型")
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(d))
    else:
        subprocess.Popen(["xdg-open", str(d)])
    return {"ok": True, "path": str(d)}


@app.post(API_PREFIX + "/pipeline/run")
def run_pipeline():
    """后台启动 M1 流水线，前端轮询 /pipeline/status 获取分步进度"""
    if PIPELINE_STATE["running"]:
        raise HTTPException(400, "流水线正在运行中，请稍候")
    videos = [f for f in RAW_DIR.iterdir()
              if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi")]
    if not videos:
        raise HTTPException(400, "media/raw_videos/ 里没有视频素材，请先上传或放入素材")

    _pipeline_cancel.clear()
    _update_pipeline(
        running=True, status="running", step="prepare",
        message=f"准备处理 {len(videos)} 个视频…", percent=1, clips=0, error="")

    def _job():
        try:
            mod = _load_pipeline_module()
            total = len(videos)
            clips = 0
            for i, v in enumerate(videos):
                if _pipeline_cancel.is_set():
                    raise mod.PipelineCancelled()
                base = round((i / total) * 88)
                _update_pipeline(step="extract", percent=base + 2,
                                 message=f"({i + 1}/{total}) 提取音轨：{v.name}")
                wav = mod.step1_extract(v, _pipeline_cancel)
                _update_pipeline(step="separate", percent=base + 12,
                                 message=f"({i + 1}/{total}) 去除背景音乐：{v.name}（首次需下载模型，较慢）")
                vocal = mod.step2_separate(wav, _pipeline_cancel)
                _update_pipeline(step="slice", percent=base + 24,
                                 message=f"({i + 1}/{total}) 静音检测切分：{v.name}")
                n = mod.step3_slice(vocal, v.stem[:12], _pipeline_cancel)
                clips += n
                _update_pipeline(clips=clips)
            _update_pipeline(status="done", step="", percent=100,
                             message=f"流水线完成，共切出 {clips} 个片段")
        except Exception as e:
            if type(e).__name__ == "PipelineCancelled":
                _update_pipeline(status="cancelled", step="", message="已取消，已完成的片段会保留")
            else:
                _update_pipeline(status="error", step="", message="流水线出错", error=str(e))
        finally:
            _update_pipeline(running=False)

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "started": True}


@app.get(API_PREFIX + "/pipeline/status")
def pipeline_status():
    with _pipeline_lock:
        return dict(PIPELINE_STATE)


@app.post(API_PREFIX + "/pipeline/cancel")
def cancel_pipeline():
    _pipeline_cancel.set()
    return {"ok": True}


@app.get(API_PREFIX + "/clips")
def list_clips():
    from pydub import AudioSegment
    clips = []
    for f in sorted(CLIPS_DIR.glob("*.wav")):
        try:
            a = AudioSegment.from_wav(str(f))
            clips.append({
                "name": f.stem,
                "duration_s": round(len(a) / 1000, 1),
                "loudness_dbfs": round(a.max_dBFS, 1),
            })
        except Exception:
            continue
    return {"clips": clips}


@app.get(API_PREFIX + "/export/rvc")
def export_rvc(request: Request):
    """把片段打包成 RVC 训练集 zip（可选 ?clips=a&clips=b 只导出指定片段）。
    RVC 训练只需要一个装 wav 的文件夹，自带说明文件。"""
    import io
    import zipfile

    wanted = request.query_params.getlist("clips")
    files = sorted(CLIPS_DIR.glob("*.wav"))
    if wanted:
        allowed = {f.stem for f in files}
        files = [CLIPS_DIR / f"{c}.wav" for c in wanted if c in allowed]
    if not files:
        raise HTTPException(400, "没有可导出的片段，请先跑素材流水线")

    readme = (
        "RVC 训练集使用说明\n"
        "================\n"
        f"共 {len(files)} 个干净人声片段（已去背景音、已按静音切分）。\n"
        "1. 解压后把整个 rvc_dataset 文件夹放到 RVC 整合包的 assets/datasets/ 下\n"
        "   （或在训练界面直接选择本文件夹）。\n"
        "2. 采样率不用管，RVC 会自动重采样；建议 40k 底模。\n"
        "3. 训练参数建议：提取算法 rmvpe，epoch 200~300（8G 显存可全默认）。\n"
        "4. 素材越多越像：3 分钟起步，10 分钟效果明显更稳。\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"rvc_dataset/{f.name}")
        z.writestr("rvc_dataset/README.txt", readme)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rvc_dataset.zip"},
    )


# ---------------- 音频设备配置（一键最优 / 自动恢复） ----------------
#
# 浏览器/前端无法直接修改 Windows 默认播放/录音设备，必须由后端走 PowerShell
# 调用 Core Audio (IPolicyConfig) 完成。脚本：m2_server/audio_config.ps1
#   -action status  查看当前默认设备
#   -action apply   一键设为变声最优配置（备份原始配置）
#   -action restore 恢复用户原始默认设备（删除备份）
# 备份文件位于 %LOCALAPPDATA%/rvc_audio_backup.txt，首次 apply 时写入。

_AUDIO_PS1 = ROOT / "m2_server" / "audio_config.ps1"


def _find_powershell() -> str | None:
    """优先用 pwsh（PowerShell 7），回退到 powershell（Windows 自带）。"""
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _run_audio_config(action: str) -> dict:
    exe = _find_powershell()
    if not exe:
        return {"ok": False, "error": "未找到 PowerShell，无法调整音频设备"}
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_AUDIO_PS1), "-action", action],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "音频配置脚本执行超时（30s）"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not out:
        return {"ok": False, "error": err or "脚本无输出"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": err or out}


@app.get(API_PREFIX + "/audio/status")
def audio_status():
    """查看当前默认播放/录音设备（三个角色：0=Console 1=Multimedia 2=Communications）。"""
    return _run_audio_config("status")


@app.post(API_PREFIX + "/audio/apply")
def audio_apply():
    """一键把音频设备调到变声最优：播放=真实扬声器，录音=CABLE Output。
    首次调用会备份用户原始默认设备，供后续 restore 使用。"""
    return _run_audio_config("apply")


@app.post(API_PREFIX + "/audio/restore")
def audio_restore():
    """用完之后恢复用户原来的默认设备（删除备份）。无备份时为空操作。"""
    return _run_audio_config("restore")


# ---------------- 静态音频 ----------------

@app.get(API_PREFIX + "/media/{kind}/{name:path}")
def media(kind: str, name: str):
    """静态音频访问。clips/voicebank 在 media/ 下，outputs 在项目根下。
    voicebank 的参考音频在 <voice_id>/reference.wav，故 name 允许多层路径。"""
    if kind not in ("clips", "outputs", "voicebank"):
        raise HTTPException(404, "invalid kind")
    base = (OUT if kind == "outputs" else ROOT / "media" / kind).resolve()
    p = (base / name).resolve()
    # 防止 ../ 之类的路径穿越（is_relative_to 精确判断层级归属）
    if not p.is_relative_to(base) or not p.is_file():
        raise HTTPException(404, f"{kind}/{name} 不存在")
    return FileResponse(str(p), media_type="audio/wav")


# ---------------- TTS（Qwen3-TTS 文字→语音） ----------------

class TTSRequest(BaseModel):
    text: str
    text_language: str = "zh"
    prompt_text: str = ""


@app.post(API_PREFIX + "/tts")
def tts_endpoint(req: TTSRequest):
    """文字→语音：用 Qwen3-TTS 袋鼠音色（002.wav）做 zero-shot 克隆。
    结果保存为 outputs/tts_*.wav 并返回 URL，便于前端下载与历史持久化。"""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    try:
        from qwen3_tts import tts as qwen_tts
        wav_bytes = qwen_tts(req.text, text_language=req.text_language,
                             prompt_text=req.prompt_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS 失败: {e}")
    fname = f"tts_{int(time.time() * 1000)}.wav"
    out = OUT / fname
    out.write_bytes(wav_bytes)
    import soundfile as sf
    d, sr = sf.read(str(out))
    return JSONResponse({
        "ok": True,
        "url": f"/api/media/outputs/{fname}",
        "duration_s": round(len(d) / sr, 1),
    })


# ---------------- RVC 袋鼠训练集（Qwen3-TTS 批量生成） ----------------

RVC_DATASET_DIR = ROOT / "media" / "rvc_dataset"
# 用户 RVC 整合包（D:\RVC）的训练集目录；导出时不存在会自动创建
RVC_EXPORT_DIR = Path(r"D:/RVC/dataset_raw/rvc_dataset")

# 20 句训练语料：与 tts_trial/qwen3_batch_tts.py 保持一致
RVC_TEXTS = [
    "怕被其他人知道这家店给你一个",
    "老板，我要两个烤串，再来一瓶可乐",
    "今天天气真不错，我们出去走走吧",
    "一二三四五六七八九十",
    "这个周末你有什么安排吗",
    "我跟你说，这家店的汉堡特别好吃",
    "快点快点，电影马上就要开始了",
    "谢谢你啊，下次请你吃饭",
    "别着急，慢慢来，安全第一",
    "昨天晚上我睡得特别香",
    "请问地铁站怎么走啊",
    "他让我转告你，明天开会改到下午",
    "我们是一家人，不用这么客气",
    "这个价格也太贵了吧",
    "手机快没电了，我先挂了啊",
    "明天早上八点，校门口见",
    "妈妈做的菜永远是最好吃的",
    "下雨了，记得带伞",
    "开饭啦，大家都过来吧",
    "坚持锻炼，身体才会越来越好",
]

RVC_GEN_STATE = {"running": False, "total": len(RVC_TEXTS), "done": 0,
                 "current": "", "error": ""}


@app.get(API_PREFIX + "/rvc/dataset")
def list_rvc_dataset():
    """列出已生成的袋鼠训练语料。"""
    import soundfile as sf
    items = []
    if RVC_DATASET_DIR.exists():
        for f in sorted(RVC_DATASET_DIR.glob("*.wav")):
            try:
                d, sr = sf.read(str(f))
                items.append({"name": f.name, "duration_s": round(len(d) / sr, 2),
                              "size_kb": round(f.stat().st_size / 1024, 1)})
            except Exception:
                continue
    return {"items": items, "export_dir": str(RVC_EXPORT_DIR)}


@app.post(API_PREFIX + "/rvc/dataset/generate")
def rvc_generate_dataset():
    """后台线程：用 Qwen3-TTS 逐句生成 20 句袋鼠语料到 media/rvc_dataset/。"""
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料正在生成中，请稍候")
    RVC_DATASET_DIR.mkdir(parents=True, exist_ok=True)

    def _job():
        RVC_GEN_STATE.update(running=True, done=0, current="", error="")
        try:
            from qwen3_tts import tts as qwen_tts
            for i, text in enumerate(RVC_TEXTS, 1):
                RVC_GEN_STATE["current"] = text
                wav = qwen_tts(text, text_language="zh")
                (RVC_DATASET_DIR / f"qwen_kangaroo_{i:03d}.wav").write_bytes(wav)
                RVC_GEN_STATE["done"] = i
        except Exception as e:
            RVC_GEN_STATE["error"] = str(e)
        finally:
            RVC_GEN_STATE["running"] = False
            RVC_GEN_STATE["current"] = ""

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "started": True, "total": len(RVC_TEXTS)}


@app.get(API_PREFIX + "/rvc/dataset/status")
def rvc_dataset_status():
    with _pipeline_lock:
        return dict(RVC_GEN_STATE)


@app.post(API_PREFIX + "/rvc/dataset/export")
def export_rvc_dataset():
    """把生成的语料同步到 RVC 整合包（D:\RVC\dataset_raw\rvc_dataset）。"""
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料生成进行中，完成后才能导出")
    if not RVC_DATASET_DIR.exists():
        raise HTTPException(400, "还没有训练语料，请先生成")
    dest = RVC_EXPORT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in RVC_DATASET_DIR.glob("*.wav"):
        shutil.copy2(f, dest / f.name)
        copied += 1
    return {"ok": True, "copied": copied, "dest": str(dest)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

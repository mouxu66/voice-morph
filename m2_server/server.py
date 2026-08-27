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

import config as cfg
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

# 可选 Bearer Token：仅当配置了 VM_API_TOKEN 时启用，否则完全不拦截（LAN-only 默认）
if cfg.API_TOKEN:
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi.responses import JSONResponse

    class _TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method == "OPTIONS" or request.url.path.endswith("/health"):
                return await call_next(request)
            if request.headers.get("Authorization", "") != f"Bearer {cfg.API_TOKEN}":
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
            return await call_next(request)

    app.add_middleware(_TokenMiddleware)

# CORS：默认允许本地前端(5173 dev)与打包后的 file:// 页面访问。
# 来源可经 VM_CORS_ORIGINS 收紧（逗号分隔）；留 "*" 维持 LAN 可用。
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.CORS_ORIGINS,
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
#   -action restore 恢复用户原始默认设备（删除备份；无备份时回退到 reset）
#   -action reset   强制恢复为真实扬声器/麦克风（兜底，无论有无备份都生效）
#   -action diag    枚举全部音频端点（含状态/角色，排查用）
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
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "音频配置脚本执行超时（30s）"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not out:
        return {"ok": False, "error": err or "脚本无输出"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": err or out}
    # 脚本明确失败（含逐项 errors）时，把明细合并到 error 便于前端展示
    if not data.get("ok"):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            data["error"] = ", ".join(str(e) for e in errs)
    return data


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
    voice_id: str = ""


def _voice_ref(voice_id: str) -> tuple[Path, str]:
    """读取音色档案的参考音频与文字稿（ref_text.txt 可缺省）。"""
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="voice_id 非法")
    ref = VOICEBANK / voice_id / "reference.wav"
    if not ref.exists():
        raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 不存在")
    ref_text = ""
    rt = VOICEBANK / voice_id / "ref_text.txt"
    if rt.exists():
        ref_text = rt.read_text("utf-8").strip()
    return ref, ref_text


@app.post(API_PREFIX + "/tts")
def tts_endpoint(req: TTSRequest):
    """文字→语音：按 voice_id 音色克隆合成（不传 voice_id 则用当前选中音色，都没有则报错）。
    结果保存为 outputs/tts_*.wav 并返回 URL，便于前端下载与历史持久化。"""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    voice_id = req.voice_id or _load_selected_voice()
    if not voice_id:
        raise HTTPException(status_code=400, detail="请先选择音色")
    ref, _ref_text = _voice_ref(voice_id)
    try:
        from qwen3_tts import tts as qwen_tts
        # 走声纹(x-vector)克隆模式：不受参考音频长度拖累，8GB 显存下稳定出声；
        # ICL 模式只保留在挖掘试听(短切片+文字稿)里使用。
        wav_bytes = qwen_tts(req.text, ref_audio=str(ref), ref_text="",
                             language="Chinese" if req.text_language.startswith("zh") else "English")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS 失败: {e}")
    fname = f"tts_{int(time.time() * 1000)}.wav"
    out = OUT / fname
    out.write_bytes(wav_bytes)
    import soundfile as sf
    d, sr = sf.read(str(out))
    return JSONResponse({
        "ok": True,
        "voice_id": voice_id,
        "url": f"/api/media/outputs/{fname}",
        "duration_s": round(len(d) / sr, 1),
    })


# ---------------- 音色挖掘（上传视频 → 自动筛音色 → 迭代试听） ----------------

MINE_STATE: dict = {
    "running": False,
    "stage": "",       # idle | running | done | error
    "message": "",
    "kept": 0,
    "clusters": [],    # [{cluster,size,members,rep:{name,path,text}}]
}
# 试听句池：与素材视频内容无关的全新句子（试听音色迁移能力，避免"复读原视频"）
PREVIEW_TEXTS = [
    "今天的市场格外热闹，到处都是新鲜的水果和蔬菜。",
    "记得明天早上八点开会，材料记得提前发给我。",
    "这幅画的颜色搭配真让人心情舒畅。",
    "山间的清晨空气清凉，鸟鸣声此起彼伏。",
    "他慢慢走进书店，在角落里找到一本旧诗集。",
    "周末我们骑车去河边，看看落日再回来。",
]


def _load_selected_voice() -> str:
    """读取当前选中的音色（挖掘保存时写入 selected_voice.json）。"""
    f = VOICEBANK / "selected_voice.json"
    if f.exists():
        try:
            return str(json.loads(f.read_text("utf-8")).get("voice_id") or "")
        except Exception:
            return ""
    return ""


def _mine_worker_thread():
    """后台挖掘线程：枚举切片 -> 调 worker /analyze。"""
    MINE_STATE.update(running=True, stage="running", message="正在转写与提取声纹…", kept=0, clusters=[])
    try:
        clips = [{"name": p.stem, "path": str(p)} for p in sorted(CLIPS_DIR.glob("*.wav"))]
        if not clips:
            raise RuntimeError("没有可用切片，请先在音色工坊解析视频")
        from qwen3_tts import analyze
        result = analyze(clips)
        MINE_STATE.update(running=False, stage="done", message="挖掘完成",
                          kept=result.get("kept", 0), clusters=result.get("clusters", []),
                          errors=result.get("errors", []))
    except Exception as e:
        MINE_STATE.update(running=False, stage="error", message=str(e))


@app.post(API_PREFIX + "/mine/run")
def mine_run():
    """对当前全部切片跑音色挖掘（后台执行，前端轮询 /mine/state）。"""
    if MINE_STATE["running"]:
        return {"ok": True, "already_running": True}
    threading.Thread(target=_mine_worker_thread, daemon=True).start()
    return {"ok": True}


@app.get(API_PREFIX + "/mine/state")
def mine_state():
    return MINE_STATE


class MinePreviewRequest(BaseModel):
    clip: str            # 切片名（不含 .wav），即候选代表切片
    text: str = ""       # 试听文本；留空则从新句池轮换（保证与视频原话不同）


@app.post(API_PREFIX + "/mine/preview")
def mine_preview(req: MinePreviewRequest):
    """用候选切片做参考，合成一句全新文本试听（ICL 模式，转写来自挖掘结果）。"""
    p = CLIPS_DIR / f"{req.clip}.wav"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"切片不存在: {req.clip}")
    # 从挖掘结果里取该切片的转写文字稿
    ref_text = ""
    for c in MINE_STATE["clusters"]:
        if c["rep"]["name"] == req.clip:
            ref_text = c["rep"]["text"]
            break
    text = req.text.strip()
    if not text:
        idx = (MINE_STATE.get("preview_seq", 0)) % len(PREVIEW_TEXTS)
        MINE_STATE["preview_seq"] = MINE_STATE.get("preview_seq", 0) + 1
        text = PREVIEW_TEXTS[idx]
    try:
        from qwen3_tts import tts as qwen_tts
        wav_bytes = qwen_tts(text, ref_audio=str(p), ref_text=ref_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听合成失败: {e}")
    fname = f"mine_{int(time.time() * 1000)}.wav"
    out = OUT / fname
    out.write_bytes(wav_bytes)
    import soundfile as sf
    d, sr = sf.read(str(out))
    return JSONResponse({
        "ok": True, "clip": req.clip, "text": text,
        "ref_text": ref_text,
        "url": f"/api/media/outputs/{fname}",
        "duration_s": round(len(d) / sr, 1),
    })


class MineSaveRequest(BaseModel):
    clip: str
    voice_id: str
    display_name: str = ""
    members: list[str] = []   # 可选：把同簇多条一起聚合为参考档案


@app.post(API_PREFIX + "/mine/save")
def mine_save(req: MineSaveRequest):
    """把满意的候选保存为正式音色：代表切片(可含同簇成员)聚合为 reference.wav + ref_text.txt。

    参考音频只取簇内排名靠前的约 20 秒（ICL 克隆对超长参考既慢又会劣化），
    成员顺序即挖掘结果的质量排序。
    """
    if not is_valid_voice_id(req.voice_id):
        raise HTTPException(status_code=400, detail="音色 ID 非法")
    from pydub import AudioSegment

    REF_CAP_MS = 20000
    members = [req.clip] + [m for m in req.members if m != req.clip]
    merged = AudioSegment.silent(duration=300)
    texts = []
    rep_text = ""
    for name in members:
        p = CLIPS_DIR / f"{name}.wav"
        if not p.exists():
            continue
        seg = AudioSegment.from_wav(str(p))
        merged += seg.set_channels(1).set_frame_rate(22050) + AudioSegment.silent(duration=300)
        for c in MINE_STATE["clusters"]:
            rep_name = c.get("rep", {}).get("name")
            if rep_name == name:
                texts.append(c["rep"]["text"])
                if name == req.clip:
                    rep_text = c["rep"]["text"]
                break
        if len(merged) >= REF_CAP_MS:
            break
    if len(merged) <= 300:
        raise HTTPException(status_code=404, detail="候选切片不存在")

    out_dir = VOICEBANK / req.voice_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = out_dir / "reference.wav"
    merged.set_channels(1).set_frame_rate(22050).export(str(ref), format="wav")
    # 文字稿与参考音频逐句对齐（聚了多条切片就拼全部文字稿），ICL 克隆更准
    (out_dir / "ref_text.txt").write_text(" ".join(texts) or rep_text, encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps(
        {"display_name": req.display_name or req.voice_id, "source": "mine"}, ensure_ascii=False),
        encoding="utf-8")
    (out_dir / "clips.txt").write_text("\n".join(members), encoding="utf-8")
    # 保存后设为当前选中音色
    (VOICEBANK / "selected_voice.json").write_text(
        json.dumps({"voice_id": req.voice_id}), encoding="utf-8")
    return {"ok": True, "voice_id": req.voice_id, "duration_s": round(len(merged) / 1000, 1),
            "clips": len(members)}


# ---------------- RVC 训练集（Qwen3-TTS 批量生成） ----------------

RVC_DATASET_DIR = ROOT / "media" / "rvc_dataset"
# RVC 整合包与训练产物路径集中由 config 定义（环境变量可覆盖）
RVC_EXPORT_DIR = cfg.RVC_EXPORT_DIR
RVC_DEFAULT_EXP = cfg.RVC_DEFAULT_EXP


def _rvc_weights_dir(exp: str | None = None) -> Path:
    """某实验名（音色 ID）的 RVC 权重目录 logs/<exp>/。"""
    return cfg.RVC_ROOT / "logs" / (exp or RVC_DEFAULT_EXP)

# 训练语料模板：默认从 data/rvc_texts.txt 读取（可经 VM_RVC_TEXTS_FILE 覆盖为任意音色专用语料）；
# 文件缺失时回退内置 20 句，保证历史行为不变。
RVC_TEXTS = cfg.load_rvc_texts()

RVC_GEN_STATE = {"running": False, "total": len(RVC_TEXTS), "done": 0,
                 "current": "", "error": ""}


@app.get(API_PREFIX + "/rvc/dataset")
def list_rvc_dataset():
    """列出已生成的训练语料。"""
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


class RvcGenerateReq(BaseModel):
    """指定用哪个音色生成语料；不传则用当前选中音色。"""
    voice_id: str | None = None


@app.post(API_PREFIX + "/rvc/dataset/generate")
def rvc_generate_dataset(req: RvcGenerateReq | None = None):
    """后台线程：用 Qwen3-TTS 按指定音色逐句生成 20 句语料到 media/rvc_dataset/。

    语料文件名前缀取音色 ID（安全化），便于多音色区分。
    """
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料正在生成中，请稍候")
    body = req or RvcGenerateReq()
    voice_id = (body.voice_id or _load_selected_voice()).strip()
    if not voice_id:
        raise HTTPException(400, "未指定 voice_id，且没有已选中的音色")
    ref_audio, ref_text = _voice_ref(voice_id)
    prefix = "rvc_" + re.sub(r"[^0-9A-Za-z_-]", "_", voice_id)
    RVC_DATASET_DIR.mkdir(parents=True, exist_ok=True)

    def _job():
        RVC_GEN_STATE.update(running=True, done=0, current="", error="")
        try:
            from qwen3_tts import tts as qwen_tts
            for i, text in enumerate(RVC_TEXTS, 1):
                RVC_GEN_STATE["current"] = text
                wav = qwen_tts(text, ref_audio=str(ref_audio), ref_text=ref_text)
                (RVC_DATASET_DIR / f"{prefix}_{i:03d}.wav").write_bytes(wav)
                RVC_GEN_STATE["done"] = i
        except Exception as e:
            RVC_GEN_STATE["error"] = str(e)
        finally:
            RVC_GEN_STATE["running"] = False
            RVC_GEN_STATE["current"] = ""

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "started": True, "total": len(RVC_TEXTS), "voice_id": voice_id}


@app.get(API_PREFIX + "/rvc/dataset/status")
def rvc_dataset_status():
    with _pipeline_lock:
        return dict(RVC_GEN_STATE)


@app.post(API_PREFIX + "/rvc/dataset/export")
def export_rvc_dataset(exp_name: str | None = None):
    """把生成的语料同步到 RVC 整合包（dataset_raw/<exp>/，供该实验名训练用）。"""
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料生成进行中，完成后才能导出")
    if not RVC_DATASET_DIR.exists():
        raise HTTPException(400, "还没有训练语料，请先生成")
    exp = exp_name or RVC_DEFAULT_EXP
    dest = RVC_EXPORT_DIR.parent / exp
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in RVC_DATASET_DIR.glob("*.wav"):
        shutil.copy2(f, dest / f.name)
        copied += 1
    # 同步一份到训练驱动默认读取的数据集目录 logs 同级 dataset/<exp>/
    default_ds = cfg.RVC_ROOT / "dataset" / exp
    default_ds.mkdir(parents=True, exist_ok=True)
    for f in RVC_DATASET_DIR.glob("*.wav"):
        shutil.copy2(f, default_ds / f.name)
    return {"ok": True, "copied": copied, "dest": str(dest),
            "train_dataset_dir": str(default_ds), "exp": exp}


@app.get(API_PREFIX + "/rvc/model")
def rvc_model_status(exp_name: str | None = None):
    """RVC 模型训练完成状态（供前端「模型已就绪」卡片展示）；可按音色 ID 查询。"""
    weights_dir = _rvc_weights_dir(exp_name or RVC_DEFAULT_EXP)
    exp = exp_name or RVC_DEFAULT_EXP
    pth = weights_dir / f"{exp}.pth"
    idx = next(weights_dir.glob("added_*.index"), None) if weights_dir.exists() else None
    trained = pth.exists() and idx is not None
    dataset_count = len(list(RVC_DATASET_DIR.glob("*.wav"))) if RVC_DATASET_DIR.exists() else 0
    return {
        "exp": exp,
        "trained": trained,
        "pth_exists": pth.exists(),
        "index_exists": idx is not None,
        "dataset_count": dataset_count,
        "weights_dir": str(weights_dir),
        "dataset_dir": str(RVC_DATASET_DIR),
    }


if __name__ == "__main__":
    uvicorn.run(app, host=cfg.SERVER_HOST, port=cfg.SERVER_PORT)

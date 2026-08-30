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
from finetune import router as ft_router
from audiobook import router as audiobook_router
from cascade import router as cascade_router
from offline_vc import router as offlinevc_router
from effects import router as effects_router
from rvc_live import router as rvc_live_router, _find_pth

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
VOICEBANK = cfg.MEDIA_DIR / "voicebank"
CLIPS_DIR = cfg.MEDIA_DIR / "clips"
RAW_DIR = cfg.MEDIA_DIR / "raw_videos"
OUT = cfg.OUTPUTS_DIR
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

# 可选 Token 鉴权：仅当配置了 VM_API_TOKEN 时启用，否则完全不拦截（LAN-only 默认）。
# 设计要点：
# - 本机回环(127.0.0.1/::1)永远放行：PC 前端(Electron/vite proxy)、桌宠、全局热键、
#   子进程都不带 token，启用鉴权不能破坏本机任何链路
# - 局域网请求需通过以下任一方式：X-API-Key 头 / Authorization: Bearer / api_key 查询参数
#   （移动端原生音频播放器请求 URL 时带不了自定义 header，所以必须支持查询参数）
if cfg.API_TOKEN:
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi.responses import JSONResponse

    class _TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)
            client = request.client.host if request.client else ""
            if client in ("127.0.0.1", "::1", "localhost"):
                return await call_next(request)
            token = cfg.API_TOKEN
            ok = (
                request.headers.get("X-API-Key", "") == token
                or request.headers.get("Authorization", "") == f"Bearer {token}"
                or request.query_params.get("api_key", "") == token
            )
            if not ok:
                return JSONResponse(status_code=401, content={"detail": "unauthorized（需 X-API-Key 头或 api_key 参数）"})
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
app.include_router(ft_router)
app.include_router(audiobook_router)
app.include_router(offlinevc_router)
app.include_router(cascade_router)
app.include_router(effects_router)


@app.get(API_PREFIX + "/health")
def health():
    import torch
    return {"status": "ok", "cuda": torch.cuda.is_available()}


# ---------------- 音色库 ----------------

def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text("utf-8"))
    except Exception:
        return {}


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
            meta = _read_meta(meta_path) if (meta_path := d / "meta.json").exists() else {}
            if meta.get("display_name"):
                display = str(meta["display_name"])
            voices.append({
                "id": d.name,
                "display_name": display,
                "reference": ref.name,
                "duration_s": round(len(AudioSegment.from_wav(str(ref))) / 1000, 1),
                "kind": meta.get("kind") or "clone",
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
    # 把来源素材写进 meta（素材库「已用于」徽标的持久依据）
    meta_p = out_dir / "meta.json"
    meta: dict = {}
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if RAW_DIR.exists():
        sources = sorted(
            f.name for f in RAW_DIR.iterdir()
            if f.suffix.lower() in _VIDEO_SUFFIXES
            and any(name.startswith(_clip_prefix(f.stem)) or name.startswith(f.stem[:12]) for name in clips)
        )
        if sources:
            meta["sources"] = sources
    try:
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    # 删除旧的音色向量缓存，下次自动重新提取
    (out_dir / "reference_se.npy").unlink(missing_ok=True)

    return {"ok": True, "voice_id": voice_id, "duration_s": round(len(merged) / 1000, 1)}


# ---------------- M1 素材流水线 ----------------

@app.get(API_PREFIX + "/raw_videos")
def list_raw_videos():
    usage = _raw_video_usage()
    videos = []
    for f in RAW_DIR.iterdir():
        if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi"):
            videos.append({"name": f.name, "size_mb": round(f.stat().st_size / 1e6, 1),
                           "used_by": usage.get(f.name, [])})
    return {"videos": videos}


def _clip_prefix(video_stem: str) -> str:
    """切片文件名前缀：完整素材名（去文件系统非法字符）。

    旧版用 stem[:12]，两个 video_260828_* 素材会碰撞导致删一个误删另一个的切片。
    """
    return re.sub(r'[\\/:*?"<>|]', "", video_stem)[:80]


def _raw_video_usage() -> dict[str, list[str]]:
    """素材名 -> 使用它的音色显示名列表。

    依据：音色 clips.txt 里的片段名以素材切片前缀开头（新旧两种前缀都兼容）。
    """
    videos = [f for f in RAW_DIR.iterdir() if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi")] if RAW_DIR.exists() else []
    usage: dict[str, list[str]] = {f.name: [] for f in videos}
    if not videos or not VOICEBANK.exists():
        return usage
    for vdir in VOICEBANK.iterdir():
        if not vdir.is_dir():
            continue
        clips_txt = vdir / "clips.txt"
        if not clips_txt.exists():
            continue
        try:
            names = [line.strip() for line in clips_txt.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            continue
        if not names:
            continue
        try:
            meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        label = meta.get("display_name") or vdir.name
        for f in videos:
            prefixes = {f.stem[:12], _clip_prefix(f.stem)}
            if any(any(n.startswith(p) for p in prefixes) for n in names):
                usage[f.name].append(label)
    return usage


@app.delete(API_PREFIX + "/raw_videos/{name}")
def delete_raw_video(name: str, force: bool = False):
    """删除素材及其派生产物（切片/音轨/分离产物/预览）。

    被音色引用时返回 409，确认删除需 force=true（不影响已有音色，
    只是不能再用该素材重新生成语料）。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = RAW_DIR / name
    if not p.exists():
        raise HTTPException(404, f"素材不存在: {name}")
    usage = _raw_video_usage().get(name, [])
    if usage and not force:
        raise HTTPException(409, f"该素材已被音色使用：{'、'.join(usage)}。删除不影响已有音色，但无法再重新生成语料")

    prefix = _clip_prefix(p.stem)
    legacy_prefix = p.stem[:12]
    # 旧版切片用 stem[:12] 命名：若目录里还有其他素材共享该前缀（如 video_260828_*），
    # legacy 匹配会误删别人的切片——此时只按新前缀清理
    legacy_safe = not any(
        v.is_file() and v.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4a", ".mp3", ".wav"}
        and v.stem != p.stem and v.stem[:12] == legacy_prefix
        for v in RAW_DIR.iterdir()
    )
    removed = {"clips": 0, "related": 0}

    def _hit(stem: str) -> bool:
        return stem.startswith(prefix) or (legacy_safe and stem.startswith(legacy_prefix))

    # 切片
    if CLIPS_DIR.exists():
        for c in list(CLIPS_DIR.iterdir()):
            if c.is_file() and _hit(c.stem):
                c.unlink(missing_ok=True)
                removed["clips"] += 1
    # 音轨 / 分离产物 / 预览（可能带 stem 子目录，一并清）
    for d_name in ("vocals", "demucs_out", "clip_previews"):
        d = cfg.MEDIA_DIR / d_name
        if not d.exists():
            continue
        for f in list(d.rglob("*")):
            if f.is_file() and _hit(f.stem):
                f.unlink(missing_ok=True)
                removed["related"] += 1
        for sub in list(d.iterdir()):
            if sub.is_dir() and _hit(sub.stem):
                shutil.rmtree(sub, True)
                removed["related"] += 1
    p.unlink()
    return {"ok": True, **removed, "used_by": usage}


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
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}


@app.post(API_PREFIX + "/upload/video")
async def upload_video(file: UploadFile = File(...)):
    """拖拽/选择上传视频或音频素材到 media/raw_videos/"""
    if not file.filename:
        raise HTTPException(400, "未提供文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _VIDEO_SUFFIXES and suffix not in _AUDIO_SUFFIXES:
        raise HTTPException(400, f"仅支持视频/音频格式：{', '.join(sorted(_VIDEO_SUFFIXES | _AUDIO_SUFFIXES))}")
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
                n = mod.step3_slice(vocal, _clip_prefix(v.stem), _pipeline_cancel)
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
    base = (OUT if kind == "outputs" else cfg.MEDIA_DIR / kind).resolve()
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
        # 优先 ICL 语气克隆(ref_text 有内容即走 ICL，音色/语气最贴原视频)；
        # ref_text 为空才回退纯声纹(x-vector)模式。6.4s 参考音 + 真实文字稿在 8GB 显存已验证可跑。
        wav_bytes = qwen_tts(req.text, ref_audio=str(ref), ref_text=_ref_text,
                             language="Chinese" if req.text_language.startswith("zh") else "English",
                             voice_id=voice_id)
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


def _mine_worker_thread(params: dict | None = None):
    """后台挖掘线程：枚举切片 -> 调 worker /analyze。"""
    params = params or {}
    MINE_STATE.update(running=True, stage="running", message="正在转写与提取声纹…", kept=0, clusters=[])
    try:
        clips = [{"name": p.stem, "path": str(p)} for p in sorted(CLIPS_DIR.glob("*.wav"))]
        if not clips:
            raise RuntimeError("没有可用切片，请先在音色工坊解析视频")
        from qwen3_tts import analyze
        result = analyze(clips, sim_threshold=params.get("sim_threshold"),
                         min_cluster_size=params.get("min_cluster_size"))
        MINE_STATE.update(running=False, stage="done", message="挖掘完成",
                          kept=result.get("kept", 0), clusters=result.get("clusters", []),
                          errors=result.get("errors", []))
    except Exception as e:
        MINE_STATE.update(running=False, stage="error", message=str(e))


class MineRunRequest(BaseModel):
    sim_threshold: float | None = None   # 聚类相似度阈值，默认 0.5，调高挖出更多不同音色
    min_cluster_size: int | None = None  # 最小簇成员数，过滤零散噪声簇


@app.post(API_PREFIX + "/mine/run")
def mine_run(req: MineRunRequest | None = None):
    """对当前全部切片跑音色挖掘（后台执行，前端轮询 /mine/state）。"""
    if MINE_STATE["running"]:
        return {"ok": True, "already_running": True}
    threading.Thread(target=_mine_worker_thread, daemon=True,
                     kwargs={"params": req.model_dump(exclude_none=True) if req else {}}).start()
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


# ---------------- A/B 音色对比（盲听评分） ----------------

# 声纹嵌入缓存：path -> (mtime, emb)，同一参考音频不反复送 worker 提取
_EMB_CACHE: dict = {}


def _speaker_emb(path: Path) -> "list[float]":
    """取某 wav 的声纹嵌入（worker /emb，与克隆同一编码器），带 mtime 缓存。"""
    mtime = path.stat().st_mtime
    cached = _EMB_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    from qwen3_tts import post
    data = json.loads(post("/emb", {"path": str(path)}, timeout=120))
    if "emb" not in data:
        raise RuntimeError(f"声纹提取失败: {data.get('error')}")
    _EMB_CACHE[str(path)] = (mtime, data["emb"])
    return data["emb"]


class AbRunRequest(BaseModel):
    voice_a: str
    voice_b: str
    text: str = ""


@app.post(API_PREFIX + "/ab/run")
def ab_run(req: AbRunRequest):
    """A/B 对比：同一句文本分别用两个音色合成，并计算各自与参考音频的声纹余弦相似度。
    顺序打乱交给前端做盲听，揭晓后再展示相似度分数。"""
    if req.voice_a == req.voice_b:
        raise HTTPException(status_code=400, detail="两个音色不能相同")
    text = req.text.strip() or PREVIEW_TEXTS[int(time.time()) % len(PREVIEW_TEXTS)]
    import numpy as np
    from qwen3_tts import tts as qwen_tts

    results = {}
    for tag, vid in (("A", req.voice_a), ("B", req.voice_b)):
        ref, _ = _voice_ref(vid)
        # 强制 x-vector 声纹模式（ref_text 置空）：A/B 考察的是音色相似度本身，
        # 且长参考 ICL 在 8GB 卡上会跌进 WDDM 共享内存慢路径（20s 参考要 20 分钟+）
        wav = qwen_tts(text, ref_audio=str(ref), ref_text="", voice_id=vid)
        fname = f"ab_{int(time.time() * 1000)}_{tag}.wav"
        out = OUT / fname
        out.write_bytes(wav)
        emb_gen = np.asarray(_speaker_emb(out))
        emb_ref = np.asarray(_speaker_emb(ref))
        sim = float(np.dot(emb_gen, emb_ref) / (np.linalg.norm(emb_gen) * np.linalg.norm(emb_ref)))
        results[tag] = {"voice_id": vid, "url": f"/api/media/outputs/{fname}", "similarity": round(sim, 3)}
    return {"ok": True, "text": text, **results}


# ---------------- 音色包导出 / 导入 ----------------

# 音色包内允许携带的档案文件（白名单，杜绝 zip-slip 与超大缓存）
_PACK_FILES = ("reference.wav", "ref_text.txt", "meta.json", "clips.txt")


@app.get(API_PREFIX + "/voicebank/{voice_id}/export")
def export_voice_pack(voice_id: str, include_rvc: bool = False):
    """打包音色档案为 zip（reference.wav + 文字稿 + meta + 片段清单）。
    include_rvc=true 时附带该音色的 RVC 权重与索引（若已训练），导入端可直接实时变声。"""
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="音色 ID 非法")
    d = VOICEBANK / voice_id
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 不存在")
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # meta.json 里补写 voice_id，导入端据此还原档案
        meta = _read_meta(d / "meta.json")
        meta["voice_id"] = voice_id
        zf.writestr("voicepack/meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for name in _PACK_FILES:
            if name == "meta.json":
                continue
            p = d / name
            if p.exists():
                zf.write(p, f"voicepack/{name}")
        if include_rvc:
            log_dir = cfg.RVC_ROOT / "logs" / voice_id
            if log_dir.exists():
                pth = log_dir / f"{voice_id}.pth"
                if not pth.exists():  # 回退训练自动产出的 G_*.pth（与 rvc_live 的规则一致）
                    pth = next(log_dir.glob("G_*.pth"), None)
                if pth is not None and pth.exists():
                    zf.write(pth, f"voicepack/rvc/{pth.name}")
                for idx in log_dir.glob("added_*.index"):
                    zf.write(idx, f"voicepack/rvc/{idx.name}")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="voicepack_{voice_id}.zip"'},
    )


@app.post(API_PREFIX + "/voicebank/import")
async def import_voice_pack(file: UploadFile = File(...), overwrite: bool = False):
    """导入音色包 zip：还原为 voicebank/<voice_id>/ 音色档案。同名默认拒绝，overwrite=1 覆盖。
    注意：微调模型（ft_model，含 3.6G 基座硬链接）不随包携带，导入后自动降级为 x-vector 声纹克隆。"""
    import io
    import zipfile

    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip 音色包")
    names = zf.namelist()
    if "voicepack/reference.wav" not in names or "voicepack/meta.json" not in names:
        raise HTTPException(status_code=400, detail="缺少 voicepack/reference.wav 或 meta.json，不是音色包")
    try:
        meta = json.loads(zf.read("voicepack/meta.json").decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="meta.json 解析失败")
    voice_id = str(meta.get("voice_id") or "").strip()
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="包内 voice_id 非法")
    dest = VOICEBANK / voice_id
    if dest.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"音色 [{voice_id}] 已存在；如需覆盖请勾选覆盖导入")

    await run_in_threadpool(lambda: shutil.rmtree(dest, True) if dest.exists() else None)
    # 包内 rvc/ 权重还原到 RVC 整合包 logs/<voice_id>/（实时变声直接可用）；整合包缺失则跳过并提示
    rvc_log_dir = cfg.RVC_ROOT / "logs" / voice_id
    rvc_restored = 0
    for name in names:
        if not name.startswith("voicepack/") or name.endswith("/"):
            continue
        rel = name[len("voicepack/"):]
        # 逐项防路径穿越：档案文件白名单 + rvc/ 一层子目录
        is_rvc = bool(re.match(r"^rvc/[A-Za-z0-9_.-]+$", rel))
        if not is_rvc and rel not in _PACK_FILES:
            continue
        target = (rvc_log_dir / rel[len("rvc/"):]) if is_rvc else (dest / rel)
        if is_rvc:
            if not cfg.RVC_ROOT.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(name))
        if is_rvc:
            rvc_restored += 1

    kind = str(meta.get("kind") or "clone")
    if kind == "finetuned" and not (dest / "ft_model" / "model.safetensors").exists():
        # 包里没带微调权重：降级为普通声纹音色，避免 /tts 去加载不存在的模型目录
        meta["kind"] = "clone"
        meta.pop("model_dir", None)
        meta["import_note"] = "微调权重未随包携带，已降级为声纹克隆"
    kind = str(meta.get("kind") or "clone")
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "voice_id": voice_id, "kind": kind, "rvc_files": rvc_restored,
            "display_name": meta.get("display_name") or voice_id}


# ---------------- RVC 训练集（Qwen3-TTS 批量生成） ----------------

RVC_DATASET_DIR = cfg.MEDIA_DIR / "rvc_dataset"
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
def list_rvc_dataset(voice_id: str | None = None):
    """列出已生成的训练语料；传 voice_id 时只看该音色的语料。"""
    import soundfile as sf
    prefix = f"rvc_{re.sub(r'[^0-9A-Za-z_-]', '_', voice_id)}_" if voice_id else None
    items = []
    if RVC_DATASET_DIR.exists():
        files = sorted(RVC_DATASET_DIR.glob("*.wav"))
        for f in files if not prefix else [f for f in files if f.name.startswith(prefix)]:
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
def export_rvc_dataset(exp_name: str | None = None, voice_id: str | None = None):
    """把语料同步到 RVC 整合包（dataset_raw/<exp>/ 与训练读取的 dataset/<exp>/）。

    传 voice_id 时只导出该音色自己的语料（按生成时的文件名前缀过滤），
    避免把 A 音色的语料混进 B 音色的训练集 —— 这是"选了音色却不像"的常见根因。
    """
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料生成进行中，完成后才能导出")
    if not RVC_DATASET_DIR.exists():
        raise HTTPException(400, "还没有训练语料，请先生成")
    exp = exp_name or voice_id or RVC_DEFAULT_EXP
    # 前缀后必须紧跟 "_"，否则 rvc_mei 会误匹配到 rvc_meituan_rat 的语料
    prefix = f"rvc_{re.sub(r'[^0-9A-Za-z_-]', '_', voice_id)}_" if voice_id else None
    files = [f for f in RVC_DATASET_DIR.glob("*.wav")
             if not prefix or f.name.startswith(prefix)]
    if not files:
        raise HTTPException(400, f"音色 [{voice_id}] 还没有语料，请先生成" if voice_id
                            else "还没有训练语料，请先生成")

    dest = RVC_EXPORT_DIR.parent / exp
    # 训练驱动默认读取 dataset/<exp>/（与 logs/<exp>/ 同级，见 cfg.rvc_exp_dirs）
    train_ds = cfg.rvc_exp_dirs(exp)[1]
    for d in (dest, train_ds):
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, d / f.name)
    return {"ok": True, "copied": len(files), "dest": str(dest),
            "train_dataset_dir": str(train_ds), "exp": exp}


@app.get(API_PREFIX + "/rvc/model")
def rvc_model_status(exp_name: str | None = None):
    """RVC 模型训练完成状态（供前端「模型已就绪」卡片展示）；可按音色 ID 查询。"""
    weights_dir = _rvc_weights_dir(exp_name or RVC_DEFAULT_EXP)
    exp = exp_name or RVC_DEFAULT_EXP
    pth = _find_pth(exp, weights_dir)
    idx = next(weights_dir.glob("added_*.index"), None) if weights_dir.exists() else None
    trained = pth is not None and idx is not None
    # 语料数按该实验自己的训练集目录统计（与 /rvc/voices 口径一致），
    # 否则多个音色共用 media/rvc_dataset 会互相虚报。
    ds_dir = cfg.rvc_exp_dirs(exp)[1]
    dataset_count = len(list(ds_dir.glob("*.wav"))) if ds_dir.exists() else 0
    return {
        "exp": exp,
        "trained": trained,
        "pth_exists": pth is not None,
        "index_exists": idx is not None,
        "dataset_count": dataset_count,
        "weights_dir": str(weights_dir),
        "dataset_dir": str(ds_dir),
    }


# ---------------- 局域网访问：托管前端静态资源（手机浏览器打开 http://<本机IP>:8000） ----------------
# 安装版前端在 app.asar 里不可读，打包时额外放一份到 backend/web_dist；开发态直接用 web/dist。
from fastapi.responses import FileResponse  # noqa: E402

_web_dist = next((c for c in [ROOT / "web_dist", ROOT / "web" / "dist"]
                  if (c / "index.html").exists()), None)
if _web_dist is not None:

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str):
        cand = (_web_dist / full_path).resolve()
        if full_path and cand.is_file() and str(cand).startswith(str(_web_dist.resolve())):
            return FileResponse(cand)
        return FileResponse(_web_dist / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=cfg.SERVER_HOST, port=cfg.SERVER_PORT)

"""素材库接口：素材列表/打标/删除、上传、打开目录。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

import config as cfg
from common import MAX_UPLOAD_BYTES
from runtime import (API_PREFIX, CLIPS_DIR, OUT, RAW_DIR, VIDEO_SUFFIXES,
                     VOICEBANK, AUDIO_SUFFIXES, clip_prefix)

router = APIRouter(prefix=API_PREFIX)


def _video_meta(name: str) -> dict:
    """读取素材打标结果 media/raw_videos/<name>.meta.json；不存在返回 {}。"""
    mp = RAW_DIR / f"{name}.meta.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _start_tagging(name: str) -> None:
    """后台线程：对素材打标（FRD F2），写 meta.json。失败不阻断上传。"""
    def _job():
        from tagging import tag_video
        src = RAW_DIR / name
        if not src.exists():
            return  # 素材已被删，静默终止
        meta = {"tagging": True, "tagged_at": int(time.time())}
        (RAW_DIR / f"{name}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        result = tag_video(src, tmp_dir=cfg.MEDIA_DIR / "_tag_tmp")
        result["tagged_at"] = int(time.time())
        try:
            (RAW_DIR / f"{name}.meta.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()


@router.get("/raw_videos")
def list_raw_videos():
    usage = _raw_video_usage()
    videos = []
    for f in RAW_DIR.iterdir():
        if f.suffix.lower() in VIDEO_SUFFIXES:
            videos.append({"name": f.name, "size_mb": round(f.stat().st_size / 1e6, 1),
                           "used_by": usage.get(f.name, []),
                           "meta": _video_meta(f.name)})
    return {"videos": videos}


@router.post("/raw_videos/{name}/tag")
def tag_raw_video(name: str):
    """对已存在素材强制（重新）打标。文件不存在返回 404。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = RAW_DIR / name
    if not p.exists():
        raise HTTPException(404, f"素材不存在: {name}")
    if p.suffix.lower() not in VIDEO_SUFFIXES and p.suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(400, "该文件不是可打标的音视频素材")
    _start_tagging(name)
    return {"ok": True, "name": name, "tagging": True}


def _raw_video_usage() -> dict[str, list[str]]:
    """素材名 -> 使用它的音色显示名列表。

    依据：音色 clips.txt 里的片段名以素材切片前缀开头（新旧两种前缀都兼容）。
    """
    videos = [f for f in RAW_DIR.iterdir() if f.suffix.lower() in VIDEO_SUFFIXES] if RAW_DIR.exists() else []
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
            prefixes = {f.stem[:12], clip_prefix(f.stem)}
            if any(any(n.startswith(p) for p in prefixes) for n in names):
                usage[f.name].append(label)
    return usage


@router.delete("/raw_videos/{name}")
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

    import shutil
    prefix = clip_prefix(p.stem)
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


@router.post("/upload/video")
async def upload_video(file: UploadFile = File(...)):
    """拖拽/选择上传视频或音频素材到 media/raw_videos/"""
    if not file.filename:
        raise HTTPException(400, "未提供文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_SUFFIXES and suffix not in AUDIO_SUFFIXES:
        raise HTTPException(400, f"仅支持视频/音频格式：{', '.join(sorted(VIDEO_SUFFIXES | AUDIO_SUFFIXES))}")
    dest = RAW_DIR / Path(file.filename).name
    if dest.exists():
        raise HTTPException(409, f"同名文件已存在：{file.filename}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝上传")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝上传")
    dest.write_bytes(data)
    _start_tagging(file.filename)  # 后台打标（FRD F2），不阻塞上传响应
    return {"ok": True, "name": file.filename, "size_mb": round(len(data) / 1e6, 1)}


class OpenFolderRequest(BaseModel):
    kind: str  # raw_videos | clips | outputs | voicebank


@router.post("/open/folder")
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

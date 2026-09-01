"""m2_server 公共工具：音色 ID 校验 / 音色档案读取 / 当前选中音色。

集中了原先在 server.py / audiobook.py / finetune.py 各自重复的实现，
避免「同一规则四处定义、改一处漏三处」。
"""
import json
import os
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

import config as cfg

# 音色 ID 白名单：仅允许字母/数字/下划线/连字符，杜绝路径穿越（如 .. 或 /）
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 上传文件大小上限（500MB，视频素材/录音够用；LAN 暴露时防恶意撑爆磁盘）
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def is_valid_voice_id(voice_id: str) -> bool:
    return bool(voice_id) and bool(_VOICE_ID_RE.match(voice_id))


def voice_ref(voice_id: str) -> tuple[Path, str]:
    """读取音色档案的参考音频与文字稿（ref_text.txt 可缺省）。

    非法 ID / 音色不存在时抛 HTTPException。返回 (reference.wav 路径, 文字稿)。
    """
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="voice_id 非法")
    ref = cfg.MEDIA_DIR / "voicebank" / voice_id / "reference.wav"
    if not ref.exists():
        raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 不存在")
    ref_text = ""
    rt = ref.parent / "ref_text.txt"
    if rt.exists():
        ref_text = rt.read_text("utf-8").strip()
    return ref, ref_text


def selected_voice() -> str:
    """读取当前选中的音色 ID（音色挖掘/保存时写入 selected_voice.json）。"""
    f = cfg.MEDIA_DIR / "voicebank" / "selected_voice.json"
    if f.exists():
        try:
            return str(json.loads(f.read_text("utf-8")).get("voice_id") or "")
        except Exception:
            return ""
    return ""


_FFMPEG_CACHE = None


def find_ffmpeg() -> str:
    """返回可用的完整 ffmpeg 可执行路径（进程内缓存，只解析一次）。

    优先级：环境变量 FFMPEG_PATH → winget 完整 build → PATH（剔除编辑器精简版）→ "ffmpeg"。
    在 Windows 下，PATH 常被编辑器自带的精简 ffmpeg（如 TRAE 的 app/bin/ffmpeg.exe）
    顶到最前，这种裁剪版缺 lavfi / wav muxer，一遇到提轨就报
    "Unable to choose an output format for *.wav"，故这里显式挑选完整版。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE

    cand = os.environ.get("FFMPEG_PATH") or ""
    if cand and Path(cand).exists():
        _FFMPEG_CACHE = cand
        return cand

    pkgs = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if pkgs.is_dir():
        for d in sorted(pkgs.iterdir(), reverse=True):
            if "ffmpeg" not in d.name.lower():
                continue
            # 完整 build 常 pack 在子目录（如 ffmpeg-9.0-full_build/bin/ffmpeg.exe），用 glob 覆盖
            for exe in d.glob("*/bin/ffmpeg.exe"):
                if exe.exists():
                    _FFMPEG_CACHE = str(exe)
                    return str(exe)

    for name in ("ffmpeg.exe", "ffmpeg"):
        w = shutil.which(name)
        if w and "TRAE SOLO CN" not in w:
            _FFMPEG_CACHE = w
            return w

    _FFMPEG_CACHE = shutil.which("ffmpeg") or "ffmpeg"
    return _FFMPEG_CACHE

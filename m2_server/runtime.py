"""跨模块共享的运行状态与小工具。

server.py 拆分后，原先定义在 server.py 里的路径常量 / 全局状态集中到这里；
各 router 模块 `from runtime import ...` 引用。
注意：PIPELINE_STATE / MINE_STATE / CAPTURE_STATE 是**原地共享的 dict**，
所有模块必须通过 `.update()` 或项赋值修改，禁止整体重新赋值（会断开共享）。
"""

import re
import threading
from pathlib import Path

import config as cfg

# ---- 路径常量（与拆分前 server.py 完全一致）----
ROOT = Path(__file__).resolve().parent.parent
VOICEBANK = cfg.MEDIA_DIR / "voicebank"
CLIPS_DIR = cfg.MEDIA_DIR / "clips"
RAW_DIR = cfg.MEDIA_DIR / "raw_videos"
OUT = cfg.OUTPUTS_DIR
OUT.mkdir(exist_ok=True)
VOICEBANK.mkdir(parents=True, exist_ok=True)

# 统一挂载 /api 前缀，开发(走 vite proxy)与生产(直连)共用同一套路径
API_PREFIX = "/api"

# 素材后缀白名单（上传/打标/流水线共用）
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}


def clip_prefix(video_stem: str) -> str:
    """切片文件名前缀：完整素材名（去文件系统非法字符）。

    旧版用 stem[:12]，两个 video_260828_* 素材会碰撞导致删一个误删另一个的切片。
    """
    return re.sub(r'[\\/:*?"<>|]', "", video_stem)[:80]


# ---- 流水线运行状态（供前端轮询分步进度/取消；capture 自动流程也复用）----
PIPELINE_STATE = {
    "running": False,
    "status": "idle",  # idle | running | done | cancelled | error
    "step": "",
    "message": "",
    "percent": 0,
    "clips": 0,
    "error": "",
    "file": "",  # 当前正在处理的素材名（失败可诊断用）
    "qc": {},  # 流水线跑完后的切片质检汇总（clip_qc.score_prefixes 产出）
}
pipeline_cancel = threading.Event()
pipeline_lock = threading.Lock()


def update_pipeline(**kw):
    """线程安全地更新流水线状态（原 server._update_pipeline）。"""
    with pipeline_lock:
        PIPELINE_STATE.update(kw)


# ---- 音色挖掘状态（mine_api 专用；capture 的自动流程也会触发挖掘）----
MINE_STATE: dict = {
    "running": False,
    "stage": "",  # idle | running | done | error
    "message": "",
    "kept": 0,
    "clusters": [],  # [{cluster,size,members,rep:{name,path,text}}]
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

# ---- 桌宠一键内录状态 ----
CAPTURE_STATE: dict = {"recording": False, "message": "", "file": ""}

# ---- GPU 独占任务登记（跨模块互斥的单一事实来源）----
# 为什么需要（2026-09-18）：RVC 推理子进程要独占显存，8GB 卡上两个并发必然 OOM。
# 此前互斥靠各模块互相 import 检查（_live_proc_alive / _cascade_alive /
# OFFLINEVC_STATE["running"]），新增的批量试音不在任何人的检查清单里 ——
# 用户在试音间开了批量任务、又去离线变声页点一下，两个推理进程就会撞在一起。
# 这里给「长任务」一个统一登记位：占用方 hold_gpu，其余路径统一查 gpu_holder_reason()。
_EXCLUSIVE_LOCK = threading.Lock()
EXCLUSIVE_TASKS: dict = {}  # 任务名 → 可读占用说明


def hold_gpu(name: str, reason: str) -> bool:
    """非阻塞占用 GPU 独占位；已被占用返回 False（调用方据此回 409）。"""
    with _EXCLUSIVE_LOCK:
        if EXCLUSIVE_TASKS:
            return False
        EXCLUSIVE_TASKS[name] = reason
        return True


def release_gpu(name: str) -> None:
    """释放独占位（幂等；未占用也当成功）。"""
    with _EXCLUSIVE_LOCK:
        EXCLUSIVE_TASKS.pop(name, None)


def gpu_holder_reason() -> str:
    """当前独占位占用说明；空闲返回空串。"""
    with _EXCLUSIVE_LOCK:
        for name, reason in EXCLUSIVE_TASKS.items():
            return reason or f"{name} 正在运行"
    return ""

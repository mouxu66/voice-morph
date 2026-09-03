# -*- coding: utf-8 -*-
"""集中配置：所有外部路径/默认值从环境变量读取，未设置时回退到项目内默认。

开源部署时只需设环境变量（VM_ 前缀），源码里不再有机器专属硬编码。
"""
import os
from pathlib import Path

# 项目根（m2_server 的上一级）
ROOT = Path(__file__).resolve().parent.parent

# 可选：自动加载项目根目录的 .env（需 python-dotenv）。
# 真实环境变量优先于 .env；未安装 dotenv 或文件不存在则静默跳过。
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass


def _path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


def _str(name: str, default: str) -> str:
    return os.environ.get(name) or default


# ---------------- 目录 ----------------
TTS_MODELS_DIR = _path("VM_TTS_MODELS_DIR", ROOT / "tts_models")
MEDIA_DIR = _path("VM_MEDIA_DIR", ROOT / "media")
OUTPUTS_DIR = _path("VM_OUTPUTS_DIR", ROOT / "outputs")

# Qwen3-TTS 模型（Base 变体，可微调）与 tokenizer
QWEN_MODEL_DIR = _path("VM_QWEN_MODEL_DIR", TTS_MODELS_DIR / "qwen3-tts-1.7b-base")
QWEN_TOKENIZER_DIR = _path("VM_QWEN_TOKENIZER_DIR", TTS_MODELS_DIR / "qwen3-tts-tokenizer-12hz")

# TTS worker 专用 Python 环境
TTS_VENV_PY = _path("VM_TTS_VENV_PY", ROOT / "tts_trial" / "venv312" / "Scripts" / "python.exe")

# RVC 整合包根目录
RVC_ROOT = _path("VM_RVC_ROOT", Path("D:/RVC"))

# 默认 RVC 实验名（新音色用各自 voice_id 作为 exp 名）
# meituan_rat 是 2026-08-29 固定源 A/B（源=用户本人录音，pitch=0）中选定的最佳音色。
RVC_DEFAULT_EXP = _str("VM_RVC_EXP", "meituan_rat")

# 语料导出目录（RVC 整合包的 dataset_raw）
RVC_EXPORT_DIR = _path("VM_RVC_EXPORT_DIR", RVC_ROOT / "dataset_raw" / "rvc_dataset")


def rvc_exp_dirs(exp: str | None = None) -> tuple[Path, Path]:
    """返回 (log_dir, dataset_dir)：RVC 权重与训练集在该实验名下的位置。"""
    name = exp or RVC_DEFAULT_EXP
    return RVC_ROOT / "logs" / name, RVC_ROOT / "dataset" / name


# ---------------- 服务进程 ----------------
# 监听地址/端口（默认 0.0.0.0:8000，LAN 可用）；生产可改 127.0.0.1 或具体网卡
SERVER_HOST = _str("VM_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("VM_SERVER_PORT", "8000"))

# CORS 来源（逗号分隔）；默认 "*" 维持 LAN 可用，生产收紧为具体域名
CORS_ORIGINS = [o.strip() for o in _str("VM_CORS_ORIGINS", "*").split(",") if o.strip()]

# 可选简单 Bearer Token；留空=关闭（保持 LAN-only 默认，不拦截）
API_TOKEN = _str("VM_API_TOKEN", "")

# ---------------- RVC 训练语料 ----------------
# 语料模板文件（每行一句），可经 VM_RVC_TEXTS_FILE 覆盖为任意音色专用语料
_RVC_TEXTS_FILE = _path("VM_RVC_TEXTS_FILE", Path(__file__).resolve().parent / "data" / "rvc_texts.txt")

# 内置默认 20 句（与历史行为一致），仅当语料文件缺失时回退
_DEFAULT_RVC_TEXTS = [
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


def load_rvc_texts() -> list[str]:
    """读取 RVC 训练语料；文件存在则按行返回，缺失时回退内置默认 20 句。"""
    if _RVC_TEXTS_FILE.exists():
        return [ln.strip() for ln in _RVC_TEXTS_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return list(_DEFAULT_RVC_TEXTS)

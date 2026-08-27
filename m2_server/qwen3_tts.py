"""Qwen3-TTS 客户端（m2_server 主进程内调用）。

职责：
    - 首次请求时，用 venv312 懒启动 qwen3_tts_service.py 常驻 worker（端口 8001）
    - analyze(): 把切片清单转发给 worker 做音色挖掘（转写+声纹+聚类）
    - tts():     把参考音频+文字稿转发给 worker 动态克隆合成，返回 wav 字节
    - 接口对齐多音色架构，供 server.py 的 /tts、/mine 端点调用

之所以走子进程：主进程在 .venv(torch2.9)，Qwen3-TTS 需 venv312(torch2.8cu129)，
不能同进程 import；用常驻 worker 避免每次请求都重新加载 4GB 模型。
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VENV312 = os.path.join(PROJECT_ROOT, "tts_trial", "venv312", "Scripts", "python.exe")
WORKER = os.path.join(os.path.dirname(__file__), "qwen3_tts_service.py")
PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"

_lock = threading.Lock()
_proc = None
_ready = False


def _ensure_worker():
    global _proc, _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        # 端口已被占用（可能是上次残留/外部已起），直接复用
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2)
            _ready = True
            return
        except Exception:
            pass
        if not os.path.exists(VENV312):
            raise RuntimeError(f"找不到 venv312 解释器: {VENV312}")
        _proc = subprocess.Popen(
            [VENV312, WORKER],
            cwd=os.path.dirname(WORKER),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW，避免弹黑窗
        )
        # 模型加载较慢（约 10~30s），轮询 /health 直到就绪
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2)
                _ready = True
                break
            except Exception:
                if _proc.poll() is not None:
                    raise RuntimeError("Qwen3-TTS worker 进程意外退出")
                time.sleep(2)
        if not _ready:
            raise RuntimeError("Qwen3-TTS worker 启动超时（模型加载失败？）")


def _post(path: str, payload: dict, timeout: int) -> bytes:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError):
        # worker 可能已被杀（如手动清理/崩溃）：重置状态，重新拉起再试一次
        global _ready, _proc
        with _lock:
            _ready = False
            _proc = None
        _ensure_worker()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()


def analyze(clips: list[dict]) -> dict:
    """音色挖掘：clips=[{name,path}] -> 簇列表（含代表切片与转写文字）。"""
    _ensure_worker()
    return json.loads(_post("/analyze", {"clips": clips}, timeout=1800))


def tts(text: str, ref_audio: str, ref_text: str = "", language: str = "Chinese") -> bytes:
    """动态克隆：ref_audio=参考wav绝对路径，ref_text 空则退化为纯声纹模式。"""
    _ensure_worker()
    return _post("/tts", {"text": text, "language": language,
                          "ref_audio": ref_audio, "ref_text": ref_text}, timeout=900)

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

# PROJECT_ROOT 可被安装版注入（VM_PROJECT_ROOT=D:\变声）：venv312 与 worker 日志都在项目目录
PROJECT_ROOT = os.environ.get("VM_PROJECT_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VENV312 = os.path.join(PROJECT_ROOT, "tts_trial", "venv312", "Scripts", "python.exe")
# worker 必须用项目目录里的脚本：cwd 决定 tts_models 模型路径解析，
# 安装版若从 resources 下启动，会在安装目录里找模型导致 HFValidationError 崩溃
WORKER = os.path.join(PROJECT_ROOT, "m2_server", "qwen3_tts_service.py")
if not os.path.exists(WORKER):
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
        _logf = open(os.path.join(os.path.dirname(WORKER), "worker_run.log"), "ab")
        _proc = subprocess.Popen(
            [VENV312, WORKER],
            cwd=os.path.dirname(WORKER),
            stdout=_logf,
            stderr=subprocess.STDOUT,
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


def analyze(clips: list[dict], sim_threshold: float | None = None,
            min_cluster_size: int | None = None) -> dict:
    """音色挖掘：clips=[{name,path}] -> 簇列表（含代表切片与转写文字）。

    sim_threshold：聚类相似度阈值（默认 0.5，调高可挖出更多不同音色）
    min_cluster_size：小于该成员数的簇直接过滤
    """
    _ensure_worker()
    payload: dict = {"clips": clips}
    if sim_threshold is not None:
        payload["sim_threshold"] = sim_threshold
    if min_cluster_size is not None:
        payload["min_cluster_size"] = int(min_cluster_size)
    return json.loads(_post("/analyze", payload, timeout=1800))


# 供 finetune 等模块复用：确保 worker 已拉起再直连；post 带死亡重试
ensure_worker = _ensure_worker
post = _post


def tts(text: str, ref_audio: str, ref_text: str = "", language: str = "Chinese",
        voice_id: str = "") -> bytes:
    """按音色合成：普通音色走克隆（ref_text 空则 x-vector 声纹模式）；
    微调音色（voicebank/<id>/meta.json kind=finetuned）自动分流到 /tts_speaker。"""
    _ensure_worker()
    if voice_id:
        meta_p = os.path.join(PROJECT_ROOT, "media", "voicebank", voice_id, "meta.json")
        try:
            meta = json.loads(open(meta_p, encoding="utf-8").read())
        except Exception:
            meta = {}
        if meta.get("kind") == "finetuned" and meta.get("model_dir"):
            return _post("/tts_speaker", {"model_dir": meta["model_dir"],
                                          "speaker": meta.get("speaker") or voice_id,
                                          "text": text, "language": language}, timeout=900)
    return _post("/tts", {"text": text, "language": language,
                          "ref_audio": ref_audio, "ref_text": ref_text}, timeout=900)

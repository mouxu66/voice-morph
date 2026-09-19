"""Qwen3-TTS 客户端（m2_server 主进程内调用）。

职责：
    - 首次请求时，用 venv312 懒启动 qwen3_tts_service.py 常驻 worker（端口 8001）
    - analyze(): 把切片清单转发给 worker 做音色挖掘（转写+声纹+聚类）
    - tts():     把参考音频+文字稿转发给 worker 动态克隆合成，返回 wav 字节
    - 接口对齐多音色架构，供 server.py 的 /tts、/mine 端点调用

之所以走子进程：主进程在 .venv(torch2.9)，Qwen3-TTS 需 venv312(torch2.8cu129)，
不能同进程 import；用常驻 worker 避免每次请求都重新加载 4GB 模型。
"""

import atexit
import contextlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

# PROJECT_ROOT 可被安装版注入（VM_PROJECT_ROOT=D:\变声）：venv312 与 worker 日志都在项目目录
PROJECT_ROOT = os.environ.get("VM_PROJECT_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
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

# ---- 合成中保护（2026-09-19）：切游戏档要卸载 worker 腾显存，但若正有任务
# （tts/transcribe/analyze）在跑，直接杀会打断合成。改为 busy 计数 + 延迟卸载：
# shutdown_worker 发现 busy>0 时不立即杀，置 _shutdown_pending 起守护线程等
# busy 归零后再回收；合成完成的瞬间自动释放显存，用户任务不受影响。 ----
_busy = 0
_busy_lock = threading.Lock()
_shutdown_pending = False
_shutdown_thread: "threading.Thread | None" = None

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW，避免弹黑窗


def _health_ok(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _port_bound(host: str = "127.0.0.1", port: int = PORT) -> bool:
    """端口是否已被占用（不关心对方是否健康）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _pids_on_port(port: int = PORT) -> list:
    """列出正在 LISTEN 该端口的 PID。"""
    try:
        # ⚠️ 必须显式指定 encoding。netstat 在中文 Windows 上输出 **GBK** 表头
        # （「活动连接」= BB EE），而 `text=True` 默认按 locale 编码解码 ——
        # 在 UTF-8 模式（PYTHONUTF8=1；PEP 686 计划 Python 3.15 起默认开启）下
        # 解码失败 → stdout 变成 **None** → 下面 `out.splitlines()` 抛
        # AttributeError → 本函数返回空列表 → `shutdown_worker()` 回收不了
        # 遗留 worker（约 4.8GB 显存泄漏）。2026-09-18 实测：
        #   cp936 环境 stdout 正常 136 行；utf-8 模式 stdout is None。
        # 按 utf-8 解 GBK 会把中文表头变成 U+FFFD，但本函数只解析 PID 行（纯 ASCII），
        # 不受影响；`errors="replace"` 保证任何编码都不会再抛。
        out = (
            subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
            or ""
        )
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        try:
            pid = int(line.split()[-1])
        except (ValueError, IndexError):
            continue
        if pid:
            pids.append(pid)
    return sorted(set(pids))


def _cmdline_of(pid: int) -> str:
    """取进程命令行。

    必须优先 PowerShell CIM：wmic 已在 Win11 24H2+ 被移除，用它会导致
    _is_our_worker 恒为 False，僵尸 worker 清理逻辑彻底失效（实测踩到）。
    """
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="ignore",
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    try:  # 老系统回退
        r = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/format:list"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="ignore",
            creationflags=_NO_WINDOW,
        )
        return r.stdout or ""
    except Exception:
        return ""


def _is_our_worker(pid: int) -> bool:
    """只回收我们自己拉起的 worker，绝不误伤占用 8001 的其他程序。"""
    if pid == os.getpid():
        return False
    return "qwen3_tts_service.py" in _cmdline_of(pid)


def _kill_pid(pid: int) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            creationflags=_NO_WINDOW,
        )


def _terminate_proc() -> None:
    """彻底回收本进程拉起的 worker。

    关键：绝不能只把 _proc 置 None（旧实现就是这么做的），那会让 worker 变成
    孤儿继续霸占 8001，导致之后每次重启都在加载完 4GB 模型后 bind 失败(10048)。
    """
    global _proc
    if _proc is None:
        return
    with contextlib.suppress(Exception):
        _proc.terminate()
    try:
        _proc.wait(timeout=10)
    except Exception:
        with contextlib.suppress(Exception):
            _proc.kill()
    _proc = None


def worker_alive() -> bool:
    """worker 是否真的在跑（通 /health 才算）。

    给状态面板用：游戏档卸载后这里变 False，前端据此展示「语音合成引擎已卸载，
    显存已释放」；不用 _ready 是因为服务重启后复用的 worker 没有 _proc/_ready。
    延迟卸载期间 worker 进程尚存（合成中），/health 仍通 → 自然返回 True，
    前端不会误报「已卸载」；等 _do_recycle 真杀掉进程后自然变 False。
    """
    return _health_ok()


def _busy_inc() -> None:
    global _busy
    with _busy_lock:
        _busy += 1


def _busy_dec() -> None:
    global _busy
    with _busy_lock:
        _busy = max(0, _busy - 1)


def _do_recycle() -> None:
    """真正的卸载动作：杀本进程拉起的 _proc + 清理端口上残留的本项目 worker。"""
    global _ready
    _ready = False
    _terminate_proc()
    for pid in _pids_on_port():
        if _is_our_worker(pid):
            print(f"[qwen3_tts] shutdown_worker 回收遗留 worker PID={pid}", flush=True)
            _kill_pid(pid)


def _recycle_when_idle() -> None:
    """守护线程：等 busy 归零后执行卸载。合成任务完成瞬间自动释放显存。"""
    global _shutdown_pending, _shutdown_thread
    while True:
        with _busy_lock:
            idle = _busy == 0
        if idle:
            break
        time.sleep(0.5)
    _do_recycle()
    _shutdown_pending = False
    _shutdown_thread = None


def shutdown_pending() -> bool:
    """是否有延迟卸载在等合成结束（供状态面板/切档接口识别）。"""
    return _shutdown_pending


def shutdown_worker() -> None:
    """供外部（如切到游戏档、server 关闭、清理脚本）显式回收 worker。

    除了杀掉本进程拉起的 _proc，还要清理端口上残留的本项目 worker：
    服务重启后 _proc 为 None、worker 被 _ensure_worker 复用，只杀 _proc 会
    漏掉这块 ~4.8GB 显存（2026-09-15 实测）。按命令行判断只杀本项目 worker，
    绝不误伤占用 8001 的其他程序。

    合成中保护（2026-09-19）：若正有 tts/transcribe/analyze 在跑（_busy>0），
    不能直接杀——会打断进行中的任务。改为延迟：置 _shutdown_pending 起守护
    线程，等 busy 归零后自动回收（合成完成瞬间释放显存）。server 退出路径
    （atexit）走 _terminate_proc 快速杀，不受此保护影响。
    """
    global _shutdown_pending, _shutdown_thread
    with _busy_lock:
        if _busy > 0:
            if not _shutdown_pending:
                _shutdown_pending = True
                _shutdown_thread = threading.Thread(target=_recycle_when_idle, daemon=True)
                _shutdown_thread.start()
            return
    _do_recycle()


atexit.register(_terminate_proc)


def _clear_stuck_worker() -> None:
    """端口被占但 /health 不通 = 卡死的僵尸 worker（多因 GPU 被挤占、模型加载挂起）。

    它会让新 worker 在加载完模型后 bind 才撞上 10048，白白浪费 10~30s 与 4GB 显存，
    必须先清掉再拉新的。
    """
    for pid in _pids_on_port():
        if _is_our_worker(pid):
            print(f"[qwen3_tts] 清理占用 {PORT} 的僵尸 worker PID={pid}", flush=True)
            _kill_pid(pid)
    for _ in range(30):  # 等端口真正释放，最多 30s
        if not _port_bound():
            return
        time.sleep(1)


def _ensure_worker():
    global _proc, _ready
    if _ready and _health_ok():
        return
    with _lock:
        if _ready and _health_ok():
            return
        _ready = False
        # 端口已被占用且健康（上次残留/外部已起/其他实例），直接复用
        if _health_ok():
            _ready = True
            return
        # 端口被占但不健康：僵尸 worker，先清场，否则新 worker 必然 10048
        if _port_bound():
            _clear_stuck_worker()
            if _health_ok():
                _ready = True
                return
            # 清不掉 = 端口被别的程序占着。此时拉起必然 10048，
            # 与其白等 10~30s 加载 4GB 模型后失败，不如立刻报清楚。
            if _port_bound():
                raise RuntimeError(
                    f"端口 {PORT} 被其他程序占用（PID={_pids_on_port()}），"
                    f"Qwen3-TTS worker 无法启动，请先关闭该进程"
                )
        if not os.path.exists(VENV312):
            raise RuntimeError(f"找不到 venv312 解释器：{VENV312}")
        with open(os.path.join(os.path.dirname(WORKER), "worker_run.log"), "ab") as _logf:
            _proc = subprocess.Popen(
                [VENV312, WORKER],
                cwd=os.path.dirname(WORKER),
                stdout=_logf,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
        # 模型加载较慢（约 10~30s，GPU 被挤占时更久），轮询 /health 直到就绪
        deadline = time.time() + 240
        while time.time() < deadline:
            if _health_ok():
                _ready = True
                return
            if _proc.poll() is not None:
                _terminate_proc()  # 已退出也要回收，避免残留句柄
                raise RuntimeError("Qwen3-TTS worker 进程意外退出（详见 m2_server/worker_run.log）")
            time.sleep(2)
        # 超时必须杀掉自己拉起的进程：否则它会继续占着 8001，让之后每次尝试都 10048
        _terminate_proc()
        raise RuntimeError("Qwen3-TTS worker 启动超时（模型加载失败？检查 GPU 是否被其他程序占用）")


def _is_timeout(exc: Exception) -> bool:
    """区分"worker 忙/卡"与"worker 已死"——两者的处置方式完全相反。"""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _post(path: str, payload: dict, timeout: int) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    for attempt in (1, 2):
        req = urllib.request.Request(
            BASE + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise  # worker 明确返回的业务错误（如 400），不该重启 worker
        except (urllib.error.URLError, OSError) as e:
            # 超时说明 worker 还活着只是在忙（如长文本走 WDDM 慢路径），
            # 此时重启只会再花 10~30s 重载 4GB 模型，雪上加霜——直接抛给上层
            if _is_timeout(e):
                raise RuntimeError(
                    f"Qwen3-TTS worker 响应超时（>{timeout}s）；worker 仍在运行，"
                    f"未重启。若为长文本，请拆短后重试"
                ) from e
            if attempt == 2:
                raise
            # 连不上 = worker 已被杀或崩溃，才重置并重拉
            global _ready
            with _lock:
                _ready = False
            _ensure_worker()
    raise RuntimeError("unreachable")


def analyze(
    clips: list[dict], sim_threshold: float | None = None, min_cluster_size: int | None = None
) -> dict:
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
    _busy_inc()
    try:
        return json.loads(_post("/analyze", payload, timeout=1800))
    finally:
        _busy_dec()


# 供 finetune 等模块复用：确保 worker 已拉起再直连；post 带死亡重试
ensure_worker = _ensure_worker
post = _post


def transcribe(path: str, vad_filter: bool = True, fast: bool = False, timeout: int = 600) -> dict:
    """语音转写：wav 路径 -> {text, quality}。语气中转链路的 ASR 环节。

    vad_filter：交给 whisper 内部过滤静音（语气中转对整段人声有效，保持默认）；
    fast：走快速转写通道（质量略降）。
    """
    _ensure_worker()
    _busy_inc()
    try:
        return json.loads(
            _post(
                "/transcribe",
                {"path": path, "vad_filter": vad_filter, "fast": fast},
                timeout=timeout,
            )
        )
    finally:
        _busy_dec()


def tts(
    text: str,
    ref_audio: str,
    ref_text: str = "",
    language: str = "Chinese",
    voice_id: str = "",
    style_ref: str = "",
    style_ref_text: str = "",
    seg_chars: int = 0,
) -> bytes:
    """按音色合成：普通音色走克隆（ref_text 空则 x-vector 声纹模式）；
    微调音色（voicebank/<id>/meta.json kind=finetuned）自动分流到 /tts_speaker。

    style_ref/style_ref_text/seg_chars：风格参考 ICL + 长文分段（见 worker /tts）。
    """
    _ensure_worker()
    _busy_inc()
    try:
        return _tts_do(
            text, ref_audio, ref_text, language, voice_id, style_ref, style_ref_text, seg_chars
        )
    finally:
        _busy_dec()


def _tts_do(
    text: str,
    ref_audio: str,
    ref_text: str,
    language: str,
    voice_id: str = "",
    style_ref: str = "",
    style_ref_text: str = "",
    seg_chars: int = 0,
) -> bytes:
    if voice_id:
        meta_p = os.path.join(PROJECT_ROOT, "media", "voicebank", voice_id, "meta.json")
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.loads(f.read())
        except Exception:
            meta = {}
        if meta.get("kind") == "finetuned" and meta.get("model_dir"):
            return _post(
                "/tts_speaker",
                {
                    "model_dir": meta["model_dir"],
                    "speaker": meta.get("speaker") or voice_id,
                    "text": text,
                    "language": language,
                },
                timeout=900,
            )
    payload: dict = {
        "text": text,
        "language": language,
        "ref_audio": ref_audio,
        "ref_text": ref_text,
    }
    if style_ref:
        payload["style_ref"] = style_ref
        if style_ref_text:
            payload["style_ref_text"] = style_ref_text
        if seg_chars > 0:
            payload["seg_chars"] = seg_chars
    return _post("/tts", payload, timeout=900)

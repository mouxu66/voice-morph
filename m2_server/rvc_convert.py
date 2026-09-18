"""RVC 文件级换声：任意 wav → 目标音色 wav（子进程跑 offline_vc_infer.py）。

抽出来是因为两处都要用：
  - `wechat_voice.send_text`（微信一键发送：TTS → RVC → 发送）
  - `tools/tts_and_send.py`（命令行真机验证）

推理在 `D:/RVC/.venv` 子进程里跑，与主服务 torch 隔离（和 offline_vc.py 同一套路）。

⚠️ 袋鼠音色的唯一来源就是这一步：TTS 零样本克隆复现不了袋鼠音色
   （2026-08-31 用户 A/B 亲耳判定），别指望 `ref_audio` 能顶替 RVC。
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from config import OUTPUTS_DIR, RVC_ROOT

RVC_VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
INFER_PY = Path(__file__).resolve().parent / "offline_vc_infer.py"

# 常驻 worker 开关：默认开。设 VM_RVC_WORKER=0 退回一次性子进程（~20s/条，仅排查用）
# 实测（RTX 5060 8GB，5s 音频）：一次性 CLI 每条 20~25s（几乎全是模型加载）；
# 常驻后第一次仍要加载，第二次起只剩推理。worker 中途崩会自动重启并重试一次。
USE_WORKER = os.environ.get("VM_RVC_WORKER", "1") != "0"
_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW：worker 不得弹黑框遮挡微信
_WORKER_LOG = OUTPUTS_DIR / "rvc_worker.log"

_worker: subprocess.Popen | None = None
_worker_lock = threading.Lock()
_task_id = itertools.count(1)


class RvcError(RuntimeError):
    pass


def resolve_model(voice: str) -> tuple[Path, Path | None]:
    """找音色的 .pth 与 added_*.index（index 可以没有，有则音色更像）。"""
    log_dir = RVC_ROOT / "logs" / voice
    pth = log_dir / f"{voice}.pth"
    if not pth.exists():
        pth = RVC_ROOT / "assets" / "weights" / f"{voice}.pth"
    if not pth.exists():
        raise RvcError(f"找不到音色模型 {voice}.pth（logs/ 与 assets/weights/ 都没有）")
    index = next(iter(sorted(log_dir.glob("added_*.index"))), None)
    return pth, index


def rvc_voice_candidates(voice_id: str) -> list[str]:
    """voicebank 音色 id → 可能对应的 RVC 实验名（按现役主力优先排序）。

    单独暴露出来是给「反向解析」用：试衣间拿到的是 RVC 实验名，要回头找它对应
    哪个 voicebank 音色（文字合成需参考音），只能拿候选集合与实验名求交，
    不能只跑 resolve_rvc_voice 比相等 —— 40k 变体与主模型会同时挂在同一个
    voicebank 名下，只比相等会把 `kangaroo_v2_40k` 判成"没有参考音"。
    """
    if not voice_id:
        return []
    return [f"{voice_id}_v2", f"{voice_id}_v2_40k", f"{voice_id}_40k", voice_id]


def resolve_rvc_voice(voice_id: str) -> str | None:
    """TTS 音色 id（voicebank/<id>，如 `kangaroo`）→ 可推理的 RVC 实验名（logs/ 下）。

    两套命名不一样：voicebank 用 `kangaroo`，RVC 实验是 `kangaroo_v2` / `kangaroo_v2_40k`。
    按现役主力优先的顺序试，都找不到返回 None（调用方据此决定"不换声"还是报错）。
    """
    # 覆盖两种实际命名：kangaroo → kangaroo_v2 / kangaroo_v2_40k
    for cand in rvc_voice_candidates(voice_id):
        try:
            resolve_model(cand)
            return cand
        except RvcError:
            continue
    return None


def _spawn_worker() -> subprocess.Popen:
    """起常驻 worker 并等它READY（第一次 snap 在 __init__ 里不做，避免 import 卡住）。

    stderr 落文件而不是 PIPE：RVC/hubert 加载期日志量大，PIPE 无人读会写满阻塞。
    """
    _WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
    err = open(_WORKER_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(RVC_VENV_PY), str(INFER_PY), "--serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
        cwd=str(RVC_ROOT), text=True, encoding="utf-8", bufsize=1,
        creationflags=_NO_WINDOW,
    )
    line = proc.stdout.readline()
    if not line:
        # worker 没打印 READY 就退了（常见的 hung/import 失败）
        proc.wait(timeout=10)
        raise RvcError(f"RVC worker 启动失败，退出码 {proc.returncode}，"
                       f"详见 {_WORKER_LOG}")
    head = json.loads(line)
    if not head.get("ready"):
        raise RvcError(f"RVC worker 握手异常: {head}")
    return proc


def _get_worker() -> subprocess.Popen:
    global _worker
    if _worker is not None and _worker.poll() is None:
        return _worker
    _worker = _spawn_worker()
    return _worker


def stop_worker() -> None:
    """关掉常驻 worker（释放 GPU 显存）。atexit 会自动做，也可手动调。"""
    global _worker
    proc, _worker = _worker, None
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()   # worker 的 stdin 循环到 EOF 自己退出
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


atexit.register(stop_worker)


def worker_call(task: dict, timeout: float = 300.0) -> dict:
    """向常驻 worker 发一个任务，返回结果 dict（不 ok 时抛 RvcError）。

    worker 中途死了会自动重启并重试一次——显卡掉线/进程被杀后不该让用户看到报错。
    """
    with _worker_lock:   # 串行：worker 一次只处理一个任务，避免 GPU 抢显存 OOM
        for attempt in (1, 2):
            proc = _get_worker()
            task = dict(task, id=next(_task_id))
            try:
                proc.stdin.write(json.dumps(task) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise RvcError("RVC worker 无响应（可能已崩溃）")
                res = json.loads(line)
            except (BrokenPipeError, ValueError, OSError) as e:
                if attempt == 1:
                    stop_worker()
                    _ = _get_worker()   # 重启后重试一轮
                    continue
                raise RvcError(f"RVC worker 通信失败: {e}")
            if not res.get("ok"):
                raise RvcError(f"RVC 换声失败: {res.get('error', '未知错误')}")
            return res
    raise RvcError("RVC worker 不可用")


def worker_status() -> dict:
    """常驻 worker 的健康状态（给 /api/offlinevc/status 之类的诊断用）。"""
    proc = _worker
    alive = proc is not None and proc.poll() is None
    return {"enabled": USE_WORKER, "alive": alive,
            "pid": (proc.pid if alive else None),
            "log": str(_WORKER_LOG)}


def rvc_warmup(voice: str) -> dict:
    """预加载某个音色（只加载不转换）——消掉首条的"模型加载"等待。

    返回 worker 的 load 统计：{"load_s": 18.2}。失败抛 RvcError（不影响后续调用）。
    """
    if not USE_WORKER:
        raise RvcError("VM_RVC_WORKER=0 时无 worker，没法预热")
    pth, index = resolve_model(voice)
    return worker_call({"cmd": "warmup", "pth": str(pth), "index": str(index or "")},
                       timeout=300)


def rvc_convert(wav: Path, voice: str, pitch: int = 0, index_rate: float = 0.5) -> Path:
    """把 wav 换成 voice 的音色，产物写进 outputs/<原名>_<voice>.wav，返回其路径。

    走常驻 worker（模型缓存，第二次起 ~1s）；`VM_RVC_WORKER=0` 才退回一次性子进程。
    """
    if not RVC_VENV_PY.exists():
        raise RvcError(f"找不到 RVC 运行环境: {RVC_VENV_PY}")
    src = Path(wav)
    if not src.is_absolute():
        src = OUTPUTS_DIR / src.name
    if not src.exists():
        raise RvcError(f"找不到待转换音频: {src}")

    pth, index = resolve_model(voice)
    out = OUTPUTS_DIR / f"{src.stem}_{voice}.wav"
    if USE_WORKER:
        try:
            worker_call({"cmd": "convert", "pth": str(pth), "index": str(index or ""),
                         "input": str(src), "output": str(out),
                         "pitch": pitch, "index_rate": index_rate}, timeout=900)
            return out
        except RvcError as e:
            # worker 起不来（比如显存被占）就退回一次性链路，别让整条发送失败
            print(f"[RVC] worker 失败，退回一次性子进程: {e}")
    cmd = [str(RVC_VENV_PY), str(INFER_PY),
           "--pth", str(pth),
           "--index", str(index) if index else "",
           "--input", str(src), "--output", str(out),
           "--pitch", str(pitch), "--index-rate", str(index_rate)]
    # cwd 必须是 RVC 根：offline_vc_infer 在 load_vc() 之前就 `from infer.audio import ...`
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       encoding="utf-8", errors="replace", cwd=str(RVC_ROOT))
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RvcError("RVC 换声失败: " + " | ".join(tail)[-400:])
    return out


def rvc_convert_logged(wav: Path, voice: str, pitch: int = 0, index_rate: float = 0.5,
                       log=print) -> Path:
    """带进度日志的版本（命令行/桌宠用）。"""
    pth, index = resolve_model(voice)
    log(f"[RVC] 换声 → {voice}（index={'有' if index else '无'}，rate={index_rate}）")
    t0 = time.time()
    out = rvc_convert(wav, voice, pitch, index_rate)
    try:
        import soundfile as sf
        d, sr = sf.read(str(out))
        dur = len(d) / sr
    except Exception:
        dur = -1
    log(f"[RVC] 完成 {out.name}（{dur:.2f}s，耗时 {time.time() - t0:.1f}s）")
    return out

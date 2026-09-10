# -*- coding: utf-8 -*-
"""启动预热：把「点一下要等一分多钟」压到十几秒。

## 为什么需要这个模块（2026-09-10 实测，RTX 5060 8GB）

5s 文案的「桌偶一键发送」链路，冷启动耗时：

| 阶段 | 冷（首次） | 预热后 |
|---|---|---|
| TTS 合成 | **41.8s**（起 worker + 加载 23.5s + warmup） | 2.9s |
| RVC 换声 | **24.7s**（每条都重新加载模型） | 0.3s |
| 微信录制发送 | ~10s（≈音频时长 + 4s 固定开销） | 同左 |
| **合计** | **~77s** | **~13s** |

TTS worker 在 `qwen3_tts_service.startup` 里就 `_load()` 并 `warmup()`，
而 `ensure_worker()` 会轮询 /health 直到就绪 —— 所以**预热 TTS = 调 ensure_worker()**，
不必发一条真实合成请求。

RVC 侧靠 `rvc_convert.rvc_warmup()` 把模型预先 load 进常驻 worker
（同一音色只加载一次，之后每条只剩推理）。

全部在**后台线程**跑：不阻塞 uvicorn 启动，期间来请求也不会被拖住。
开关 `VM_WARMUP=0` 关闭（排查显存问题时用）。
"""
from __future__ import annotations

import os
import threading
import time

ENABLED = os.environ.get("VM_WARMUP", "1") != "0"

_state: dict = {"running": False, "done": False, "steps": [], "started_at": 0.0,
                "finished_at": 0.0, "error": ""}
_lock = threading.Lock()


def status() -> dict:
    with _lock:
        return dict(_state)


def _note(step: str, seconds: float, ok: bool = True, detail: str = "") -> None:
    with _lock:
        _state["steps"].append({"step": step, "seconds": round(seconds, 2),
                                "ok": ok, "detail": detail})


def _warm_tts() -> None:
    try:
        from qwen3_tts import ensure_worker
        ensure_worker()   # 内部轮询 /health，直到模型加载 + warmup 完成
    except Exception as e:
        _note("TTS worker", 0.0, False, str(e)[:200])
        raise
    finally:
        pass


def _warm_rvc() -> None:
    from rvc_convert import rvc_warmup, resolve_rvc_voice
    voice = resolve_rvc_voice(os.environ.get("VM_WARMUP_VOICE", "kangaroo"))
    if not voice:
        _note("RVC", 0.0, False, "没找到可预热的音色（跳过）")
        return
    rvc_warmup(voice)


def run() -> dict:
    """按顺序预热 TTS 与 RVC，返回统计（谁失败都不抛出，记进 steps）。"""
    t0 = time.time()
    with _lock:
        if _state["running"] or _state["done"]:
            return dict(_state)
        _state["running"] = True
        _state["started_at"] = t0

    steps = (("TTS worker", _warm_tts), ("RVC", _warm_rvc))
    for name, fn in steps:
        s = time.time()
        try:
            fn()
            _note(name, time.time() - s)
        except Exception as e:
            _note(name, time.time() - s, False, str(e)[:200])

    with _lock:
        _state["running"] = False
        _state["done"] = True
        _state["finished_at"] = time.time()
        _state["total_s"] = round(time.time() - t0, 2)
        return dict(_state)


def start_background(delay_s: float = 2.0) -> None:
    """uvicorn 起来后再异步预热（delay 让 HTTP 端口先对外可用）。"""
    if not ENABLED:
        return

    def _runner() -> None:
        time.sleep(delay_s)
        try:
            run()
        except Exception:
            pass

    threading.Thread(target=_runner, name="warmup", daemon=True).start()

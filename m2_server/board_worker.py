"""声板常驻播放器（`sound.fx-board` 的后端子进程，2026-09-23）。

为什么是**独立进程**、而不是复用 `play_worker.py`
------------------------------------------------
`play_worker.py` 是微信发送链路的播放器，它的行协议是**单命令串行**消费的：
一条命令从 `playing` 到 `done` 期间，父进程的 `_PlayWorkerHandle` 会把 stdout 上
**所有**行都当成自己那条命令的响应。声板如果借它播"爆炸"，就会出现：

    TTS 人声正在播（占着 handle）→ 用户点"爆炸" → 爆炸的 `playing/done`
    被 TTS 的 handle 吃掉 → TTS 那条命令提前认为播完 → 微信提前松开发送。

这是**静默错发**级别的故障（消息被截断，而界面显示成功），而它只在"发送期间点声板"
这条组合路径上出现 —— 正是用户主场景。所以声板**自带一个 worker**，两条链路各自
持有自己的管道，互不消费对方的行。

另一个好处：不用改 `wechat_voice.py` / `play_worker.py` 一行 —— 设计稿 §七 的边界
（"不碰微信自动化"）在实现层也是省心的选择。

与原播放器的三点差异
--------------------
1. **非阻塞**：`sd.play(..., blocking=False)` 立即返回，`playing` 行即回 ——
   点格子要的是"马上听到"（One-shot 语义），不是"等它播完"。
2. **抢占**：新命令先 `sd.stop()` 再起播（连点两下爆炸 = 重新炸一次，不排队）。
3. **预载**：启动时把素材目录里的 wav 全读成 float32 常驻内存（6 条 ≈ 694KB），
   点格子零 IO；`{"wav": ...}` 给**用户导入**的素材用（按需读，单条小文件毫秒级）。

行协议（stdout，每行一个 JSON，flush）
-------------------------------------
    {"type":"ready","samples":6}                启动就绪（import + 预载已完成）
    {"type":"playing","id":"boom","duration_s":1.4}
    {"type":"stopped"}
    {"type":"error","msg":"device_not_found:..."}
stdin：每行一个 JSON 命令
    {"wav":"/abs/path.wav","id":"boom","gain":0.9}   播放（抢占当前）
    {"stop":true}                                    停止当前播放
    EOF                                              退出

`gain` 默认 0.9：声板的声音要与微信里那段人声**叠加**（系统混音），
留 10% 余量避免削波。
"""

import json
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

#: 与 `wechat_voice.OUTPUT_DEVICE_KEYWORD` / `play_worker` 同义：CABLE 的**渲染端**。
#: `|` 分隔多候选（中文 Windows 叫「扬声器 (VB-Audio Virtual Cable)」，英文是
#: `CABLE Input (...)` 且 MME 会截断到 31 字符）—— 2026-09-19 那次"写死单个端点词"
#: 的教训写在 play_worker 里，这里照抄，不重复踩。
KEYWORD = (sys.argv[1] if len(sys.argv) > 1 else "VB-Audio Virtual Cable|CABLE Input").lower()

#: 预载目录（插件自带素材）+ 导入目录（用户自己的），都可缺省。
PRELOAD_DIRS = [Path(p) for p in sys.argv[2:] if p]

DEFAULT_GAIN = 0.9


def _resolve_device():
    """按 KEYWORD 在 MME 下找播放端设备索引（与 play_worker 同算法）。"""
    apis = sd.query_hostapis()
    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    for kw in [k.strip().lower() for k in KEYWORD.split("|") if k.strip()]:
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == mme and d["max_output_channels"] > 0 and kw in d["name"].lower():
                return i
    return None


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _load(path: Path):
    """读一条素材 → (float32 单声道, sr)；失败返回 None（坏文件不该拖垮整个声板）。"""
    try:
        data, sr = sf.read(str(path), dtype="float32")
    except Exception:
        return None
    if data.ndim > 1:
        data = data.mean(axis=1)
    return np.ascontiguousarray(data, dtype="float32"), int(sr)


def main() -> int:
    idx = _resolve_device()
    if idx is None:
        _emit({"type": "error", "msg": f"device_not_found:{KEYWORD}"})
        return 1

    cache: dict[str, tuple] = {}
    for d in PRELOAD_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.wav")):
            got = _load(f)
            if got is not None:
                cache[f.stem] = got

    _emit({"type": "ready", "samples": len(cache)})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
        except Exception:
            continue

        if cmd.get("stop"):
            try:
                sd.stop()
                _emit({"type": "stopped"})
            except Exception as e:
                _emit({"type": "error", "msg": str(e)[:300]})
            continue

        wav = cmd.get("wav")
        if not wav:
            _emit({"type": "error", "msg": "missing wav"})
            continue
        sid = str(cmd.get("id") or Path(wav).stem)
        try:
            gain = float(cmd.get("gain", DEFAULT_GAIN))
        except Exception:
            gain = DEFAULT_GAIN
        gain = float(min(1.5, max(0.0, gain)))

        got = cache.get(sid)
        if got is None:
            got = _load(Path(wav))
            if got is None:
                _emit({"type": "error", "msg": f"sample_unreadable:{Path(wav).name}"})
                continue
            cache[sid] = got

        data, sr = got
        try:
            sd.stop()  # 抢占：连点两下 = 重新炸一次，不排队
            sd.play(data * gain, sr, device=idx)
            _emit({"type": "playing", "id": sid, "duration_s": round(len(data) / sr, 3)})
        except Exception as e:
            _emit({"type": "error", "msg": str(e)[:300]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

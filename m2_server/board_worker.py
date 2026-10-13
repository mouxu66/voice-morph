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

垫底层（2026-10-13，「实时垫歌」）：`{"bgm": ...}` 系命令
----------------------------------------------------
「边唱边说」的实时版：说话/实时变声进行中，往 CABLE **垫一层长 BGM**。
与 one-shot 的根本区别是**两层互不打断**：

    one-shot（sd.play 全局流）  抢占式：新命令 stop 旧的，短促、点完即完
    BGM 垫层（独立 OutputStream） 持续式：垫上之后一直响，点音效/播 TTS 都不影响它

为什么必须是独立流：`sd.play` 全局只有一个播放器，第二次调用就是抢占 —— 长歌会被
下一格音效掐断，正是调研钉死的故障场景（board_worker 不能借 play_worker 的
同一类问题）。独立 `sd.OutputStream` 在 Windows 共享模式下由系统混音器自动与
其它流混合，与"两个程序各播各的"同一机制。

行协议追加（stdin 命令 / stdout 响应，仍是一命令一响应）：
    {"bgm":"/abs/path.wav","id":"...","gain":0.3,"loop":true}
        → {"type":"bgm_playing","id":...,"duration_s":...}
    {"bgm_stop":true}      → {"type":"bgm_stopped"}   （只停垫层，不碰 one-shot）
    {"bgm_gain":0.25}      → {"type":"bgm_gain","gain":...}（实时调音量，不用重开）

⚠️ 协议不变量：stdout **绝不发未经请求的行**。歌播完（loop=false 且到尾）只在
垫层流里填零静音，**不 emit "bgm_stopped"** —— 行协议是「一命令一响应」，父进程
的 `_command` 靠这个不变量读"恰好一行"；回调线程若自作主张发一行，就会顶掉
下一条命令的响应（响应错位 = 静默错发，本模块存在的理由就是防它）。
`gain` 默认 0.3（≈ -10dB）：垫层要一直压在人声下面，与 one-shot 的 0.9
（瞬时音效、与人声同量级）不是一个语义。
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
#: 垫底层默认增益（≈ -10dB）：长 BGM 要一直压在人声下面，与 one-shot 的 0.9 不是一个语义
BGM_DEFAULT_GAIN = 0.3


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

    # ---- 垫底层状态（回调线程与命令循环共享；单变量读写靠 GIL 原子性即可）----
    bgm = {"stream": None, "data": None, "pos": 0, "gain": BGM_DEFAULT_GAIN, "loop": True}

    def _bgm_cb(outdata, frames, time_info, status):  # noqa: ARG001 —— sounddevice 回调签名
        """垫层回调：从 data[pos:] 拉一块填进输出；到尾按 loop 回绕或填零。

        ⚠️ 只填缓冲，**绝不 _emit**——协议不变量见文件头（未请求的行会顶掉
        下一条命令的响应）。loop=false 播完就是持续静音，父进程按 duration_s
        自行把界面状态收回去。
        """
        d = bgm["data"]
        if d is None:
            outdata.fill(0)
            return
        buf = np.zeros(frames, dtype=np.float32)
        remaining = frames
        while remaining > 0:
            p = bgm["pos"]
            if p >= d.size:
                if bgm["loop"]:
                    bgm["pos"] = p = 0
                else:
                    break
            take = min(remaining, d.size - p)
            buf[frames - remaining : frames - remaining + take] = d[p : p + take]
            bgm["pos"] = p + take
            remaining -= take
        outdata[:, 0] = buf * bgm["gain"]

    def _bgm_stop_stream():
        """停掉并关闭垫层流（重复调用安全；one-shot 的 sd.play 不受影响）。"""
        st = bgm["stream"]
        bgm["stream"] = None
        bgm["data"] = None
        bgm["pos"] = 0
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass

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

        # ---- 垫底层命令（先于 one-shot 分支：互不抢占是本组命令的存在理由）----
        if cmd.get("bgm_stop"):
            _bgm_stop_stream()
            _emit({"type": "bgm_stopped"})
            continue
        if "bgm_gain" in cmd:
            try:
                g = float(cmd["bgm_gain"])
            except Exception:
                g = BGM_DEFAULT_GAIN
            bgm["gain"] = float(min(1.5, max(0.0, g)))
            _emit({"type": "bgm_gain", "gain": bgm["gain"]})
            continue
        bgm_wav = cmd.get("bgm")
        if bgm_wav:
            sid = str(cmd.get("id") or Path(bgm_wav).stem)
            got = cache.get(sid)
            if got is None:
                got = _load(Path(bgm_wav))
                if got is None:
                    _emit({"type": "error", "msg": f"sample_unreadable:{Path(bgm_wav).name}"})
                    continue
                cache[sid] = got
            data, dsr = got
            try:
                gain = float(cmd.get("gain", BGM_DEFAULT_GAIN))
            except Exception:
                gain = BGM_DEFAULT_GAIN
            _bgm_stop_stream()  # 换歌 = 先停旧的（垫层只有一层，不是队列）
            bgm["data"] = data
            bgm["pos"] = 0
            bgm["gain"] = float(min(1.5, max(0.0, gain)))
            bgm["loop"] = bool(cmd.get("loop", True))
            try:
                stream = sd.OutputStream(
                    samplerate=int(dsr), channels=1, dtype="float32",
                    device=idx, callback=_bgm_cb,
                )
                stream.start()
            except Exception as e:
                bgm["data"] = None
                _emit({"type": "error", "msg": str(e)[:300]})
                continue
            bgm["stream"] = stream
            _emit({"type": "bgm_playing", "id": sid, "duration_s": round(len(data) / dsr, 3)})
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

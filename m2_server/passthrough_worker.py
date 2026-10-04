"""真声直讲 · 常驻直通 worker（`sound.tts` 域，2026-10-04）。

干什么
------
把**麦克风的声音原样**灌进 CABLE（虚拟声卡），让学生那头听到老师的真声；
同时**还能接收 wav 命令**，把它**叠加**在真声之上（热键插一句 AI 音）。

    真麦 ──InputStream──→ 队列 ──OutputStream──→ CABLE Input
                                    ↑
                      插话 wav ────┘（叠加，不打断真声）

为什么必须自持 OutputStream、不能用 sd.play
-------------------------------------------
`sd.play()` / `sd.stop()` 是 sounddevice 的**全局单例**：起第二路会 stop 掉第一路。
而现有 `board_worker.py` 用的正是 `sd.play` —— 它**接不了这个需求**：
一按热键，老师在讲的真声会被**整个掐断**。

实测（2026-10-04 真机）：两路 OutputStream 可同时开、同时写同一 CABLE 设备 →
真混音。所以本 worker 自己持有输出流，插话只是在回调里**叠加**一段 buffer。

为什么是独立 worker、不复用 board_worker
----------------------------------------
与 `board_worker.py` 文件头记的同一个理由：**行协议是单命令串行消费的**，
两条链路共用一个管道会互相吃掉对方的 `playing`/`done` 行 → 静默错判"播完了"。
所以各持管道。

行协议（stdout，每行一个 JSON，flush）
-------------------------------------
    {"type":"ready","out_device":"扬声器 (VB-Audio Virtual Cable)","in_device":"麦克风阵列 (Senary Audio)"}
    {"type":"playing","id":"phrase_xxx","duration_s":1.9}     ← 插话开始叠加
    {"type":"stopped"}                                        ← 插话被停（直通继续）
    {"type":"error","msg":"device_not_found:..."}

stdin：每行一个 JSON 命令
    {"wav":"/abs/path.wav","id":"phrase_xxx","gain":0.9}   叠加播放（抢占上一条插话）
    {"stop_clip":true}                                     停掉插话（**直通不受影响**）
    EOF                                                    退出

★ 与 board_worker 的关键差异：`{"stop":true}` 在那边是 `sd.stop()`（全停），
  在这里**绝不能停直通** —— 所以命令名特意区分成 `stop_clip`。

采样率
------
统一 48k（Windows 共享模式原生率）。插话素材若是别的采样率（项目里 TTS 产物
常见 24k），在叠加时线性插值，避免"音调变尖/变慢"。
"""

import contextlib
import json
import queue
import sys
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

#: CABLE 的**渲染端**（灌声音进去的那头）。`|` 分隔多候选 —— 中文 Windows 叫
#: 「扬声器 (VB-Audio Virtual Cable)」，英文是 `CABLE Input (...)` 且 MME 会截断到
#: 31 字符。这个常量与 wechat_voice / play_worker / board_worker 同源同义。
KEYWORD = (
    sys.argv[1] if len(sys.argv) > 1 else "VB-Audio Virtual Cable|CABLE Input"
).lower()

#: 系统默认录音设备名（父进程传入，通常就是老师正在用的真麦）。
#: 传空则由 worker 自己现读默认设备。
WANT_INPUT = sys.argv[2] if len(sys.argv) > 2 else ""

#: 48k = Windows 共享模式原生率，避免重采样损失（同 rvc_live 的链路口径）。
SR = 48000

#: 10ms 一块 —— 与声板/级联同量级的低延迟粒度。
BLOCK = 480

#: 插话默认增益。与 board_worker 的 0.9 同口径：留 10% 余量防叠加削波。
DEFAULT_GAIN = 0.9

#: 队列上限。满了丢块（音频回调**绝不能阻塞**）——丢块表现为一闪而过的爆音，
#: 比卡死整条链路好得多。
Q_MAX = 64


def _mentions_16ch(name: str) -> bool:
    """名字里是否提到 VB-Audio 的 **16 通道变体**（`CABLE In 16 Ch`）。

    与 `rvc_live._mentions_16ch` 同规则。枚举名里这段是英文，故只认英文串。
    """
    s = (name or "").lower()
    return "16 ch" in s or "16ch" in s


def _device_matches(dev_name: str, want: str) -> bool:
    """设备名模糊匹配（`want` 可用 `|` 分隔多候选，任一命中即真）。

    照抄 `rvc_live._device_matches` 的**全部四条判据**，逐条都有事故背书：

      1. **两边都按前缀比较**（`a[:28]` / `b[:28]`）：MME 把名字截断到 31 字符，
         配置里写完整名就永远匹配不上（2026-09-19「写死单个端点词」事故）。
      2. **`b in a or a in b`**：中英文系统命中点不同（中文名完整只有驱动名命中，
         英文名被截断只有端点词命中），单靠一个方向会漏。
      3. ★ **16 通道变体否决**：`CABLE In 16 Ch (...)` 是同一块 VB-Audio 声卡的
         **另一端点**，不在链路里 —— 选中它 = 声音灌进**没人听的端点**，
         表现是"一切正常但对面听不到"。`b in a` 那一支会让它误命中，故硬否决。
      4. 空名直接 False（避免空串把什么都匹配上）。
    """
    a = (dev_name or "").lower()
    if not a:
        return False
    dev_is_16ch = _mentions_16ch(a)
    for cand in (want or "").split("|"):
        b = cand.strip().lower()
        if not b:
            continue
        if dev_is_16ch and not _mentions_16ch(b):
            continue
        if b in a or a in b or a[:28] in b or b[:28] in a:
            return True
    return False


def _mme_index() -> int | None:
    """MME 主机的索引。三个既有播放器（board/play/wechat）都走 MME。"""
    apis = sd.query_hostapis()
    return next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)


def _resolve_output(keyword: str) -> int | None:
    """找 CABLE 渲染端设备索引（MME 视角，含 16-Ch 否决）。"""
    mme = _mme_index()
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != mme or d["max_output_channels"] <= 0:
            continue
        if _device_matches(d["name"], keyword):
            return i
    return None


def _resolve_input(want: str) -> int | None:
    """找采集设备（真麦）。

    `want` 非空 → 按名字解析（父进程传的是"启动那一刻的系统默认麦"，
    这样老师在界面上选的设备与这里一致）；为空 → 用 sounddevice 的默认输入。
    """
    if not want:
        try:
            return sd.default.device[0]
        except Exception:
            return None
    mme = _mme_index()
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != mme or d["max_input_channels"] <= 0:
            continue
        if _device_matches(d["name"], want):
            return i
    # 传进来的名字解析不到（设备被拔了/改名了）→ 退回默认，别让整条链路起不来
    try:
        return sd.default.device[0]
    except Exception:
        return None


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _load(path) -> tuple[np.ndarray, int] | None:
    """读一条 wav → (float32 单声道, sr)；失败返回 None（坏文件不拖垮直通）。"""
    try:
        data, sr = sf.read(str(path), dtype="float32")
    except Exception:
        return None
    if data.ndim > 1:
        data = data.mean(axis=1)
    return np.ascontiguousarray(data, dtype="float32"), int(sr)


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """线性插值重采样。

    只用于**插话素材**（TTS 产物常见 24k），真声直通那一路是设备原生 48k、
    不经过这里。线性插值对语音质量足够，且不需要 scipy 依赖。
    """
    if src_sr == dst_sr or len(x) == 0:
        return x
    n = max(1, int(round(len(x) * dst_sr / src_sr)))
    return np.interp(
        np.linspace(0, len(x) - 1, n), np.arange(len(x)), x
    ).astype("float32")


def main() -> int:
    out_idx = _resolve_output(KEYWORD)
    if out_idx is None:
        _emit({"type": "error", "msg": f"device_not_found:{KEYWORD}"})
        return 1
    in_idx = _resolve_input(WANT_INPUT)
    if in_idx is None:
        _emit({"type": "error", "msg": "input_device_not_found"})
        return 1

    out_name = sd.query_devices(out_idx)["name"]
    in_name = sd.query_devices(in_idx)["name"]

    #: 真声从 InputStream 回调 → 队列 → OutputStream 回调。
    #: 两块跑在不同线程，且块大小可能不齐，所以用队列解耦（不直接拷贝）。
    mic_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=Q_MAX)

    def mic_cb(indata, frames, time_info, status):  # noqa: ANN001
        if status:  # 溢出等：仅记录，不中断
            print(f"[passthrough][mic] {status}", file=sys.stderr)
        with contextlib.suppress(queue.Full):
            mic_q.put_nowait(indata[:, 0].copy())
            # 丢块（见 Q_MAX 注释：宁可爆一下，不可阻塞回调）

    #: 插话状态。`data` 非 None 表示正在叠加；`pos` 是已消费到的样本位置。
    #: ★ 这是「混音」的实现：真声 buffer 上**相加**一段插话，而不是替换。
    clip_lock = threading.Lock()
    clip: dict = {"data": None, "pos": 0}

    def out_cb(outdata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"[passthrough][out] {status}", file=sys.stderr)

        # ① 真声：把队列里的块拼成 frames 长
        buf = np.zeros(frames, dtype="float32")
        got = 0
        while got < frames:
            try:
                piece = mic_q.get_nowait()
            except queue.Empty:
                break
            take = min(frames - got, len(piece))
            buf[got : got + take] = piece[:take]
            got += take
            if take < len(piece):
                # 块比剩余空间大：把尾巴塞回去留待下一轮（不丢样本）
                with contextlib.suppress(queue.Full):
                    mic_q.put_nowait(piece[take:])
                break

        # ② 插话：叠加（不是替换）
        with clip_lock:
            cd = clip["data"]
            if cd is not None and clip["pos"] < len(cd):
                p = clip["pos"]
                take = min(frames, len(cd) - p)
                buf[:take] += cd[p : p + take]
                clip["pos"] += take
                if clip["pos"] >= len(cd):
                    clip["data"] = None
                    clip["pos"] = 0

        # ③ 削波保护：两路相加可能超 ±1.0，硬限幅（比整段归一化更保真）
        np.clip(buf, -1.0, 1.0, out=buf)
        outdata[:, 0] = buf

    try:
        out_stream = sd.OutputStream(
            device=out_idx, samplerate=SR, channels=1, dtype="float32",
            blocksize=BLOCK, callback=out_cb,
        )
        in_stream = sd.InputStream(
            device=in_idx, samplerate=SR, channels=1, dtype="float32",
            blocksize=BLOCK, callback=mic_cb,
        )
        out_stream.start()
        in_stream.start()
    except Exception as e:
        _emit({"type": "error", "msg": f"stream_open_failed:{type(e).__name__}:{e}"[:300]})
        return 1

    _emit({"type": "ready", "out_device": out_name, "in_device": in_name})

    def _stop_clip() -> None:
        with clip_lock:
            clip["data"] = None
            clip["pos"] = 0

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmd = json.loads(raw)
            except Exception:
                continue

            # ★ 只停插话，绝不停直通 —— 命令名与 board_worker 的 `stop` 刻意区分
            if cmd.get("stop_clip"):
                _stop_clip()
                _emit({"type": "stopped"})
                continue

            wav = cmd.get("wav")
            if not wav:
                _emit({"type": "error", "msg": "missing wav"})
                continue
            sid = str(cmd.get("id") or "")
            try:
                gain = float(cmd.get("gain", DEFAULT_GAIN))
            except Exception:
                gain = DEFAULT_GAIN
            gain = float(min(1.5, max(0.0, gain)))

            got = _load(wav)
            if got is None:
                _emit({"type": "error", "msg": f"sample_unreadable:{wav}"[:300]})
                continue
            data, sr = got
            data = _resample(data, sr, SR) * gain
            with clip_lock:
                clip["data"] = data
                clip["pos"] = 0
            _emit(
                {
                    "type": "playing",
                    "id": sid,
                    "duration_s": round(len(data) / SR, 3),
                }
            )
    finally:
        for nm, s in (("input", in_stream), ("output", out_stream)):
            try:
                s.stop()
                s.close()
            except Exception as e:
                print(f"[passthrough] {nm} 流关闭异常: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

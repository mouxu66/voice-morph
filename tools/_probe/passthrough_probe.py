"""最小验证版：真声直通 CABLE + 热键插 AI 音（混音）。

> ⚠️ **这不是产品代码** —— 产品实现在 `m2_server/passthrough_worker.py` +
> `m2_server/passthrough.py`（2026-10-04 落地）。本脚本保留下来是因为它是
> 「真声直讲」可行性**唯一可复现的真机验证载体**（设计文档 §六 引用了它）：
> 想再核一遍链路、复量传递比时跑它，别去动产品代码。
> 与产品的差别：只做"搬运 + 插播"，没有路由 / 互斥 / 状态上报 / 设备解析的
> 完整判据 —— 那些在 `passthrough_worker.py` 里。

目的
----
验证「老师真声讲课 + 偶发插一句 AI 音，学生听到连续混音」这个场景能不能成立。
**不改正式代码**，只做一次性实测。

★ 触发方式（2026-10-04 改）
--------------------------
最初用 `sys.stdin.readline()` 收「回车=插播」——但 agent 后台跑时 **stdin 立刻 EOF**，
脚本开流不到 1 秒就自己退出了。改成**文件触发**：
监听 `<触发文件>` 的 mtime，一变就插播一次。适合后台常驻 + 外部任意方式触发。

    touch <trigger>   → 插播一次

它做的事
--------
  路 1（常驻）：麦克风(真麦) --原样搬运--> CABLE Input   ← 老师的真声
  路 2（触发）：按回车 --播 wav-->        CABLE Input   ← AI 插话
  CABLE Output --> 微信/钉钉等（把麦克风设成 CABLE Output）

★ 为什么必须用 OutputStream 而不是 sd.play
-----------------------------------------
`sd.play()` / `sd.stop()` 是 sounddevice 的**全局单例**：起第二路会 stop 掉第一路。
而本项目现有播放器（board_worker.py）正是这么写的 —— 所以它**接不了这个需求**：
一按热键就会把老师的真声掐断。这里必须走 `OutputStream`，两路各持一条流。

★ 安全
------
- 本脚本**只往 CABLE 灌声音**，不动系统默认设备（切默认麦请另用 audio_config.ps1）。
- 退出时自动关闭所有流，不留残余。
- Ctrl+C / 回车 q 都可退出。

用法
----
    python tools/_probe/passthrough_probe.py <插播.wav> [触发文件]
    后台常驻：监听触发文件 mtime，变化即插播一次；
    无参运行时（tty）也可用回车/q 交互。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

#: CABLE 的**渲染端**（灌声音进去的那头）。与项目常量同义：
#: 中文 Windows 叫「扬声器 (VB-Audio Virtual Cable)」，英文是 `CABLE Input (...)`。
#: ⚠️ 必须排除 `CABLE In 16 Ch (...)` —— 项目里明确否决过（不在变声链路里）。
DEVICE_KEYWORD = "vb-audio virtual cable"

#: 真声直通的采样率。48k 是 Windows 共享模式的原生率，避免重采样。
SR = 48000
BLOCK = 480  # 10ms 一块，足够低延迟


def _resolve_device() -> int | None:
    """找 CABLE 渲染端（有输出通道、名字命中、且不是 16 通道变体）。"""
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if d["max_output_channels"] > 0 and DEVICE_KEYWORD in name:
            if "16 ch" in name:
                continue  # 否决：不在变声链路里，声音灌进去没人听
            return i
    return None


def _input_device() -> int | None:
    """默认麦克风（真麦）。"""
    try:
        return sd.default.device[0]
    except Exception:
        return None


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    return np.ascontiguousarray(mono, dtype="float32"), int(sr)


def main() -> int:
    out_idx = _resolve_device()
    if out_idx is None:
        print("✗ 找不到 CABLE 渲染端（VB-Audio Virtual Cable）")
        return 1
    in_idx = _input_device()
    if in_idx is None:
        print("✗ 找不到默认麦克风")
        return 1

    dev_out = sd.query_devices(out_idx)
    dev_in = sd.query_devices(in_idx)
    print(f"  麦克风(真麦) → {dev_in['name']}")
    print(f"  输出目标     → {dev_out['name']}  (idx={out_idx})")
    print()

    # 插播素材（可选；没有就只验直通）
    clip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    clip: tuple[np.ndarray, int] | None = None
    if clip_path:
        if not clip_path.is_file():
            print(f"✗ 插播文件不存在: {clip_path}")
            return 1
        clip = _load_wav(clip_path)
        print(f"  插播素材     → {clip_path.name}（{len(clip[0]) / clip[1]:.1f}s）")

    # ---- 路 1：真声直通 ----
    # 输入流（采集真麦）→ 队列 → 输出流（写进 CABLE）。
    # 用队列是因为两块的回调不同线程，且输入/输出块大小可能不齐。
    mic_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=50)
    stop_evt = threading.Event()

    def mic_cb(indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"  [mic] {status}", file=sys.stderr)
        try:
            mic_q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # 满了就丢块，不许阻塞音频回调

    #: 插播用的混音缓冲：非 None 时，输出回调把它叠加到真声上。
    #: ★ 这正是「混音」——两路声音相加，而不是互相打断。
    overlay_lock = threading.Lock()
    overlay = {"data": None, "pos": 0, "sr": SR}

    def out_cb(outdata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"  [out] {status}", file=sys.stderr)
        # ① 真声
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
                # 没取完的塞回去（简化处理：丢弃尾部，10ms 粒度损失可忽略）
                try:
                    mic_q.put_nowait(piece[take:])
                except queue.Full:
                    pass
                break

        # ② 叠加插播（如果有）
        with overlay_lock:
            od = overlay["data"]
            if od is not None and overlay["pos"] < len(od):
                p = overlay["pos"]
                take = min(frames, len(od) - p)
                # 采样率不同则线性插值（插播素材多半不是 48k）
                if overlay["sr"] != SR:
                    idx = np.linspace(p, p + take - 1, num=take)
                    src = np.interp(
                        idx * overlay["sr"] / SR,
                        np.arange(len(od)),
                        od,
                    ).astype("float32")
                else:
                    src = od[p : p + take]
                buf[:take] += src * 0.9  # 留 10% 余量防削波（同 board_worker 的 gain 设计）
                overlay["pos"] += take
                if overlay["pos"] >= len(od):
                    overlay["data"] = None
                    overlay["pos"] = 0

        np.clip(buf, -1.0, 1.0, out=buf)
        outdata[:, 0] = buf

    try:
        in_stream = sd.InputStream(
            device=in_idx, samplerate=SR, channels=1, dtype="float32",
            blocksize=BLOCK, callback=mic_cb,
        )
        out_stream = sd.OutputStream(
            device=out_idx, samplerate=SR, channels=1, dtype="float32",
            blocksize=BLOCK, callback=out_cb,
        )
        out_stream.start()
        in_stream.start()
    except Exception as e:
        print(f"✗ 开流失败: {type(e).__name__}: {e}")
        return 1

    print()
    print("=== 已开始：真声正在直通 CABLE ===")
    if clip:
        print("    回车 = 插播一次 AI 音 | q + 回车 = 退出")
    else:
        print("    （未指定插播 wav，只验直通）| q + 回车 = 退出")
    print()

    def trigger():
        with overlay_lock:
            overlay["data"] = clip[0] if clip else None
            overlay["pos"] = 0
            overlay["sr"] = clip[1] if clip else SR

    # ---- 触发循环 ----
    # ★ 为什么不用 stdin 收回车：agent 后台跑时 stdin **立刻 EOF**，
    #   早期版本在这里 `break` → 开流不到 1s 就自己关掉（2026-10-04 实测踩到）。
    #   现在：给了触发文件就监听 mtime（后台常驻可用）；没有则退回交互，
    #   但 **EOF 不再退出**，转空转保活。
    trig_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        if trig_path is not None:
            trig_path.parent.mkdir(parents=True, exist_ok=True)
            trig_path.touch()
            last = trig_path.stat().st_mtime_ns
            print(f"  监听触发文件: {trig_path}")
            while not stop_evt.is_set():
                time.sleep(0.15)
                try:
                    cur = trig_path.stat().st_mtime_ns
                except FileNotFoundError:
                    continue
                if cur != last:
                    last = cur
                    if clip:
                        trigger()
                        print(f"  → 插播 {len(clip[0]) / clip[1]:.2f}s")
        else:
            while True:
                line = sys.stdin.readline()
                if not line:
                    print("  [stdin EOF] 转入保活（Ctrl+C 退出）", file=sys.stderr)
                    while not stop_evt.is_set():
                        time.sleep(0.5)
                    break
                if line.strip().lower() in ("q", "quit", "exit"):
                    break
                if clip:
                    trigger()
                    print(f"  → 插播 {len(clip[0]) / clip[1]:.2f}s")
                else:
                    print("  （无插播素材，忽略）")
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        for nm, s in (("输入", in_stream), ("输出", out_stream)):
            try:
                s.stop()
                s.close()
            except Exception as e:
                print(f"  {nm}流关闭异常: {e}", file=sys.stderr)
        print("已退出，所有流已关闭。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

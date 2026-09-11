# -*- coding: utf-8 -*-
"""
CABLE 桥接诊断工具 —— 判定虚拟音频究竟坏在哪一层。

分三个维度扫描:
  1) apis     每种 Windows 音频 API(MME / DirectSound / WASAPI / WDM-KS)各测一遍
              —— 老程序(微信等)常走 MME/DS,现代 python 库走 WASAPI,两者可以不同命
  2) formats  固定 WASAPI 播放端,遍历采集端采样率(8000..48000)
              —— IM 软件惯用 8k/16k 单声道开麦,若 SRC 断裂则该格式恒静音
  3) pair     默认的 Bohr 全配对

用法:
    python tools/cable_diag.py --list
    python tools/cable_diag.py            # 全跑
    python tools/cable_diag.py --apis
    python tools/cable_diag.py --formats
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_SECONDS = 2.0
LEAD_S = 0.25
TAIL_S = 0.6
INTEREST = ("vb-audio", "cable", "steam streaming", "nahimic", "virtual")


def _api_name(i: int) -> str:
    return sd.query_hostapis(sd.query_devices(i)["hostapi"])["name"]


def _match(name: str) -> bool:
    """设备名是否属于我们关心的虚拟/桥接设备。"""
    low = name.lower()
    return any(k in low for k in INTEREST)


def list_devices(only_interest: bool = True) -> None:
    print("=" * 78)
    print("设备清单 (全部 hostapi)")
    print("=" * 78)
    for i, d in enumerate(sd.query_devices()):
        name = d["name"]
        api = _api_name(i)
        if only_interest and not _match(name):
            continue
        role = []
        if d["max_output_channels"]:
            role.append(f"OUT{d['max_output_channels']}ch @{int(d['default_samplerate'])}")
        if d["max_input_channels"]:
            role.append(f"IN{d['max_input_channels']}ch @{int(d['default_samplerate'])}")
        if not role:
            continue
        print(f"  [{i:>2}] {api:<18} {name[:48]:<50} {' / '.join(role)}")
    print()


def _tone(sr: int, channels: int, seconds: float = TEST_SECONDS) -> np.ndarray:
    n = int(sr * seconds)
    t = np.arange(n) / sr
    sig = 0.5 * np.sin(2 * np.pi * 440.0 * t) + 0.3 * np.sin(2 * np.pi * 1760.0 * t)
    return np.tile(sig[:, None], (1, channels)).astype("float32")


def test_pair(out_dev: int, in_dev: int, sr_in: int | None = None,
              sr_out: int | None = None) -> dict:
    oi = sd.query_devices(out_dev, "output")
    ii = sd.query_devices(in_dev, "input")
    sr_o = sr_out or int(oi["default_samplerate"])
    sr_i = sr_in or int(ii["default_samplerate"])
    co = min(2, int(oi["max_output_channels"]))
    ci = min(2, int(ii["max_input_channels"]))
    r = {"out": oi["name"], "in": ii["name"], "api": _api_name(out_dev),
         "sr_out": sr_o, "sr_in": sr_i, "ok": False,
         "rms": 0.0, "peak": 0.0, "err": ""}

    # 采集端以多种采样率依次尝试: 这台机器的虚拟端点对格式极挑剔,
    # 直接用设备默认速率常常 Invalid sample rate。
    for cand in [sr_i, 48000, 44100, sr_o, 32000, 16000, 8000]:
        got = _attempt(out_dev, in_dev, sr_o, cand, co, ci, buf_of(co, sr_o))
        if got is None:
            continue
        got["out"], got["in"], got["api"] = oi["name"], ii["name"], _api_name(out_dev)
        got["sr_out"] = sr_o
        return got
    r["err"] = r["err"] or f"Input open FAIL at all rates (tried incl. {sr_i})"
    return r


def buf_of(channels: int, sr: int, seconds: float = TEST_SECONDS) -> np.ndarray:
    return _tone(sr, channels, seconds)


def _attempt(out_dev: int, in_dev: int, sr_out: int, sr_in: int,
             co: int, ci: int, buf: np.ndarray) -> dict | None:
    """单次尝试。返回 None 表示采集端以该速率/通道数打不开。"""
    chunks: list[np.ndarray] = []

    def cb(indata, frames, tinfo, status):  # noqa: ARG001
        chunks.append(indata.copy())

    try:
        istream = sd.InputStream(device=in_dev, samplerate=sr_in, channels=ci,
                                 dtype="float32", callback=cb)
        istream.start()
    except Exception:
        return None
    r = {"sr_in": sr_in, "ok": True, "rms": 0.0, "peak": 0.0, "err": ""}
    try:
        time.sleep(LEAD_S)
        try:
            sd.play(buf, samplerate=sr_out, device=out_dev, blocking=False)
        except Exception as e:
            r["err"] = f"Output FAIL: {type(e).__name__}: {e}"
            return r
        time.sleep(TEST_SECONDS + TAIL_S)
    finally:
        istream.stop()
        istream.close()
    data = np.concatenate(chunks) if chunks else np.zeros((1, ci), "float32")
    f = data.astype("float64")
    r.update(rms=float(np.sqrt(np.mean(f ** 2))), peak=float(np.max(np.abs(f))))
    return r


def _show(r: dict) -> None:
    flag = "有声!" if r["peak"] > 1e-4 else "静音<<<<"
    err = f"   {r['err']}" if r["err"] else ""
    print(f"  [{r['api']:<16}] PLAY {r['out'][:40]:<42} @{r['sr_out']}")
    print(f"  {'':<18} REC  {r['in'][:40]:<42} @{r['sr_in']}")
    print(f"  {'':<18} -> peak={r['peak']:.6f} rms={r['rms']:.6f}  {flag}{err}\n")


def _find(api_kw: str, name_kw: str, want_out: bool) -> list[int]:
    res = []
    for i, d in enumerate(sd.query_devices()):
        if api_kw.lower() not in _api_name(i).lower():
            continue
        if name_kw.lower() not in d["name"].lower():
            continue
        n = d["max_output_channels"] if want_out else d["max_input_channels"]
        if n:
            res.append(i)
    return res


def scan_apis() -> None:
    print("=" * 78)
    print("维度 1: 按音频 API 扫描 (老 API 与新 API 是否同命)")
    print("=" * 78)
    targets = [
        # (API, 播放端关键词, 采集端关键词)
        ("MME", "vb-audio virtual cable", "cable output"),
        ("DirectSound", "vb-audio virtual cable", "cable output"),
        ("WASAPI", "vb-audio virtual cable", "cable output"),
        ("WASAPI", "cable in 16 ch", "cable output"),
        ("WASAPI", "steam streaming speakers", "steam streaming microphone"),
        ("MME", "steam streaming speakers", "steam streaming microphone"),
        ("DirectSound", "steam streaming speakers", "steam streaming microphone"),
    ]
    seen = set()
    for api, okw, ikw in targets:
        outs, ins = _find(api, okw, True), _find(api, ikw, False)
        if not outs or not ins:
            print(f"  [{api}] 找不到 {okw!r} / {ikw!r}, 跳过\n")
            continue
        for o, ci in zip(outs, ins):
            if (o, ci) in seen:
                continue
            seen.add((o, ci))
            _show(test_pair(o, ci))


def scan_formats() -> None:
    print("=" * 78)
    print("维度 2: 采集端采样率遍历 (播放端固定 WASAPI CABLE Input @48k)")
    print("=" * 78)
    outs = _find("WASAPI", "vb-audio virtual cable", True)
    ins = _find("WASAPI", "cable output", False)
    if not outs or not ins:
        print("  找不到设备,跳过")
        return
    o, ci = outs[0], ins[0]
    print(f"  播放端=[{o}] {sd.query_devices(o)['name']}")
    print(f"  采集端=[{ci}] {sd.query_devices(ci)['name']}\n")
    for sr in (8000, 16000, 22050, 32000, 44100, 48000):
        r = test_pair(o, ci, sr_in=sr, sr_out=48000)
        line = f"  采集 @{sr:>5} Hz -> peak={r['peak']:.6f}  {'有声!' if r['peak'] > 1e-4 else '静音<<<<'}"
        if r["err"]:
            line += f"   {r['err']}"
        print(line)
    print()


def scan_pair() -> None:
    print("=" * 78)
    print("维度 3: 全虚拟设备配对")
    print("=" * 78)
    outs, ins = [], []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] and ("vb-audio" in d["name"].lower() or "steam" in d["name"].lower()):
            outs.append(i)
        if d["max_input_channels"] and ("vb-audio" in d["name"].lower() or "steam" in d["name"].lower()):
            ins.append(i)
    for o in outs:
        for ci in ins:
            no = sd.query_devices(o)["name"].lower()
            ni = sd.query_devices(ci)["name"].lower()
            if no.split("(")[0].strip() == ni.split("(")[0].strip():
                continue
            _show(test_pair(o, ci))


def scan_channels() -> None:
    """IM 软件常以 mono 开麦; 另外 MME/DS 层 CABLE Output 是 16ch,
    客户端请求 16ch 时若 channel mask 与主声道不对也可能读到空数据。"""
    print("=" * 78)
    print("维度 4: 采集端通道数 / 采样率组合遍历")
    print("=" * 78)
    for api, okw in (("WASAPI", "vb-audio virtual cable"),
                     ("MME", "vb-audio virtual cable")):
        outs = _find(api, okw, True)
        ins = _find(api, "cable output", False)
        if not outs or not ins:
            print(f"  [{api}] 找不到设备, 跳过\n")
            continue
        o, ci_ = outs[0], ins[0]
        maximal = int(sd.query_devices(ci_)["max_input_channels"])
        # 播放端采样率必须恒定用设备默认值: 跟着采集端一起降会让播放先失败,
        # 那样测到的静音是"播失败"而非"采集失败"。
        play_sr = int(sd.query_devices(o)["default_samplerate"])
        print(f"  [{api}] PLAY [{o}] {sd.query_devices(o)['name']} @{play_sr}")
        print(f"        REC  [{ci_}] {sd.query_devices(ci_)['name']} max_in={maximal}")
        buf = buf_of(2, play_sr)
        for sr in (play_sr, 48000, 16000, 8000):
            for ch in sorted({1, 2, maximal}):
                if ch > maximal:
                    continue
                r = _attempt(o, ci_, play_sr, sr, 2, ch, buf)
                if r is None:
                    print(f"        REC @{sr:>5}Hz / {ch:>2}ch -> 采集端打不开")
                    continue
                flag = "有声!" if r["peak"] > 1e-4 else "静音<<<<"
                extra = f"  {r['err']}" if r.get("err") else ""
                print(f"        REC @{sr:>5}Hz / {ch:>2}ch -> peak={r['peak']:.6f}  {flag}{extra}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all-devices", action="store_true")
    ap.add_argument("--apis", action="store_true")
    ap.add_argument("--formats", action="store_true")
    ap.add_argument("--channels", action="store_true")
    ap.add_argument("--pair", action="store_true", help="全虚拟设备两两配对(慢, 分钟级)")
    args = ap.parse_args()

    list_devices(not args.all_devices)
    if args.list:
        return 0
    if not (args.apis or args.formats or args.channels or args.pair):
        args.apis = args.formats = args.channels = True
    if args.apis:
        scan_apis()
    if args.formats:
        scan_formats()
    if args.channels:
        scan_channels()
    if args.pair:
        scan_pair()
    return 0


if __name__ == "__main__":
    sys.exit(main())

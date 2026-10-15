# -*- coding: utf-8 -*-
"""补录宽频素材的 A/B：同一输入/seed/pitch，只换权重。

三个臂（回答的问题各不同，别混成一句"哪个好"）：
    · myvoice_tilt_t00 —— 旧 12.2min 素材**原样**训的（最老的对照）
    · myvoice_tilt_t03 —— 现役：旧素材 +3dB/oct 预加重（不重录拿到的杠杆）
    · myvoice_supp2026 —— 本轮：旧素材原样 + 4 条新宽频录音（说话+唱歌，15.06min）

单变量纪律：三臂共用同一条分离人声、同一个 rvc worker（模型缓存）、
pitch=−12、index_rate=0.5，**只有权重不同**。`summarize()` 机械核对这一点，
配置串味（哪个臂 pitch/index 不一样）直接抛错，防止 A/B 里混进第二列。

客观分（口径全部复用既有、已守过的工具）：
    · 绝对频段 dBFS —— `cover_tilt_absbands.abs_bands`（"是更亮还是只是更响"）
    · CAM++ 对你的参考音 / 对原唱同段 —— `cover_metrics.secs`
      （⚠️ 三臂同 pitch，互相比是有效的；绝对值带 −12 移调的音高偏置，
      见 cover_pitch_ruler 的结论，别当绝对像度读）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ab = _load("ab", "experiments/cover_tilt_absbands.py")
cm = _load("cover_metrics", "experiments/cover_metrics.py")

#: (臂标签, RVC 实验名, 说明)。顺序即页面顺序。
VOICES = [
    ("t00 旧素材原样", "myvoice_tilt_t00", "12.2min PPT 录音，无处理"),
    ("t03 现役(预加重)", "myvoice_tilt_t03", "旧素材 +3dB/oct —— 不重录的杠杆"),
    ("supp 补录宽频", "myvoice_supp2026", "旧素材原样 + 4 条新录音(说话+唱歌)，15.06min"),
]

#: A/B 必须一致的参数（summarize 用它做单变量核对）
SHARED = {"pitch": -12, "index_rate": 0.5}


def summarize(rows: list[dict]) -> dict:
    """把每个臂的测量汇总成对比表；**机械核对 A/B 只有"权重"一列不同**。

    rows: [{name, voice, pitch, index_rate, bands:{...}, cam_me, cam_orig}, ...]
    配置串味（任何一臂 pitch/index 不一致）直接 ValueError —— 否则读出来的
    频段/像度差可能来自第二变量，而报告里看不出来（同族：multi_pitch 的单变量纪律）。
    """
    if not rows:
        raise ValueError("没有测量结果")
    for key, val in SHARED.items():
        vals = {r.get(key) for r in rows}
        if vals != {val}:
            raise ValueError(f"A/B 不是单变量：{key} 各臂不一致 {sorted(vals)}（应全为 {val}）")
    base = next((r for r in rows if r["voice"] == "myvoice_tilt_t00"), rows[0])
    table = []
    for r in rows:
        b = r["bands"]
        table.append({
            "name": r["name"], "voice": r["voice"],
            "rms_dbfs": b["rms_dbfs"],
            "4-8k_dbfs": b["4-8k_dbfs"], "8-11k_dbfs": b["8-11k_dbfs"],
            "4-8k_net_vs_rms": round(b["4-8k_dbfs"] - b["rms_dbfs"], 2),
            "8-11k_net_vs_rms": round(b["8-11k_dbfs"] - b["rms_dbfs"], 2),
            "cam_me": r.get("cam_me"), "cam_orig": r.get("cam_orig"),
            "d48_vs_t00": round(b["4-8k_dbfs"] - base["bands"]["4-8k_dbfs"], 2),
            "d811_vs_t00": round(b["8-11k_dbfs"] - base["bands"]["8-11k_dbfs"], 2),
        })
    return {"shared": dict(SHARED), "base": base["voice"], "table": table}


def _mp3(src: pathlib.Path, dst: pathlib.Path, start: float, seconds: float, ffmpeg: str) -> bool:
    import subprocess

    cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
           "-i", str(src), "-ac", "1", "-ar", "44100", "-b:a", "128k", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0 and dst.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description="补录素材 A/B（三权重，单变量）")
    ap.add_argument("--vocals", type=pathlib.Path,
                    default=ROOT / "media/covers/song2/01_vocals.wav")
    ap.add_argument("--ref", type=pathlib.Path, default=ROOT / "media/ft_work/ppt12/ref_24k.wav")
    ap.add_argument("--start", type=float, default=85.0)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--out", type=pathlib.Path,
                    default=ROOT / "media/covers/song2/supp_eval")
    args = ap.parse_args()

    from common import find_ffmpeg
    import rvc_convert

    ffmpeg = find_ffmpeg()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    rows = []

    # 原唱同段（CAM++ "对原唱" 的参照 + 试听对照）
    orig_seg = out / "orig_segment.wav"
    _mp3(args.vocals, out / "orig_segment.mp3", args.start, args.seconds, ffmpeg)
    import subprocess as _sp
    _sp.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{args.start:.2f}",
             "-t", f"{args.seconds:.2f}", "-i", str(args.vocals),
             "-ac", "1", "-ar", "40000", "-c:a", "pcm_s16le", str(orig_seg)], check=True)

    for label, voice, why in VOICES:
        t = time.time()
        rvc_out = pathlib.Path(rvc_convert.rvc_convert(
            args.vocals, voice, pitch=SHARED["pitch"], index_rate=SHARED["index_rate"]))
        full = out / f"full_{voice}.wav"
        shutil.copy2(rvc_out, full)
        seg = out / f"seg_{voice}.wav"
        clip = out / f"clip_{voice}.mp3"
        _sp.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{args.start:.2f}",
                 "-t", f"{args.seconds:.2f}", "-i", str(full),
                 "-ac", "1", "-ar", "40000", "-c:a", "pcm_s16le", str(seg)], check=True)
        _mp3(full, clip, args.start, args.seconds, ffmpeg)

        bands = ab.abs_bands(seg)
        cam_me = cm.secs(seg, args.ref)
        cam_orig = cm.secs(seg, orig_seg)
        rows.append({"name": label, "voice": voice, "why": why,
                     "pitch": SHARED["pitch"], "index_rate": SHARED["index_rate"],
                     "convert_s": round(time.time() - t, 1),
                     "bands": bands, "cam_me": cam_me, "cam_orig": cam_orig})
        print(f"[{label}] {time.time()-t:5.1f}s | 4-8k {bands['4-8k_dbfs']:7.2f} "
              f"8-11k {bands['8-11k_dbfs']:7.2f} | CAM++ 你 {cam_me} / 原唱 {cam_orig}")

    summary = summarize(rows)
    report = {"vocals": str(args.vocals), "ref": str(args.ref),
              "segment": [args.start, args.start + args.seconds],
              "rows": rows, "summary": summary}
    (out / "supp_eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    # ---- 试听页（复用 cover_metrics.build_page，探针口径一致）
    import base64

    def uri(p: pathlib.Path) -> str:
        return "data:audio/mpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")

    items = [("原唱这一段（对照，没换声）", [
        {"src": uri(out / "orig_segment.mp3"), "name": "原唱人声（demucs 分离）",
         "note": f"{args.start:.0f}–{args.start+args.seconds:.0f}s，A/B 的『原』"}])]
    arm_rows = []
    for r in rows:
        voice = r["voice"]
        arm_rows.append({"src": uri(out / f"clip_{voice}.mp3"), "name": r["name"],
                         "tag": f"4-8k {r['bands']['4-8k_dbfs']}dB · CAM你 {r['cam_me']}",
                         "note": f"{r['why']}｜CAM++ 对原唱 {r['cam_orig']}"})
    items.append(("换成我的声音（三权重，pitch 都为 −12，只有权重不同）", arm_rows))
    intro = (f"段：第 {args.start:.0f}–{args.start+args.seconds:.0f}s（《唯一》歌声最密段）\n"
             "三臂同一分离人声 / 同 seed / pitch −12 / index 0.5，只有训练权重不同。\n"
             "CAM++ 是语音声纹，歌声上只做三臂之间的相对排序，绝对值带移调音高偏置。")
    page = cm.build_page(items, title="补录宽频素材 A/B（supp vs t03 vs t00）",
                         intro=intro, out=out / "listen.html")
    print(f"\n→ {page}")
    print(f"→ {out / 'supp_eval.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

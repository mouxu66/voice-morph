# -*- coding: utf-8 -*-
"""翻唱体检：把「混杂 / 不纯」拆成可量的几条轴，并生成 A/B 试听页。

为什么要把主观抱怨拆成数字（2026-10-14）
    用户的话是「声音混杂、没有最纯粹的声音」。这句话里其实混着三件不同的事，
    而它们各自的**修法完全不同**，所以必须分开测：

    ① **闷（缺少高频）** —— 用分频段能量比与谱质心。它可以是输入丢的、\n       也可以是模型自己合成的天花板（见 `knobs.json` 的 ceiling 段）。\n    ② **掺了别人的音色** —— 用 CAM++ 声纹分别对「你的参考音」与「原唱这一句」\n       求余弦。对原唱的相似度越高 = 越像\"两个声音混在一起\"。\n    ③ **旋钮能救多少** —— 同一段、同一 seed，只换一个旋钮逐字节比 md5\n       （在 `cover_knobs.py` 里做，这里只读它的报告）。\n\n口径与边界（引用数字时必须一起说）\n    - CAM++ 是**语音**声纹尺子，用在歌声上只能做「同一输入、同一 seed 下的相对比较」，\n      不能当成绝对像度读；\n    - **不测 NatScore**：项目自己的裁决书（`m1f_千问微调vsRVC-声音克隆裁决`）已实测\n      它把真人排在所有合成之下（差 3~7 分），绝对值不可跨\"真人/合成\"比较 ——\n      引它只会得到一个看着权威的数，所以这里刻意不算。\n    - 频段比只看**有声帧**：静音段会把所有配置都拉向\"一样\"，掩盖差异。\n\n用法\n    .venv/Scripts/python.exe experiments/cover_metrics.py \\\n        --knobs media/covers/knobs/knobs.json \\\n        --ref media/ft_work/ppt12/ref_24k.wav \\\n        --orig media/covers/lorezo_feed_the_one/01_original_vocal.wav \\\n        --start 78 --seconds 20 --page\n"""

from __future__ import annotations

import argparse
import base64
import html
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 分频段（Hz）。上界刻意停在 16k：源是 44.1k、输出是 40k，
#: 再往上比就不是\"同一个东西\"了（源还有 16k+）。
BANDS: list[tuple[int, int, str]] = [
    (0, 4000, "0-4k"),
    (4000, 8000, "4-8k"),
    (8000, 11000, "8-11k"),
    (11000, 16000, "11-16k"),
]

#: 有声帧门限：比本片最响帧低 35 dB 以上的帧不算（静音/极弱段）
VOICED_DROP_DB = 35.0


def frame_spectrum(path: Path, *, max_seconds: float | None = None) -> dict:
    """有声帧的平均功率谱 → 分频段能量比 + 谱质心。

    ★ 为什么不用整段 FFT：20s 里必然有换气与间奏，整段窗会把差异抹平；
      按 1024 点帧（约 25ms @40k）逐帧算、只留有声音的帧，差异才露得出来。
    """
    import numpy as np
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = d.mean(1)
    if max_seconds:
        mono = mono[: int(max_seconds * sr)]
    n = 1024
    hop = n // 2
    win = np.hanning(n).astype(np.float32)
    frames = [mono[i : i + n] for i in range(0, len(mono) - n, hop)]
    if not frames:
        return {"error": "太短"}
    X = np.stack([np.abs(np.fft.rfft(f * win)) ** 2 for f in frames])
    rms = np.sqrt(X.sum(1) / n)
    keep = rms >= rms.max() * (10 ** (-VOICED_DROP_DB / 20))
    P = X[keep].mean(0)
    fr = np.fft.rfftfreq(n, 1 / sr)
    total = float(P.sum()) + 1e-12
    out = {
        "sr": int(sr),
        "seconds": round(len(mono) / sr, 2),
        "voiced_frames": int(keep.sum()),
        "frames": int(len(frames)),
        "centroid_hz": int(round(float((fr * P).sum() / total))),
    }
    raw = {name: float(P[(fr >= lo) & (fr < hi)].sum() / total) for lo, hi, name in BANDS}
    # ★ 以 0-4k 为 0 dB 的**相对** dB —— 直接比两个频段的能量比只能得出
    #   "0.0002 vs 0.0109" 这种读不出量级的数；换成 dB 才能说清"差 16 个 dB"。
    #
    # ★ 必须先在**未四舍五入**的比值上算 dB：8-11k 那种近乎为空的频段能量比是
    #   1e-8 量级，先 `round(..., 5)` 会把它变成 0.0，再取 log 就得到假的 -120 dB
    #   （2026-10-14 实测踩过：同一个文件算出 -75.5 dB 与 -120 dB 两个值）。
    ref = max(raw["0-4k"], 1e-15)
    out["band_db"] = {
        name: (round(10 * float(np.log10(raw[name] / ref)), 1) if raw[name] > 0 else None)
        for _lo, _hi, name in BANDS
    }
    for _lo, _hi, name in BANDS:
        out[f"band_{name}"] = float(f"{raw[name]:.6g}")
    # 一阶差分（频谱倾斜），越大越刺、越小越闷
    out["rolloff95_hz"] = int(round(float(fr[np.searchsorted(np.cumsum(P) / total, 0.95)])))
    return out


def secs(path: Path, ref: Path) -> float | None:
    """CAM++ 声纹余弦（复用试音间那把尺子的实现，不另写一份）。"""
    try:
        import audition_score

        audition_score._prepare_paths()
        return round(audition_score._secs(Path(path), Path(ref)), 4)
    except Exception as e:  # noqa: BLE001
        print(f"[体检] secs 失败 {Path(path).name}: {e}")
        return None


def mp3(src: Path, dst: Path, *, start: float = 0.0, seconds: float = 20.0) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(src),
        "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "96k", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[体检] mp3 失败 {src.name}: {r.stderr.strip()[-200:]}")
    return dst.exists()


def _data_uri(p: Path) -> str:
    return "data:audio/mpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def build_page(items: list[dict], *, title: str, intro: str, out: Path) -> Path:
    """极简试听页：每条一个播放按钮，音频内嵌（不依赖外部文件，双击即用）。

    ★ 与 `tools/listen_page.py` 的分工：那个是**盲听打分**（要发放包 + 揭盲表 +
      进度存储），这个是**开发者自查**（我要看见每条是什么）。别把两者混起来用。
    """
    blocks = []
    for sec_title, rows in items:
        rows_html = []
        for r in rows:
            tag = f'<span class="tag">{html.escape(r["tag"])}</span>' if r.get("tag") else ""
            note = f'<div class="note">{html.escape(r["note"])}</div>' if r.get("note") else ""
            rows_html.append(
                f'<div class="item"><div class="row"><button onclick="p(this)" '
                f'data-src="{r["src"]}">▶ 播放</button>'
                f'<b>{html.escape(r["name"])}</b>{tag}</div>{note}</div>'
            )
        blocks.append(
            f'<h2>{html.escape(sec_title)}</h2><div class="list">{"".join(rows_html)}</div>'
        )
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.6 system-ui, "Microsoft YaHei", sans-serif; max-width: 780px;
        margin: 0 auto; padding: 18px 16px 60px; }}
 h1 {{ font-size: 19px; margin: 0 0 6px; }}
 h2 {{ font-size: 15px; margin: 22px 0 8px; color: #2b6cb0; }}
 .sub {{ color: #666; font-size: 13px; white-space: pre-wrap; margin-bottom: 10px; }}
 .item {{ border: 1px solid #d8d8d8; border-radius: 10px; padding: 9px 11px; margin-bottom: 7px; }}
 .item.playing {{ border-color: #2b6cb0; box-shadow: 0 0 0 2px #2b6cb033; }}
 .row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
 button {{ font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid #bbb;
           background: #fff; cursor: pointer; }}
 .tag {{ font: 12px/1 ui-monospace, monospace; color: #444; background: #eee;
         border-radius: 5px; padding: 3px 6px; }}
 .note {{ font-size: 13px; color: #555; margin-top: 4px; }}
</style></head><body>
<h1>{html.escape(title)}</h1><div class="sub">{html.escape(intro)}</div>
{"".join(blocks)}
<script>
let cur = null;
function p(btn) {{
  const wasPlaying = cur && !cur.paused && cur.dataset.src === btn.dataset.src;
  if (cur) {{ cur.pause(); document.querySelectorAll('.item.playing').forEach(e => e.classList.remove('playing')); }}
  if (wasPlaying) {{ cur = null; return; }}
  const a = new Audio(btn.dataset.src);
  cur = a; a.dataset.src = btn.dataset.src;
  btn.closest('.item').classList.add('playing');
  a.play().catch(e => alert('浏览器拦了播放，再点一次：' + e.message));
  a.onended = () => {{ btn.closest('.item').classList.remove('playing'); }};
}}
window.__vmClips = () => document.querySelectorAll('[data-src]').length;
// 播放钩子（与 tools/listen_page.py 同一套约定）：自动播放策略要求真实用户手势，
// 所以只能"点一下再看 currentTime" —— 没有它就无法在浏览器里证明"真的在响"，
// 而不是只渲染了一排按钮。
window.__vmAudio = () => (cur ? {{t: cur.currentTime, paused: cur.paused, dur: cur.duration}} : null);
</script></body></html>
"""
    out.write_text(doc, encoding="utf-8")
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="翻唱体检：闷/混杂/旋钮收益")
    ap.add_argument("--knobs", required=True, type=Path, help="cover_knobs.py 的报告")
    ap.add_argument("--ref", type=Path, help="你的参考音（CAM++ 对照）")
    ap.add_argument("--orig", type=Path, help="原唱整首（切出同段做对照）")
    ap.add_argument("--start", type=float, default=78.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--page", action="store_true", help="生成试听页")
    ap.add_argument("--out", type=Path, default=None, help="产物目录（默认与 knobs 同目录）")
    args = ap.parse_args()

    knobs = json.loads(args.knobs.read_text(encoding="utf-8"))
    out_dir = (args.out or args.knobs.parent).resolve()
    clips_dir = out_dir / "clips"
    rep: dict = {"knobs": str(args.knobs), "files": {}, "notes": []}

    # ---- 原唱同段（既是听感对照，也是\"掺了多少原唱\"的声纹参照）----
    orig_clip = out_dir / "_orig_singer.wav"
    if args.orig:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{args.start:.3f}",
             "-t", f"{args.seconds:.3f}", "-i", str(args.orig.resolve()),
             "-ac", "1", "-ar", "44100", str(orig_clip)], check=False,
        )
    targets: list[tuple[str, str, Path]] = []
    if orig_clip.exists():
        # ★ 对照必须是**未降采样**的原唱（44.1k）：拿 16k 那份当对照会让
        #   8 kHz 以上天然为空，从而把"高频掉得多"这件事**说小**（2026-10-14 踩过）。
        targets.append(("source", "原唱这一段（44.1k，对照用）", orig_clip))
    for c in knobs["configs"]:
        targets.append((c["name"], c["why"], Path(c["wav"])))
    ceil = knobs.get("ceiling") or {}
    for key, label in (("in_wav", "天花板·输入（你自己的说话素材）"),
                       ("out_wav", "天花板·输出（同一段过模型）")):
        if ceil.get(key) and Path(ceil[key]).exists():
            targets.append((key.replace("_wav", "").replace("in", "ceiling_in")
                        .replace("out", "ceiling_out"), label, Path(ceil[key])))

    for name, why, path in targets:
        m = frame_spectrum(path)
        m["secs_vs_your_ref"] = secs(path, args.ref) if args.ref else None
        m["secs_vs_orig_singer"] = secs(path, orig_clip) if orig_clip.exists() else None
        m["why"] = why
        m["path"] = str(path)
        rep["files"][name] = m
        db = m.get("band_db", {})
        print(f"[体检] {name:14s} 质心 {m.get('centroid_hz')}Hz · "
              f"4-8k {db.get('4-8k')}dB · 8-11k {db.get('8-11k')}dB · "
              f"11-16k {db.get('11-16k')}dB（相对 0-4k） · "
              f"secs(你) {m['secs_vs_your_ref']} · secs(原唱) {m['secs_vs_orig_singer']}")

    if ceil.get("in_wav") and ceil.get("out_wav"):
        a, b = rep["files"]["ceiling_in"], rep["files"]["ceiling_out"]
        rep["ceiling_loss"] = {
            "centroid_drop_pct": round(100 * (1 - b["centroid_hz"] / max(a["centroid_hz"], 1)), 1),
            "band_4-8k_db_change": round(b["band_db"]["4-8k"] - a["band_db"]["4-8k"], 1),
            "band_8-11k_db_change": (
                round(b["band_db"]["8-11k"] - a["band_db"]["8-11k"], 1)
                if b["band_db"]["8-11k"] is not None and a["band_db"]["8-11k"] is not None
                else None
            ),
        }
        print(f"[体检] 天花板（模型在它自己的域内）：质心降 "
              f"{rep['ceiling_loss']['centroid_drop_pct']}% · "
              f"4-8k {rep['ceiling_loss']['band_4-8k_db_change']:+} dB · "
              f"8-11k {rep['ceiling_loss']['band_8-11k_db_change']:+} dB")

    (out_dir / "metrics.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[体检] 报告 → {out_dir / 'metrics.json'}")

    if args.page:
        page_items: list[tuple[str, list[dict]]] = []
        rows = []
        if orig_clip.exists():
            dst = clips_dir / "source.mp3"
            if mp3(orig_clip, dst):
                rows.append({"name": "原唱这一段", "tag": "对照", "src": _data_uri(dst),
                             "note": "这是 demucs 分出来的原唱，没经过任何换声。"})
        for c in knobs["configs"]:
            m = rep["files"][c["name"]]
            dst = clips_dir / f"{c['name']}.mp3"
            if not mp3(Path(c["wav"]), dst):
                continue
            tag = "与 A 逐字节相同" if c["name"] in json.dumps(knobs["identical_groups"]) else f"质心 {m['centroid_hz']}Hz"
            note = c["why"]
            if m["secs_vs_orig_singer"] is not None:
                note += (f"　·　对原唱声纹 {m['secs_vs_orig_singer']}（越高=越像掺了原唱）"
                         f"　·　对你 {m['secs_vs_your_ref']}")
            rows.append({"name": c["name"], "tag": tag, "src": _data_uri(dst), "note": note})
        page_items.append(("换声结果（同一 20 秒、同一 seed，只换一个旋钮）", rows))

        if ceil.get("out_wav"):
            rows2 = []
            for name, label in (("ceiling_in", "输入的你自己（原样）"),
                                ("ceiling_out", "过模型之后")):
                if name not in rep["files"]:
                    continue
                m = rep["files"][name]
                dst = clips_dir / f"{name}.mp3"
                if not mp3(Path(m["path"]), dst, seconds=25.0):
                    continue
                rows2.append({"name": label, "tag": f"质心 {m['centroid_hz']}Hz",
                              "src": _data_uri(dst),
                              "note": " · ".join(f"{k} {v}dB" for k, v in m["band_db"].items())
                                      + "（相对 0-4k）"})
            if rows2:
                page_items.append(("模型天花板：拿它自己的训练素材过一遍", rows2))

        intro = (
            f"素材：lorezo_feed_the_one 第 {args.start:.0f}–{args.start + args.seconds:.0f} 秒"
            f"（歌声最密的段落）\n"
            f"模型：{knobs['voice']} · seed {knobs['seed']} · 变调 {knobs['pitch']:+d} 半音\n"
            "每条都是同一段输入、同一个随机种子，只换一个旋钮 —— 所以听到的差异就是那个旋钮的。"
        )
        page = build_page(page_items, title="翻唱体检 · 旋钮 + 天花板", intro=intro,
                          out=out_dir / "listen.html")
        size = page.stat().st_size / 1048576
        rep["page"] = {"path": str(page), "mb": round(size, 1)}
        (out_dir / "metrics.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[体检] 试听页 → {page}（{size:.1f} MB，音频已内嵌）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

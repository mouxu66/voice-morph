# -*- coding: utf-8 -*-
"""唱歌域过采样重训的一版到底值不值：`myvoice_singdom` vs `myvoice_supp2026`。

### 为什么做这一版（2026-10-19 的决定链）
- supp2026 = 15.06min，其中唱歌只有 **91.95s（10%）**，最高 236Hz；
- 《成都》实测 cos 0.314（同人基线 0.74）→ 是**说话模型唱不了歌**；
- 高音天花板已被 m1o 推翻（不是"缺档位"）、移调造高音被耳朵否决；
- ⇒ 唯一还没试过的变量是**素材的域**：把已有唱歌素材过采样到 24.6%（`cover_singdom_dataset.py`），
  其余（说话素材、epochs=40、v2、同样的 40k）**一律不动**。

### 口径（这是"一版"而不是"一锤子"）
- 输入用**训练集外**的歌声：`media/covers/lowsong_20261018/sep/in.wav`（《好久不见》分离人声），
  不是训练过的那 74s —— 否则比的只是"背得熟不熟"；
- 两版同输入、同 pitch、同 index_rate、同推理路径（都走 `rvc_convert` 常驻 worker）；
- **它只能回答"域权重有没有用"**：过采样用的是同一条 74s，不会变出新的发声方式。
  真正补唱歌音色仍得录新素材（`docs/附录-可执行规程.md`）。

读数：`cover_metrics.frame_spectrum`（分频段/质心）+ `cover_post_chain._timbre`
（CAM++ 裸余弦与**音高对齐**余弦 —— 歌声跨音高必须看对齐列，见 §8.76）。
★ 数字只负责"有没有变化"，**"像不像人唱"由耳朵裁决**（`listen.html`）。

用法（`.venv`）：
    ./.venv/Scripts/python.exe experiments/cover_singdom_ab.py run
    ./.venv/Scripts/python.exe experiments/cover_singdom_ab.py page
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server", ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 新版（唱歌域过采样）与基线（supp2026）
VOICES = [("singdom", "myvoice_singdom"), ("supp2026", "myvoice_supp2026")]
#: 训练集外的歌声（《好久不见》分离人声，低音歌、E2–A3）
DEFAULT_INPUT = ROOT / "media" / "covers" / "lowsong_20261018" / "sep" / "in.wav"
OUT_DIR = ROOT / "media" / "covers" / "singdom_ab"
PITCH = 0
INDEX_RATE = 0.5


def _ffmpeg() -> str:
    import common

    return common.find_ffmpeg()


def _clip(src: Path, dst: Path, start: float, seconds: float, *, ar: int = 40000) -> Path:
    """取同一段、统一成 40k 单声道（两版模型必须看到逐字节相同的输入）。"""
    cmd = [_ffmpeg(), "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src), "-t", str(seconds), "-ar", str(ar), "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True, timeout=300)
    return dst


def run_arms(*, source: Path, start: float, seconds: float, out_dir: Path = OUT_DIR) -> dict[str, Path]:
    """两版模型转同一段；返回 {版本: wav}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    inp = _clip(source, out_dir / "00_input_40k.wav", start, seconds)
    import rvc_convert

    arms: dict[str, Path] = {"input": inp}
    for tag, voice in VOICES:
        p = Path(rvc_convert.rvc_convert(inp, voice, pitch=PITCH, index_rate=INDEX_RATE))
        # 产物落在 outputs 目录，拷到实验目录后再算指标（路径统一、便于复核）
        dst = out_dir / f"{tag}.wav"
        cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(p), "-ar", "40000", "-ac", "1",
               "-c:a", "pcm_s16le", str(dst)]
        subprocess.run(cmd, check=True, timeout=300)
        arms[tag] = dst
        print(f"[A/B] {tag}（{voice}）← {p.name}")
    (out_dir / "ab.json").write_text(json.dumps(
        {"source": str(source), "start": start, "seconds": seconds, "pitch": PITCH,
         "index_rate": INDEX_RATE, "voices": dict(VOICES),
         "arms": {k: str(v) for k, v in arms.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    return arms


ROWS = [
    ("input", "① 输入：原唱这一段（参照）", "训练集外的歌声"),
    ("singdom", "② myvoice_singdom（唱歌域过采样 ×3）", "本次重训的唯一变量"),
    ("supp2026", "③ myvoice_supp2026（现役基线）", "当前产品默认模型"),
]


def build_page(arms: dict[str, Path], out_dir: Path = OUT_DIR) -> Path:
    import cover_metrics
    from cover_post_chain import _timbre

    ref = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"
    report: dict[str, dict] = {}
    cards: list[str] = []
    for key, title, sub in ROWS:
        p = arms.get(key)
        if p is None or not p.exists():
            cards.append(f"<div class='card'><div class='t'>{title}</div><span class='missing'>（缺文件）</span></div>")
            continue
        m = cover_metrics.frame_spectrum(p)
        m.update(_timbre(p, ref) if key != "input" else {})
        report[key] = m
        tmp = p.with_suffix(".page_tmp.mp3")
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp)],
                       check=True, timeout=300)
        b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink()
        note = (f"质心 {m.get('centroid_hz')}Hz · 4–8k {m.get('band_db', {}).get('4-8k')}dB · "
                f"8–11k {m.get('band_db', {}).get('8-11k')}dB · rolloff95 {m.get('rolloff95_hz')}Hz · "
                f"像你（裸/对齐）{m.get('cos')}/{m.get('cos_aligned')}")
        cards.append(f"<div class='card'><div class='t'>{title}</div>"
                     f"<div class='s'>{sub}<br>{note}</div>"
                     f"<audio controls preload='metadata' src='{b64}'></audio></div>")

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>唱歌域重训 A/B</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:880px;margin:24px auto;padding:0 16px;line-height:1.7}
 h1{font-size:21px} .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;
 padding:12px 14px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px} .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 audio{width:100%;height:34px} .missing{color:#b00;font-size:13px}
 .sub{color:#555;font-size:13.5px} footer{margin:28px 0;color:#888;font-size:12px}
</style></head><body>
<h1>唱歌域过采样重训：一版 A/B</h1>
<p class="sub">输入是<b>训练集外</b>的歌声（《好久不见》低音歌分离人声），两版模型同输入、同 pitch=0、
同 index_rate=0.5、同推理路径。请只回答一件事：<b>② 比 ③ 更像"一个人在唱歌"吗</b>——
具体听两处：<b>长音的稳定度</b>（有没有抖/断）、<b>咬字后的收尾</b>（有没有塑料感）。
"像你"的裸余弦会随音高失配假红，看<b>对齐</b>那一列（§8.76）。</p>
<p class="sub">本页的结论上限：过采样用的是同一条 74s 唱歌素材，它只能回答"<b>域权重</b>有没有用"，
不能回答"补录新唱歌素材有没有用"（那要录完再训一次）。</p>
__BODY__
<footer>2026-10-19 · experiments/cover_singdom_ab.py · pitch __PITCH__ · index __IR__</footer>
</body></html>"""
    dst = out_dir / "listen.html"
    dst.write_text(html.replace("__BODY__", "\n".join(cards))
                   .replace("__PITCH__", str(PITCH)).replace("__IR__", str(INDEX_RATE)), encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="唱歌域重训 A/B（singdom vs supp2026）")
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--start", type=float, default=45.0)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args()

    arms: dict[str, Path] = {}
    if a.step in ("run", "all"):
        arms = run_arms(source=a.input, start=a.start, seconds=a.seconds, out_dir=a.out)
    if a.step in ("page", "all"):
        if not arms:
            arms = {"input": a.out / "00_input_40k.wav"}
            arms.update({tag: a.out / f"{tag}.wav" for tag, _ in VOICES})
        build_page(arms, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

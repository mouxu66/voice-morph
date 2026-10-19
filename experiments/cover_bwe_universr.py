# -*- coding: utf-8 -*-
"""神经带宽扩展（UniverSR）接入翻唱链路：与「源移植 / 谱折叠 / 只抬 EQ」同页 A/B。

### 为什么是 UniverSR（2026-10-19 调研，见 docs/product/research/m1q_*.md）
同一张对照表（UniverSR 论文，16k 输入 ≈ 8k 截止）音乐 LSD-HF：**AudioSR 1.48 /
FlashSR 1.48 / UniverSR 0.93**；24k 输入：1.47 / 1.62 / **0.96**。
它是 MIT 代码、57M 参数、4 步 midpoint 流匹配、**不用独立 vocoder**（直接复数谱），
`from_pretrained("woongzip1/universr-audio")` 一行加载 —— 8GB 的 5060 跑得动。
（FlashSR 权重许可未知、AudioSR 依赖老 torch、AP-BWE 是语音训练，都不如它合适。）

### 与今天三臂的关系（`cover_hf_bands.py`）
那里比的是"8–12k **从哪抄**"：A 只抬残留、B 抄原唱、C 把自己的 4–8k 折上去。
本实验加两臂 **D1/D2 = 让模型"生成"8–12k 及以上**（到 20k）：
  · D1 = BWE(干声) → **现役后期链**（与 A/B/C 逐项可比）
  · D2 = BWE(干声) → 后期链**去掉空气感 EQ**（已有真高频后再 +3dB@9k 大概率过头；
       两条都出来，让耳朵说要不要留那个 EQ）
★ 五臂共享同一条输入干声、同一个 seed，唯一差别是"高频怎么来的"。

### 口径与边界（引用数字时必须一起说）
- **源的高频不是 ground truth**：这是"用你的音色唱别人的歌"，真值（你唱这一句的
  8–12k）不存在。源只提供**亮度参照**（`hf_gap_vs_src`）与听觉对照。
- UniverSR 输出 48k，本项目链路是 40k → 落回 40k（>20k 被丢掉，符合预期）。
- `input_sr=16000`（内容只到 8k）；`seed=1234` 固定（不固定的话每次采样新噪声，
  就不是单变量了）。

用法：
    ./.venv/Scripts/python.exe experiments/cover_bwe_universr.py all
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

import cover_hf_bands as hfb  # noqa: E402  复用同一批度量与 I/O（不另写一份尺子）

#: 跑手用主 .venv 的 python，但**永远走子进程**（不把 timm/torchdiffeq/模型带进后端；
#: 为什么没建独立 venv 见 `tools/universr_enhance.py` 模块头：cu128 轮子在这条线路上
#: 只有 ~217KB/s，而主 venv 已有同版本 torch）。
USR_PY = ROOT / ".venv" / "Scripts" / "python.exe"
RUNNER = ROOT / "tools" / "universr_enhance.py"

#: 上游三臂的来源目录（`cover_hf_bands.py` 的产物）与本次的输出目录
SRC_DIR = ROOT / "media" / "covers" / "song2" / "hf8k"
OUT_DIR = ROOT / "media" / "covers" / "song2" / "bwe"

#: 有效带宽：RVC 干声的 8–12k 基本是空的（实测 −55dB），所以按 8k 截止喂
INPUT_SR = 16000
SEED = 1234
CHAIN_RATE = 40000


def _to_40k(src: Path, dst: Path) -> Path:
    """48k → 40k（链路统一采样率；>20k 的频率按预期被丢掉）。"""
    subprocess.run([hfb._ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                    "-ar", str(CHAIN_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
                   check=True, timeout=300)
    return dst


def enhance(dry: Path, dst: Path, *, input_sr: int = INPUT_SR, seed: int = SEED) -> dict:
    """调独立 venv 里的跑手；返回它打印的那行 JSON（含 run_s / peak）。"""
    if not USR_PY.exists():
        raise SystemExit(f"找不到 python：{USR_PY}\n（按 docs/product/research/m1q_*.md 的安装步骤补齐依赖）")
    cmd = [str(USR_PY), str(RUNNER), "--input", str(dry), "--output", str(dst),
           "--input-sr", str(input_sr), "--seed", str(seed)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=1800, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"UniverSR 失败（{r.returncode}）：\n{(r.stderr or '')[-1500:]}")
    line = [ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")]
    if not line:
        raise SystemExit(f"UniverSR 没打印结果行：\n{(r.stdout or '')[-800:]}")
    return json.loads(line[-1])


def run_arms(out_dir: Path = OUT_DIR, *, src_dir: Path = SRC_DIR) -> dict[str, Path]:
    from cover_post_chain import apply_filters, post_filters

    out_dir.mkdir(parents=True, exist_ok=True)
    dry = src_dir / "rvc_dry.wav"
    if not dry.exists():
        raise SystemExit(f"缺少干声 {dry}（先跑 experiments/cover_hf_bands.py run）")
    for name in ("A_dry_post.wav", "B_src_hf_post.wav", "C_fold_post.wav", "00_source_40k.wav"):
        if not (src_dir / name).exists():
            raise SystemExit(f"缺少上流产物 {src_dir / name}")

    # ① 神经带宽扩展：干声 → 48k → 落回 40k
    up48 = out_dir / "D_bwe_48k.wav"
    info = enhance(dry, up48)
    print(f"[UniverSR] {info['seconds_in']}s → 加载 {info['load_s']}s + 推理 {info['run_s']}s "
          f"（{info['ode']}，seed={info['seed']}，peak {info['peak']}）")
    d40 = _to_40k(up48, out_dir / "D_bwe_40k.wav")

    # ② 两臂：同一份 BWE 产物，只差"要不要那个 +3dB@9k 空气感 EQ"
    apply_filters(d40, out_dir / "D1_bwe_chain.wav", post_filters())
    apply_filters(d40, out_dir / "D2_bwe_chain_no_air.wav", post_filters(air_db=0.0))
    # ③ 参照臂：上游原样拷进来（同页可比；不重算，避免"跨路径产物不可比"）
    for name, dst in (("A_dry_post.wav", "A_dry_post.wav"), ("B_src_hf_post.wav", "B_src_hf_post.wav"),
                      ("C_fold_post.wav", "C_fold_post.wav"), ("00_source_40k.wav", "00_source_40k.wav")):
        (out_dir / dst).write_bytes((src_dir / name).read_bytes())

    arms = {k: out_dir / v for k, v in ARM_FILES.items()}
    (out_dir / "bwe.json").write_text(json.dumps(
        {"model": "woongzip1/universr-audio", "input_sr": INPUT_SR, "seed": SEED,
         "chain_rate": CHAIN_RATE, "univer": info,
         "arms": {k: str(v) for k, v in arms.items()}}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"[臂] {len(arms)} 条 → {out_dir}")
    return arms


#: 页面顺序：先参照、再五臂（A/B/C 与 cover_hf_bands 同源，D1/D2 是本次新增）
ARM_FILES: dict[str, str] = {
    "00_source": "00_source_40k.wav",
    "A_dry": "A_dry_post.wav",
    "B_src_hf": "B_src_hf_post.wav",
    "C_fold": "C_fold_post.wav",
    "D1_bwe": "D1_bwe_chain.wav",
    "D2_bwe_no_air": "D2_bwe_chain_no_air.wav",
}
ROWS = [
    ("00_source", "① 原唱这一段（参照，非真值）", "源的高频长什么样"),
    ("A_dry", "② A · 现役链（只抬已有残留）", "当前产品行为"),
    ("B_src_hf", "③ B · 源高频移植 + 现役链", "抄原唱的 8–12k"),
    ("C_fold", "④ C · 谱折叠 BWE + 现役链", "把自己的 4–8k 折上去"),
    ("D1_bwe", "⑤ D1 · UniverSR 神经超分 + 现役链", "★ 本次新增：生成高频（到 20k）"),
    ("D2_bwe_no_air", "⑥ D2 · UniverSR + 链（去掉空气感 EQ）", "已有真高频后再 +3dB@9k 可能过头"),
]


def build_page(out_dir: Path = OUT_DIR, *, src_dir: Path = SRC_DIR) -> Path:
    ref = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"
    meta = json.loads((out_dir / "bwe.json").read_text(encoding="utf-8")) if (out_dir / "bwe.json").exists() else {}
    src_metrics = hfb.metrics(src_dir / "00_source_40k.wav", ref)
    src_hf = src_metrics.get("hf_rel0_4k_db")
    report: dict[str, dict] = {}
    cards: list[str] = []
    for key, title, sub in ROWS:
        p = out_dir / ARM_FILES[key]
        if not p.exists():
            cards.append(f"<div class='card'><div class='t'>{title}</div><span class='missing'>（缺文件）</span></div>")
            continue
        m = hfb.metrics(p, ref)
        if src_hf is not None and m.get("hf_rel0_4k_db") is not None:
            m["hf_gap_vs_src_db"] = round(float(m["hf_rel0_4k_db"]) - float(src_hf), 2)
        report[key] = m
        tmp = p.with_suffix(".page_tmp.mp3")
        subprocess.run([hfb._ffmpeg(), "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp)],
                       check=True, timeout=300)
        b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink()
        note = (f"4–8k {m.get('mid_rel0_4k_db')}dB · 8–12k {m.get('hf_rel0_4k_db')}dB · "
                f"离源亮度 {m.get('hf_gap_vs_src_db')}dB · 质心 {m.get('centroid_hz')}Hz · "
                f"rolloff95 {m.get('rolloff95_hz')}Hz · 斜率 {m.get('slope_1k_8k')} · "
                f">8k 调制CV {m.get('hf_mod_cv')} · 像你 {m.get('cos')}")
        cards.append(f"<div class='card'><div class='t'>{title}</div>"
                     f"<div class='s'>{sub}<br>{note}</div>"
                     f"<audio controls preload='metadata' src='{b64}'></audio></div>")

    info = meta.get("univer") or {}
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>神经带宽扩展 A/B（UniverSR）</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:900px;margin:24px auto;padding:0 16px;line-height:1.7}
 h1{font-size:21px} .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;
 padding:12px 14px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px} .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 audio{width:100%;height:34px} .missing{color:#b00;font-size:13px}
 .sub{color:#555;font-size:13.5px} footer{margin:28px 0;color:#888;font-size:12px}
</style></head><body>
<h1>8–12k 缺口：抄（源/折叠） vs <b>生成</b>（UniverSR 神经超分）</h1>
<p class="sub">同一段干声、同一条后期链、同一个 seed，唯一差别是 8–12k 怎么来的。请只听三件事：
<b>① ⑥ 比 ② 是不是真的"亮而不沙"；② ⑤ 和 ⑥ 哪个更自然（那个 +3dB@9k 还该不该留）；
③ 高频里有没有"凭空编出来的"嘶声/金属味</b>（神经超分最典型的失败形态）。</p>
<p class="sub">口径：源的那条<b>不是真值</b>（这是你的音色唱别人的歌，"你唱这一句的 8–12k"本来就不存在），
它只给亮度参照。<b>「像你」只做同输入同 seed 的相对比较</b>（CAM++ 是语音尺子，歌声上不可读绝对值）。</p>
<p class="sub">本次推理：__INFO__</p>
__BODY__
<footer>2026-10-19 · experiments/cover_bwe_universr.py · 模型 woongzip1/universr-audio · 采样率 __SR__Hz</footer>
</body></html>"""
    dst = out_dir / "listen.html"
    info_txt = (f"{info.get('seconds_in')}s 音频 → 加载 {info.get('load_s')}s + 推理 {info.get('run_s')}s"
                f"（{info.get('ode')}，seed {info.get('seed')}）") if info else "（未记录）"
    dst.write_text(html.replace("__BODY__", "\n".join(cards)).replace("__INFO__", info_txt)
                   .replace("__SR__", str(CHAIN_RATE)), encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="UniverSR 神经带宽扩展 A/B")
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    ap.add_argument("--src-dir", type=Path, default=SRC_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--input-sr", type=int, default=INPUT_SR)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()
    globals()["INPUT_SR"], globals()["SEED"] = a.input_sr, a.seed

    if a.step in ("run", "all"):
        run_arms(a.out, src_dir=a.src_dir)
    if a.step in ("page", "all"):
        build_page(a.out, src_dir=a.src_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

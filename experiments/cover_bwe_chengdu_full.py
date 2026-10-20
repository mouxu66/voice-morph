# -*- coding: utf-8 -*-
"""《成都》**整曲** A/B：五臂 + 伴奏（衔接 28s 切片版，2026-10-20）。

为什么再做整曲版（用户 2026-10-20 的原话）
    「把他整个歌曲都要做上来，因为不做整个歌曲的话，有点重音，我听听全部音乐平衡，
      加上伴奏的样子。」
    28s 切片版（`cover_bwe_chengdu.py`）能判断"电不电"，但判断不了**整首的平衡**：
    - 切片反复听同一段，听感会被熟悉度带偏（用户说的"有点重音"）；
    - 干声口径听不出"人声压不压伴奏"——那是混音配平的事，只有带伴奏才成立；
    - 副歌/间奏/尾奏的电音分布不均，切片可能正好落在最好或最坏的一段。

与切片版的三处差异（其余口径逐字沿用）
    1. **整曲 320.88s 一次跑完**，不切段、**不需要预热前导**——产品 `P0_原调_post.wav`
       本来就是全曲跑链的，起点一致，天然可比（切片版要 2s 前导是为了模拟"热机"）。
    2. **BWE 只跑一次**，D1/D2/D3 共享同一份 `D_bwe_40k.wav`。三臂仍只差"后期怎么处理"，
       但省掉两轮 320s 推理（每轮约 10 分钟）。
    3. **带伴奏**：用产品**同一组实测增益**（`chengdu/arms.json` 里 P0 的
       `vocal_gain`/`acc_gain`）走同一条 `amix normalize=0` 两遍防削波。
       ⇒ 「响度平衡」这个变量被钉死，听到的差异只剩音色本身。

为什么混音参数从 arms.json 读、而不是用 `cover_api.auto_vocal_gain` 现算
    `auto_vocal_gain` 会把**每一臂**的人声都配平到"比伴奏高 2.5dB"。那样 D 臂多出来的
    高频能量会被自动压回去，**恰好抹掉要对比的东西**，也偏离用户熟悉的《成都》听感。
    固定用产品那一组（1.7232 / 0.6977）才是单变量。
    ⚠️ 代价：BWE 会轻微改变人声 RMS，四臂混音后的整体响度差就是**真实的人声电平差**，
    页面如实记录（`mix_report`），不替用户判断。

自检（三处，任一不过就 SystemExit）
    ① A 臂（重跑产品链）vs 产品 `P0_原调_post.wav` —— 整曲、无预热，**期望逐样本一致**；
    ② A 臂混音 vs 产品 `P0_原调_mix.wav` —— 证明本脚本的混音口径与 `cover_api.mix_back`
       一致（混音命令与增益都是照抄的，这里用产物验证，不靠"我抄对了"的自我保证）；
    ③ 时长：四臂干声必须与干声源等长（±1 样本），混音必须与伴奏等长。

用法：
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu_full.py all
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu_full.py page   # 只重出页
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server", ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402

import cover_bwe_universr as bwe  # noqa: E402  enhance()/_to_40k()/子进程边界
import cover_hf_bands as hfb  # noqa: E402  度量与 I/O（不另写一份尺子）
from cover_bwe_chengdu import (  # noqa: E402  与切片版共用同一批常量/函数
    TILT_DB_PER_OCT,
    TILT_FROM_HZ,
    _hf_tilt,
    PRODUCT_CHAIN,
)
from cover_post_chain import apply_filters, post_filters  # noqa: E402

SONG_DIR = ROOT / "media" / "covers" / "midsong_20261018" / "chengdu"
DRY_SRC = SONG_DIR / "P0_原调_raw.wav"     # 整曲产品干声（supp2026 / ir 0.5 / pitch 0）
POST_SRC = SONG_DIR / "P0_原调_post.wav"   # 产品后期版（自检①）
MIX_SRC = SONG_DIR / "P0_原调_mix.wav"     # 产品混合版（自检②）
ARMS_JSON = SONG_DIR / "arms.json"         # 混音增益的真相源
STEMS = ROOT / "media" / "covers" / "midsong_20261018" / "candidates" / "成都-赵雷"
ACCOMP = STEMS / "accomp.wav"
VOCAL_SRC = STEMS / "vocals.wav"
ORIG_MP3 = STEMS / "source.mp3"
OUT_DIR = SONG_DIR.parent / "chengdu_bwe_full"
REF = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"

SEED = 1234
INPUT_SR = 16000
CHAIN_RATE = 40000
MIX_RATE = 44100
#: 混音真峰值上限（与 `cover_api.MIX_CEILING` 同值；这里只做断言，不替代产品逻辑）
MIX_CEILING = 0.98

SELFCHECK_TOL = 1e-3

ARM_FILES: dict[str, str] = {
    "A_dry": "A_full_post.wav",
    "D1_bwe": "D1_bwe_post.wav",
    "D2_bwe_no_air": "D2_bwe_no_air_post.wav",
    "D3_bwe_tilted": "D3_bwe_tilted_post.wav",
}
MIX_FILES: dict[str, str] = {
    "A_dry": "A_full_mix.wav",
    "D1_bwe": "D1_bwe_mix.wav",
    "D2_bwe_no_air": "D2_bwe_no_air_mix.wav",
    "D3_bwe_tilted": "D3_bwe_tilted_mix.wav",
}


# ---------------------------------------------------------------- 纯函数（可测）

def mix_gains_from_arms(path: Path = ARMS_JSON, arm: str = "P0_原调") -> tuple[float, float]:
    """从产品 `arms.json` 读某一臂**实际生效**的 (人声增益, 伴奏增益)。

    读 `vocal_gain`/`acc_gain` 而不是自己算：那两个值是产品混音时**已经过防削波
    等比缩放**的结果（初值 ×k），直接拿来用才与 `P0_原调_mix.wav` 对得上。
    缺字段一律抛错 —— 静默回落 1.0 会让四臂听起来"人声太小"，而被误判成音色问题。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if arm not in data:
        raise KeyError(f"{path} 里没有臂 {arm!r}（有：{sorted(k for k in data if not k.startswith('_'))}）")
    node = data[arm]
    for key in ("vocal_gain", "acc_gain"):
        if key not in node:
            raise KeyError(f"{path} 的 {arm}.{key} 缺失 —— 不能猜增益，先回去查产品混音记录")
    vg, ag = float(node["vocal_gain"]), float(node["acc_gain"])
    if not (vg > 0 and ag > 0):
        raise ValueError(f"{arm} 的增益非正：vocal={vg}, acc={ag}")
    return vg, ag


def compare(a: Path, b: Path) -> dict:
    """两条同格式 wav 的逐样本/等效比对（自检与测试共用同一把尺子）。"""
    ya, sa = hfb._read(Path(a))
    yb, sb = hfb._read(Path(b))
    out: dict = {"sr_a": sa, "sr_b": sb, "len_a": int(len(ya)), "len_b": int(len(yb))}
    if sa != sb:
        out.update(tier="采样率不同", corr=None, max_abs_diff=None, peak_rel=None, ok=False)
        return out
    n = min(len(ya), len(yb))
    if n < 2:
        out.update(tier="空音频", corr=None, max_abs_diff=None, peak_rel=None, ok=False)
        return out
    diff = float(np.max(np.abs(ya[:n] - yb[:n])))
    corr = float(np.corrcoef(ya[:n], yb[:n])[0, 1])
    pa = float(np.max(np.abs(ya)))
    pb = float(np.max(np.abs(yb)))
    peak_rel = abs(pa - pb) / max(pb, 1e-9)
    exact = len(ya) == len(yb) and diff <= SELFCHECK_TOL
    equiv = (len(ya) == len(yb) and corr >= 0.999 and diff <= 0.05 and peak_rel <= 0.02)
    out.update(corr=corr, max_abs_diff=diff, peak_a=pa, peak_b=pb, peak_rel=peak_rel,
               tier="逐样本一致" if exact else ("等效一致" if equiv else "不一致"),
               ok=bool(exact or equiv))
    return out


def _mix_once(vocals: Path, accomp: Path, gains: tuple[float, float], out: Path,
              *, float_out: bool = False) -> None:
    """与 `cover_api._mix_once` 逐字同一条命令（改这里必须同步那边，测试有守卫）。"""
    vg, ag = gains
    cmd = [
        hfb._ffmpeg(), "-y", "-loglevel", "error",
        "-i", str(vocals), "-i", str(accomp),
        "-filter_complex",
        f"[0:a]volume={vg}[v];[1:a]volume={ag}[a];[v][a]amix=inputs=2:duration=longest:normalize=0",
        "-ar", str(MIX_RATE), "-ac", "2",
    ] + (["-c:a", "pcm_f32le"] if float_out else []) + [str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("混音失败: " + " | ".join(tail)[-400:])


def _true_peak(path: Path) -> float:
    import soundfile as sf

    d, _ = sf.read(str(path), dtype="float64")
    return float(np.abs(d).max()) if d.size else 0.0


def mix_track(vocals: Path, accomp: Path, out: Path,
              gains: tuple[float, float]) -> dict:
    """两遍防削波混音（与 `cover_api.mix_back` 同语义），返回实际生效的增益。

    多出来的只是**记录**：切片版没有混音，这里必须留下"真峰值多少、有没有被缩"，
    否则四臂响度差会被误读成音色差。
    """
    probe = out.with_name(f".{out.stem}.probe.f32.wav")
    try:
        _mix_once(vocals, accomp, gains, probe, float_out=True)
        peak = _true_peak(probe)
    finally:
        probe.unlink(missing_ok=True)
    applied = gains
    # 容差不是"放过削波"：产品记录的增益本身已是缩放后的值，重跑探测会读到
    # 0.9800x（略超 0.98 一个浮点级），不加容差每次都会再缩 0.0001 并误报"已缩"。
    over = peak > MIX_CEILING + 1e-3
    if over:
        k = MIX_CEILING / peak
        applied = (round(gains[0] * k, 4), round(gains[1] * k, 4))
    _mix_once(vocals, accomp, applied, out)
    return {"requested": list(gains), "true_peak": round(peak, 4),
            "applied": list(applied), "scaled": bool(over)}


def _duration(path: Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate)


# ---------------------------------------------------------------- 跑臂

def run(out_dir: Path = OUT_DIR, *, seed: int = SEED) -> dict[str, Path]:
    """整曲五臂 + 四臂混音 + 原唱参照。返回干声臂路径。"""
    out_dir = Path(out_dir)
    mix_dir = out_dir / "mix"
    out_dir.mkdir(parents=True, exist_ok=True)
    mix_dir.mkdir(parents=True, exist_ok=True)

    dry, dsr = hfb._read(DRY_SRC)
    if dsr != CHAIN_RATE:
        raise SystemExit(f"干声采样率不是 {CHAIN_RATE}：{dsr}")
    secs = len(dry) / dsr
    print(f"[输入] 整曲 {secs:.2f}s @{dsr}Hz · 干声峰值 {float(np.max(np.abs(dry))):.4f}")

    # ① A 臂：整曲重跑产品链（无预热——产品 post 本身就是全曲跑的）
    chain = post_filters(**PRODUCT_CHAIN)
    chain_no_air = post_filters(**{**PRODUCT_CHAIN, "air_db": 0.0})
    a_path = out_dir / ARM_FILES["A_dry"]
    apply_filters(DRY_SRC, a_path, chain)
    sc1 = compare(a_path, POST_SRC)
    if not sc1.get("ok"):
        raise SystemExit(f"自检①失败：A 臂与产品 post 不一致 → {sc1}")
    print(f"[自检①] A 臂 vs 产品 post：{sc1['tier']}"
          f"（max|diff| {sc1['max_abs_diff']:.3g}，corr {sc1['corr']:.6f}）✓")

    # ② 原唱参照（整曲干声，用于高频度量；不是真值）
    hfb._to_40k_mono(VOCAL_SRC, out_dir / "00_source_40k.wav")

    # ③ BWE 只跑一次，三臂共享
    up48 = out_dir / "D_bwe_48k.wav"
    info = bwe.enhance(DRY_SRC, up48, input_sr=INPUT_SR, seed=seed)
    print(f"[UniverSR] {info['seconds_in']}s → 加载 {info['load_s']}s + "
          f"推理 {info['run_s']}s（{info['chunks']} 块 × {info['chunk_s']}s，"
          f"seed {info['seed']}，peak {info['peak']}）")
    d40 = bwe._to_40k(up48, out_dir / "D_bwe_40k.wav")

    d1_path = out_dir / ARM_FILES["D1_bwe"]
    d2_path = out_dir / ARM_FILES["D2_bwe_no_air"]
    d3_path = out_dir / ARM_FILES["D3_bwe_tilted"]
    apply_filters(d40, d1_path, chain)
    apply_filters(d40, d2_path, chain_no_air)

    tilt_in, tsr = hfb._read(d40)
    # 显式传切片版的常量（而不是吃默认值）：两个实验的滚降档必须同源，改了这里就是
    # 改了对照条件 —— 让它出现在调用点，比藏在函数默认参数里可审。
    tilted, tilt_info = _hf_tilt(tilt_in, tsr, from_hz=TILT_FROM_HZ,
                                 db_per_oct=TILT_DB_PER_OCT)
    tilt_path = out_dir / "D_bwe_tilted_40k.wav"
    hfb._write(tilt_path, tilted, tsr)
    apply_filters(tilt_path, d3_path, chain)

    # ④ 时长守卫：四臂必须与干声等长（ffmpeg 理论上不会改长度，但守卫比假设便宜）
    ref_len = len(dry)
    for name in ARM_FILES.values():
        got = hfb._read(out_dir / name)[0]
        if abs(len(got) - ref_len) > 1:
            raise SystemExit(f"自检③失败：{name} 长度 {len(got)} ≠ 干声 {ref_len}")

    # ⑤ 混音：四臂共用产品实测增益
    gains = mix_gains_from_arms()
    print(f"[混音] 固定增益（来自产品 arms.json）：人声 ×{gains[0]} · 伴奏 ×{gains[1]}")
    mix_report: dict[str, dict] = {}
    a_mix = mix_dir / MIX_FILES["A_dry"]
    mix_report["A_dry"] = mix_track(a_path, ACCOMP, a_mix, gains)
    sc2 = compare(a_mix, MIX_SRC)
    if not sc2.get("ok"):
        raise SystemExit(f"自检②失败：A 臂混音与产品 mix 不一致 → {sc2}")
    print(f"[自检②] A 臂混音 vs 产品 mix：{sc2['tier']}"
          f"（max|diff| {sc2['max_abs_diff']:.3g}，corr {sc2['corr']:.6f}）✓")
    for key in ("D1_bwe", "D2_bwe_no_air", "D3_bwe_tilted"):
        mix_report[key] = mix_track(out_dir / ARM_FILES[key], ACCOMP,
                                    mix_dir / MIX_FILES[key], gains)

    # ⑥ 原曲（原唱+伴奏）直接拷 mp3，页面拿它当"整首平衡"的锚
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ORIG_MP3, audio_dir / "00_orig_full.mp3")

    stats = {"seconds": round(secs, 2), "sr": dsr,
             "dry_peak": round(float(np.max(np.abs(dry))), 4)}
    arms = {k: out_dir / v for k, v in ARM_FILES.items()}
    (out_dir / "arms.json").write_text(json.dumps({
        "song": "成都-赵雷", "voice": "myvoice_supp2026", "index_rate": 0.5, "pitch": 0,
        "scope": "full_song", "dry_src": str(DRY_SRC), "vocal_src": str(VOCAL_SRC),
        "accomp": str(ACCOMP), "chain": chain, "chain_no_air": chain_no_air,
        "seed": seed, "input_sr": INPUT_SR, "univer": info, "tilt": tilt_info,
        "mix_gains": {"vocal": gains[0], "acc": gains[1]},
        "mix_report": mix_report,
        "selfcheck_post": sc1, "selfcheck_mix": sc2,
        "audio": stats,
        "arms": {k: str(v) for k, v in arms.items()},
        "mix": {k: str(mix_dir / v) for k, v in MIX_FILES.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[臂] 干声 {len(arms)} 条 + 混音 {len(mix_report)} 条 → {out_dir}")
    return arms


# ---------------------------------------------------------------- 出页

def _to_mp3(src: Path, dst: Path, *, bitrate: str = "128k") -> Path:
    """转 128k mp3。

    为什么整曲页**不用 base64 内嵌**（切片版是内嵌的）：320s × 128k ≈ 5.1MB，
    base64 膨胀 1.33 → 单条 6.8MB，九条就是 60MB 的 HTML，浏览器打开会卡死。
    改同目录相对引用，页面本体 < 100KB，file:// 直接能播。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    subprocess.run([hfb._ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                    "-b:a", bitrate, str(dst)], check=True, timeout=900)
    return dst


ROWS = [
    ("orig", "① 原唱整曲（原唱 + 伴奏）", "整首平衡的锚：人声该在伴奏的哪个位置"),
    ("A_dry", "② A · 现役产品链（不用 UniverSR）", "★ 你在《成都》页听过的那个版本，现在带伴奏"),
    ("D1_bwe", "③ D1 · UniverSR + 同一条产品链", "唯一差别：8k 以上由模型生成"),
    ("D2_bwe_no_air", "④ D2 · UniverSR + 去掉 +6dB@7k", "已有真高频后，那个 shelf 还该不该留"),
    ("D3_bwe_tilted", "⑤ D3 · UniverSR + 8k 以上滚降整形", "★ 把 11–16k 上翘压回原唱形状"),
]


def build_page(out_dir: Path = OUT_DIR, *, ref: Path | None = REF) -> Path:
    out_dir = Path(out_dir)
    meta = json.loads((out_dir / "arms.json").read_text(encoding="utf-8"))
    audio_dir = out_dir / "audio"
    mix_dir = out_dir / "mix"

    src_m = hfb.metrics(out_dir / "00_source_40k.wav", ref)
    src_hf = src_m.get("hf_rel0_4k_db")

    report: dict[str, dict] = {}
    cards: list[str] = []
    for key, title, sub in ROWS:
        if key == "orig":
            mp3 = audio_dir / "00_orig_full.mp3"
            note = "原曲（未经过本项目任何处理）"
            report["orig"] = {"file": str(mp3), "seconds": round(_duration_from_mp3(mp3), 2)}
        else:
            dry = out_dir / ARM_FILES[key]
            mix = mix_dir / MIX_FILES[key]
            m = hfb.metrics(dry, ref)
            if src_hf is not None and m.get("hf_rel0_4k_db") is not None:
                m["hf_gap_vs_src_db"] = round(float(m["hf_rel0_4k_db"]) - float(src_hf), 2)
            mix_info = (meta.get("mix_report") or {}).get(key) or {}
            m["mix_gain_applied"] = mix_info.get("applied")
            m["mix_true_peak"] = mix_info.get("true_peak")
            report[key] = m
            mp3 = _to_mp3(mix, audio_dir / f"{key}_mix.mp3")
            note = (f"4–8k {m.get('mid_rel0_4k_db')}dB · 8–12k {m.get('hf_rel0_4k_db')}dB · "
                    f"离源亮度 {m.get('hf_gap_vs_src_db')}dB · 质心 {m.get('centroid_hz')}Hz · "
                    f"rolloff95 {m.get('rolloff95_hz')}Hz · 斜率 {m.get('slope_1k_8k')} · "
                    f">8k 调制CV {m.get('hf_mod_cv')} · 像你 {m.get('cos')}")
            if mix_info:
                note += (f"<br><span class='dim'>混音：人声 ×{mix_info['applied'][0]} · "
                         f"伴奏 ×{mix_info['applied'][1]} · 真峰 {mix_info['true_peak']}"
                         f"{'（超顶已等比缩）' if mix_info.get('scaled') else ''}</span>")

        cards.append(
            f"<div class='card'><div class='t'>{title}</div>"
            f"<div class='s'>{sub}<br>{note}</div>"
            f"<audio controls preload='metadata' data-key='{key}' src='audio/{mp3.name}'></audio>"
            f"<div class='jump'><a href='#' data-t='0'>从头</a>"
            f"<a href='#' data-t='60'>1:00</a><a href='#' data-t='120'>2:00</a>"
            f"<a href='#' data-t='180'>3:00</a><a href='#' data-t='239.5'>切片点 3:59</a>"
            f"<a href='#' data-t='270'>4:30</a></div></div>")

    # 干声对照（折叠）：判音色/电音仍要走干声口径（伴奏会污染周期度/包络指标）
    dry_cards: list[str] = []
    for key in ("A_dry", "D1_bwe", "D2_bwe_no_air", "D3_bwe_tilted"):
        dry = out_dir / ARM_FILES[key]
        mp3 = _to_mp3(dry, audio_dir / f"{key}_dry.mp3")
        dry_cards.append(
            f"<div class='card'><div class='t'>{key} · 干声（无伴奏）</div>"
            f"<audio controls preload='none' data-key='dry_{key}' src='audio/{mp3.name}'></audio></div>")

    info = meta.get("univer") or {}
    audio = meta.get("audio") or {}
    sc1 = meta.get("selfcheck_post") or {}
    sc2 = meta.get("selfcheck_mix") or {}
    gains = meta.get("mix_gains") or {}
    info_txt = (f"{info.get('seconds_in')}s 人声 → 加载 {info.get('load_s')}s + "
                f"推理 {info.get('run_s')}s（{info.get('chunks')} 块 × "
                f"{info.get('chunk_s')}s，{info.get('ode')}，seed {info.get('seed')}）"
                if info else "（未记录）")

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>《成都》整曲 A/B · 五臂 + 伴奏</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:920px;margin:24px auto;padding:0 16px 80px;line-height:1.7}
 h1{font-size:21px;margin-bottom:6px}
 .lead{color:#555;font-size:13.5px}
 .bar{position:sticky;top:0;z-index:9;background:#fff;border:1px solid #dde2e9;border-radius:10px;
      padding:8px 12px;margin:12px 0;box-shadow:0 2px 6px rgba(0,0,0,.06);
      display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:13px}
 .bar b{font-variant-numeric:tabular-nums}
 .bar label{color:#556;user-select:none}
 .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;
       padding:12px 14px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px} .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 .dim{color:#99a} audio{width:100%;height:34px;margin-top:2px}
 .jump{font-size:12px;color:#889;margin-top:5px} .jump a{color:#4a6fb5;text-decoration:none;
       margin-right:10px;cursor:pointer}
 .jump a:hover{text-decoration:underline}
 details{margin:22px 0} summary{cursor:pointer;font-weight:600;font-size:14.5px;color:#334}
 .sub{color:#555;font-size:13.5px} .warn{background:#fff8e6;border-color:#f0dcae}
 footer{margin:28px 0;color:#888;font-size:12px}
</style></head><body>
<h1>《成都》<b>整曲</b> A/B · 五臂 + 伴奏</h1>
<p class="lead">整曲 __SECS__s，一处不剪。上一版是 28s 切片（判"电不电"够用，判不了整首平衡）；
这一版把全曲、伴奏、混音配平都摆上来，听的是<b>整首歌的平衡</b>——副歌压不压伴奏、间奏后接回来顺不顺、尾奏收得干不干净。</p>

<div class="bar">
 <span>参考位置：<b id="pos">0:00</b> / <b id="dur">--:--</b></span>
 <label><input type="checkbox" id="sync" checked> 切臂时保持播放位置</label>
 <span class="dim">（勾上：换一条会自动跳到同一秒，方便同位置对比）</span>
</div>

<p class="sub">单变量口径：同一段整曲产品干声（supp2026 / ir 0.5 / pitch 0）、同一条现役后期链
（+6dB@7k + De-Ess + 轻压缩）、同一个 seed <b>__SEED__</b>，唯一差别是 <b>8k 以上怎么来</b>
（不补 / BWE 生成 / BWE+去 shelf / BWE+滚降）。</p>

<p class="sub"><b>混音口径</b>：四臂共用产品实测的那组增益（人声 ×__VG__ · 伴奏 ×__AG__，
来自 <code>chengdu/arms.json</code> 的 P0 记录），同一条 <code>amix normalize=0</code> 两遍防削波。
<b>不做每臂自动配平</b>——自动配平会把 D 臂多出来的高频能量压回去，正好抹掉要对比的东西。
所以四臂之间的整体响度差<b>就是真实的人声电平差</b>，页面逐条记了真峰值。</p>

<p class="sub">自检：① A 臂重跑产品链 vs 产品 post —— <b>__SC1__</b>（max|diff| __D1__，corr __C1__）；
② A 臂混音 vs 产品 mix —— <b>__SC2__</b>（max|diff| __D2__，corr __C2__）。
两条都过，说明"五臂之间只差高频"这句话有产物级证据，不是我说说。</p>

<p class="sub">请只听三件事：<b>① ② ③ ⑤ 的整首平衡</b>（人声是不是一直在伴奏上面、副歌会不会糊）；
<b>② ③ 哪个更自然</b>（③ 多出来的高频在伴奏里是"空气感"还是"嘶声"）；
<b>③ ③ vs ⑤</b>（11–16k 上翘压掉后，整首还电不电）——③ 电而 ⑤ 不电 = 上翘是元凶。</p>

__BODY__

<details><summary>干声对照（无伴奏）——判音色/电音要走这个口径</summary>
<p class="sub">伴奏会污染周期度与包络类指标；要量"电音"必须在干声上看。
这四条与上面同源（只是没混伴奏），切臂同样保持位置。</p>
__DRY__
</details>

<p class="sub">口径提醒：① <b>不是真值</b>（这是你的音色唱别人的歌，"你唱这一句的 8–12k"不存在），
它只给整首平衡与亮度参照；「像你」只做同输入同 seed 的相对比较（CAM++ 是语音尺子，歌声上不可读绝对值）。</p>
<p class="sub">本次推理：__INFO__</p>
<footer>2026-10-20 · experiments/cover_bwe_chengdu_full.py · 整曲 __SECS__s · 模型 woongzip1/universr-audio · 40kHz 单声道干声 → 44.1kHz 立体声混音</footer>
<script>
const audios=[...document.querySelectorAll('audio')];
const posEl=document.getElementById('pos'), durEl=document.getElementById('dur');
const syncEl=document.getElementById('sync');
let master=null;
const fmt=t=>{t=Math.max(0,Math.floor(t||0));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0')};
function setT(a,t){const go=()=>{try{a.currentTime=t}catch(e){}};
  if(a.readyState>=1)go(); else a.addEventListener('loadedmetadata',go,{once:true});}
audios.forEach(a=>{
  a.addEventListener('play',()=>{
    if(master&&master!==a){const t=master.currentTime;master.pause();
      if(syncEl.checked)setT(a,t);}
    master=a;durEl.textContent=fmt(a.duration);
  });
  a.addEventListener('timeupdate',()=>{if(a===master){posEl.textContent=fmt(a.currentTime);
    if(a.duration)durEl.textContent=fmt(a.duration);}});
});
setInterval(()=>{if(!master||master.paused||!syncEl.checked)return;
  audios.forEach(b=>{if(b!==master&&b.readyState>=1&&Math.abs(b.currentTime-master.currentTime)>0.4){
    try{b.currentTime=master.currentTime}catch(e){}}});},500);
document.querySelectorAll('.jump a').forEach(a=>{
  a.addEventListener('click',ev=>{ev.preventDefault();
    const t=parseFloat(a.dataset.t)||0;
    if(master){setT(master,t);if(master.paused)master.play();}
    else{const first=audios[0];setT(first,t);first.play();}});
});
</script>
</body></html>"""
    dst = out_dir / "listen.html"
    dst.write_text(
        html.replace("__BODY__", "\n".join(cards))
            .replace("__DRY__", "\n".join(dry_cards))
            .replace("__SECS__", str(audio.get("seconds") or round(_duration(DRY_SRC), 2)))
            .replace("__SEED__", str(meta.get("seed")))
            .replace("__VG__", str(gains.get("vocal")))
            .replace("__AG__", str(gains.get("acc")))
            .replace("__SC1__", str(sc1.get("tier", "（未记录）")))
            .replace("__D1__", f"{sc1.get('max_abs_diff', float('nan')):.3g}")
            .replace("__C1__", f"{sc1.get('corr', float('nan')):.6f}")
            .replace("__SC2__", str(sc2.get("tier", "（未记录）")))
            .replace("__D2__", f"{sc2.get('max_abs_diff', float('nan')):.3g}")
            .replace("__C2__", f"{sc2.get('corr', float('nan')):.6f}")
            .replace("__INFO__", info_txt), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e3:.0f} KB · mp3 在同目录 audio/）")
    return dst


def _duration_from_mp3(path: Path) -> float:
    """mp3 时长（秒）——只用于页面显示，读不到就返回 0。"""
    try:
        ff = Path(hfb._ffmpeg())
        probe = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
        r = subprocess.run([str(probe), "-v", "error",
                            "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="《成都》整曲五臂 + 伴奏 A/B")
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args()

    if a.step in ("run", "all"):
        run(a.out, seed=a.seed)
    if a.step in ("page", "all"):
        build_page(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

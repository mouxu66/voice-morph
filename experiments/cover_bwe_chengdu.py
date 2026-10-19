# -*- coding: utf-8 -*-
"""《成都》A/B：UniverSR 神经带宽扩展「用 vs 不用」——唱素材罩得住的中音歌上。

为什么单做这一版（承接 m1q，2026-10-19）
    m1q 的六臂全在《好久不见》（低音歌，中位 147Hz）上测。用户当场追问：
    「我有提供唱歌的部分，只是没有太高音」——singdom 过采样后唱歌占比 24.6%、
    素材中位 130Hz。《成都》中位 183.8Hz 落在覆盖区内，所以这版问的是：
    **在我有唱歌训练数据的歌上，BWE 用 vs 不用差多少**。

单变量口径（与 m1q 相同，唯一差别是"8–12k 怎么来"）
    - 同一段干声：《成都》P0 原调**产品产物**（supp2026 / ir 0.5 / pitch 0）的
      rvc_dry 切段（不是重转的——重转会有 seed/worker 噪声，两个变量一起动）
    - 同一个 seed=1234、input_sr=16000（有效带宽 8k 截止）
    - A = 现役产品后期链；D1 = BWE + 同一条链；D2 = BWE + 同一条链去掉 +6dB@7k
    - 源（原唱同段）只做亮度参照，**不是真值**（"你唱这一句的 8–12k"不存在）
    - ★ 自检：A 臂与产品 `P0_原调_post.wav` 同段比对，两级判定——
        ① 逐样本一致（max|diff| ≤ 1e-3）；② 等效一致（corr ≥ 0.999 且
        max|diff| ≤ 0.05 且峰值差 ≤ 2%）。都不满足 → SystemExit。
      **为什么必须分级**（2026-10-19 实测）：产品后期版由 app cover 管线生成，
      与本机 ffmpeg 复现同一条链有**滤波器实现级**系统差异（max|diff| 0.027、
      corr 0.99995、峰值逐位相同、无时移；全曲跑链/2s 预热/44.1k 路径全部排除过）。
      要求逐样本一致会把这种实现差异误判成"暗换变量"；而 corr/峰值/幅度三级
      仍能一刀拦住真正的变量置换（换增益、换链、切错段）。

切段（`scan` 步骤实测，2026-10-19，0.5s 窗中位 + 静音门，全曲 641 窗）
    239.5s 起 28.0s：voiced 0.919 / f0 中位 183.9Hz（全曲 183.8）/ ≥300Hz 占比 0.0
    即整段落在唱歌素材覆盖区——「高音区音色覆盖」这个病在这一段上不犯。

用法：
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu.py scan   # 重扫最佳切段
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu.py run    # 跑臂（含 BWE）
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu.py page    # 出听感页
    ./.venv/Scripts/python.exe experiments/cover_bwe_chengdu.py all
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

import numpy as np  # noqa: E402

import cover_bwe_universr as bwe  # noqa: E402  复用 enhance()/_to_40k()/子进程边界
import cover_hf_bands as hfb  # noqa: E402  复用度量与 I/O（不另写一份尺子）
from cover_post_chain import apply_filters, post_filters  # noqa: E402

CHENGDU = ROOT / "media" / "covers" / "midsong_20261018" / "chengdu"
DRY_SRC = CHENGDU / "P0_原调_raw.wav"   # 产品干声（supp2026 / ir 0.5 / pitch 0）
POST_SRC = CHENGDU / "P0_原调_post.wav"  # 产品后期版（A 臂自检对照）
VOCAL_SRC = (ROOT / "media" / "covers" / "midsong_20261018" / "candidates"
             / "成都-赵雷" / "vocals.wav")
OUT_DIR = CHENGDU.parent / "chengdu_bwe"
REF = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"

EXCERPT_START = 239.5
EXCERPT_SECONDS = 28.0
SEED = 1234
INPUT_SR = 16000
#: 后期链（acompressor/deesser 是**有状态**滤波器）的预热前导：产品后期版是对全曲
#: 320s 跑链的（239.5s 处早已热机），对 28s 切片裸跑链会在开头 ~180ms（release）
#: 内不一致（实测 max|diff| 0.115 vs 预热后 0.027）。A/D 两臂用同一前导，保持单变量。
PRE_ROLL_S = 2.0

#: 现役产品链（arms.json `_meta.post_chain` 逐字一致：treble g=6 f=7000 + deess + comp）
PRODUCT_CHAIN = {"air_db": 6.0, "air_hz": 7000.0}
SELFCHECK_TOL = 1e-3   # ① 逐样本一致（合成/同构建 ffmpeg 可达）
SELFCHECK_EQUIV = dict(corr_min=0.999, max_diff=0.05, peak_rel=0.02)  # ② 等效一致

#: 臂 → 文件名。★ run 与 page 只认这张表（2026-10-19 在 hf_bands 上踩过：
#: page 单独跑会读到没过后期链的中间产物，读数"看起来正常"但整臂错）。
ARM_FILES: dict[str, str] = {
    "00_source": "00_source_40k.wav",
    "A_dry": "A_dry_post.wav",
    "D1_bwe": "D1_bwe_post.wav",
    "D2_bwe_no_air": "D2_bwe_no_air_post.wav",
}
ROWS = [
    ("00_source", "① 原唱这一段（亮度参照，非真值）", "源的高频长什么样"),
    ("A_dry", "② A · 现役产品链（不用 UniverSR）", "你在《成都》页听过的 P0 后期版"),
    ("D1_bwe", "③ D1 · UniverSR + 同一条产品链 ★", "唯一差别：8–12k 由模型生成"),
    ("D2_bwe_no_air", "④ D2 · UniverSR + 去掉 +6dB@7k", "已有真高频后，这个 shelf 还该不该留"),
]


# ---------------------------------------------------------------- 纯函数（可测）

def cut_excerpt(y: np.ndarray, sr: int, start: float, seconds: float) -> np.ndarray:
    """按时间切段（样本数取整到最近样本；越界时断言，不静默截短）。"""
    i0 = int(round(start * sr))
    i1 = int(round((start + seconds) * sr))
    if i0 < 0 or i1 > len(y):
        raise ValueError(f"切段越界：[{i0}, {i1}) 不在 [0, {len(y)})")
    return y[i0:i1]


def pick_excerpt(f0: np.ndarray, vflag: np.ndarray, *, frame_hz: float, target_hz: float,
                 seconds: float, min_voiced: float = 0.6, stride_s: float = 0.5,
                 max_frac_ge: float = 0.35, ge_hz: float = 300.0) -> dict:
    """在整曲 f0/浊音序列里挑"最像目标音高、且不太高"的窗口。

    评分 = voiced 占比 × (1 − |中位 − 目标|/150)，并要求 ≥300Hz 帧占比不超过
    `max_frac_ge`（这一段要落在**唱素材覆盖区**内，不是去探高音天花板）。
    纯函数：不读文件、不 import 音频库，测试用合成序列直接喂。
    """
    total_s = len(f0) / frame_hz
    win = int(round(seconds * frame_hz))
    stride = max(1, int(round(stride_s * frame_hz)))
    if win <= 0 or win > len(f0):
        raise ValueError(f"窗口 {seconds}s 超出序列长度 {total_s:.1f}s")
    best: dict | None = None
    for s in range(0, len(f0) - win + 1, stride):
        vv = vflag[s:s + win]
        ff = f0[s:s + win]
        voiced = float(np.mean(vv))
        if voiced < min_voiced:
            continue
        med = float(np.nanmedian(ff[vv])) if np.any(vv) else float("nan")
        if not np.isfinite(med):
            continue
        ge = float(np.mean(ff[vv] >= ge_hz))
        if ge > max_frac_ge:
            continue
        score = voiced * (1.0 - abs(med - target_hz) / 150.0)
        cand = {"start": round(s / frame_hz, 2), "voiced": round(voiced, 3),
                "f0_median_hz": round(med, 1), "frac_ge300": round(ge, 3),
                "score": round(score, 4)}
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is None:
        raise ValueError(f"找不到满足 voiced≥{min_voiced} 且 ≥{ge_hz}Hz≤{max_frac_ge} 的窗口")
    return best


def excerpt_stats(y: np.ndarray, sr: int) -> dict:
    """切段的 voiced 占比 / f0 中位 / ≥300Hz 占比（pyin，16k 上跑，~5s）。"""
    # librosa 是 lazy_loader：`import librosa` 不触发导入，**首次属性访问**才发
    # importlib.resources 的 DeprecationWarning，而本项目把它升级成 error
    # （pyproject filterwarnings）→ 屏蔽必须包住**调用**，只包 import 不够
    # （与 cover_space_double.py:212 同一坑，2026-10-18 实测）。
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import librosa

        y16 = librosa.resample(np.asarray(y, dtype=np.float32), orig_sr=sr, target_sr=16000)
        f0, vflag, _ = librosa.pyin(y16, sr=16000, fmin=100, fmax=320,
                                    frame_length=2048, hop_length=256)
    v = np.asarray(vflag, dtype=bool)
    med = float(np.nanmedian(f0[v])) if np.any(v) else float("nan")
    return {"voiced": round(float(np.mean(v)), 3), "f0_median_hz": round(med, 1),
            "frac_ge300": round(float(np.mean(f0[v] >= 300.0)), 3) if np.any(v) else None}


def _embed_mp3(path: Path) -> str:
    """wav → 128k mp3 → base64 data URI（页面自包含、可整体转发）。"""
    tmp = path.with_suffix(".page_tmp.mp3")
    subprocess.run([hfb._ffmpeg(), "-y", "-loglevel", "error", "-i", str(path),
                    "-b:a", "128k", str(tmp)], check=True, timeout=300)
    b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
    tmp.unlink()
    return b64


# ---------------------------------------------------------------- 跑臂

def run(out_dir: Path = OUT_DIR, *, start: float = EXCERPT_START,
        seconds: float = EXCERPT_SECONDS, seed: int = SEED) -> dict[str, Path]:
    """A（不用 BWE）/ D1（BWE + 产品链）/ D2（BWE 去空气 EQ）三臂 + 源参照。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dry, dsr = hfb._read(DRY_SRC)
    if dsr != 40000:
        raise SystemExit(f"干声采样率不是 40k：{dsr}")
    if start < PRE_ROLL_S:
        raise SystemExit(f"切段起点 {start}s < 预热前导 {PRE_ROLL_S}s，往前挪")
    exc = cut_excerpt(dry, dsr, start, seconds)
    dry_path = out_dir / "rvc_dry_excerpt.wav"
    hfb._write(dry_path, exc, dsr)
    # 链路输入 = 预热前导 + 切段（A/D 两臂共用同一段前导）
    pr = cut_excerpt(dry, dsr, start - PRE_ROLL_S, seconds + PRE_ROLL_S)
    pr_path = out_dir / "rvc_dry_preroll.wav"
    hfb._write(pr_path, pr, dsr)

    src40 = hfb._to_40k_mono(VOCAL_SRC, out_dir / ARM_FILES["00_source"],
                             start=start, seconds=seconds)
    print(f"[切段] {start}s+{seconds}s · {exc.shape[0] / dsr:.2f}s · 预热 {PRE_ROLL_S}s · "
          f"源参照 {src40.name}")

    chain = post_filters(**PRODUCT_CHAIN)
    chain_no_air = post_filters(**{**PRODUCT_CHAIN, "air_db": 0.0})

    def _chain_then_cut(src: Path, dst: Path, filters: str) -> None:
        """跑后期链 → 切掉预热前导 → 落盘（A/D 两臂同一处理，保持单变量）。"""
        full = src.with_suffix(".chain_full.wav")
        apply_filters(src, full, filters)
        y, s = hfb._read(full)
        hfb._write(dst, cut_excerpt(y, s, PRE_ROLL_S, seconds), s)
        full.unlink()

    a_path = out_dir / ARM_FILES["A_dry"]
    _chain_then_cut(pr_path, a_path, chain)

    # ★ 自检：A 臂必须与产品后期版一致（逐样本 or 等效）——证明没有暗换变量
    prod, psr = hfb._read(POST_SRC)
    ref_exc = cut_excerpt(prod, psr, start, seconds)
    got, _ = hfb._read(a_path)
    n = min(len(got), len(ref_exc))
    diff = float(np.max(np.abs(got[:n] - ref_exc[:n]))) if n else float("inf")
    corr = float(np.corrcoef(got[:n], ref_exc[:n])[0, 1]) if n > 1 else 0.0
    pk_a = float(np.max(np.abs(got))) if len(got) else 0.0
    pk_p = float(np.max(np.abs(ref_exc))) if len(ref_exc) else 0.0
    peak_rel = abs(pk_a - pk_p) / max(pk_p, 1e-9)
    eq = SELFCHECK_EQUIV
    exact = len(got) == len(ref_exc) and diff <= SELFCHECK_TOL
    equiv = (len(got) == len(ref_exc) and corr >= eq["corr_min"]
             and diff <= eq["max_diff"] and peak_rel <= eq["peak_rel"])
    if not (exact or equiv):
        raise SystemExit(
            f"A 臂与产品后期版不一致（len {len(got)} vs {len(ref_exc)}，"
            f"max|diff| {diff:.6g}，corr {corr:.6f}，峰值 {pk_a:.4f} vs {pk_p:.4f}）"
            f"——既不逐样本一致也不等效，链路被暗换，先查原因再跑")
    tier = "逐样本一致" if exact else "等效一致（滤波器实现级差异）"
    print(f"[自检] A 臂 vs 产品 P0_原调_post.wav 同段：{tier}"
          f"（max|diff| {diff:.3g}，corr {corr:.5f}，峰值 {pk_a:.3f}/{pk_p:.3f}）✓")

    up48 = out_dir / "D_bwe_48k.wav"
    info = bwe.enhance(pr_path, up48, input_sr=INPUT_SR, seed=seed)
    print(f"[UniverSR] {info['seconds_in']}s → 加载 {info['load_s']}s + 推理 {info['run_s']}s"
          f"（{info['ode']}，seed {info['seed']}，peak {info['peak']}）")
    d40 = bwe._to_40k(up48, out_dir / "D_bwe_40k.wav")

    d1_path = out_dir / ARM_FILES["D1_bwe"]
    d2_path = out_dir / ARM_FILES["D2_bwe_no_air"]
    _chain_then_cut(d40, d1_path, chain)
    _chain_then_cut(d40, d2_path, chain_no_air)

    stats = excerpt_stats(exc, dsr)
    arms = {k: out_dir / v for k, v in ARM_FILES.items()}
    # ★ 臂清单与臂产物同一次写（10-18 教训：arms.json 漏存 src 臂 → 页面 KeyError）
    (out_dir / "arms.json").write_text(json.dumps({
        "song": "成都-赵雷", "voice": "myvoice_supp2026", "index_rate": 0.5, "pitch": 0,
        "dry_src": str(DRY_SRC), "vocal_src": str(VOCAL_SRC),
        "start": start, "seconds": seconds, "pre_roll_s": PRE_ROLL_S,
        "chain": chain, "chain_no_air": chain_no_air,
        "seed": seed, "input_sr": INPUT_SR, "univer": info,
        "selfcheck": {"max_abs_diff": diff, "corr": corr, "peak_a": pk_a,
                      "peak_product": pk_p, "peak_rel": peak_rel, "tier": tier},
        "excerpt": stats,
        "arms": {k: str(v) for k, v in arms.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[臂] {len(arms)} 条 → {out_dir} · 切段 {stats}")
    return arms


# ---------------------------------------------------------------- 出页

def build_page(out_dir: Path = OUT_DIR, *, ref: Path | None = REF) -> Path:
    out_dir = Path(out_dir)
    meta = json.loads((out_dir / "arms.json").read_text(encoding="utf-8"))
    src_hf: float | None = None
    report: dict[str, dict] = {}
    cards: list[str] = []
    for key, title, sub in ROWS:
        p = out_dir / ARM_FILES[key]
        if not p.exists():
            cards.append(f"<div class='card'><div class='t'>{title}</div>"
                         f"<span class='missing'>（缺文件）</span></div>")
            continue
        m = hfb.metrics(p, ref)
        if key == "00_source":
            src_hf = m.get("hf_rel0_4k_db")
        elif src_hf is not None and m.get("hf_rel0_4k_db") is not None:
            m["hf_gap_vs_src_db"] = round(float(m["hf_rel0_4k_db"]) - float(src_hf), 2)
        report[key] = m
        note = (f"4–8k {m.get('mid_rel0_4k_db')}dB · 8–12k {m.get('hf_rel0_4k_db')}dB · "
                f"离源亮度 {m.get('hf_gap_vs_src_db')}dB · 质心 {m.get('centroid_hz')}Hz · "
                f"rolloff95 {m.get('rolloff95_hz')}Hz · 斜率 {m.get('slope_1k_8k')} · "
                f">8k 调制CV {m.get('hf_mod_cv')} · 像你 {m.get('cos')}")
        cards.append(f"<div class='card'><div class='t'>{title}</div>"
                     f"<div class='s'>{sub}<br>{note}</div>"
                     f"<audio controls preload='metadata' src='{_embed_mp3(p)}'></audio></div>")

    info = meta.get("univer") or {}
    exc = meta.get("excerpt") or {}
    sc = meta.get("selfcheck") or {}
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>成都 A/B · UniverSR 用 vs 不用</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:900px;margin:24px auto;padding:0 16px;line-height:1.7}
 h1{font-size:21px} .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;
 padding:12px 14px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px} .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 audio{width:100%;height:34px} .missing{color:#b00;font-size:13px}
 .sub{color:#555;font-size:13.5px} footer{margin:28px 0;color:#888;font-size:12px}
</style></head><body>
<h1>《成都》A/B · UniverSR <b>用 vs 不用</b>（唱素材罩得住的中音歌）</h1>
<p class="sub">为什么是这首歌：m1q 的六臂全在低音歌《好久不见》（147Hz）上测。本页换成<b>中位 183.8Hz 的《成都》</b>，
切段 __START__s 起 __SECONDS__s（voiced __VOICED__、f0 中位 __MED__Hz = 全曲中位、≥300Hz 占比 __GE__）——
整段落在唱歌素材覆盖区（素材中位 130Hz、singdom 过采样后唱歌占 24.6%），<b>「高音区音色覆盖」这个病在这一段不犯</b>。</p>
<p class="sub">单变量：同一段产品干声（supp2026 / ir 0.5 / pitch 0 的 P0 原调切段）、同一条现役产品后期链
（+6dB@7k + De-Ess + 轻压缩）、同一个 seed，<b>唯一差别是 8–12k 怎么来</b>。A 臂已与产品后期版同段自检：
__TIER__（max|diff| __DIFF__、corr __CORR__、峰值 __PKA__/__PKP__ —— 产品由 app 管线生成，与本机 ffmpeg 有滤波器实现级差异，不影响 A/B 单变量）。</p>
<p class="sub">请只听三件事：<b>① ② → ③ 是不是真的"亮而不沙"，高频里有没有凭空编出来的嘶声/金属味</b>（神经超分最典型的失败形态）；
<b>② ③ ④ 哪个更自然</b>（已有真高频之后，那个 +6dB@7k 还该不该留）；
<b>③ 整体有没有"更像一个人在对你唱"</b>。都是干声口径（后期链后、未混伴奏），周期度/包络类指标必须干声才准。</p>
<p class="sub">口径：① 原唱那条<b>不是真值</b>（这是你的音色唱别人的歌），只给亮度参照；
「像你」只做同输入同 seed 的相对比较（CAM++ 是语音尺子，歌声上不可读绝对值）。
本次推理：__INFO__</p>
__BODY__
<footer>2026-10-19 · experiments/cover_bwe_chengdu.py · 模型 woongzip1/universr-audio · 40kHz 单声道 · seed __SEED__</footer>
</body></html>"""
    info_txt = (f"{info.get('seconds_in')}s 音频 → 加载 {info.get('load_s')}s + 推理 {info.get('run_s')}s"
                f"（{info['ode']}，seed {info['seed']}）") if info else "（未记录）"
    dst = out_dir / "listen.html"
    dst.write_text(html
                   .replace("__START__", str(meta.get("start")))
                   .replace("__SECONDS__", str(meta.get("seconds")))
                   .replace("__VOICED__", str(exc.get("voiced")))
                   .replace("__MED__", str(exc.get("f0_median_hz")))
                   .replace("__GE__", str(exc.get("frac_ge300")))
                   .replace("__TIER__", str(sc.get("tier", "（未记录）")))
                   .replace("__DIFF__", f"{sc.get('max_abs_diff', float('nan')):.3g}")
                   .replace("__CORR__", f"{sc.get('corr', float('nan')):.5f}")
                   .replace("__PKA__", f"{sc.get('peak_a', float('nan')):.3f}")
                   .replace("__PKP__", f"{sc.get('peak_product', float('nan')):.3f}")
                   .replace("__INFO__", info_txt)
                   .replace("__SEED__", str(meta.get("seed")))
                   .replace("__BODY__", "\n".join(cards)), encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


# ---------------------------------------------------------------- scan

def scan(*, start_hint: float = EXCERPT_START) -> list[dict]:
    """整曲扫一遍 0.5s 窗，打印最佳切段候选（改默认切段前先跑它）。"""
    import warnings

    y, sr = hfb._read(DRY_SRC)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import librosa

        y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)
        f0, vflag, _ = librosa.pyin(y16, sr=16000, fmin=100, fmax=320,
                                    frame_length=2048, hop_length=256)
    frame_hz = 16000 / 256
    win = int(round(0.5 * frame_hz))
    cands: list[dict] = []
    for s in range(0, len(f0) - win + 1, win):
        vv = np.asarray(vflag[s:s + win], dtype=bool)
        ff = f0[s:s + win]
        if not np.any(vv):
            continue
        cands.append({"start": round(s / frame_hz, 2),
                      "voiced": round(float(np.mean(vv)), 3),
                      "f0_median_hz": round(float(np.nanmedian(ff[vv])), 1),
                      "frac_ge300": round(float(np.mean(ff[vv] >= 300.0)), 3)})
    # 用同一把尺子复算默认切段的分数，确认 scan 与 pick_excerpt 口径一致
    f0s = np.asarray(f0, dtype=float)
    vfs = np.asarray(vflag, dtype=bool)
    try:
        best = pick_excerpt(f0s, vfs, frame_hz=frame_hz, target_hz=183.8,
                            seconds=EXCERPT_SECONDS)
        print(f"[pick_excerpt] {best}")
    except ValueError as e:
        print(f"[pick_excerpt] {e}")
    cands.sort(key=lambda c: -c["voiced"] * (1 - abs(c["f0_median_hz"] - 183.8) / 150))
    print(f"[scan] 前 8 个 0.5s 窗（默认切段附近 hint={start_hint}s）：")
    for c in cands[:8]:
        print(f"  {c}")
    return cands


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="《成都》UniverSR 用 vs 不用 A/B")
    ap.add_argument("step", choices=["scan", "run", "page", "all"], nargs="?", default="all")
    ap.add_argument("--start", type=float, default=EXCERPT_START)
    ap.add_argument("--seconds", type=float, default=EXCERPT_SECONDS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args()

    if a.step == "scan":
        scan()
    if a.step in ("run", "all"):
        run(a.out, start=a.start, seconds=a.seconds, seed=a.seed)
    if a.step in ("page", "all"):
        build_page(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

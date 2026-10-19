# -*- coding: utf-8 -*-
"""8–12k 缺口怎么补：源高频移植 vs 谱折叠（三臂，同一后期链）。

### 问题（2026-10-19，从 m1o 的"域"结论继续）
现役后期链只是 `treble=g=…:f=9000` 把**已有的**高频抬起来。但 RVC 输出的 8–12k
本来就比源低得多（源 −24.5dB、输出 −53.7dB，相对 0–4k），**放大不能造出不存在的东西**。
所以要区别两件完全不同的事：
    · 抬 EQ（放大残留）：还能更亮，但残留里的成分比例没变；
    · **带宽扩展**（造高频）：从别处拿 8–12k，或者用自己 4–8k 折上去。
本实验把这三者放在同一条后期链下比。

### 三臂（单变量：**只改 8–12k 怎么来的**，其他全同）
| 臂 | 8–12k 的来源 |
|---|---|
| A | 无（现役链：只有 treble 抬已有残留） |
| B | **源高频移植**：源人手 8–12k 带通取出，按「4–8k 电平」匹配后叠回 |
| C | **谱折叠 BWE**：把输出自己的 4–8k **镜像**到 8–12k（16k−f），同一目标电平 |

B/C 共享**同一个目标亮度**（由源的 8–12k/4–8k 比值决定）与**同一个门控**
（都只在输出的 4–8k 有声处注入）—— 这样两臂的差别只能是**高频内容从哪来**，
而不是"哪个更亮"或"哪个注入得更多"。

### ⚠️ 这不是产品加工，能不能进链路要过耳朵
- 源高频是**原唱的**：移植进去会带原唱齿音/换气，可能"更亮但更像别人"（cos 会掉）；
- 谱折叠会带来**非谐波成分**（8–12k 与基频不成整数比）→ 可能听着发"沙"或"电话音";
- 神经带宽扩展**不在本实验里**：本机没有可用的 BWE 权重，硬上一个没标定的模型
  等于把两个变量一起动。它作为"C′"待定，结论里明确写"未测"。

指标口径（沿用项目既有尺子，不新造）：
    · `cover_metrics.frame_spectrum` → 4–8k / 8–11k 相对 0–4k 的 dB、谱质心、rolloff95
    · 1k→8k 滚降斜率（dB/oct，越大越亮）：`rolloff_slope_db_per_oct`
    · >8k 包络调制 CV：`modulation_cv`（越高越"抖"，合成痕迹的一种）
    · CAM++ 声纹 cos（对训练参考音）= "像你"，**只做同输入同 seed 的相对比较**
      （它是语音尺子，歌声上绝对值不可读，见 `cover_metrics` 模块头）

用法（`.venv`，走 rvc_convert 常驻 worker）：
    ./.venv/Scripts/python.exe experiments/cover_hf_bands.py run   # 转换 + 三臂
    ./.venv/Scripts/python.exe experiments/cover_hf_bands.py page  # 指标 + listen.html
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server", ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 目标频段（Hz）：缺口就在这一段
HF_LO, HF_HI = 8000.0, 12000.0
#: 电平锚点频段：移植/折叠的电平都按它匹配（"按 4–8k 电平匹配"）
MID_LO, MID_HI = 4000.0, 8000.0
#: 注入门控：输出的 4–8k 包络低于峰值这么多 dB 的帧不注入（防把气口/静音变沙）
GATE_FLOOR_DB = -45.0
#: 帧长/帧移（秒）—— 包络与门控的时间分辨率
FRAME_S, HOP_S = 0.020, 0.010
#: 峰安全线：注入后超过就整段等比缩（不做限幅，限幅会造新谐波）
PEAK_CEILING = 0.99

VOICE = "myvoice_supp2026"
#: 默认输入：BS Roformer 抠出的干净主唱（185–210s，25s）——与 cover_post_chain 同一条
DEFAULT_SOURCE = ROOT / "media" / "covers" / "song2" / "f0sweep" / "blind3" / "raw" / "bs_frazer_becruily_lead.wav"
OUT_DIR = ROOT / "media" / "covers" / "song2" / "hf8k"


# ---------------------------------------------------------------- 纯函数（可单测）

def bandpass(x: np.ndarray, sr: int, lo: float, hi: float, order: int = 4) -> np.ndarray:
    """零相位带通（Butterworth + `filtfilt`），返回与 `x` 等长的 float32。

    ★ 必须零相位：本实验的产物要**逐样本相加**回干声，如果滤波器带相位延迟，
      注入的 8–12k 会相对干声"迟到"几毫秒 —— 听感变成"沙沙的尾音"，
      而指标（只看能量）完全正常。`filtfilt` 前后向各滤一次，零相位。
    """
    from scipy.signal import butter, filtfilt

    ny = sr / 2.0
    hi = min(hi, ny * 0.99)
    if lo <= 0:
        b, a = butter(order, hi / ny, btype="low")
    else:
        b, a = butter(order, [lo / ny, hi / ny], btype="band")
    y = filtfilt(b, a, np.asarray(x, dtype=np.float64))
    return y.astype(np.float32)


def rms(x: np.ndarray) -> float:
    """整段 RMS。空数组返回 0（调用方自己决定 eps 怎么兜）。"""
    a = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _frame_rms(x: np.ndarray, sr: int, frame_s: float = FRAME_S, hop_s: float = HOP_S) -> np.ndarray:
    """无窗逐帧 RMS（帧长/帧移见模块常量）。"""
    n, hop = max(1, int(frame_s * sr)), max(1, int(hop_s * sr))
    a = np.ascontiguousarray(np.asarray(x, dtype=np.float64))  # ★ 先转 float64 再取步长，
    if len(a) < n:                                             #   不能用 float32 的 strides
        return np.array([rms(a)], dtype=np.float64)
    win = np.lib.stride_tricks.as_strided(
        a, shape=((len(a) - n) // hop + 1, n), strides=(a.strides[0] * hop, a.strides[0])
    )
    return np.sqrt((win * win).mean(axis=1))


def gate_gain(x: np.ndarray, sr: int, *, floor_db: float = GATE_FLOOR_DB) -> np.ndarray:
    """逐样本门控增益（0–1）：包络低于「峰值 − floor_db」的地方关闭，边界线性过渡。

    ★ 用 `np.interp` 把帧级门控升到样本级，而不是在帧边界硬切换：
      硬切会在每个 20ms 边界造一个冲击，8–12k 的冲击听起来就是"咔"。
    """
    env = _frame_rms(x, sr)
    if env.size == 0 or env.max() <= 0:
        return np.zeros(len(x), dtype=np.float32)
    thr = env.max() * (10 ** (floor_db / 20.0))
    m = (env >= thr).astype(np.float64)
    # 帧中心时刻 → 样本时刻
    hop = max(1, int(HOP_S * sr))
    centers = np.arange(len(m)) * hop + max(1, int(FRAME_S * sr)) / 2.0
    centers = np.clip(centers, 0, len(x) - 1)
    g = np.interp(np.arange(len(x), dtype=np.float64), centers, m, left=0.0, right=0.0)
    return g.astype(np.float32)


def _peak_guard(y: np.ndarray, ceiling: float = PEAK_CEILING) -> tuple[np.ndarray, float]:
    """超峰等比缩，返回 (信号, 用的增益)。增益 1.0 = 没动。"""
    pk = float(np.max(np.abs(y))) if y.size else 0.0
    if pk <= ceiling or pk <= 0:
        return y, 1.0
    g = ceiling / pk
    return (y * g).astype(np.float32), float(g)


def hf_target_ratio(src: np.ndarray, sr: int) -> float:
    """源的「8–12k / 4–8k」RMS 比 —— 三臂共享的目标亮度。"""
    hf, mid = bandpass(src, sr, HF_LO, HF_HI), bandpass(src, sr, MID_LO, MID_HI)
    return rms(hf) / max(rms(mid), 1e-12)


def hf_transplant(dry: np.ndarray, src: np.ndarray, sr: int, *,
                  ratio: float | None = None, gate_db: float = GATE_FLOOR_DB) -> tuple[np.ndarray, dict]:
    """B 臂：把**源**的 8–12k 按 4–8k 电平匹配后叠进 `dry`。

    注入量 = rms(dry 4–8k) × ratio（ratio 默认取源自己的 8–12k/4–8k），
    所以"有多亮"由目标决定、"什么成分"由源决定 —— 与 C 臂唯一不同的就是后者。
    """
    y = np.asarray(dry, dtype=np.float32)
    s = np.asarray(src, dtype=np.float32)
    n = min(len(y), len(s))
    if n < len(y) or n < len(s):
        y, s = y[:n], s[:n]
    if ratio is None:
        ratio = hf_target_ratio(s, sr)
    src_hf = bandpass(s, sr, HF_LO, HF_HI)
    dry_mid = bandpass(y, sr, MID_LO, MID_HI)
    want = rms(dry_mid) * float(ratio)
    have = rms(src_hf)
    gain = want / have if have > 0 else 0.0
    inj = src_hf * gain * gate_gain(y, sr, floor_db=gate_db)
    out, pk_gain = _peak_guard(y + inj)
    return out, {"arm": "src_hf", "ratio": round(float(ratio), 5), "inject_gain": round(float(gain), 4),
                 "inject_rms": round(float(rms(inj)), 6), "peak_gain": round(pk_gain, 4),
                 "trimmed_samples": int(len(dry) - n)}


def spectral_fold(dry: np.ndarray, sr: int, *, ratio: float,
                  gate_db: float = GATE_FLOOR_DB,
                  src_band: tuple[float, float] = (MID_LO, MID_HI)) -> tuple[np.ndarray, dict]:
    """C 臂：把输出自己的 4–8k **镜像折叠**到 8–12k（bin `f` ← `16k − f`）。

    折叠（而非移调复制）保持频谱的**镜像对称**，所以 8–12k 的能量分布与 4–8k 同形，
    不会凭空造出一条"第二条谐波序列"（移调复制才有可能）。
    电平同样按 4–8k × `ratio` 匹配 → 与 B 臂同一目标亮度。
    """
    y = np.asarray(dry, dtype=np.float32)
    n = len(y)
    X = np.fft.rfft(y.astype(np.float64))
    fr = np.fft.rfftfreq(n, 1 / sr)
    fold_sum = 2 * HF_LO  # 镜像轴：8k ← 8k（16k − f）
    dst = (fr >= HF_LO) & (fr < HF_HI)
    # ★ 必须向量化：8–12k 在 25s/40k 上是 ~10 万个 bin，逐个 `argmin` 去找镜像
    #   就是 10 万 × 50 万 的比较（2026-10-19 实测卡死 3 分钟以上）。
    j = np.round((fold_sum - fr[dst]) * n / sr).astype(np.int64)
    ok = (j > 0) & (j < len(X))
    inj_f = np.zeros_like(X)
    inj_f[np.flatnonzero(dst)[ok]] = X[j[ok]]
    fold = np.fft.irfft(inj_f, n=n).astype(np.float32)
    dry_mid = bandpass(y, sr, MID_LO, MID_HI)
    want = rms(dry_mid) * float(ratio)
    have = rms(fold)
    gain = want / have if have > 0 else 0.0
    inj = fold * gain * gate_gain(y, sr, floor_db=gate_db)
    out, pk_gain = _peak_guard(y + inj)
    return out, {"arm": "fold", "ratio": round(float(ratio), 5), "inject_gain": round(float(gain), 4),
                 "inject_rms": round(float(rms(inj)), 6), "peak_gain": round(pk_gain, 4)}


# ---------------------------------------------------------------- 指标（薄封装）

def rel_band_db(x: np.ndarray, sr: int, band: tuple[float, float],
                ref_band: tuple[float, float] = (0.0, 4000.0)) -> float:
    """`band` 相对 `ref_band` 的 RMS dB（正 = 该频段比参考带响）。"""
    a, b = bandpass(x, sr, *band), bandpass(x, sr, *ref_band)
    ra = rms(a)
    rb = max(rms(b), 1e-12)
    return round(20 * float(np.log10(max(ra, 1e-12) / rb)), 2)


def modulation_cv(x: np.ndarray, sr: int, band: tuple[float, float] = (HF_LO, HF_HI)) -> float:
    """>8k 包络的变异系数（标准差/均值）—— 合成痕迹越高越可疑。"""
    env = _frame_rms(bandpass(x, sr, *band), sr)
    env = env[env > env.max() * 0.01] if env.size else env
    if env.size < 3 or env.mean() <= 0:
        return 0.0
    return round(float(env.std() / env.mean()), 4)


def band_psd(x: np.ndarray, sr: int, center: float, *, half_oct: float = 0.25) -> float:
    """**带宽公平**的频带幅度：`rms(x) / sqrt(带宽)`（即局部功率谱密度的平方根）。

    ★ 为什么不能直接比两个任意宽度的带（2026-10-19 实测踩过）：白噪声在 1–2k（1 个倍频、
      1000Hz 宽）与 8–12k（4000Hz 宽）的 RMS 天然差 6dB —— 直接相减会得到
      "谱斜率 +2.15 dB/oct"，而白噪声的真实斜率是 0。所以两侧都用**同一个
      `±half_oct` 的窄带**（默认 ±1/4 倍频），带宽相等，比值才是斜率。
    """
    lo, hi = center * 2 ** (-half_oct), center * 2**half_oct
    bw = max(hi - lo, 1.0)
    return rms(bandpass(x, sr, lo, hi)) / np.sqrt(bw)


def rolloff_slope_db_per_oct(x: np.ndarray, sr: int) -> float:
    """1k→8k 的滚降斜率（dB/oct），带宽公平版（见 `band_psd`）。"""
    p1, p2 = band_psd(x, sr, 1000.0), band_psd(x, sr, 8000.0)
    octs = 3.0  # log2(8000/1000)
    return round(20 * float(np.log10(max(p2, 1e-15) / max(p1, 1e-15))) / octs, 3)


def metrics(path: Path, ref: Path | None = None) -> dict:
    """一条产物的全部读数（音频格式统一：40k 单声道 wav）。"""
    import soundfile as sf

    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = x.mean(1)
    out = {"sr": int(sr), "seconds": round(len(y) / sr, 2),
           "hf_rel0_4k_db": rel_band_db(y, sr, (HF_LO, HF_HI)),
           "mid_rel0_4k_db": rel_band_db(y, sr, (MID_LO, MID_HI)),
           "hf_over_mid_db": round(rel_band_db(y, sr, (HF_LO, HF_HI), (MID_LO, MID_HI)), 2),
           "hf_mod_cv": modulation_cv(y, sr),
           "slope_1k_8k": rolloff_slope_db_per_oct(y, sr)}
    try:
        import cover_metrics

        out.update({k: v for k, v in cover_metrics.frame_spectrum(path).items()
                    if k in ("centroid_hz", "rolloff95_hz", "band_db", "voiced_frames")})
        if ref is not None and ref.exists():
            out["cos"] = cover_metrics.secs(path, ref)
    except Exception as e:  # noqa: BLE001
        out["metrics_error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------- 跑臂 / 出页

def _ffmpeg() -> str:
    import common

    return common.find_ffmpeg()


def _read(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return d.mean(1).astype(np.float32), int(sr)


def _write(path: Path, y: np.ndarray, sr: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(y, dtype=np.float32), sr, subtype="PCM_16")


def _to_40k_mono(src: Path, dst: Path, *, start: float = 0.0, seconds: float = 0.0) -> Path:
    """统一成 40k 单声道 wav（与 RVC 输出同采样率，才能逐样本叠加）。"""
    cmd = [_ffmpeg(), "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-ar", "40000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True, timeout=300)
    return dst


def run_arms(*, source: Path, start: float, seconds: float, voice: str = VOICE,
             out_dir: Path = OUT_DIR) -> dict:
    """转换 + 三臂（A 现役链 / B 源高频 / C 谱折叠），返回 {臂: 路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    src40 = _to_40k_mono(source, out_dir / "00_source_40k.wav", start=start, seconds=seconds)
    src, sr = _read(src40)
    print(f"[输入] {source.name} → {src40.name}（{sr}Hz、{len(src) / sr:.1f}s）")

    import rvc_convert

    converted = rvc_convert.rvc_convert(src40, voice, pitch=0, index_rate=0.5)
    dry, dsr = _read(converted)
    if dsr != sr:
        raise SystemExit(f"采样率不一致：源 {sr} vs RVC 输出 {dsr}")
    _write(out_dir / "rvc_dry.wav", dry, sr)
    print(f"[RVC] {voice} → rvc_dry.wav（{len(dry) / sr:.1f}s）")

    ratio = hf_target_ratio(src, sr)
    print(f"[目标亮度] 源 8–12k/4–8k = {ratio:.4f}（{20 * np.log10(max(ratio, 1e-12)):.1f} dB）")

    b, meta_b = hf_transplant(dry, src, sr, ratio=ratio)
    c, meta_c = spectral_fold(dry, sr, ratio=ratio)
    _write(out_dir / "B_src_hf.wav", b, sr)
    _write(out_dir / "C_fold.wav", c, sr)
    print(f"[B] {meta_b}\n[C] {meta_c}")

    # 链前产物（留档，便于查"是注入的问题还是后期链的问题"）
    dry_src = {"A_dry": out_dir / "rvc_dry.wav", "B_src_hf": out_dir / "B_src_hf.wav",
               "C_fold": out_dir / "C_fold.wav"}
    # 三臂 + 源一起过同一条现役后期链（A 臂就是"现役产品行为"）
    from cover_post_chain import apply_filters, post_filters

    chain = post_filters()
    dsts = arm_paths(out_dir)
    for key, src_path in dry_src.items():
        apply_filters(src_path, dsts[key], chain)
    apply_filters(src40, out_dir / "00_source_post.wav", chain)
    arms = dict(dsts)
    (out_dir / "hf_arms.json").write_text(json.dumps(
        {"voice": voice, "source": str(source), "start": start, "seconds": seconds,
         "chain": chain, "ratio": round(float(ratio), 5), "B": meta_b, "C": meta_c,
         "arms": {k: str(v) for k, v in arms.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[臂] {len(arms)} 条 → {out_dir}")
    return arms


#: 臂 → **文件名**。★ 页面只认这张表：run 与 page 必须读写同一批路径，
#: 否则 `page` 单独跑时会去读
#: **没过后期的中间产物**（2026-10-19 实测踩过：第二遍 `page` 把 B/C 读成了链前版本，
#: 4–8k 整整差 3.4dB，而 A 臂因为文件名不同直接报「缺文件」——读数看起来仍然"正常"）。
ARM_FILES: dict[str, str] = {
    "00_source": "00_source_40k.wav",
    "A_dry": "A_dry_post.wav",
    "B_src_hf": "B_src_hf_post.wav",
    "C_fold": "C_fold_post.wav",
}


def arm_paths(out_dir: Path) -> dict[str, Path]:
    """规范路径表（run 写完、page 再读，两边只认它）。"""
    return {k: out_dir / v for k, v in ARM_FILES.items()}


ROWS = [
    ("00_source", "① 原唱这一段（参照，非目标）", "源本身的高频长什么样"),
    ("A_dry", "② A · 现役链（只抬已有残留）", "当前产品行为"),
    ("B_src_hf", "③ B · 源高频移植 + 现役链", "8–12k 来自原唱"),
    ("C_fold", "④ C · 谱折叠 BWE + 现役链", "8–12k 由自己的 4–8k 镜像而来"),
]


def build_page(arms: dict[str, Path], out_dir: Path = OUT_DIR) -> Path:
    """听感页：每臂一条音频 + 读数 + 只问一件事（亮得自不自然）。"""
    ref = ROOT / "media" / "voicebank" / VOICE / "reference.wav"
    with_metrics: dict[str, dict] = json.loads((out_dir / "hf_arms.json").read_text(encoding="utf-8"))
    report: dict[str, dict] = {}
    parts: list[str] = []
    for key, title, sub in ROWS:
        p = arms.get(key)
        if p is None or not p.exists():
            parts.append(f"<div class='card'><div class='t'>{title}</div><span class='missing'>（缺文件）</span></div>")
            continue
        m = metrics(p, ref)
        report[key] = m
        tmp = p.with_suffix(".page_tmp.mp3")
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp)],
                       check=True, timeout=300)
        b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink()
        note = (f"4–8k {m.get('mid_rel0_4k_db')}dB · 8–12k {m.get('hf_rel0_4k_db')}dB · "
                f"质心 {m.get('centroid_hz')}Hz · 滚降斜率 {m.get('slope_1k_8k')}dB/oct · "
                f">8k 调制CV {m.get('hf_mod_cv')} · 像你 {m.get('cos')}")
        parts.append(f"<div class='card'><div class='t'>{title}</div>"
                     f"<div class='s'>{sub}<br>{note}</div>"
                     f"<audio controls preload='metadata' src='{b64}'></audio></div>")

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>8–12k 缺口补法 A/B/C</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:880px;margin:24px auto;padding:0 16px;line-height:1.7}
 h1{font-size:21px} .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;
 padding:12px 14px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px} .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 audio{width:100%;height:34px} .missing{color:#b00;font-size:13px}
 .sub{color:#555;font-size:13.5px} footer{margin:28px 0;color:#888;font-size:12px}
</style></head><body>
<h1>8–12k 缺口：抬 EQ / 源高频移植 / 谱折叠</h1>
<p class="sub">同一段输入、同 seed、同 pitch=0、同模型、**同一条后期链**；三臂唯一的差别是 8–12k 从哪来。
请只回答一个问题：<b>哪一个听起来最像"一个人在唱"，而不是"更亮"</b>。
另外留意两件具体的坏事：B 有没有带出<b>原唱的齿音/换气</b>（像别人了）；C 有没有<b>沙沙的或电话音</b>（非谐波成分）。</p>
<p class="sub">已确认不必再试的：换 f0 提取器（10-19 FCPE 实测：中位差 0.1 半音、八度错误率与 RMVPE 同档，
只快 27%）；再训练更久（e10→e40 +0.0036，在 seed 噪声内）。</p>
__BODY__
<footer>2026-10-19 · experiments/cover_hf_bands.py · 模型 __VOICE__ · 输入 __INPUT__</footer>
</body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "listen.html"
    dst.write_text(html.replace("__BODY__", "\n".join(parts))
                   .replace("__VOICE__", VOICE)
                   .replace("__INPUT__", str(with_metrics.get("source", ""))), encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="8–12k 缺口三臂 A/B")
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args()

    arms: dict[str, Path] = {}
    if a.step in ("run", "all"):
        arms = run_arms(source=a.source, start=a.start, seconds=a.seconds, voice=a.voice, out_dir=a.out)
    if a.step in ("page", "all"):
        if not arms:
            arms = arm_paths(a.out)
        build_page(arms, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

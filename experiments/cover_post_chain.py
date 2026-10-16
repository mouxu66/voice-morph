# -*- coding: utf-8 -*-
"""《唯一》高音段（185–210s）"还能再优化"实验：后期链 + 气口叠回 + index_rate 0.3。

### 背景（2026-10-16 用户听感，已定案的部分）
不剥离和声 → 明显电音；**先剥离和声/主唱后 → 听感很好**，但"还能再优化一下"。
所以本实验只做"锦上添花"的加工，**不改模型、不改音高**，全部单变量、同一 25s 窗口、
同一条已确认的输入（BS Roformer 抠出的干净主唱）、同 seed、同 pitch=0：

| 臂 | 含义 |
|---|---|
| A | 现役：index_rate 0.5（＝已有"高音直唱"臂，重算并**逐字节自检**与旧文件一致） |
| B | index_rate 0.3（唯一没试过的检索档；0.7 已被用户盲听否掉） |
| C | A + 后期链（空气感 EQ + De-Ess + 轻压缩） |
| D | B + 后期链 |
| E | C + 气口叠回（源里的换气/气声按 −18dB 叠回） |
| F | A + 气口叠回（隔离"气口叠回"这一个变量） |

### 为什么是这几件
- 社区 playbook（memory 10-16）：EQ / De-Ess 5–8k / 轻压缩 2:1–3:1 / 气口叠回 是"一眼 AI"解药；
  项目自己的探针也见过两轴：**输出比输入更"完美周期"**（缺气声/微颤 ⇒ 像合成器）、
  **8–12k 高频比输入掉很多**（闷）。空气感 EQ 治后者、气口叠回治前者。
- index_rate 是"越往说话索引平均、越加工"的旋钮（0.7 实测 8–12k −3dB、包络变平）；
  0.3 是反方向那一档，**从未跑过**，本实验顺手补上。

### 纪律
★ **不改产品链路**：本脚本只写实验产物到 `media/covers/song2/opt25/`（gitignore）。
  产品化（把某一臂的加工搬进 `cover_api`）必须等**用户耳朵裁决**后才做 —— 与 m1k/knobs
  的既有流程一致。

### ⚠️ 跨路径产物不可比（2026-10-16 探针实测，见 `docs/犯错档案-工程.md` §8.77）
RVC 生成链对输入的**微小扰动**不是「小扰动」，而是**另一次随机抽样**：
  · 同一 16k 数组喂进程内 / 喂 worker → **corr 1.0000、max|diff| 0**（两次逐字节一致）；
  · 同一数组但**先存成 16-bit wav 再读回** → corr **0.54**；
  · scipy `resample_poly` 重采样 vs `infer.audio.load_audio`（soxr）→ corr **0.66**。
所以「旧实验脚本的产物」与「产品 worker 链路的产物」**不能逐字节比 / 不能互相当基线**，
也不能把二者差值归因于代码变更。本脚本的 A 臂自检因此只断言**同一路径重复可复现**，
与旧文件的差异只作**信息性**报告（附带相关性，便于分辨「换了代码」与「换了路径」）。
★ 纯函数（滤镜串构造、气口提取、叠回）与 I/O 分离：守卫
  `m2_server/tests/test_cover_post_chain.py` 不需要 ffmpeg / librosa / GPU。

用法：
    python experiments/cover_post_chain.py run    # 转换 + 加工 6 臂
    python experiments/cover_post_chain.py page    # 指标 + listen.html
    python experiments/cover_post_chain.py all
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter1d

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

#: 输入：BS Roformer 抠出的干净主唱，185–210s，40k 单声道（f0_p50 331Hz，全曲最高段）
INPUT = ROOT / "media" / "covers" / "song2" / "f0sweep" / "lead_185-210.wav"
#: 旧"高音直唱"臂（A 臂的自检对象：同输入/同模型/同 seed/同 index ⇒ 应逐字节一致）
OLD_A = ROOT / "media" / "covers" / "song2" / "highdirect" / "direct_myvoice_tilt_t03.wav"
OUT_DIR = ROOT / "media" / "covers" / "song2" / "opt25"

VOICE = "myvoice_tilt_t03"  # 现役（m1k §3：presence 与"像你"两段都赢 t03 系列）
PITCH = 0
SEED_NOTE = "worker 默认 INFER_SEED（与旧臂同）"

#: 气口叠回电平：−18dB 是"听得见换气、但不加噪声"的起点（太靠上会变成嘶声）。
BREATH_GAIN_DB = -18.0


# ---------------------------------------------------------------- 纯函数（可单测）

def _num(v: float) -> str:
    """ffmpeg 滤镜数值的格式化：**绝不允许出现 1e-07 这种科学计数法**（滤镜会当场报错），
    极小值（< 1e-4）直接写 0 = 该档关闭。"""
    if abs(v) < 1e-4:
        return "0"
    return f"{round(v, 4):g}"


def post_filters(
    *,
    air_db: float = 3.0,
    air_hz: float = 9000.0,
    deess_i: float = 0.4,
    deess_m: float = 0.5,
    deess_f: float = 0.5,
    comp_threshold_db: float = -18.0,
    comp_ratio: float = 2.5,
    comp_attack_ms: float = 12.0,
    comp_release_ms: float = 180.0,
    comp_makeup_db: float = 2.0,
    reverb: bool = False,
    reverb_mix: tuple[float, float] = (0.08, 0.05),
) -> str:
    """后期链的 ffmpeg `-af` 滤镜串（空串 = 不加工）。

    顺序：**空气感 → De-Ess → 轻压缩 [→ 微混响]**。
    先抬 9k 再把齿音压回去：RVC 输出 8–12k 比输入低很多（闷），但直接抬会把 s/z 抬尖，
    所以两步必须成对出现（只留 treble 不留 deesser 不是这套链的目标形态）。

    参数都是"保守档"：默认值下它应该听不出"被处理过"，只该听出"更亮、更稳、齿音不扎"。
    混响默认关 —— 混响是**混音决策**，会掩盖干声的真实毛病，不适合放进"自然度"对照。
    """
    parts: list[str] = []
    if air_db:
        parts.append(f"treble=g={_num(air_db)}:f={_num(air_hz)}")
    if deess_i:
        parts.append(f"deesser=i={_num(deess_i)}:m={_num(deess_m)}:f={_num(deess_f)}")
    if comp_ratio:
        parts.append(
            f"acompressor=threshold={_num(comp_threshold_db)}dB:ratio={_num(comp_ratio)}"
            f":attack={_num(comp_attack_ms)}:release={_num(comp_release_ms)}"
            f":makeup={_num(comp_makeup_db)}dB"
        )
    if reverb:
        a, b = reverb_mix
        parts.append(f"aecho=0.8:0.7:35|60:{_num(a)}|{_num(b)}")
    return ",".join(parts)


def frame_to_sample_weight(
    voiced: np.ndarray, n_samples: int, sr: int, smooth_ms: float = 60.0
) -> np.ndarray:
    """帧级"浊音标志" → 采样级权重（1 = 浊音、0 = 非浊音），并做时间平滑。

    平滑是必须的：pyin 的浊音判定在音头/音尾会跳变，直接用会切出"咔哒"；
    60ms 的移动平均让权重过渡成斜坡，叠回去的是**气声**而不是**断点**。
    """
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float32)
    if len(voiced) == 0:
        return np.zeros(n_samples, dtype=np.float32)
    w = np.repeat(np.asarray(voiced, dtype=np.float32), max(1, int(np.ceil(n_samples / len(voiced)))))
    w = w[:n_samples]
    n_win = max(1, int(round(sr * smooth_ms / 1000.0)))
    if n_win > 1:
        # ★ mode="nearest"（复制边缘）而不是零填充：零填充会把**开头 30ms**的权重压到 0.5，
        #   于是气口轨在音频最前面漏进浊音（2026-10-16 单测实测：漏到 0.22 幅度）。
        #   uniform_filter1d 是 O(n)、且常数段（全浊音/全非浊音）原样保留。
        w = uniform_filter1d(w, size=n_win, mode="nearest")
    return np.clip(w, 0.0, 1.0).astype(np.float32)


def breath_track(
    src: np.ndarray,
    voiced: np.ndarray,
    sr: int,
    *,
    floor_db: float = -50.0,
    smooth_ms: float = 60.0,
) -> np.ndarray:
    """从**源**里取出"非浊音成分"当气口轨（换气 / 气声 / 气声型辅音）。

    RVC 只搬音色与基频，换气声这类**非周期成分**基本被丢掉 —— 这是"像合成器"的一因。
    源里的非浊音成分（乘以 (1 − 浊音权重)）正好就是它，不用另外合成噪声。

    `floor_db` 以下的样本清零：不叠底噪（安静房间的 −60dB 底噪叠上去只是变脏）。
    """
    src = np.asarray(src, dtype=np.float32)
    w = frame_to_sample_weight(voiced, len(src), sr, smooth_ms)
    breath = src * (1.0 - w)
    thr = 10.0 ** (floor_db / 20.0)
    breath = np.where(np.abs(breath) < thr, 0.0, breath).astype(np.float32)
    return breath


def overlay_breath(vocal: np.ndarray, breath: np.ndarray, *, gain_db: float = BREATH_GAIN_DB) -> np.ndarray:
    """把气口轨按 `gain_db` 叠进换声输出（多余长度截断、缺的补零）。"""
    v = np.asarray(vocal, dtype=np.float32)
    b = np.asarray(breath, dtype=np.float32) * (10.0 ** (gain_db / 20.0))
    n = min(len(v), len(b))
    out = v.copy()
    out[:n] = out[:n] + b[:n]
    return out


def _peak_normalize(x: np.ndarray, ceiling: float = 0.98) -> np.ndarray:
    """只在超顶时等比缩（叠回/压缩抬起来的那点增益不该变成削波）。"""
    peak = float(np.abs(x).max()) if x.size else 0.0
    if peak > ceiling:
        x = x * (ceiling / peak)
    return x.astype(np.float32)


# ---------------------------------------------------------------- I/O 与 ffmpeg

def _ffmpeg() -> str:
    from common import find_ffmpeg

    return find_ffmpeg()


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if getattr(d, "ndim", 1) > 1:
        d = d.mean(axis=1)
    return np.asarray(d, dtype=np.float32), int(sr)


def write_wav(path: Path, data: np.ndarray, sr: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), _peak_normalize(data), sr, subtype="PCM_16")


def apply_filters(src: Path, dst: Path, filters: str) -> None:
    """跑一遍 ffmpeg 加工链（filters 为空则直接复制）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not filters:
        dst.write_bytes(src.read_bytes())
        return
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-af", filters, str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("后期链失败: " + (r.stderr or "").strip()[-400:])


def voiced_flags(src: np.ndarray, sr: int) -> np.ndarray:
    """帧级浊音标志（pyin）。只在真跑时用 —— 纯函数部分不依赖 librosa。"""
    import librosa

    y = librosa.resample(src, orig_sr=sr, target_sr=22050) if sr != 22050 else src
    _, voiced, _ = librosa.pyin(
        np.asarray(y, dtype=np.float32), fmin=65.0, fmax=1000.0, sr=22050, frame_length=2048
    )
    return np.asarray(voiced, dtype=bool)


def _convert(src_name: str, index_rate: float) -> Path:
    """跑 RVC（走常驻 worker，模型缓存）——输入先复制成独立名字，避免产物互相覆盖。"""
    import rvc_convert

    work = OUT_DIR / "work"
    work.mkdir(parents=True, exist_ok=True)
    src_copy = work / f"{src_name}.wav"
    src_copy.write_bytes(INPUT.read_bytes())
    return rvc_convert.rvc_convert(src_copy, VOICE, pitch=PITCH, index_rate=index_rate)


def run_arms() -> dict:
    """跑 6 臂，产物写进 OUT_DIR，返回 {臂: 路径}。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chain = post_filters()
    src, sr = read_wav(INPUT)
    voiced = voiced_flags(src, sr)
    breath = breath_track(src, voiced, sr)
    print(f"[输入] 浊音帧 {int(voiced.sum())}/{len(voiced)} · 气口轨能量占比 "
          f"{(float((breath ** 2).sum()) / float((src ** 2).sum() + 1e-12)):.4f}")

    arms: dict[str, Path] = {}

    # A/B：两个 index 档（同一输入、同 seed）
    conv: dict[str, Path] = {}
    for tag, ir in (("A_ir05", 0.5), ("B_ir03", 0.3)):
        conv[tag] = _convert(tag, ir)
        print(f"[RVC] {tag} ← index_rate={ir}")

    # A 臂自检 ①：同一路径再转一次必须**逐字节一致**（播种与路径自洽）
    import hashlib

    again = _convert("A_ir05_repeat", 0.5)
    det = hashlib.md5(conv["A_ir05"].read_bytes()).hexdigest() == hashlib.md5(again.read_bytes()).hexdigest()
    print(f"[自检①] 同一路径重复转换逐字节一致：{'✅' if det else '❌（播种/环境有问题，本轮 A/B 不可信）'}")

    # A 臂自检 ②：与旧"高音直唱"文件比 —— **只作信息性报告**（旧文件走的是进程内 +
    # scipy 重采样，输入不同 ⇒ 输出本就是另一次抽样，见模块头 §跨路径产物不可比）。
    if OLD_A.exists():
        import soundfile as sf

        x, _ = sf.read(str(conv["A_ir05"]), dtype="float64")
        y, _ = sf.read(str(OLD_A), dtype="float64")
        n = min(len(x), len(y))
        c = float(np.corrcoef(x[:n], y[:n])[0, 1]) if n else 0.0
        print(f"[自检②] 与旧高音直唱文件（进程内/scipy 路径）相关性 {c:.3f}"
              " —— 路径不同，波形本不可比，只供分辨「换了代码」与「换了路径」")

    for tag, path in conv.items():
        arms[tag] = OUT_DIR / f"{tag}.wav"
        y, ysr = read_wav(path)
        write_wav(arms[tag], y, ysr)

    # C/D：A、B 过后期链
    for tag, src_tag in (("C_ir05_post", "A_ir05"), ("D_ir03_post", "B_ir03")):
        arms[tag] = OUT_DIR / f"{tag}.wav"
        apply_filters(arms[src_tag], arms[tag], chain)
        print(f"[后期] {tag} ← {chain}")

    # G：剂量加倍的空气感（+6dB@7k）——首轮指标显示 8–12k 缺口有 38dB，+3dB 档杯水车薪
    chain_air6 = post_filters(air_db=6.0, air_hz=7000.0)
    arms["G_air6"] = OUT_DIR / "G_air6.wav"
    apply_filters(arms["A_ir05"], arms["G_air6"], chain_air6)
    print(f"[后期] G_air6 ← {chain_air6}")

    # E：C + 气口叠回；F：A + 气口叠回（隔离变量）；H：G + 气口叠回
    for tag, base in (("E_post_breath", "C_ir05_post"), ("F_breath", "A_ir05"),
                      ("H_air6_breath", "G_air6")):
        y, ysr = read_wav(arms[base])
        arms[tag] = OUT_DIR / f"{tag}.wav"
        write_wav(arms[tag], overlay_breath(y, breath), ysr)
        print(f"[气口] {tag} ← {base} + 源气口轨 {BREATH_GAIN_DB:g}dB")

    arms["input_lead"] = OUT_DIR / "input_lead.wav"
    write_wav(arms["input_lead"], src, sr)
    return arms


# ---------------------------------------------------------------- 指标 + 试听页

def _metrics(path: Path) -> dict:
    """客观指标：音高分布 / ≥300Hz 占比 / 周期度 / 频带 / 谱平坦度 / 音高对齐 CAM++。"""
    import librosa

    y22, _ = librosa.load(str(path), sr=22050, mono=True)
    f0, voiced, _ = librosa.pyin(np.asarray(y22, dtype=np.float32), fmin=65, fmax=1200,
                                 sr=22050, frame_length=2048)
    v = f0[np.isfinite(f0)]
    m = {"f0_p50": round(float(np.median(v)), 1) if v.size else None,
         "frac_ge300": round(float(np.mean(v >= 300)), 3) if v.size else None}

    # 频带（去静音帧后的平均谱，相对 0–4k）
    win, hop = 2048, 1024
    frames = np.lib.stride_tricks.sliding_window_view(y22, win)[::hop]
    energy = (frames ** 2).sum(axis=1)
    frames = frames[energy > np.percentile(energy, 30)]
    spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1 / 22050)
    ref = (spec[(freqs >= 20) & (freqs < 4000)] ** 2).sum() + 1e-12
    for lo, hi in ((4000, 8000), (8000, 12000)):
        m[f"band_{lo//1000}k"] = round(float(10 * np.log10(((spec[(freqs >= lo) & (freqs < hi)] ** 2).sum() + 1e-12) / ref)), 1)
    m["sfm"] = round(float(np.exp(np.mean(np.log(spec + 1e-12))) / (np.mean(spec) + 1e-12)), 4)

    # 40ms 自相关周期度（与 elec 页同口径）
    acs = []
    for s in range(0, max(1, len(y22) - 4096), 4096):
        seg = y22[s:s + 4096]
        if (seg ** 2).mean() < 1e-6:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        ac = ac / (ac[0] + 1e-12)
        lo, hi = int(22050 / 700), int(22050 / 70)
        acs.append(float(ac[lo:hi].max()) if hi > lo else 0.0)
    m["ac_med"] = round(float(np.median(acs)), 3) if acs else None
    return m


def _timbre(path: Path, ref_path: Path) -> dict:
    """音高对齐后的 CAM++ 余弦 + 裸余弦（§8.76 的口径：跨音高必须对齐才可读绝对值）。"""
    from speaker_sep import _sv_embed
    import pyworld as pw

    def rd(p):
        import soundfile as sf
        from math import gcd

        from scipy.signal import resample_poly

        d, sr = sf.read(str(p), dtype="float32", always_2d=False)
        if getattr(d, "ndim", 1) > 1:
            d = d.mean(axis=1)
        if sr != 16000:
            g = gcd(16000, sr)
            d = resample_poly(d, 16000 // g, sr // g).astype(np.float32)
        return np.ascontiguousarray(d, dtype=np.float32)

    def med(p, a):
        f0, t = pw.dio(np.ascontiguousarray(a, dtype=np.float64), 16000, f0_floor=60.0, f0_ceil=900.0)
        f0 = pw.stonemask(np.ascontiguousarray(a, dtype=np.float64), f0, t, 16000)
        v = f0[f0 > 0]
        return float(np.median(v)) if v.size else None

    def emb(a):
        ds = []
        for s in range(0, max(1, len(a) - 16000), 10 * 16000):
            seg = a[s:s + 10 * 16000]
            if len(seg) < 16000 or (seg ** 2).mean() < 1e-6:
                continue
            e = _sv_embed(seg)
            if e is not None:
                ds.append(e)
        if not ds:
            return None
        m = np.mean(ds, axis=0)
        return m / (np.linalg.norm(m) + 1e-12)

    ref = rd(ref_path)
    e_ref = emb(ref)
    a = rd(path)
    f0 = med(path, a)
    e = emb(a)
    if e is None or e_ref is None or not f0:
        return {"cos": None, "cos_aligned": None}
    ratio = f0 / (med(ref_path, ref) or f0)
    f0r, t = pw.dio(np.ascontiguousarray(ref, dtype=np.float64), 16000, f0_floor=60.0, f0_ceil=900.0)
    f0r = pw.stonemask(np.ascontiguousarray(ref, dtype=np.float64), f0r, t, 16000)
    sp = pw.cheaptrick(np.ascontiguousarray(ref, dtype=np.float64), f0r, t, 16000)
    ap = pw.d4c(np.ascontiguousarray(ref, dtype=np.float64), f0r, t, 16000)
    up = np.ascontiguousarray(pw.synthesize(f0r * ratio, sp, ap, 16000), dtype=np.float32)
    e_up = emb(up)
    return {
        "cos": round(float(np.dot(e, e_ref) / (np.linalg.norm(e) * np.linalg.norm(e_ref))), 3),
        "cos_aligned": round(float(np.dot(e, e_up) / (np.linalg.norm(e) * np.linalg.norm(e_up))), 3) if e_up is not None else None,
    }


#: 试听页的臂顺序 + 标题（先说「看什么」、再给客观数字）。
#: D（B+后期链）与 G/H 重复度高，不入页 —— 它的数字仍在 metrics.json 里备查。
#: 页上 7 段：先去 ①→④，再把 ⑤⑥⑦ 与 ④ 对比，判断「还是不是更自然」。
ROWS: list[tuple[str, str, str]] = [
    ("input_lead", "① 输入：干净主唱（源头）", "被 RVC 替换的原料，是你听到的「电」的出生地"),
    ("A_ir05", "② A · 现役 index 0.5（你上轮听到「很好」的那版）", "基线：后面全部与它比"),
    ("B_ir03", "③ B · index 0.3（更少往说话索引平均）", "唯一没试过的检索档（实测影响极小）"),
    ("C_ir05_post", "④ C · A + 后期链（+3dB 空气感 + De-Ess + 轻压缩）", "保守档，应该听不出「被处理过」"),
    ("G_air6", "⑤ G · 后期链 +6dB@7k（剂量加倍）", "8–12k 缺口有 38dB，这一档才可能真变亮"),
    ("F_breath", "⑥ F · A + 气口叠回（换气/气声 −18dB）", "只隔离「气口」这一个变量"),
    ("E_post_breath", "⑦ E · C + 气口叠回", "后期链 + 气口，最「人」的候选"),
    ("H_air6_breath", "⑧ H · G + 气口叠回", "最亮的候选"),
]


def build_page(arms: dict[str, Path]) -> Path:
    ref = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"
    metrics: dict[str, dict] = {}
    parts: list[str] = []
    for key, title, sub in ROWS:
        p = arms.get(key)
        if p is None or not p.exists():
            parts.append(f'<div class="card"><div class="t">{title}</div><div class="s">{sub}</div><span class="missing">（缺文件）</span></div>')
            continue
        m = _metrics(p)
        m.update(_timbre(p, ref))
        metrics[key] = m
        tmp = p.with_suffix(".page_tmp.mp3")
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp)],
                       check=True, timeout=300)
        b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink()
        note = (f"p50 {m['f0_p50']}Hz · ≥300Hz {m['frac_ge300']} · 周期度 {m['ac_med']} · "
                f"4–8k {m['band_4k']}dB · 8–12k {m['band_8k']}dB · 谱平坦度 {m['sfm']} · "
                f"像你（裸/对齐音高）{m['cos']}/{m['cos_aligned']}")
        parts.append(f'<div class="card"><div class="t">{title}</div><div class="s">{sub}<br>{note}</div>'
                     f'<audio controls preload="metadata" src="{b64}"></audio></div>')

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>《唯一》高音段"再优化" A/B</title>
<style>
 body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f5f6f8;color:#222;
      max-width:880px;margin:24px auto;padding:0 16px;line-height:1.7}
 h1{font-size:21px} h2{font-size:16px;margin:26px 0 10px;color:#1a4a7a}
 .sub{color:#555;font-size:13.5px;margin:6px 0 4px}
 .card{background:#fff;border:1px solid #e2e6ec;border-radius:10px;padding:12px 14px;margin:10px 0;
       box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card .t{font-weight:600;font-size:14px}
 .card .s{color:#667;font-size:12.5px;margin:4px 0 8px}
 audio{width:100%;height:34px}
 .missing{color:#b00;font-size:13px}
 footer{margin:30px 0;color:#888;font-size:12px}
</style></head><body>
<h1>《唯一》185–210s · 主唱剥离之后再优化（人声干声，无伴奏）</h1>
<p class="sub">前提：你已经确认「先剥离和声/伴唱」把电音治好了（这一步不在本页里重复，它就是本条链路的输入）。</p>
<p class="sub">本页只问一件事：<b>在已经「听着很好」的版本上，还能不能更自然</b>。七段都来自同一条链路、
同一个 25s 窗口、同一 seed、pitch=0，只是加工不同 —— <b>请只凭耳朵</b>挑出最像「一个人在唱」的那一段，
然后告诉我编号。客观数字只做参考：<b>「像你」的裸余弦会随音高失配假红</b>
（见 docs/犯错档案-工程 §8.76），所以看「对齐音高」那一列才有意义。</p>
<p class="sub">已确认无用、不必再试的：<b>index_rate 0.3</b>（③ 与 ② 各项几乎相同）、
<b>换 f0 提取器 / 关检索</b>（10-15 四配置实测：同 f0 方法下 index 0.5 与 0 逐字节相同）。</p>
__BODY__
<footer>2026-10-16 · experiments/cover_post_chain.py · 输入 media/covers/song2/f0sweep/lead_185-210.wav（BS Roformer 主唱剥离）· 模型 myvoice_tilt_t03</footer>
</body></html>"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / "listen.html"
    dst.write_text(html.replace("__BODY__", "\n".join(parts)), encoding="utf-8")
    (OUT_DIR / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[页] %s（%.1f MB）" % (dst, dst.stat().st_size / 1e6))
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    arms: dict[str, Path] = {}
    if a.step in ("run", "all"):
        arms = run_arms()
    if a.step in ("page", "all"):
        if not arms:
            arms = {k: OUT_DIR / f"{k}.wav" for k, _, _ in ROWS}
        build_page(arms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

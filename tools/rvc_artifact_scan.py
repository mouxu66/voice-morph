"""扫描 RVC 参数对「电音感」的影响，用客观指标替代人耳排序。

「带电音」在听感上通常由三件事构成：
  1. 金属感/毛刺   → 高频非谐波能量上升，spectral flatness 变高
  2. 机械/autotune → f0 的帧间抖动被抹平，f0_delta 变小
  3. 噪声底噪抬高  → 静音段能量上升

本脚本在同一段输入上扫 protect / index_rate / lowpass / f0_method，
输出上述指标 + CAM++ 音色分，让人不必逐条盲听就能定位方向。

用法：
    python tools/rvc_artifact_scan.py --input media/dialect_probe/eric_chengdu/s01.wav
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_PTH = r"D:/RVC/assets/weights/myvoice_ppt12_full.pth"
DEFAULT_IDX = (
    r"D:/RVC/logs/myvoice_ppt12_full/"
    r"added_IVF817_Flat_nprobe_1_myvoice_ppt12_full_v2.index"
)
DEFAULT_REF = r"D:/变声/media/ft_work/ppt12/ref_24k.wav"

# (名字, pitch, 其余参数) —— baseline 就是现役默认值
CONFIGS: list[tuple[str, int, dict]] = [
    ("base_p33_ir50_lp11k", -12, {"index_rate": 0.5, "protect": 0.33}),
    # protect 三档实测产出 md5 完全相同 —— 见 docs §10.2
    ("p50", -12, {"index_rate": 0.5, "protect": 0.50}),
    ("ir30", -12, {"index_rate": 0.30, "protect": 0.33}),
    ("ir75", -12, {"index_rate": 0.75, "protect": 0.33}),
    # lowpass 默认 11k，砍高频是「去毛刺」还是「变闷」由 hf/flat 判
    ("lp8k", -12, {"index_rate": 0.5, "protect": 0.33, "lowpass_hz": 8000.0}),
    ("lp16k", -12, {"index_rate": 0.5, "protect": 0.33, "lowpass_hz": 16000.0}),
    # 关键对照：同样管线不降调 / 少降调
    ("pitch0", 0, {"index_rate": 0.5, "protect": 0.33}),
    ("pitch-6", -6, {"index_rate": 0.5, "protect": 0.33}),
]


def metrics(path: Path) -> dict:
    """算「电音」代理指标。全部是同批音频间的相对比较才有意义。

    注意 f0 抖动**必须用半音单位**：用 Hz 绝对差会让降八度的结果看起来
    抖动减半，那只是尺度变化，不是真的被抹平（第一版就踩了这个坑）。
    """
    import librosa
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(path), dtype="float32")
    if y.ndim > 1:
        y = y.mean(1)
    y16 = librosa.resample(y, orig_sr=sr, target_sr=16000) if sr != 16000 else y

    S = np.abs(librosa.stft(y16, n_fft=2048, hop_length=512))
    flat = float(np.mean(librosa.feature.spectral_flatness(S=S)))
    freqs = librosa.fft_frequencies(sr=16000, n_fft=2048)
    hf = float(S[freqs > 6000].sum() / (S.sum() + 1e-9))
    # 谱通量：帧间频谱跳变，金属/毛刺感的直接代理
    flux = float(np.mean(np.linalg.norm(np.diff(S, axis=1), axis=0)))

    # f0 抖动用半音：12*log2(f[i+1]/f[i])，与音高绝对尺度无关
    f0, _, _ = librosa.pyin(y16, fmin=60, fmax=320, sr=16000, frame_length=2048)
    v = f0[np.isfinite(f0)]
    semi = (float(np.mean(np.abs(12 * np.log2(v[1:] / v[:-1]))))
            if len(v) > 2 else float("nan"))

    rms = librosa.feature.rms(S=S)[0]
    floor = float(np.percentile(rms, 10))
    return {"flat": flat, "hf": hf, "semi": semi, "flux": flux, "floor": floor}


def campp(ref: Path, target: Path) -> float:
    """CAM++ 音色相似度。

    RVC 的 venv 里没有 funasr/modelscope，_sv_embed 会返回 None。
    这时返回 nan 而不是炸掉 —— 指标由主 venv 的 analyze 阶段补算。
    """
    import numpy as np

    sys.path.insert(0, str(ROOT / "m2_server"))
    import speaker_sep as ss

    a, b = ss._sv_embed(ss._read16k(ref)), ss._sv_embed(ss._read16k(target))
    if a is None or b is None:
        return float("nan")
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--pth", default=DEFAULT_PTH)
    ap.add_argument("--index", default=DEFAULT_IDX)
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--pitch", type=int, default=-12)
    ap.add_argument("--out-dir", default=str(ROOT / "media/dialect_probe/artifact_scan"))
    args = ap.parse_args()

    in_path = Path(args.input).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # setup_env 会 chdir，故先钉绝对路径
    import m2_server.offline_vc_infer as ovc

    ovc.setup_env()
    from infer.audio import load_audio

    engine = ovc.load_vc(args.pth, args.index)
    audio_in = load_audio(str(in_path), 16000)
    print(f"输入 {in_path.name}  pitch={args.pitch}")

    rows = []
    for name, pitch, kw in CONFIGS:
        y = ovc.convert_audio(engine, audio_in, pitch=pitch, seed=1234, **kw)
        import soundfile as sf

        f = out_dir / f"{name}.wav"
        sf.write(str(f), y, engine.tgt_sr)
        m = metrics(f)
        m["campp"] = campp(Path(args.ref), f)
        rows.append((name, m))
        print(f"  {name:20s} pitch={pitch:3d} flat={m['flat']:.5f} hf={m['hf']:.4f} "
              f"semi={m['semi']:5.3f} flux={m['flux']:.4f} "
              f"floor={m['floor']:.4f} campp={m['campp']:.4f}")

    # 参照：未转换的源，作为「干净」上界
    src = metrics(in_path)
    print(f"\n{'源(未转换)':20s} {'':10s} flat={src['flat']:.5f} hf={src['hf']:.4f} "
          f"semi={src['semi']:5.3f} flux={src['flux']:.4f} floor={src['floor']:.4f}")
    print(f"\n产物目录 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

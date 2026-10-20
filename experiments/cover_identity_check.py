"""这段到底是谁在唱：把"像童声"从耳朵问题变成可量的事实。

用户 2026-10-20 18:0x 的第二次裁决
    「这段根本就不是赵雷的声音啊……这段童声甚至都不需要替换我的声音，
      直接用它的声音都可以」

上一轮（`m1v`）我给出的结论是"源就是赵雷，错的是音区"，证据是
旧候选 vs 「官方 MV」分离轨 CAM++ = 0.9866。那份结论有一个**逻辑漏洞，必须先认**：

    CAM++ 0.9866 只证明「两个文件是同一个人」，**不证明这个人是赵雷**。
    我把一个自己标成"官方 MV（太合音乐）"的视频当成金标准，再用它和被怀疑的文件
    互相印证 —— 如果那个标签是错的（下载到的是同一个翻唱），0.9866 就是**循环论证**，
    它只是把同一个错答案抄了两遍。这正是 §2.x 里"自证"的经典形态。

所以这一轮不再问"两个文件像不像"，改问三个**不依赖标签**的问题：

  ① 逐 30 秒的音高表 —— 一个成男歌手唱《成都》，主歌在 130–190Hz、副歌顶到 260–330Hz；
     儿童/女声整曲会在 280–380Hz。**整首歌的音高分布本身就是身份证据**，
     不需要知道谁是谁。
  ② 同一批候选里的 CAM++ 矩阵 —— 把"成都旧""成都真"放进已知成男（朴树/周华健/薛之谦/
     毛不易/逃跑计划）中间。不同人之间 0.05–0.2，同一个人 0.5+。如果成都两条只跟彼此像、
     跟所有已知成男都不像，那"到底是谁"仍然没答，但"跟这批人不是一个人"是确定的。
  ③ 用户参照音（成男，116Hz）的位置 —— 作为"成男"这把尺子的锚点。

输出只陈述能证明的：\`same_as\` / \`adult_male_range\`。不写"这是赵雷"这种事 ——
没有可信标签的样本，谁都证不了身份；能证的是"和谁同一个人"和"音区属于哪一档"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_metrics as M  # noqa: E402

CAND = ROOT / "media" / "covers" / "midsong_20261018" / "candidates"
OUT = ROOT / "media" / "covers" / "midsong_20261018" / "register"

#: 逐段音高表的段长（秒）。30s 足够长到"一首歌的一段"，又短到能看出段落切换。
SEG = 30.0
#: 成男唱歌的判据上限：整曲有声帧中位超过这个值，就不是成男本色演唱。
ADULT_MALE_MEDIAN_MAX = 240.0


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float32), int(sr)


def f0_track(y: np.ndarray, sr: int, *, fast: bool = True) -> np.ndarray:
    """音高曲线。

    `fast=True` 用 YIN（约 100× 实时）—— 整曲分布表要跑 6 首歌，pyin 会到半小时。
    报单个窗口的"源 xxx Hz"仍必须用 pyin（`fast=False`）：m1v §7 坑 1 就是
    YIN 在边界吐 500Hz 而 pyin 复核只有 178.8Hz。
    """
    y16 = librosa.resample(np.asarray(y, dtype=np.float64), orig_sr=sr, target_sr=16000)
    if fast:
        f0 = librosa.yin(y16, fmin=70.0, fmax=600.0, sr=16000,
                         frame_length=1024, hop_length=320)
        # yin 没有 voiced 标记：用逐帧能量门限把静音/乐器的假音高剔掉
        rms = librosa.feature.rms(y=y16, frame_length=1024, hop_length=320)[0]
        floor = max(float(np.percentile(rms, 20)), 1e-4)
        keep = rms[: len(f0)] >= floor
        f0 = f0[: len(rms)][keep]
    else:
        f0, _v, _p = librosa.pyin(
            y16, fmin=70.0, fmax=600.0, sr=16000, frame_length=1024, hop_length=320
        )
    f0 = np.asarray(f0, dtype=np.float64)
    # 剔除 fmax 边界失败帧，否则中位数会被顶到 600Hz
    return f0[np.isfinite(f0) & (f0 < 590.0)]


def segment_table(path: Path, seg: float = SEG) -> dict:
    y, sr = read(path)
    dur = len(y) / sr
    rows = []
    for t0 in np.arange(0.0, dur, seg):
        t1 = min(t0 + seg, dur)
        a, b = int(t0 * sr), int(t1 * sr)
        if b - a < sr:  # 尾巴短于一秒不量
            continue
        v = f0_track(y[a:b], sr)
        if v.size < 20:
            rows.append({"t": round(float(t0), 1), "med": None, "voiced": int(v.size)})
            continue
        rows.append({
            "t": round(float(t0), 1),
            "med": round(float(np.median(v)), 1),
            "p90": round(float(np.percentile(v, 90)), 1),
            "voiced": int(v.size),
        })
    all_v = f0_track(y, sr)
    med = float(np.median(all_v)) if all_v.size else float("nan")
    return {
        "path": str(path.relative_to(ROOT)),
        "dur": round(dur, 1),
        "median": round(med, 1),
        "p10": round(float(np.percentile(all_v, 10)), 1) if all_v.size else None,
        "p90": round(float(np.percentile(all_v, 90)), 1) if all_v.size else None,
        "adult_male_range": bool(med <= ADULT_MALE_MEDIAN_MAX),
        "segments": rows,
    }


def cam(a: Path, b: Path) -> float:
    """CAM++ 余弦。复用 `cover_metrics.secs`（试音间那把尺子），不另写一份。"""
    v = M.secs(a, b)
    return float(v) if v is not None else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="identity.json")
    args = ap.parse_args()

    vocals = {
        "成都旧候选": CAND / "成都-赵雷" / "vocals.wav",
        "成都真源": (CAND / "成都-赵雷-真" / "demucs" / "htdemucs" / "source"
                     / "vocals.wav"),
        "朴树": CAND / "平凡之路-朴树" / "vocals.wav",
        "周华健": CAND / "朋友-周华健" / "vocals.wav",
        "薛之谦": CAND / "演员-薛之谦" / "vocals.wav",
        "毛不易": CAND / "消愁-毛不易" / "vocals.wav",
        "逃跑计划": CAND / "夜空中最亮的星-逃跑计划" / "vocals.wav",
        "你的参照": ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav",
    }
    vocals = {k: v for k, v in vocals.items() if v.exists()}
    missing = [k for k, v in vocals.items() if not v.exists()]

    print("=== ① 逐 30 秒音高（pyin，70–600Hz，剔除边界帧）===")
    tables = {}
    for name, p in vocals.items():
        t = segment_table(p)
        tables[name] = t
        segs = " ".join(
            f"{s['t']:.0f}s:{s['med']:.0f}" if s["med"] else f"{s['t']:.0f}s:—"
            for s in t["segments"]
        )
        flag = "成男音区" if t["adult_male_range"] else "★不是成男本色"
        print(f"\n{name}（{t['dur']:.0f}s，整曲中位 {t['median']:.0f}Hz "
              f"p10 {t['p10']:.0f} / p90 {t['p90']:.0f} → {flag}）")
        print(f"  {segs}")

    names = list(vocals)
    # CAM++ 只算成都那两条 × 全体（不是全矩阵）：14 对就够回答"和谁同一个人"，
    # 全矩阵 49 对会把运行时间从 2 分钟拉到 20 分钟，而多出来的对谁都不看。
    rows_needed = [n for n in names if n.startswith("成都")] or names
    print(f"\n=== ② CAM++（同一个人 0.5+，不同人 0.05–0.2）=== 行：{rows_needed}")
    mat: dict[str, dict[str, float]] = {n: {} for n in names}
    for a in rows_needed:
        for b in names:
            c = 1.0 if a == b else cam(vocals[a], vocals[b])
            mat[a][b] = round(c, 3)
            mat.setdefault(b, {})[a] = round(c, 3)
            print(f"  {a:<8} vs {b:<8} {c:.3f}", flush=True)

    # 结论只写能证的
    out = {"segments": tables, "cam": mat, "missing": missing}
    dst = OUT / args.json
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {dst}")

    cdui = mat.get("成都旧候选", {}).get("成都真源")
    print("\n=== 能证的三件事 ===")
    if cdui is not None:
        verdict = "同一个人的同一版录音" if cdui >= 0.5 else "不是同一个人"
        print(f"1. 成都旧候选 vs 成都真源：CAM++ {cdui:.4f} → {verdict}")
        print("   ⚠️ 这只说明两者相同，**不说明他们是谁** —— 上一轮把它读成"
              "「源就是赵雷」是循环论证。")
    for k in ("成都旧候选", "成都真源"):
        t = tables.get(k)
        if t:
            print(f"2. {k} 整曲中位 {t['median']:.0f}Hz → "
                  f"{'落在成男本色音区' if t['adult_male_range'] else '超出成男本色音区'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

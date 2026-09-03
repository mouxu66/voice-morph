# -*- coding: utf-8 -*-
"""DeepFilterNet 降噪强度标定（P2-5 验收工具）。

背景：降噪与弱人声是一对矛盾——压得狠，底噪去得干净，但弱人声/远场声会被
当噪声压掉，表现为语音断裂（pyin 的 f0 直接归零）。`audio_enhance` 已暴露
`atten_lim_db` 三档，但"哪档该做默认"需要数据说话，本工具就是干这个的。

指标（每条切片 × 每档，与原始信号对比）：

    noise_db      非语音段底噪（20ms 帧 RMS 的 20 分位）→ 降幅＝去噪收益
    voiced_loss   原本有声、增强后失声的帧占比 → 弱人声损伤（越低越好，最关键）
    voiced_ratio  pyin 判定有声帧占比 → 掉太多＝压断
    speech_ratio  能量 VAD 的语音占比（clip_qc 口径）→ 语音被整体压掉的程度
    snr_db        clip_qc 口径的信噪比 → 越高越干净

命令行：
    python m2_server/enhance_calib.py --limit 8
    python m2_server/enhance_calib.py --prefix feidudu_merged
    python m2_server/enhance_calib.py --levels light,standard,strong --limit 12

输出：控制台对照表 + 落盘 outputs/enhance_calib.json（含自动推荐档位）。
推荐规则：取"底噪降幅 ≥3dB" 且 "voiced_loss ≤5%" 的最强档；都不满足则取
voiced_loss 最小的一档（宁可留噪，不可压断人声——压断是不可逆的）。
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import config as cfg
from audio_enhance import ATTEN_LIM_PRESETS, enhance_file, resolve_atten_lim

CALIB_DIR = cfg.OUTPUTS_DIR
OUT_JSON = CALIB_DIR / "enhance_calib.json"

# 推荐判据
MIN_NOISE_DROP_DB = 3.0    # 至少降这么多底噪才算有收益
MAX_VOICED_LOSS = 0.05     # 弱人声损伤上限（5% 有声帧被压没）


def _read16k(path: Path) -> tuple[np.ndarray, int]:
    """读成 16k 单声道 float32（与 pitch_advice 同一口径）。"""
    import soundfile as sf
    x, sr = sf.read(str(path), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), int(sr)


def _noise_db(x: np.ndarray, sr: int) -> float:
    """非语音段底噪：20ms 帧 RMS 的 20 分位（dB）。"""
    n = max(1, int(sr * 0.02))
    if x.size < n:
        return -99.0
    m = (x.size // n) * n
    rms = np.sqrt((x[:m].reshape(-1, n) ** 2).mean(axis=1) + 1e-12)
    return 20.0 * math.log10(float(np.percentile(rms, 20)) + 1e-12)


def _speech_ratio(x: np.ndarray, sr: int) -> float:
    try:
        from clip_qc import analyze_signal
        return float(analyze_signal(x, sr).get("speech_ratio", 0.0))
    except Exception:
        return float("nan")


def measure(x: np.ndarray, sr: int) -> dict:
    """测一条信号的可比指标。"""
    from pitch_advice import f0_curve
    _, voiced = f0_curve(x, sr)
    return {
        "noise_db": round(_noise_db(x, sr), 1),
        "voiced_ratio": round(float(np.mean(voiced)) if voiced.size else 0.0, 3),
        "speech_ratio": round(_speech_ratio(x, sr), 3),
        "voiced": voiced,
    }


def compare(orig: np.ndarray, enh: np.ndarray, sr: int) -> dict:
    """原始 vs 增强：算增益与损伤（帧级对齐后比较有声帧）。"""
    o, e = measure(orig, sr), measure(enh, sr)
    k = min(len(o["voiced"]), len(e["voiced"]))
    if k:
        ov, ev = o["voiced"][:k], e["voiced"][:k]
        lost = float(np.mean(ov & ~ev)) if ov.any() else 0.0
    else:
        lost = 0.0
    o.pop("voiced"), e.pop("voiced")
    return {
        **{f"enh_{kk}": vv for kk, vv in e.items()},
        "noise_drop_db": round(o["noise_db"] - e["noise_db"], 1),
        "voiced_loss": round(lost, 3),
        "voiced_ratio_delta": round(e["voiced_ratio"] - o["voiced_ratio"], 3),
        "speech_ratio_delta": round(e["speech_ratio"] - o["speech_ratio"], 3),
    }


def calib_one(path: Path, levels: list[str], save_dir: Path | None = None) -> dict:
    """单条切片：原始指标 + 各档增强后的对比。

    save_dir：给定时把「原声 + 各档增强」一并写到该目录供 A/B 试听，不删除；
    否则增强产物只用于算指标、算完即删。
    """
    import soundfile as sf
    x, sr = _read16k(path)
    base = measure(x, sr)
    base.pop("voiced")
    out = {"name": path.stem, "duration_s": round(len(x) / sr, 1), "orig": base,
           "levels": {}}
    if save_dir:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(save_dir / f"{path.stem}_orig.wav"), x, sr)
    for lv in levels:
        try:
            if save_dir:
                dst = save_dir / f"{path.stem}_{lv}.wav"
                enhance_file(path, dst, atten_lim_db=resolve_atten_lim(lv))
                y, sr2 = _read16k(dst)
            else:
                tmp = CALIB_DIR / f"_calib_{path.stem}_{lv}.wav"
                enhance_file(path, tmp, atten_lim_db=resolve_atten_lim(lv))
                y, sr2 = _read16k(tmp)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            out["levels"][lv] = compare(x, y, min(sr, sr2))
        except Exception as e:  # 单档失败不拖垮整轮
            out["levels"][lv] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _avg(items: list[dict], key: str) -> float:
    vals = [i[key] for i in items if isinstance(i.get(key), (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else float("nan")


def recommend(summary: dict) -> tuple[str, str]:
    """按"先保人声、再谈去噪"的规则挑默认档。"""
    order = ["strong", "standard", "light"]  # 由强到弱
    rows = summary["levels"]
    for lv in order:
        r = rows.get(lv)
        if not r:
            continue
        if (r.get("noise_drop_db", 0) >= MIN_NOISE_DROP_DB
                and r.get("voiced_loss", 1) <= MAX_VOICED_LOSS):
            return lv, (f"底噪降 {r['noise_drop_db']}dB、弱人声损伤 {r['voiced_loss']:.1%}，"
                        f"收益与损伤都可接受")
    best = min(rows.items(), key=lambda kv: kv[1].get("voiced_loss", 9))[0]
    return best, (f"多档都压断了弱人声（最低损伤 {rows[best]['voiced_loss']:.1%}），"
                  f"保守取该档")


def pick_noisy(n: int, pool: int = 20) -> list[Path]:
    """挑 SNR 最低（底噪最重）的 n 条切片做 A/B——越脏越能听出档位差异。"""
    from clip_qc import _read, analyze_signal
    scored = []
    for p in sorted((cfg.MEDIA_DIR / "clips").glob("*.wav"))[:pool]:
        try:
            x, sr = _read(p)
            scored.append((analyze_signal(x, sr).get("snr_db", 0.0), p))
        except Exception:
            continue
    scored.sort(key=lambda t: t[0])
    return [p for _, p in scored[:n]]


def run(paths: list[Path], levels: list[str], save_dir: Path | None = None,
        write_json: bool = True) -> dict:
    items = []
    for i, p in enumerate(paths, 1):
        print(f"[{i}/{len(paths)}] {p.stem}", flush=True)
        items.append(calib_one(p, levels, save_dir))

    summary_levels: dict[str, dict] = {}
    for lv in levels:
        rows = [it["levels"][lv] for it in items if "error" not in it["levels"].get(lv, {})]
        if not rows:
            continue
        summary_levels[lv] = {
            "noise_drop_db": _avg(rows, "noise_drop_db"),
            "voiced_loss": _avg(rows, "voiced_loss"),
            "voiced_ratio_delta": _avg(rows, "voiced_ratio_delta"),
            "speech_ratio_delta": _avg(rows, "speech_ratio_delta"),
            "n": len(rows),
        }
    summary = {"levels": summary_levels, "clips": len(items)}
    lv, why = recommend(summary)
    summary["recommended"] = lv
    summary["reason"] = why

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "levels_tested": levels,
        "presets": {k: v for k, v in ATTEN_LIM_PRESETS.items()},
        "summary": summary,
        "items": items,
    }
    _print(summary, levels)
    if write_json:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n详细结果：{OUT_JSON}")
    if save_dir:
        print(f"试听文件：{save_dir}")
    return payload


def _print(summary: dict, levels: list[str]):
    print(f"\n切片 {summary['clips']} 条 · 底噪降幅 vs 弱人声损伤")
    print(f"{'档位':<10}{'底噪降幅':>10}{'弱声损伤':>10}{'有声占比Δ':>12}{'语音占比Δ':>12}")
    for lv in levels:
        r = summary["levels"].get(lv)
        if not r:
            continue
        print(f"{lv:<10}{r['noise_drop_db']:>9.1f}dB{r['voiced_loss']:>9.1%}"
              f"{r['voiced_ratio_delta']:>+12.3f}{r['speech_ratio_delta']:>+12.3f}")
    print(f"\n推荐默认档：{summary['recommended']}（{summary['reason']}）")
    print("口径说明：弱声损伤 = 原本有声、增强后失声的帧占比。light 档混回干声多，"
          "残留噪声会干扰 pyin 判定，故其数值里含「噪声混淆」成分，不全是真压断；"
          "跨档比较时优先看「有声占比Δ」（负值越大＝压断越明显）。")


def _main():
    ap = argparse.ArgumentParser(description="DeepFilterNet 降噪强度标定（P2-5）")
    ap.add_argument("--limit", type=int, default=8, help="取前 N 条切片（默认 8）")
    ap.add_argument("--prefix", default="", help="只取该前缀的切片")
    ap.add_argument("--clips", nargs="*", default=[], help="显式指定切片文件")
    ap.add_argument("--levels", default="light,standard,strong",
                    help="要对比的档位，逗号分隔")
    ap.add_argument("--save-dir", default="",
                    help="把「原声+各档增强」写到该目录供 A/B 试听（不删除）")
    ap.add_argument("--pick-noisy", type=int, default=0,
                    help="自动挑 SNR 最低（底噪最重）的 N 条切片，A/B 差异最明显")
    args = ap.parse_args()

    if args.clips:
        paths = [Path(p) for p in args.clips]
    elif args.pick_noisy:
        paths = pick_noisy(args.pick_noisy)
    else:
        clips_dir = cfg.MEDIA_DIR / "clips"
        glob = f"{args.prefix}*.wav" if args.prefix else "*.wav"
        paths = sorted(clips_dir.glob(glob))[: max(1, args.limit)]
    if not paths:
        print("没有可用切片")
        return
    save_dir = Path(args.save_dir) if args.save_dir else None
    run(paths, [s.strip() for s in args.levels.split(",") if s.strip()],
        save_dir, write_json=save_dir is None)


if __name__ == "__main__":
    _main()

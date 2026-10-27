# -*- coding: utf-8 -*-
"""唱歌域数据集：把已有的 1.5 分钟唱歌**过采样**到与说话同一量级，再重训一版。

为什么只改「域」不改「音高」（2026-10-19 的决定）
    supp2026 = 15.06 分钟，其中唱歌只有 **91.95s（10%）**，而且最高 236Hz。
    《成都》实测 cos(像你) 0.314、同人基线 0.74 —— 这是**说话模型唱不了歌**，
    不是"训练集没有高音档位"（10-18 已推翻 272Hz 天花板，见 m1o）。
    10-17 的移调造高音又被耳朵否决（"高音部分简直就是灾难"）。
    ⇒ 下一次重训唯一值得动的变量是**素材的域**，不是音高。

做法
    保留 supp2026 的全部 85 条（说话为主），把已有的干净唱歌/高音素材按权重复制：
      · supp2026_sing.wav（73.8s）×3
      · supp2026_longglide.wav（18.2s）×3
      · 目标声音_488Hz_参考.wav（2.6s，10-17 实测 HNR 27.9 的干净假声）×3
      · 20261017_125706.m4a（10.9s，245Hz 稳定长音）×1
    唱歌有效时长 91.95s → **294.6s（24.6%）**，总时长 15.06 → 19.97min。
    ★ 3× 是保守档：再往上（4×/5×）就是在同一条 74s 素材上过拟合，
      评测会分不清"域权重有用"与"背下了这一段"。

⚠️ 这是**过采样**不是新素材：同一条音频在训练里被看到多次，能改变分布权重，
   但不会变出新的音色/发声方式。它的结论上限是"域权重是否有用"；
   要真正补唱歌音色仍得录新素材（`docs/附录-可执行规程.md`）。

用法：
    ./.venv/Scripts/python.exe experiments/cover_singdom_dataset.py --build
    pd 训练：D:/RVC/train_rvc_voice.py --exp myvoice_singdom \
        --dataset D:/RVC/dataset_raw/myvoice_singdom_40k --epochs 40
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RVC = Path("D:/RVC")

#: 基础数据集（85 条 / 15.06min，原样全收）
BASE = RVC / "dataset_raw" / "myvoice_supp2026_40k"

#: 额外过采样：(源文件, 权重, 标签, 是否唱歌)
EXTRA: list[tuple[Path, int, str, bool]] = [
    (BASE / "supp2026_sing.wav", 3, "sing", True),
    (BASE / "supp2026_longglide.wav", 3, "glide", True),
    (ROOT / "media" / "raw_recordings" / "2026-10-supp" / "sing2" / "目标声音_488Hz_参考.wav", 3, "falsetto488", True),
    (ROOT / "media" / "raw_recordings" / "2026-10-supp" / "sing2" / "20261017_125706.m4a", 1, "sustain245", True),
]

#: 唱歌素材的原始时长（秒）—— 用于算"唱歌占比"，不靠猜
SING_SECONDS = {"supp2026_sing.wav": 73.75, "supp2026_longglide.wav": 18.20,
                "目标声音_488Hz_参考.wav": 2.60, "20261017_125706.m4a": 10.94}

OUT = RVC / "dataset_raw" / "myvoice_singdom_40k"
#: 基础集的**输入** manifest（supp2026 的 15.06min 真相源）
BASE_MANIFEST = RVC / "dataset_raw" / "myvoice_supp2026_manifest.json"
#: 本脚本的**输出** manifest
MANIFEST = RVC / "dataset_raw" / "myvoice_singdom_manifest.json"


def build_plan() -> list[dict]:
    """返回要写的文件清单：[{src, dst, weight, tag, singing, seconds}]。

    纯函数（不碰磁盘），守卫直接 import 它 —— 训练集构建错了只能在训练完才发现，
    代价是 25 分钟 GPU，所以权重和总时长必须在写盘前就算清楚。
    """
    plan: list[dict] = []
    for p in sorted(BASE.glob("*.wav")):
        plan.append({"src": p, "name": p.name, "weight": 1, "tag": "base", "singing": False,
                     "seconds": 0.0})
    for src, w, tag, singing in EXTRA:
        sec = SING_SECONDS.get(src.name, 0.0)
        for i in range(w):
            name = f"singdom_{tag}_{i + 1}.wav"
            plan.append({"src": src, "name": name, "weight": w, "tag": tag,
                         "singing": singing, "seconds": sec})
    return plan


def coverage(plan: list[dict]) -> dict:
    """算总条数 / 唱歌条数与时长。基础集时长从 manifest 读（15.06min）。"""
    base_seconds = 0.0
    if BASE_MANIFEST.exists():
        try:
            base_seconds = float(json.loads(BASE_MANIFEST.read_text(encoding="utf-8"))
                                 .get("minutes_total", 0.0)) * 60.0
        except Exception:
            base_seconds = 0.0
    if base_seconds <= 0:
        # 兜底：supp2026 是 85 条 / 15.06min（10-15 实测），不重读音频
        base_seconds = 903.6
    sing = sum(r["seconds"] for r in plan if r["singing"])
    total = base_seconds + sing
    return {"files": len(plan), "singing_files": sum(1 for r in plan if r["singing"]),
            "singing_seconds": round(sing, 2), "total_seconds": round(total, 2),
            "singing_share": round(sing / total, 4) if total else 0.0}


def _ffmpeg() -> str:
    """优先项目自带的 find_ffmpeg，退回 PATH。"""
    try:
        sys.path.insert(0, str(ROOT / "m2_server"))
        from common import find_ffmpeg

        return find_ffmpeg()
    except Exception:
        return "ffmpeg"


def build(out: Path = OUT) -> dict:
    plan = build_plan()
    cov = coverage(plan)
    if cov["total_seconds"] < 600:
        raise SystemExit(f"[失败] 总时长 {cov['total_seconds'] / 60:.1f}min < 10min")
    out.mkdir(parents=True, exist_ok=True)
    # 只清自己写过的（singdom_*），不碰基础集 —— 目录是新建的，但保险
    for p in out.glob("singdom_*.wav"):
        p.unlink()
    written = 0
    for row in plan:
        dst = out / row["name"]
        src = row["src"]
        if src.suffix.lower() == ".wav" and not row["tag"].startswith(("falsetto", "base")):
            shutil.copy2(src, dst)
        elif src.suffix.lower() == ".wav":
            # 采样率/声道统一交给 ffmpeg（基础集本来就是 40k mono）
            cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                   "-ar", "40000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0 or not dst.exists():
                raise RuntimeError(f"转码失败 {src.name}: {(r.stderr or '')[-300:]}")
        else:
            cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                   "-ar", "40000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0 or not dst.exists():
                raise RuntimeError(f"转码失败 {src.name}: {(r.stderr or '')[-300:]}")
        written += 1
    report = {"built_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
              "out": str(out), "base_seconds": 903.6, "written": written,
              **cov, "source": "supp2026_40k + 唱歌过采样（非新素材）"}
    MANIFEST.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="构建唱歌域过采样数据集")
    ap.add_argument("--build", action="store_true", help="真的写盘（不加只打印计划）")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    plan = build_plan()
    cov = coverage(plan)
    print(f"[计划] {cov['files']} 条 · 总 {cov['total_seconds'] / 60:.2f}min · "
          f"唱歌 {cov['singing_seconds']:.1f}s（{cov['singing_share'] * 100:.1f}%）")
    if not args.build:
        for r in plan:
            if r["tag"] != "base":
                print(f"  + {r['name']:<32s} ← {r['src'].name}")
        return 0
    print(json.dumps(build(args.out), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

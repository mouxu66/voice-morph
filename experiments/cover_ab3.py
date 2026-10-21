"""三臂盲听：同一条源、同一个音高，只换「音色从哪来」。

为什么要这三臂
    用户 2026-10-20 的裁决是「根本都不像我的声音」；m1y 找到的根因是**参照音选错了对象**
    （8.9 秒 PPT 收音 vs 他本人的真唱）。但 m1y 只做了度量，没有让他用耳朵裁决。
    这一轮把候选收敛到三条、且**只在“音色来源”上不同**：

      ① RVC `myvoice_singdom` —— 他自己的唱歌素材训过的模型（现役基线）
      ② Seed-VC + 他本人的 73.7s 真唱当参照 —— 零样本，但参照终于换对了
      ③ DDSP-SVC 2700 —— 声码器先验补高频的路线（训练集只有 11 分钟）

    三条都用同一段《成都》285–305s、同一个音高（不降调），
    锚点仍是他本人的真唱。**哪条最像，只能耳朵判。**

用法
    .venv/Scripts/python.exe experiments/cover_ab3.py            # 缺的臂现场生成 + 度量 + 页面
    .venv/Scripts/python.exe experiments/cover_ab3.py --measure  # 只度量 + 出页面
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_register as REG  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402
import cover_voice_ab as AB  # noqa: E402

BASE = ROOT / "media" / "covers" / "midsong_20261018"
OUT = BASE / "ab3"
SRC = BASE / "register" / "src.wav"
VOICE_AB = BASE / "voice_ab"
#: 他本人的真唱（全带宽，44.1k）—— 既是锚点也是 Seed-VC 的参照
MY_SING_44K = VOICE_AB / "_my_sing_44k.wav"
ANCHOR = VOICE_AB / "anchor_my_sing.wav"

DDSP_ROOT = Path(os.environ.get("VM_DDSP_ROOT", "D:/DDSP-SVC"))
DDSP_PY = Path(os.environ.get("VM_RVC_PYTHON", "D:/RVC/.venv/Scripts/python.exe"))
DDSP_CKPT = DDSP_ROOT / "exp" / "myvoice" / "model_2700.pt"

#: (key, 最终 wav, 人话说明) —— 说明只进 json，不进盲听页
ARMS = [
    ("rvc_singdom", VOICE_AB / "singdom_p0.wav", "RVC myvoice_singdom，他自己的唱歌素材训的"),
    ("seedvc_true_sing", OUT / "seedvc_true_sing.wav", "Seed-VC 零样本，参照=他本人的 73.7s 真唱"),
    ("ddsp_2700", OUT / "ddsp_2700.wav", "DDSP-SVC 2700，44.1k NSF-HiGAN 声码器"),
]

SEED = 20261021
WINDOW = "《成都》285–305s（C 段副歌）"


def _fresh(dst: Path, src: Path) -> bool:
    return dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime


def ensure_seedvc() -> Path:
    """Seed-VC：源=同一段切片，参照=他的真唱（全带宽）。"""
    dst = OUT / "seedvc_true_sing.wav"
    if _fresh(dst, SRC):
        return dst
    if not MY_SING_44K.exists():
        raise SystemExit(f"缺他的真唱解码：{MY_SING_44K}（先跑 cover_voice_ab.py）")
    tmp = OUT / "_seedvc_run"
    if tmp.exists():
        shutil.rmtree(tmp)
    got = Path(P.run_seedvc(SRC, MY_SING_44K, tmp, steps=30, shift=0))
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(got, dst)
    shutil.rmtree(tmp, ignore_errors=True)
    return dst


def ensure_ddsp() -> Path:
    """DDSP-SVC 2700：同一段切片、key=0（不升降调）。"""
    dst = OUT / "ddsp_2700.wav"
    if _fresh(dst, SRC):
        return dst
    if not DDSP_CKPT.exists():
        raise SystemExit(f"缺 DDSP 模型：{DDSP_CKPT}")
    staging = Path(os.environ.get("TEMP", "D:/tmp")) / "ab3_ddsp_in"
    raw = Path(os.environ.get("TEMP", "D:/tmp")) / "ab3_ddsp_out"
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, staging / SRC.name)
    raw.mkdir(parents=True, exist_ok=True)
    for old in raw.glob("*.wav"):
        old.unlink()
    cmd = [
        str(DDSP_PY), "batch_infer.py",
        "-m", str(DDSP_CKPT.relative_to(DDSP_ROOT)),
        "-i", str(staging), "-o", str(raw),
        "-k", "0", "-pe", "rmvpe",
    ]
    r = subprocess.run(cmd, cwd=str(DDSP_ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        print(r.stdout[-3000:], file=sys.stderr)
        print(r.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"DDSP 推理失败：{r.returncode}")
    got = raw / SRC.name
    if not got.exists():
        raise SystemExit(f"DDSP 没产物：{raw}")
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(got, dst)
    return dst


def build_key(rows: list[dict], order: list[int]) -> dict:
    shuffled = [rows[i] for i in order]
    return {
        "src": {"window": WINDOW, "f0": REG.median_f0(SRC)},
        "seed": SEED,
        "mapfile": "ab3.json",
        "map": {AB.LABELS[i]: r["key"] for i, r in enumerate(shuffled)},
        "rows": {r["key"]: {k: v for k, v in r.items() if k not in ("key", "path")}
                 for r in rows},
        "notes": {k: note for k, _p, note in ARMS},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过生成，只度量 + 出页面")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if not SRC.exists():
        raise SystemExit(f"缺源切片：{SRC}（先跑 experiments/cover_register.py）")
    if not ANCHOR.exists():
        raise SystemExit(f"缺锚点：{ANCHOR}（先跑 experiments/cover_voice_ab.py）")

    if not args.measure:
        print("  · Seed-VC（参照=你的真唱）…", flush=True)
        ensure_seedvc()
        print("  · DDSP-SVC 2700 …", flush=True)
        ensure_ddsp()

    rows = []
    for key, path, _note in ARMS:
        if not path.exists():
            raise SystemExit(f"缺臂 {key}：{path}")
        r = AB.measure(path, ANCHOR)
        r["key"] = key
        r["path"] = str(path)
        rows.append(r)
        print(f"{key:<18}{r['f0']:>6.0f}Hz  hf {r['hf_db']:>7.1f}  "
              f"像锚 {r['cam_anchor']:.3f}  像 8.9s 收音 {r['cam_speech_ref']:.3f}")

    order = list(range(len(rows)))
    random.Random(SEED).shuffle(order)
    key = build_key(rows, order)
    page = AB.build_page([rows[i] for i in order], ANCHOR, key, OUT)
    (OUT / "ab3.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print(f"\n页面：{page}\n映射：{OUT / 'ab3.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

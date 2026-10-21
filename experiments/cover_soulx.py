"""SoulX-Singer-SVC：零样本歌声转换（Apache-2.0，42k 小时训练，无需 MIDI/歌词）。

为什么试它
    Seed-VC 的最佳臂（真唱 25s 参照 + cfg1.0/50）CAM++ 0.800，但它是 22.05k 语音
    架构改的；RVC 是他自己素材训的，CAM++ 0.717。SoulX 是 24k、F5 系 flow-matching、
    42k 小时歌声数据训的 SVC，且官方明确支持「几秒参考 + 直接翻唱」——正好贴着
    「最小素材」这个目标。它也是 Apache-2.0，权重可分发。

这一轮扫的变量（同一条源，同一段《成都》）
    ① 参照窗口：37–62s（他的真唱，音区接近）/ 37–47s / 0–20s
    ② auto_shift：SoulX 的 auto_shift 会把输出整体移到参照音区（会改调！），
       默认关闭，只留一条对照臂
    ③ cfg：2.0 / 3.0（官方默认）/ 4.0，步数 32 / 48

用法
    .venv/Scripts/python.exe experiments/cover_soulx.py            # 缺的臂现场生成 + 度量 + 页面
    .venv/Scripts/python.exe experiments/cover_soulx.py --measure  # 只度量 + 出页面
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

import cover_clip_attrib as M  # noqa: E402
import cover_hf_tracking as T  # noqa: E402
import cover_register as REG  # noqa: E402
import cover_voice_ab as AB  # noqa: E402

BASE = ROOT / "media" / "covers" / "midsong_20261018"
OUT = BASE / "soulx"
SRC = BASE / "register" / "src.wav"
VOICE_AB = BASE / "voice_ab"
MY_SING_44K = VOICE_AB / "_my_sing_44k.wav"
ANCHOR = VOICE_AB / "anchor_my_sing.wav"

SVC_ROOT = ROOT / "_ext" / "SoulX-Singer"
SVC_PY = Path(os.environ.get("VM_RVC_PYTHON", "D:/RVC/.venv/Scripts/python.exe"))
SVC_CKPT = SVC_ROOT / "pretrained_models" / "SoulX-Singer" / "model-svc.pt"
RMVPE = SVC_ROOT / "pretrained_models" / "rmvpe" / "rmvpe.pt"

#: (key, 参照窗口(s), auto_shift, cfg, steps) —— 说明只进 json，不进盲听页
#: 8 条：真唱里 5s / 10s / 25s 三种参照长度，外加 auto_shift 与 cfg/步数对照；
#: 20s（0–20，中位 129Hz）与 cfg2.0 已在首轮扫过，明显更差，故不进页。
ARMS = [
    ("soulx_ref37_noshift_cfg3_s32", (37.0, 25.0), False, 3.0, 32),
    ("soulx_ref37_auto_cfg3_s32", (37.0, 25.0), True, 3.0, 32),
    ("soulx_ref10_noshift_cfg3_s32", (37.0, 10.0), False, 3.0, 32),
    ("soulx_ref5_50_55_noshift_cfg3_s32", (50.0, 5.0), False, 3.0, 32),
    ("soulx_ref5_45_50_noshift_cfg3_s32", (45.0, 5.0), False, 3.0, 32),
    ("soulx_ref37_noshift_cfg4_s48", (37.0, 25.0), False, 4.0, 48),
]

#: 串联臂：先把别人已经转好的产物当 target 再让 SoulX 过一遍，看能不能再抬一档
#: （Seed-VC 在「RVC → Seed-VC」上拿到过 0.824，单向不一定最优）
CASCADES = [
    ("soulx_on_seedvc", BASE / "seedvc_sweep" / "ref37_cfg10_s50.wav"),
    ("soulx_on_rvc", VOICE_AB / "singdom_p0.wav"),
]

#: 跨模型决赛页：把现役三条（RVC / Seed-VC 最优 / SoulX 最优）放同一页，
#: 免得用户在两三个页面之间来回换着听（换页本身就会打断"像不像"的判断）
FINALISTS = [
    ("rvc_singdom_p0", VOICE_AB / "singdom_p0.wav", "RVC myvoice_singdom"),
    ("seedvc_ref37_cfg10_s50", BASE / "seedvc_sweep" / "ref37_cfg10_s50.wav",
     "Seed-VC 25s 真唱参照 cfg1.0/50"),
    ("soulx_ref37_cfg3", OUT / "soulx_ref37_noshift_cfg3_s32.wav",
     "SoulX 25s 参照 cfg3.0/32"),
    ("soulx_ref10_cfg3", OUT / "soulx_ref10_noshift_cfg3_s32.wav",
     "SoulX 10s 参照 cfg3.0/32"),
    ("soulx_cfg4_s48", OUT / "soulx_ref37_noshift_cfg4_s48.wav",
     "SoulX 25s 参照 cfg4.0/48"),
    ("soulx_on_rvc", OUT / "soulx_on_rvc.wav", "SoulX 串联：RVC 产物再转一次"),
]

SEED = 20261021
WINDOW = "《成都》285–305s（C 段副歌）"


def _fresh(dst: Path, src: Path) -> bool:
    return dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime


def cut_prompt(start: float, dur: float) -> tuple[Path, Path]:
    """从 44.1k 真唱里切参照，转 24k；缺 f0 就用仓库里的 RMVPE 补。"""
    tag = f"ref{start:.0f}_{start + dur:.0f}"
    wav = OUT / f"prompt_{tag}.wav"
    f0 = OUT / f"prompt_{tag}_f0.npy"
    OUT.mkdir(parents=True, exist_ok=True)
    if not _fresh(wav, MY_SING_44K):
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(MY_SING_44K),
             "-ss", f"{start:g}", "-t", f"{dur:g}", "-ac", "1", "-ar", "24000", str(wav)],
            check=True, timeout=600,
        )
    if not f0.exists() or f0.stat().st_mtime < wav.stat().st_mtime:
        subprocess.run([str(SVC_PY), "make_f0.py", "cuda", str(wav)],
                       cwd=str(SVC_ROOT), check=True, timeout=600)
    return wav, f0


def ensure_arm(key: str, window: tuple[float, float], auto: bool,
               cfg: float, steps: int, target: Path | None = None) -> Path:
    """跑一条臂。`target` 为空=原始源切片；否则是串联臂的输入（已是 24k）。"""
    dst = OUT / f"{key}.wav"
    prompt, prompt_f0 = cut_prompt(*window)
    target_wav = target or (OUT / "target.wav")
    target_f0 = target_wav.with_suffix("").as_posix() + "_f0.npy"
    target_f0 = Path(target_f0)
    if _fresh(dst, prompt) and _fresh(dst, target_wav):
        return dst
    if not SVC_CKPT.exists():
        raise SystemExit(f"缺 SoulX 权重：{SVC_CKPT}")
    if not RMVPE.exists():
        raise SystemExit(f"缺 RMVPE：{RMVPE}")
    if not target_f0.exists() or target_f0.stat().st_mtime < target_wav.stat().st_mtime:
        subprocess.run([str(SVC_PY), "make_f0.py", "cuda", str(target_wav)],
                       cwd=str(SVC_ROOT), check=True, timeout=600)
    tmp = OUT / f"_run_{key}"
    if tmp.exists():
        shutil.rmtree(tmp)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SVC_ROOT)
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["VM_SEED"] = str(SEED)
    cmd = [
        str(SVC_PY), "run_svc_seeded.py",
        "--model_path", str(SVC_CKPT),
        "--prompt_wav_path", str(prompt),
        "--target_wav_path", str(target_wav),
        "--prompt_f0_path", str(prompt_f0),
        "--target_f0_path", str(target_f0),
        "--save_dir", str(tmp),
        "--pitch_shift", "0",
        "--n_steps", str(steps),
        "--cfg", str(cfg),
        "--fp16",
    ]
    if auto:
        cmd.append("--auto_shift")
    r = subprocess.run(cmd, cwd=str(SVC_ROOT), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        print(r.stdout[-3000:], file=sys.stderr)
        print(r.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"SoulX 推理失败：{r.returncode}")
    got = tmp / "generated.wav"
    if not got.exists():
        raise SystemExit(f"SoulX 没产物：{tmp}")
    shutil.copy2(got, dst)
    shutil.rmtree(tmp, ignore_errors=True)
    return dst


def ensure_target(src: Path, dst: Path) -> Path:
    """任意输入 → 24k 单声道（SoulX 内部按 24k 处理）。"""
    OUT.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", "24000", str(dst)],
        check=True, timeout=600,
    )
    return dst


def build_key(rows: list[dict], order: list[int]) -> dict:
    shuffled = [rows[i] for i in order]
    return {
        "src": {"window": WINDOW, "f0": REG.median_f0(SRC)},
        "seed": SEED,
        "mapfile": "answer.json",
        "map": {AB.LABELS[i]: r["key"] for i, r in enumerate(shuffled)},
        "rows": {r["key"]: {k: v for k, v in r.items() if k not in ("key", "path")}
                 for r in rows},
        "notes": {key: f"窗口{win[0]:g}s+{win[1]:g}s auto={auto} cfg={cfg} steps={steps}"
                  for key, win, auto, cfg, steps in ARMS}
        | {key: f"串联：target={src.name}，窗口37s+25s cfg=3.0 steps=32"
           for key, src in CASCADES},
    }


def build_finalists_page() -> Path:
    """跨模型决赛页：RVC / Seed-VC 最优 / SoulX 三种，同页盲听。"""
    rows = []
    for key, path, note in FINALISTS:
        if not path.exists():
            raise SystemExit(f"缺决赛臂 {key}：{path}")
        r = AB.measure(path, ANCHOR)
        r.update({k: v for k, v in T.tracking(*M.read(path)).items()
                  if k in ("r_env", "slope", "gap_drop_db")})
        r.update({"key": key, "path": str(path), "note": note})
        rows.append(r)
    order = list(range(len(rows)))
    random.Random(SEED + 1).shuffle(order)
    shuffled = [rows[i] for i in order]
    key = {
        "src": {"window": WINDOW, "f0": REG.median_f0(SRC)},
        "seed": SEED + 1,
        "mapfile": "answer_final.json",
        "map": {AB.LABELS[i]: r["key"] for i, r in enumerate(shuffled)},
        "rows": {r["key"]: {k: v for k, v in r.items() if k not in ("key", "path", "note")}
                 for r in rows},
        "notes": {r["key"]: r["note"] for r in rows},
    }
    out = OUT / "finalists"
    page = AB.build_page(shuffled, ANCHOR, key, out)
    (out / "answer_final.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return page


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过生成，只度量 + 出页面")
    args = ap.parse_args()

    if not SRC.exists():
        raise SystemExit(f"缺源切片：{SRC}")
    if not ANCHOR.exists():
        raise SystemExit(f"缺锚点：{ANCHOR}")

    OUT.mkdir(parents=True, exist_ok=True)
    ensure_target(SRC, OUT / "target.wav")
    rows = []
    for key, window, auto, cfg, steps in ARMS:
        path = OUT / f"{key}.wav"
        if not args.measure:
            print(f"  · {key} …", flush=True)
            path = ensure_arm(key, window, auto, cfg, steps)
        r = AB.measure(path, ANCHOR)
        r.update({k: v for k, v in T.tracking(*M.read(path)).items()
                  if k in ("r_env", "slope", "gap_drop_db")})
        r.update({"key": key, "window": list(window), "auto_shift": auto,
                  "cfg": cfg, "steps": steps, "path": str(path)})
        rows.append(r)
        print(f"{key:<34}{r['f0']:>6.0f}Hz cam {r['cam_anchor']:.3f}  "
              f"hf {r['hf_db']:>6.1f}  r_env {r['r_env']:.2f} slope {r['slope']:.2f} "
              f"gap {r['gap_drop_db']:.1f}")

    for key, src in CASCADES:
        tag = key.replace("soulx_on_", "cascade_")
        target = ensure_target(src, OUT / f"{tag}_target.wav")
        path = OUT / f"{key}.wav"
        if not args.measure:
            print(f"  · {key}（target={src.name}）…", flush=True)
            path = ensure_arm(key, (37.0, 25.0), False, 3.0, 32, target=target)
        r = AB.measure(path, ANCHOR)
        r.update({k: v for k, v in T.tracking(*M.read(path)).items()
                  if k in ("r_env", "slope", "gap_drop_db")})
        r.update({"key": key, "window": [37.0, 25.0], "auto_shift": False,
                  "cfg": 3.0, "steps": 32, "path": str(path), "cascade_from": src.name})
        rows.append(r)
        print(f"{key:<34}{r['f0']:>6.0f}Hz cam {r['cam_anchor']:.3f}  "
              f"hf {r['hf_db']:>6.1f}  r_env {r['r_env']:.2f} slope {r['slope']:.2f} "
              f"gap {r['gap_drop_db']:.1f}")

    (OUT / "soulx_metrics.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    order = list(range(len(rows)))
    random.Random(SEED).shuffle(order)
    key = build_key(rows, order)
    page = AB.build_page([rows[i] for i in order], ANCHOR, key, OUT)
    (OUT / "answer.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    final = build_finalists_page()
    print(f"\n指标：{OUT / 'soulx_metrics.json'}\nSoulX 八臂页：{page}\n"
          f"跨模型决赛页：{final}\n映射：{OUT / 'answer.json'} / {OUT / 'finalists' / 'answer_final.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

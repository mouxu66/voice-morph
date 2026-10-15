# -*- coding: utf-8 -*-
"""构建 2026-10 补录混训数据集（旧 12.2min 说话 + 新录宽频说话/唱歌）。

单变量纪律（2026-10-15）：
    · **旧素材原样复制**（逐字节 hash 核对），一个样本都不改 —— 这样新模型与
      `myvoice_tilt_t03` 的训练集差异只有「多出这 4 条新录音」（注：t03 的旧素材
      做过 +3dB/oct 预加重；本轮要回答的问题是"真·宽频素材"的效果，所以旧的走原样，
      A/B 时同时保留 t03 当对照，别把两个变量混在一句话里）。
    · 新素材只做**解码**（m4a → 40k 单声道 pcm_s16，与训练预处理器同 SR），
      **不切、不加重、不降噪**：切片交给 `train.preprocess`（3.7s 静音切片，全项目
      所有模型同一口径），任何额外处理都会成为新变量。
    · 产物目录在 RVC 的 dataset_raw 下，训练驱动自己会重采样/切片/提特征。

安全：`build()` 只允许重建名字以 `myvoice_supp` 开头的目录，防止 rmtree 误伤。

可测性：复制/校验/冲突检测是纯文件逻辑；ffmpeg 解码通过 `decode=` 注入，
守卫用假解码器造合成 wav，不需要真 ffmpeg。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

#: 新录音 → 数据集内文件 stem（ASCII，避免下游任何编码问题）。
NEW_ITEMS: list[tuple[str, str]] = [
    ("speak/词语.m4a", "supp2026_words"),
    ("speak/句子.m4a", "supp2026_sentences"),
    ("sing/长音和滑音.m4a", "supp2026_longglide"),
    ("sing/唱歌.m4a", "supp2026_sing"),
]

SR = 40000
ALLOWED_PREFIX = "myvoice_supp"


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def check_stem_collisions(old_stems: list[str], new_stems: list[str]) -> None:
    """新旧 stem 重名就拒绝（否则新解码会静默覆盖一条旧素材，混进谁也说不清的变量）。"""
    dupes = sorted(set(old_stems) & set(new_stems))
    if dupes:
        raise ValueError(f"新旧素材文件名冲突：{dupes}")
    new_dupes = sorted({s for s in new_stems if new_stems.count(s) > 1})
    if new_dupes:
        raise ValueError(f"新素材内部文件名重复：{new_dupes}")


def decode_ffmpeg(src: Path, dst: Path, ffmpeg: str, sr: int = SR) -> None:
    """m4a/任意格式 → `sr` 单声道 pcm_s16 wav（不做任何响度/滤波处理）。"""
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
           "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"解码失败 {src.name}: {(r.stderr or '')[-300:]}")


def verify_wav(path: Path, sr: int = SR) -> float:
    """断言产物是 `sr` 单声道且有内容，返回秒数。"""
    import soundfile as sf

    i = sf.info(str(path))
    if i.samplerate != sr:
        raise ValueError(f"{path.name} 采样率 {i.samplerate} ≠ {sr}")
    if i.channels != 1:
        raise ValueError(f"{path.name} 不是单声道（{i.channels} ch）")
    if i.duration <= 0.1:
        raise ValueError(f"{path.name} 时长异常：{i.duration}s")
    return float(i.duration)


def _default_decode(src: Path, dst: Path) -> None:
    from common import find_ffmpeg

    decode_ffmpeg(src, dst, find_ffmpeg())


def build(old_dir: Path, new_root: Path, dst: Path, items: list[tuple[str, str]],
          *, decode=_default_decode) -> dict:
    """构建混训集：旧 wav 原样复制（hash 核对）+ 新录音解码成 40k mono。

    返回 manifest（条数/时长/每个文件的来源）。`decode(src,dst)` 可注入
    （测试用假解码器造合成 wav，不需要真 ffmpeg）。
    """
    if dst.name and not dst.name.startswith(ALLOWED_PREFIX):
        raise ValueError(f"安全门：拒绝重建非 {ALLOWED_PREFIX}* 目录 {dst}")
    if not old_dir.is_dir():
        raise FileNotFoundError(old_dir)

    old_files = sorted(old_dir.glob("*.wav"))
    if not old_files:
        raise ValueError(f"旧数据集里没有 wav：{old_dir}")
    missing_new = [rel for rel, _ in items if not (new_root / rel).exists()]
    if missing_new:
        raise FileNotFoundError(f"新录音缺失：{missing_new}")

    check_stem_collisions([p.stem for p in old_files], [stem for _, stem in items])

    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    # 1) 旧素材：原样复制 + 逐字节核对
    old_rows = []
    for src in old_files:
        out = dst / src.name
        shutil.copy2(src, out)
        if md5(out) != md5(src):
            raise RuntimeError(f"旧素材复制后 hash 不一致：{src.name}")
        old_rows.append(src.name)

    # 2) 新素材：解码
    new_rows = []
    for rel, stem in items:
        src = new_root / rel
        out = dst / f"{stem}.wav"
        decode(src, out)
        seconds = verify_wav(out)
        new_rows.append({"src": str(src), "file": out.name, "seconds": round(seconds, 2)})
        print(f"[新素材] {rel} → {out.name}（{seconds:.1f}s @{SR}）")

    import soundfile as sf

    total_min = round(
        (sum(sf.info(str(dst / n)).duration for n in old_rows)
         + sum(r["seconds"] for r in new_rows)) / 60, 2)
    manifest = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "old_dir": str(old_dir),
        "old_copied_byte_identical": len(old_rows),
        "new": new_rows,
        "files_total": len(old_rows) + len(new_rows),
        "minutes_total": total_min,
        "sr": SR,
        "note": "旧素材未做预加重（与 t03 的唯一差别来源：本集为原样旧素材+宽频新素材）",
    }
    # ★ manifest 写在数据集目录**外**：train.preprocess 会 glob 目录里所有文件并把
    # 非音频也当输入（实测 85 wav + manifest.json = 86 输入，它优雅跳过但白报一次失败）。
    manifest_path = dst.parent / f"{dst.name}_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] {dst}：{manifest['files_total']} 文件 / {total_min} 分钟")
    print(f"       manifest → {manifest_path}")
    return manifest


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="构建补录混训数据集（旧素材只读复制 + 新录音解码）")
    ap.add_argument("--old", type=Path,
                    default=Path("D:/RVC/dataset_raw/myvoice_ppt12_full_40k"))
    ap.add_argument("--new-root", type=Path,
                    default=ROOT / "media/raw_recordings/2026-10-supp")
    ap.add_argument("--dst", type=Path,
                    default=Path("D:/RVC/dataset_raw/myvoice_supp2026_40k"))
    args = ap.parse_args()
    build(args.old, args.new_root, args.dst, NEW_ITEMS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把一段音频做成 voicebank 音色档案（一行命令）。

为什么需要它
------------
`media/` 整个被 gitignore，音色档案不进仓库 —— 换机器/重装后要重建。
参考音也可能换（比如从「RVC 转换结果」换成「用户真录音」）。
两条都需要同一个动作：**标准化 + 落盘 + 写清单**，本脚本只做这一件事。

不做的事
--------
不生成音频、不跑 RVC、不改默认音色（除非显式 --set-default）。
生成参考音的链路见 docs/交接-AI必读.md §13。

用法
----
    # 用一段音频建/更新音色（ref_text 必须是这段音频的逐字文本）
    python tools/make_voicebank_entry.py \
        --audio media/dialect_probe/style/greedy_chat_rvc_p-2.wav \
        --text "诶你在爪子哦？啷个半天不回个消息嘛，急死个人。" \
        --id sichuan_me --name "四川话·我的音色" --set-default

    # 只校验现有条目
    python tools/make_voicebank_entry.py --id sichuan_me --check
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICEBANK = ROOT / "media" / "voicebank"

# Qwen3-TTS 输出恒为 24kHz 单声道；参考音统一到这个规格，
# 避免 pydub/soundfile 在不同采样率上出现隐式转换差异。
TARGET_SR = 24000
# 参考音太短克隆不稳定、太长会拖慢每次合成（ICL 要过一遍 ref codec）
MIN_SEC, MAX_SEC = 3.0, 15.0


def _ffmpeg_convert(src: Path, dst: Path) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("找不到 ffmpeg，请先装好并加进 PATH")
    subprocess.run(
        [exe, "-v", "error", "-i", str(src), "-ar", str(TARGET_SR), "-ac", "1",
         "-c:a", "pcm_s16le", "-y", str(dst)],
        check=True,
    )


def _probe(path: Path) -> tuple[int, int, float]:
    import soundfile as sf

    info = sf.info(str(path))
    return info.samplerate, info.channels, info.duration


def _check(vid: str) -> int:
    d = VOICEBANK / vid
    ref = d / "reference.wav"
    txt = d / "ref_text.txt"
    if not ref.is_file():
        print(f"[FAIL] 缺 {ref}")
        return 1
    sr, ch, dur = _probe(ref)
    ok = True
    if sr != TARGET_SR or ch != 1:
        print(f"[WARN] 规格 {sr}Hz/{ch}ch，期望 {TARGET_SR}Hz/1ch（推理能跑，但可能引入转换差异）")
    if not (MIN_SEC <= dur <= MAX_SEC):
        print(f"[WARN] 时长 {dur:.2f}s 不在 {MIN_SEC}-{MAX_SEC}s（太短克隆不稳，太长每次合成变慢）")
    if not txt.is_file():
        print("[WARN] 缺 ref_text.txt —— 会退化成 x-vector 模式，语气/口音都弱很多")
        ok = False
    else:
        body = txt.read_text("utf-8").strip()
        if not body:
            print("[FAIL] ref_text.txt 为空")
            ok = False
        else:
            print(f"[OK] ref_text: {body[:40]}{'…' if len(body) > 40 else ''}")
    print(f"[OK] {ref}  {dur:.2f}s  {sr}Hz {ch}ch")
    return 0 if ok else 1


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="音色 id（英文/数字/下划线，作为目录名）")
    ap.add_argument("--audio", help="要作为参考音的音频；不给则只校验")
    ap.add_argument("--text", help="参考音的逐字文本（必须与音频完全一致）")
    ap.add_argument("--name", help="显示名，缺省用 id")
    ap.add_argument("--note", default="", help="meta.json 里的备注")
    ap.add_argument("--set-default", action="store_true", help="同时设为默认音色")
    ap.add_argument("--check", action="store_true", help="只校验现有条目")
    args = ap.parse_args()

    if args.check or not args.audio:
        return _check(args.id)

    if not args.text:
        raise SystemExit("--text 必填：零样本 ICL 模式要靠它对齐，写错会直接毁掉韵律")

    src = Path(args.audio).resolve()
    if not src.is_file():
        raise SystemExit(f"音频不存在：{src}")

    d = VOICEBANK / args.id
    d.mkdir(parents=True, exist_ok=True)
    ref = d / "reference.wav"
    if src != ref:
        _ffmpeg_convert(src, ref)

    sr, ch, dur = _probe(ref)
    print(f"参考音 {ref}  {dur:.2f}s  {sr}Hz {ch}ch")
    if not (MIN_SEC <= dur <= MAX_SEC):
        print(f"[WARN] 时长不在 {MIN_SEC}-{MAX_SEC}s 区间，克隆质量可能不稳")

    (d / "ref_text.txt").write_text(args.text.strip(), encoding="utf-8")
    meta = {
        "display_name": args.name or args.id,
        "kind": "clone_voice",
        "note": args.note,
        "sources": [src.name],
    }
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.set_default:
        sel = VOICEBANK / "selected_voice.json"
        sel.write_text(json.dumps({"voice_id": args.id}), encoding="utf-8")
        print(f"默认音色 → {args.id}")

    print(f"完成：{d}")
    return _check(args.id)


if __name__ == "__main__":
    raise SystemExit(main())

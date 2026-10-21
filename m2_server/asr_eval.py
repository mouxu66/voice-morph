"""中文 ASR 评测的三件小事：文本归一化、字错率（CER）、按说话人切分。

为什么单独一个模块（2026-10-21，潮汕话起步）
    潮汕话没有商用数据，只能拿公开语料做研究；而研究里最容易出错、也最容易
    让结论作废的三步，恰好都不是模型本身：

    ① **简繁不统一**：whisper 在 `language=zh` 下**默认输出繁体**（本机实测：
       「但現有方法門檻太高用不上」）。不归一化就直接算 CER，会把简繁差异
       当成识别错误 —— 一个完全正确的句子也能凭空多出 30% 的"错字"。
       ⚠️ 仓库里 `/transcribe` 早就用 `initial_prompt="以下是简体中文。"` 压着
       （`qwen3_tts_service.py`），评测脚本**必须带同一句 prompt**；本模块的
       归一化是第二道保险，不是替代品。
    ② **标点/空白**：`，。！？` 和空格不承载识别信息，却是 CER 的常见假红源。
    ③ **按说话人切分**：Teochew-Wild 只有 20 个说话人。随机按句切分会让同一个人
       同时出现在 train/test 里，CER 会好看到不真实（论文里的 10.01% 是作者自己的
       测试集，不是 speaker-disjoint）。所以切分必须**按人**，并且**确定性**可复现。

设计取舍
    - **不引新依赖**：简繁转换优先用 `zhconv`，其次 `opencc`，两个都没有就原样返回
      并让调用方知道（`converter=None`）。CI 的 requirements 里两者都没列，硬依赖
      会让干净环境整片红；这里用**注入式** converter，测试不依赖任何一个库。
    - **方言字不许丢**：潮汕正字里有大量生僻字（阮/厝/食/汝/伊）。归一化只删
      标点与空白，CJK 扩展区（\\u3400-\\u4dbf）与主区（\\u4e00-\\u9fff）全部保留。
    - **CER 以"字"为单位**：潮汕拼音标注（带声调数字）与汉字标注是两套口径，
      不能混算。要比拼音就整份都用拼音，`hanzi_only` 用来把拉丁字母/数字剔掉。
    - 失败不猜：`cer()` 的参考为空时**抛错**而不是返回 0.0 —— 0.0 会被当成
      "完美识别"，是最危险的那种假绿。批量统计时 `corpus_cer()` 跳过空参考并计数。

用法
    # 按说话人切分（20 人 → 2 eval / 2 dev / 其余 train）
    .venv/Scripts/python.exe m2_server/asr_eval.py \\
        --manifest teochew_wild.jsonl --split-out D:/tmp/tw_split.json

    # 代码里算 CER
    from asr_eval import cer, corpus_cer
    cer("但現有方法門檻太高", "但现有方法门槛太高")     # -> 0.0（归一化后一致）
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

#: 只保留汉字（含扩展 A 区）、拉丁字母、数字。标点/空白/emoji 一律删掉。
#: 拉丁字母保留是为了拼音标注（潮汕拼音带声调数字），`hanzi_only=True` 时可再剔。
_KEEP = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fff0-9A-Za-z]")
_HANZI = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: manifest 里允许的列名别名（不同语料的表头不统一，但语义只有这三个）
_WAV_KEYS = ("wav", "audio", "path", "file")
_TEXT_KEYS = ("text", "transcript", "sentence", "ref")
_SPK_KEYS = ("speaker", "spk", "sid", "speaker_id")


# --------------------------------------------------------------------------
# 一、归一化
# --------------------------------------------------------------------------
def _default_converter() -> Callable[[str], str] | None:
    """简繁转换器：zhconv 优先（纯 Python、无额外数据），其次 opencc；都没有就 None。"""
    try:
        import zhconv  # type: ignore

        return lambda s: zhconv.convert(s, "zh-cn")
    except Exception:
        pass
    try:
        import opencc  # type: ignore

        cc = opencc.OpenCC("t2s")
        return cc.convert
    except Exception:
        return None


def normalize_zh(
    text: str | None,
    *,
    converter: Callable[[str], str] | None = None,
    t2s: bool = True,
    hanzi_only: bool = False,
) -> str:
    """把一句标注/识别结果压成「只比字」的形式。

    converter 显式传入时用它（测试用假转换器，不依赖第三方库）；传 None 表示
    **自动探测**；要完全关闭简繁转换，请传 `t2s=False`。
    """
    if text is None:
        return ""
    s = str(text)
    if t2s:
        conv = converter if converter is not None else _default_converter()
        if conv is not None:
            s = conv(s)
    if hanzi_only:
        return "".join(_HANZI.findall(s))
    return _KEEP.sub("", s)


# --------------------------------------------------------------------------
# 二、字错率
# --------------------------------------------------------------------------
def edit_distance(ref: Sequence[str], hyp: Sequence[str]) -> int:
    """字符级 Levenshtein（两行滚动，O(len(ref) * len(hyp))）。"""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(
                prev[j] + 1,          # 删除
                cur[j - 1] + 1,       # 插入
                prev[j - 1] + (r != h),  # 替换
            ))
        prev = cur
    return prev[-1]


def cer(
    ref: str,
    hyp: str,
    *,
    converter: Callable[[str], str] | None = None,
    t2s: bool = True,
    hanzi_only: bool = False,
) -> float:
    """单句字错率。参考为空时**抛错**，见模块头「失败不猜」。"""
    r = normalize_zh(ref, converter=converter, t2s=t2s, hanzi_only=hanzi_only)
    h = normalize_zh(hyp, converter=converter, t2s=t2s, hanzi_only=hanzi_only)
    if not r:
        raise ValueError("参考文本归一化后为空：不能把空参考算成 0 错（那是假绿）")
    return edit_distance(r, h) / len(r)


def corpus_cer(
    pairs: Iterable[tuple[str, str]],
    *,
    converter: Callable[[str], str] | None = None,
    t2s: bool = True,
    hanzi_only: bool = False,
) -> dict:
    """按句聚合：总编辑距离 / 总参考字数（不是各句 CER 的平均）。

    跳过归一化后为空的参考，并把条数记在 `skipped` 里 —— 静默跳过等于改分母。
    """
    edits = ref_len = used = skipped = 0
    for ref, hyp in pairs:
        r = normalize_zh(ref, converter=converter, t2s=t2s, hanzi_only=hanzi_only)
        if not r:
            skipped += 1
            continue
        h = normalize_zh(hyp, converter=converter, t2s=t2s, hanzi_only=hanzi_only)
        edits += edit_distance(r, h)
        ref_len += len(r)
        used += 1
    return {
        "cer": (edits / ref_len) if ref_len else None,
        "edits": edits,
        "ref_chars": ref_len,
        "utterances": used,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# 三、按说话人切分
# --------------------------------------------------------------------------
def _pick(row: dict, keys: Sequence[str]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def canonical_row(row: dict) -> dict:
    """把不同表头的行统一成 {wav, text, speaker}，缺字段直接报错而不是留空。"""
    wav = _pick(row, _WAV_KEYS)
    text = _pick(row, _TEXT_KEYS)
    spk = _pick(row, _SPK_KEYS)
    missing = [n for n, v in (("wav", wav), ("text", text), ("speaker", spk)) if not v]
    if missing:
        raise ValueError(f"manifest 行缺字段 {missing}：{row!r}")
    out = {"wav": wav, "text": text, "speaker": spk}
    aliases = set(_WAV_KEYS) | set(_TEXT_KEYS) | set(_SPK_KEYS)
    for k, v in row.items():
        # 别名列（audio/transcript/spk…）已经映射进 wav/text/speaker，不重复保留
        if k not in aliases and v not in (None, ""):
            out[k] = v
    return out


def split_by_speaker(
    rows: Iterable[dict],
    *,
    n_eval: int = 2,
    n_dev: int = 2,
    seed: int = 20261021,
) -> dict:
    """按说话人切分，保证三份之间**没有一个共享的人**。确定性（同一 seed 同一结果）。

    说话人少于 `n_eval + n_dev + 1` 时抛错：凑不出互不相交的三份还硬切，
    等于把"训练集里的人"混进评测，结论不可用。
    """
    rows = [canonical_row(r) for r in rows]
    speakers = sorted({r["speaker"] for r in rows})
    need = n_eval + n_dev + 1
    if len(speakers) < need:
        raise ValueError(
            f"只有 {len(speakers)} 个说话人，切不出 {n_eval} eval + {n_dev} dev + 训练集"
            f"（至少需要 {need} 人）"
        )
    shuffled = list(speakers)
    random.Random(seed).shuffle(shuffled)
    eval_spk = set(shuffled[:n_eval])
    dev_spk = set(shuffled[n_eval:n_eval + n_dev])
    train_spk = set(shuffled[n_eval + n_dev:])
    parts = {
        "train": [r for r in rows if r["speaker"] in train_spk],
        "dev": [r for r in rows if r["speaker"] in dev_spk],
        "eval": [r for r in rows if r["speaker"] in eval_spk],
    }
    return {
        **parts,
        "speakers": {"train": sorted(train_spk), "dev": sorted(dev_spk),
                     "eval": sorted(eval_spk)},
        "counts": {k: len(v) for k, v in parts.items()},
        "seed": seed,
    }


# --------------------------------------------------------------------------
# 四、manifest 读取
# --------------------------------------------------------------------------
def load_manifest(path: str | Path) -> list[dict]:
    """读 JSONL / JSON 数组 / CSV / TSV，统一成 canonical 行。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"manifest 不存在：{p}")
    suffix = p.suffix.lower()
    if suffix in (".jsonl", ".json"):
        rows: list[dict] = []
        text = p.read_text(encoding="utf-8")
        if suffix == ".json":
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError("JSON manifest 必须是对象数组")
            return [canonical_row(r) for r in data]
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{lineno} 不是合法 JSON：{e}") from e
        return [canonical_row(r) for r in rows]
    if suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        with p.open(encoding="utf-8-sig", newline="") as fh:
            return [canonical_row(r) for r in csv.DictReader(fh, delimiter=delim)]
    raise ValueError(f"不认识的 manifest 后缀：{suffix}（只收 .jsonl/.json/.csv/.tsv）")


# --------------------------------------------------------------------------
# 五、命令行
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="中文 ASR 评测：按说话人切分 / 算 CER")
    ap.add_argument("--manifest", required=True, help=".jsonl/.json/.csv/.tsv，列含 wav/text/speaker")
    ap.add_argument("--split-out", help="把切分结果写成 JSON（默认只打印摘要）")
    ap.add_argument("--n-eval", type=int, default=2)
    ap.add_argument("--n-dev", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--check-files", action="store_true", help="检查 wav 路径是否都存在")
    args = ap.parse_args(argv)

    rows = load_manifest(args.manifest)
    split = split_by_speaker(rows, n_eval=args.n_eval, n_dev=args.n_dev, seed=args.seed)
    print(f"总条数 {len(rows)}｜说话人 {len({r['speaker'] for r in rows})} 人")
    for part in ("train", "dev", "eval"):
        print(f"  {part:5} {split['counts'][part]:5} 条  {len(split['speakers'][part])} 人")
    missing = 0
    if args.check_files:
        base = Path(args.manifest).resolve().parent
        for r in rows:
            w = Path(r["wav"])
            if not (w if w.is_absolute() else base / w).is_file():
                missing += 1
        print(f"  缺失 wav：{missing} 条")
    if args.split_out:
        out = Path(args.split_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"切分已写入 {out}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

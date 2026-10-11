# -*- coding: utf-8 -*-
"""RVC 变声验收（`tools/voice_qc.py` 100 分制 `emb_sim` 项）的**双空间标定** —— 先测后改。

现役判据是什么
--------------
`voice_qc.run_voice()` 的 `emb_sim` 项 = **变声输出 vs 目标音色参考** 的余弦，
`pass = sim >= 0.95`，分数 `f = clamp01((sim - 0.90) / 0.07)` 折进 25 分；
而它打的是 worker `/emb` —— **Qwen 自空间**（与克隆同一编码器）。
2026-10-11 的 pilotC 实测：自空间在**克隆**路径上同人区间只有 ~0.07 行程、
0.95 离异人上界只差 0.0034（见 `tools/voice_dual_gate.py`）。

但 RVC 验收是**另一条路径** —— 另一个模型、把真实录音转换成目标音色 —— 刻度未必相同。
**结论不能照搬，先在这条路径上测一遍。**（这正是用户给的口径：先测后改。）

同款设计（与 pilotC 一致，只是配对语义换成验收口径）
--------------------------------------------------
    同人 = 变声输出 vs 它**自己的目标音色参考**（验收现行比较）
    异人 = 同一份输出 vs **其它音色**的参考音（含「输出 vs 输入者本人」这条最狠的对照）
两把尺子各算一遍：Qwen 自空间（worker `/emb`）+ CAM++ 独立声纹（`speaker_sep`）。

数据怎么来
---------
- 变声输出：真跑 RVC 离线推理（`m2_server/offline_vc_infer.py`，与验收同一条命令、
  同样 ffmpeg 16k 预处理、pitch=0、index-rate=0.5）。
- 目标参考：优先按验收的 `_pick_emb_ref` 口径（voicebank/<音色>/reference.wav），
  缺了就用市场音色自带的 preview 音频（`outputs/market/<音色>_preview.wav`）。
  **每支模型的目标参考都必须能解析出来**，否则那支模型算不了同人对，脚本会跳过并写明原因。
- 输入：他人的参考音（3~30s，长的截到 30s），刻意**不含目标本人** —— 与验收
  `_pick_test_input` 的「跨音色泛化」口径一致。

判据换不换：规则写死，不靠人拍
------------------------------
`decide()`（纯函数，有单测）：
    keep    同人最低 ≥ 0.95 **且** 异人最高 ≤ 0.95，且两侧余量都 ≥ 0.02
    fragile 上面都成立但有一侧余量 < 0.02（能用，但一改就崩 → 必须加 audit）
    switch  任一侧越线（误杀或误收）
无论结论如何，报告里都会给出「若改用双门槛会是哪三个数」（`voice_dual_gate.derive`），
以及双门槛在**这批**配对上的重放结果（`voice_dual_gate.audit`）。

**绝对阈值之外，还会算一遍「相对排序」**（`rank_analysis()`）——
本批实测两把尺子的同人/异人簇**重叠**，`derive()` 直接报「无阈值可推」，
那就换一个不需要刻度的问法：「输出离自己的目标参考，是不是比离别的参考都近」。
这是本脚本对「换不换判据」给出的第二个、也是更可执行的一层结论。

用法
----
    .venv/Scripts/python.exe tools/voice_qc_ruler_calib.py            # 缺输出就推理，嵌入有缓存
    .venv/Scripts/python.exe tools/voice_qc_ruler_calib.py --reuse    # 不推理，只复算
    .venv/Scripts/python.exe tools/voice_qc_ruler_calib.py --force    # 忽略嵌入缓存全重算
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "m2_server") not in sys.path:
    sys.path.insert(0, str(ROOT / "m2_server"))

import voice_dual_gate as dual_gate          # noqa: E402  双门槛判据（纯函数）
import voicebank_dual_embed as dual_embed    # noqa: E402  两把尺子 + AUC/EER/d′ 的既有实现

#: 现役判据（`voice_qc.run_voice` 的 `emb_sim` 项 pass 线）。改这个数 = 改验收口径。
LEGACY_FLOOR = 0.95

#: 「余量 < 它就叫 fragile」：判据能用但一改就崩的宽度（0.95 附近差 0.02 已经很窄）。
FRAGILE_MARGIN = 0.02

#: 目标（模型 → 目标参考音候选 → 说话人标签）。候选按验收 `_pick_emb_ref` 的优先级排：
#: voicebank/<音色>/reference.wav → 市场音色的 preview 音频。
TARGETS: tuple[dict, ...] = (
    {"exp": "kangaroo_v2",
     "refs": ("media/voicebank/kangaroo/reference.wav",),
     "speaker": "kangaroo"},
    {"exp": "kangaroo_v2_40k",
     "refs": ("media/voicebank/kangaroo/reference.wav",),
     "speaker": "kangaroo"},
    {"exp": "katoong_lanyangyang",
     "refs": ("outputs/market/katoong_lanyangyang_preview.wav",),
     "speaker": "katoong_lanyangyang"},
    {"exp": "katoong_manbo",
     "refs": ("outputs/market/katoong_manbo_preview.wav",),
     "speaker": "katoong_manbo"},
)

#: 输入（他人参考音；`clip_s` = 超长的截到该秒数，与验收「3~30s」口径对齐）。
INPUTS: tuple[dict, ...] = (
    {"id": "aishell3_ssb0407",
     "src": "media/voicebank/aishell3_ssb0407/reference.wav",
     "speaker": "aishell3_ssb0407"},
    {"id": "teacher_voice",
     "src": "media/voicebank/teacher_voice/reference.wav",
     "speaker": "teacher_voice", "clip_s": 30},
    {"id": "vbsyn_trial_ssb0033",
     "src": "media/voicebank/vbsyn_trial_ssb0033_60s/reference.wav",
     "speaker": "vbsyn_trial_ssb0033", "clip_s": 30},
)

#: 只作异人对照、不当输入的参考音（多两个说话人，异人样本更够用）。
CROSS_ONLY_REFS: tuple[dict, ...] = (
    {"id": "vbsyn_trial_ssb0603",
     "src": "media/voicebank/vbsyn_trial_ssb0603_60s/reference.wav",
     "speaker": "vbsyn_trial_ssb0603", "clip_s": 30},
    {"id": "vbsyn_trial_ssb0737",
     "src": "media/voicebank/vbsyn_trial_ssb0737_60s/reference.wav",
     "speaker": "vbsyn_trial_ssb0737", "clip_s": 30},
)


def calib_dir() -> Path:
    """落盘根 = `<OUTPUTS_DIR>/qc_calib`。延迟 import config（同 voicebank_eval 的理由）。"""
    import config as cfg

    return Path(cfg.OUTPUTS_DIR) / "qc_calib"


# ---------------- 素材准备 ----------------

def _to_16k_mono(src: Path, dst: Path, clip_s: float | None = None) -> Path:
    """与验收同一套预处理：ffmpeg → 16k 单声道（可选截到 clip_s 秒）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-af", "aresample=16000", "-ac", "1"]
    if clip_s:
        cmd += ["-t", f"{clip_s:.3f}"]
    cmd.append(str(dst))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 预处理失败（{src.name}）：{r.stderr.strip()[:300]}")
    return dst


def prepare_inputs(out_dir: Path) -> dict[str, Path]:
    """输入参考音 → 16k 单声道（长的截到 30s）。返回 `{id: wav}`。"""
    out: dict[str, Path] = {}
    for spec in INPUTS:
        src = ROOT / spec["src"]
        if not src.exists():
            continue
        out[spec["id"]] = _to_16k_mono(src, out_dir / "in" / f"{spec['id']}.wav",
                                       spec.get("clip_s"))
    return out


def prepare_refs(out_dir: Path) -> dict[str, dict]:
    """打分用的参考音池：目标参考 + 输入参考 + 只作对照的参考，各带说话人标签。

    返回 `{ref_id: {"path": Path, "speaker": str, "kind": "target"|"input"|"cross_only"}}`。
    **同一支参考音只存一份**（目标与输入撞车时以 target 身份为准 —— 同人标签优先）；
    目标参考的 id 按**说话人**命名（两支袋鼠模型共用同一支目标参考，不该重复算两次）。
    """
    pool: dict[str, dict] = {}

    def _add(ref_id: str, src: str, speaker: str, kind: str, clip_s: float | None) -> None:
        if ref_id in pool:
            return
        path = ROOT / src
        if not path.exists():
            return
        wav = _to_16k_mono(path, out_dir / "refs" / f"{ref_id}.wav", clip_s)
        pool[ref_id] = {"path": wav, "speaker": speaker, "kind": kind}

    for t in TARGETS:
        src = next((s for s in t["refs"] if (ROOT / s).exists()), None)
        if src:
            _add(f"{t['speaker']}_target", src, t["speaker"], "target", None)
    for spec in INPUTS:
        _add(spec["id"], spec["src"], spec["speaker"], "input", spec.get("clip_s"))
    for spec in CROSS_ONLY_REFS:
        _add(spec["id"], spec["src"], spec["speaker"], "cross_only", spec.get("clip_s"))
    return pool


def resolve_targets(out_dir: Path) -> list[dict]:
    """解析每支模型的权重 / index / 目标参考。**解析不出来的整支跳过并留原因**（不硬凑）。"""
    import voice_qc

    rvc_root = Path(voice_qc.cfg.RVC_ROOT)
    resolved: list[dict] = []
    for spec in TARGETS:
        exp = spec["exp"]
        weight = rvc_root / "assets" / "weights" / f"{exp}.pth"
        if not weight.exists():
            resolved.append({"exp": exp, "skip": f"权重缺失：{weight}"})
            continue
        indexes = sorted((rvc_root / "logs" / exp).glob("*.index"))
        ref_path = next((ROOT / s for s in spec["refs"] if (ROOT / s).exists()), None)
        if ref_path is None:
            resolved.append({"exp": exp, "skip": "目标参考音缺失（候选全无）"})
            continue
        resolved.append({
            "exp": exp, "speaker": spec["speaker"], "weight": weight,
            "index": indexes[0] if indexes else None, "ref": ref_path,
        })
    return resolved


def convert_all(targets: list[dict], inputs: dict[str, Path], out_dir: Path,
                *, reuse: bool = False, force: bool = False) -> list[dict]:
    """跑 RVC 推理，产出 `{exp}__{input_id}.wav`。命令与验收完全一致（同一行参数）。

    `reuse=True`（`--reuse`）**不跑推理**：已有输出直接用，缺的直接跳过并提示 ——
    标定在自己机器上反复跑，每次都重推 12 遍既慢又没必要。
    `force=True`（`--force`）反过来：已有输出也重推（换模型/换预处理后用）。
    """
    import voice_qc

    out: list[dict] = []
    for t in targets:
        if "skip" in t:
            print(f"[calib] 跳过 {t['exp']}：{t['skip']}", flush=True)
            continue
        for in_id, in_wav in inputs.items():
            # 输入者本人不当输入（跨音色泛化口径；同人标签由目标参考占住）
            if t["speaker"] == _input_speaker(in_id):
                continue
            dst = out_dir / "out" / f"{t['exp']}__{in_id}.wav"
            if reuse and dst.exists():
                pass                      # 复用已有输出
            elif reuse:
                print(f"[calib] --reuse：缺 {dst.name}，跳过", flush=True)
                continue
            elif not dst.exists() or force:
                print(f"[calib] RVC 推理：{t['exp']} ← {in_id}", flush=True)
                _run_infer(t, in_wav, dst, voice_qc)
            out.append({"exp": t["exp"], "speaker": t["speaker"], "input": in_id,
                        "ref": str(t["ref"].relative_to(ROOT)),
                        "path": str(dst.relative_to(ROOT))})
    return out


def _input_speaker(in_id: str) -> str:
    for spec in list(INPUTS) + list(CROSS_ONLY_REFS):
        if spec["id"] == in_id:
            return str(spec["speaker"])
    return in_id


def _run_infer(target: dict, in_wav: Path, dst: Path, voice_qc) -> None:
    """RVC 离线推理（**与 `voice_qc.run_voice` 同一条命令**：pitch=0、index-rate=0.5）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [str(voice_qc.RVC_VENV_PY), str(voice_qc.INFER_PY),
         "--pth", str(target["weight"]),
         "--index", str(target["index"]) if target["index"] else "",
         "--input", str(in_wav), "--output", str(dst),
         "--pitch", "0", "--index-rate", "0.5"],
        capture_output=True, text=True, timeout=1800,
        encoding="utf-8", errors="replace", cwd=str(Path(voice_qc.cfg.RVC_ROOT)))
    if r.returncode != 0 or not dst.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"RVC 推理失败（{target['exp']}）: " + " | ".join(tail)[-400:])


# ---------------- 配对（纯函数，单测直接打它） ----------------

def build_pairs(outputs: list[dict], refs: dict[str, dict]) -> list[dict]:
    """同人 = 输出 vs 自己的目标参考；异人 = 输出 vs 其它说话人的参考。

    ★ 标签只看 `speaker` 是否相等，不看路径 —— 谁与谁同人是**说话人**的事实，
    写成标签才不会因为换了参考音文件就悄悄变（pilotC 同一条纪律）。
    """
    rows: list[dict] = []
    for o in outputs:
        for ref_id, r in sorted(refs.items()):
            role = "same" if r["speaker"] == o["speaker"] else "cross"
            # 每份输出的「同人」只能有一支参考（目标音色），多出来的说明标定表写错了
            if role == "same" and r["kind"] != "target":
                continue
            rows.append({
                "role": role, "exp": o["exp"], "input": o["input"],
                "out_rel": o["path"], "ref_id": ref_id, "ref_rel": r["path"].relative_to(ROOT).as_posix(),
                "target_speaker": o["speaker"], "ref_speaker": r["speaker"],
            })
    return rows


# ---------------- 两把尺子（带缓存） ----------------

def embed_all(paths: list[str], cache_file: Path, *, force: bool = False,
              start_worker: bool = True) -> dict[str, dict]:
    """给所有涉及的 wav 算两把尺子的嵌入，落 `embeddings.json` 复用。

    Qwen 自空间要 worker 8001（本脚本**只在需要时才拉起**，跑完归还原来的状态）；
    CAM++ 走 `speaker_sep` 单例，本机进程内算。
    """
    cache: dict[str, dict] = {}
    if cache_file.exists() and not force:
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 缓存坏了就全重算，不阻断
            cache = {}

    todo = [p for p in dict.fromkeys(paths) if p not in cache or not cache[p].get("qwen")]
    if todo:
        was_alive = False
        if start_worker:
            import qwen3_tts

            was_alive = qwen3_tts.worker_alive()
            if not was_alive:
                print("[calib] 拉起 Qwen worker（算自空间嵌入）…", flush=True)
                qwen3_tts._ensure_worker()   # noqa: SLF001 —— 项目内模块，按需拉起
        try:
            for i, rel in enumerate(todo, 1):
                path = ROOT / rel
                entry = dict(cache.get(rel) or {})
                if not entry.get("cam"):
                    entry["cam"] = dual_embed._cam_embedding(path).tolist()
                if not entry.get("qwen"):
                    entry["qwen"] = dual_embed._qwen_embedding(path).tolist()
                entry["duration_s"] = round(float(_duration(path)), 3)
                cache[rel] = entry
                print(f"[calib] 嵌入 {i}/{len(todo)}：{rel}", flush=True)
        finally:
            if start_worker and not was_alive:
                import qwen3_tts

                try:
                    qwen3_tts.shutdown_worker()   # 跑完把不属于本轮的 worker 收掉
                except Exception:  # noqa: BLE001 —— 收不掉不该让结果作废
                    pass
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def _duration(path: Path) -> float:
    import soundfile as sf

    return float(sf.info(str(path)).duration)


def _jsonable(obj):
    """`Path` → 相对 ROOT 的字符串（WindowsPath 连 `json.dumps` 都过不了，踩过）。"""
    if isinstance(obj, Path):
        try:
            return obj.relative_to(ROOT).as_posix()
        except ValueError:
            return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def score_pairs(pairs: list[dict], embeds: dict[str, dict]) -> list[dict]:
    """给每对配对算两把尺子的余弦（向量来自缓存，纯算术）。"""
    out = []
    for p in pairs:
        a, b = embeds.get(p["out_rel"]), embeds.get(p["ref_rel"])
        if not a or not b:
            continue
        row = dict(p)
        row["qwen_cos"] = round(dual_embed.cos(a["qwen"], b["qwen"]), 4)
        row["cam_cos"] = round(dual_embed.cos(a["cam"], b["cam"]), 4)
        out.append(row)
    return out


# ---------------- 分析与判定 ----------------

def decide(same_min: float, cross_max: float, floor: float = LEGACY_FLOOR,
           fragile: float = FRAGILE_MARGIN) -> dict:
    """现役 0.95 判据在这批同人/异人分上「站不站得住」——规则写死，不靠人拍。

    keep    同人最低 ≥ floor（不误杀）且 异人最高 ≤ floor（不误收），两侧余量 ≥ fragile
    fragile 两侧都过线但有一侧余量 < fragile（能用，但一改就崩 → 必须加 audit/双门槛）
    switch  任一侧越线（会误杀同人，或把异人放进来）

    返回 `{verdict, margin_low, margin_high, floor}`；`margin_low = same_min − floor`
    （同人侧余量，负数 = 误杀），`margin_high = floor − cross_max`（异人侧余量，负数 = 误收）。
    """
    low = round(float(same_min) - float(floor), 4)
    high = round(float(floor) - float(cross_max), 4)
    if low < 0 or high < 0:
        verdict = "switch"
    elif low < fragile or high < fragile:
        verdict = "fragile"
    else:
        verdict = "keep"
    return {"verdict": verdict, "margin_low": low, "margin_high": high, "floor": floor}


def _rank_metrics(group: list[dict], tgt: dict, key: str) -> tuple[int, float | None]:
    """目标参考在 `group` 里按 `key` 的排名（1 起）与相对最强竞争者的余量。"""
    order = sorted(group, key=lambda p: -p[key])
    rank = order.index(tgt) + 1
    others = [p[key] for p in group if p is not tgt]
    margin = round(tgt[key] - max(others), 4) if others else None
    return rank, margin


def rank_analysis(scored: list[dict]) -> dict:
    """逐输出的「目标参考排第几 / 领先多少」——**不需要刻度的另一层判据**。

    ★ 为什么单列这一节：本批实测两把尺子的同人/异人簇**重叠**（`derive()` 抛
    ValueError），即**任何绝对阈值都有错判**；而「输出离自己的目标参考比离任何别的
    参考都更近」是个**相对**问法，不依赖刻度对齐。两者要分开看：
    「绝对线站不住」不等于「这把尺子没用」。

    返回 `{n_outputs, qwen_rank1, cam_rank1, *_margin_min/max, rows}`；打分行缺
    `exp`/`input`（比如只留分数的证据副本）时返回 `{"error": ...}`，不硬凑。
    """
    by_out: dict[tuple, list[dict]] = {}
    for p in scored:
        key = (p.get("exp"), p.get("input"))
        if key == (None, None):
            return {"error": "打分行缺 exp/input，排序分析不可用"}
        by_out.setdefault(key, []).append(p)

    rows: list[dict] = []
    for (exp, inp), group in sorted(by_out.items(), key=lambda kv: str(kv[0])):
        tgt = next((p for p in group if p["role"] == "same"), None)
        if tgt is None:
            continue
        row = {"exp": exp, "input": inp, "refs": len(group)}
        for key, name in (("qwen_cos", "qwen"), ("cam_cos", "cam")):
            rank, margin = _rank_metrics(group, tgt, key)
            row[f"{name}_rank"] = rank
            row[f"{name}_margin"] = margin
        rows.append(row)
    if not rows:
        return {"error": "没有可排序的输出（样本为空）"}

    def _rate(name: str) -> float:
        return round(sum(r[f"{name}_rank"] == 1 for r in rows) / len(rows), 4)

    def _extreme(name: str, fn) -> float | None:
        vals = [r[f"{name}_margin"] for r in rows if r[f"{name}_margin"] is not None]
        return round(fn(vals), 4) if vals else None

    return {
        "n_outputs": len(rows),
        "qwen_rank1_rate": _rate("qwen"),
        "cam_rank1_rate": _rate("cam"),
        "qwen_margin_min": _extreme("qwen", min),
        "qwen_margin_max": _extreme("qwen", max),
        "cam_margin_min": _extreme("cam", min),
        "cam_margin_max": _extreme("cam", max),
        "rows": rows,
    }


def analyze(scored: list[dict]) -> dict:
    """两把尺子的分布/AUC/EER/d′ + 现役 0.95 的表现 + 若改双门槛会是哪三个数。"""
    same = [p for p in scored if p["role"] == "same"]
    cross = [p for p in scored if p["role"] != "same"]
    rulers = {}
    for key, label in (("qwen_cos", "自空间（Qwen /emb）"), ("cam_cos", "独立声纹（CAM++）")):
        s = [p[key] for p in same]
        c = [p[key] for p in cross]
        rulers[key] = {
            "label": label,
            "same": dual_embed.brief(s), "cross": dual_embed.brief(c),
            "auc": dual_embed.auc(s, c), "eer": dual_embed.eer(s, c),
            "dprime": dual_embed.dprime(s, c),
            "same_pass_at_legacy": round(sum(v >= LEGACY_FLOOR for v in s) / len(s), 4) if s else None,
            "cross_pass_at_legacy": round(sum(v >= LEGACY_FLOOR for v in c) / len(c), 4) if c else None,
        }

    q = rulers["qwen_cos"]
    decision = decide(q["same"]["min"], q["cross"]["max"])

    # 若改用双门槛会是哪三个数（还是从**这批**实测簇界推导，规则同 voice_dual_gate）
    derived: dict[str, dict] = {}
    for key in ("cam_cos", "qwen_cos"):
        try:
            derived[key] = dual_gate.derive(rulers[key]["same"]["min"], rulers[key]["cross"]["max"])
        except ValueError as e:
            derived[key] = {"error": str(e)}
    # 双门槛在这批配对上的重放（同人全过 / 异人全拒才算「可用」）
    audit = dual_gate.audit(scored)

    return {"n_same": len(same), "n_cross": len(cross), "rulers": rulers,
            "legacy_decision": decision, "dual_derived": derived,
            "dual_audit": audit, "rank": rank_analysis(scored)}


# ---------------- 报告 ----------------

def write_report(results: dict, out_path: Path) -> Path:
    """生成 `report.md`：现役判据 → 两把尺子实测 → 换/不换的结论 → 双门槛备选数。"""
    r = results
    q = r["rulers"]["qwen_cos"]
    c = r["rulers"]["cam_cos"]
    d = r["legacy_decision"]
    lines = [
        "# RVC 变声验收 · `emb_sim` 判据双空间标定",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　|　"
        f"样本：{r['n_same']} 同人对 + {r['n_cross']} 异人对",
        f"- 现役判据：**Qwen 自空间余弦 ≥ {LEGACY_FLOOR}**（`tools/voice_qc.py` 的 "
        f"`emb_sim` 项，100 分制里占 25 分）",
        "- 配对口径：同人 = 变声输出 vs 它自己的目标音色参考；异人 = 同一份输出 vs "
        "其它音色参考（含「输出 vs 输入者本人」）",
        "",
        "## 1 结论（换不换）",
        "",
        f"**`{d['verdict']}`** —— 同人侧余量 {d['margin_low']:+.4f}、异人侧余量 "
        f"{d['margin_high']:+.4f}（相对 {d['floor']}）。",
        "",
        _verdict_text(d),
        "",
        "## 2 两把尺子实测",
        "",
        "| 尺子 | 同人 mean（min–max） | 异人 mean（min–max） | AUC | EER | d′ | "
        f"同人 ≥{LEGACY_FLOOR} | 异人 ≥{LEGACY_FLOOR} |",
        "|---|---|---|---|---|---|---|---|",
        _row(q), _row(c),
        "",
        "## 3 若改用双门槛会是哪三个数（同样从这批簇界按 7:3 推导）",
        "",
        "| 尺子 | pass | warn/floor | 备注 |",
        "|---|---|---|---|",
        _derive_row("cam_cos", c), _derive_row("qwen_cos", q),
        "",
        "## 4 双门槛在本批配对上的重放",
        "",
        f"- 同人 {r['dual_audit']['n_same']}：{r['dual_audit']['same']}",
        f"- 异人 {r['dual_audit']['n_cross']}：{r['dual_audit']['cross']}",
        f"- 同人通过率 {r['dual_audit']['same_pass_rate']}　异人通过率 "
        f"{r['dual_audit']['cross_pass_rate']}",
        "- ⚠️ 可见**照搬 pilotC 那套门槛（`voice_dual_gate.DUAL`）在这条路径上是错的**：",
        "  它会把多数同人判成 review/reject —— 两条路径的刻度不能互用（这正是本标定的意义）。",
        "",
        "## 5 相对排序（绝对阈值推不出来时，换一种问法）",
        "",
        _rank_head(r.get("rank") or {}),
        "",
        *_rank_rows(r.get("rank") or {}),
        "",
        *_rank_note(r.get("rank") or {}),
        "",
        "## 6 边界（照抄，别藏）",
        "",
        "- 目标音色只有 **3 个说话人 / 4 支模型**（袋鼠两支 + 卡通两支），输入是 "
        "他人参考音 3 段 —— 结论只对**这条验收链路**负责，不外推到别的音色/语料。",
        "- 参考音时长 4.2~30s 不等（卡通市场音色只带 4.2s preview），两把尺子都对时长敏感，"
        "换素材要重标。",
        "- 公开语料（AISHELL-3、VBDataset 试听）与自录素材混用；只发布分数，不发布音频。",
        "",
        "## 7 复现",
        "",
        "```bash",
        ".venv/Scripts/python.exe tools/voice_qc_ruler_calib.py --reuse   # 输出已在时只复算",
        "```",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _verdict_text(d: dict) -> str:
    if d["verdict"] == "keep":
        return (f"现役 {d['floor']} 在这条路径上**站得住**：同人与异人都没被压到线上，"
                f"两侧余量都 ≥ {FRAGILE_MARGIN}。**不改判据。**")
    if d["verdict"] == "fragile":
        return (f"现役 {d['floor']} 能用但**没有余量**（有一侧 < {FRAGILE_MARGIN}）—— "
                f"换一批素材/模型就可能翻车。**建议不改阈值，但把 `audit()` 加进验收回归。**")
    bad = "同人被误杀" if d["margin_low"] < 0 else "异人被放进来"
    return (f"现役 {d['floor']} **站不住**（{bad}）。**要改**，但换什么得看下面三节哪一节能落地："
            f"§3 两簇分得开才能推绝对门槛；§4 是 pilotC 门槛在本路径上的重放（差 = 不能照搬）；"
            f"§5 两簇重叠时的相对判据。")


def _row(r: dict) -> str:
    s, c = r["same"], r["cross"]
    def f(x, n: int = 4) -> str:
        return "—" if x is None else f"{x:.{n}f}"   # AUC/d′ 在样本不足时是 None，别崩
    return (f"| {r['label']} | {f(s['mean'])}（{f(s['min'])}–{f(s['max'])}） | "
            f"{f(c['mean'])}（{f(c['min'])}–{f(c['max'])}） | {f(r['auc'])} | "
            f"{f(r['eer'])} | {f(r['dprime'], 3)} | {f(r['same_pass_at_legacy'], 3)} | "
            f"{f(r['cross_pass_at_legacy'], 3)} |")


def _derive_row(key: str, ruler: dict) -> str:
    try:
        d = dual_gate.derive(ruler["same"]["min"], ruler["cross"]["max"])
    except (ValueError, TypeError, KeyError) as e:
        return f"| {ruler['label']} | — | — | 推不了（两簇重叠或样本为空）：{e} |"
    note = "主判两档（pass/warn）" if key == "cam_cos" else "自空间下限（pass 即 floor）"
    return f"| {ruler['label']} | {d['pass']} | {d['warn']} | {note} |"


def _f4(x) -> str:
    return "—" if x is None else f"{x:+.4f}"


def _rank_head(rank: dict) -> str:
    if rank.get("error"):
        return f"（排序分析不可用：{rank['error']}）"
    return ("判据：同一份输出 vs **全部参考音**，看目标参考排第几 —— "
            "`rank=1` 且余量 > 0 才算「认得出是自己的音色」。")


def _rank_rows(rank: dict) -> list[str]:
    if rank.get("error"):
        return []
    lines = ["| 输出 | 参考数 | 自空间 rank | 自空间 余量 | 独立声纹 rank | 独立声纹 余量 |",
             "|---|---|---|---|---|---|"]
    for row in rank["rows"]:
        lines.append(f"| {row['exp']} ← {row['input']} | {row['refs']} | "
                     f"{row['qwen_rank']} | {_f4(row['qwen_margin'])} | "
                     f"{row['cam_rank']} | {_f4(row['cam_margin'])} |")
    return lines


def _rank_note(rank: dict) -> list[str]:
    if rank.get("error"):
        return []
    return [
        f"- 自空间 rank-1 率 **{rank['qwen_rank1_rate']}**"
        f"（余量 {_f4(rank['qwen_margin_min'])} ~ {_f4(rank['qwen_margin_max'])}）",
        f"- 独立声纹 rank-1 率 **{rank['cam_rank1_rate']}**"
        f"（余量 {_f4(rank['cam_margin_min'])} ~ {_f4(rank['cam_margin_max'])}）",
        "",
        "**读法**：独立声纹若整列 rank=1 且余量全为正，说明它有**分辨力**，只是「绝对刻度」"
        "被本批的短参考（4.2s 市场 preview）与近邻音色摊薄了 —— 那该换的是**判法**"
        "（相对余量），不是换尺子。反之若它连第 1 都排不出来，才说明这条链路测不出音色，"
        "得回头看素材/模型。",
    ]


# ---------------- 入口 ----------------

def run(*, reuse: bool = False, force: bool = False) -> dict:
    """一轮完整标定：准备素材 → （可选）推理 → 配对 → 打分 → 分析 → 落盘。"""
    out_dir = calib_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = prepare_inputs(out_dir)
    if not inputs:
        raise RuntimeError("没有任何可用输入参考音（3~30s 的他人参考一个都没找到）")
    refs = prepare_refs(out_dir)
    targets = resolve_targets(out_dir)

    outputs = convert_all(targets, inputs, out_dir, reuse=reuse, force=force)
    if not outputs:
        raise RuntimeError("一支变声输出都没产出（模型/输入都缺？看上面的跳过原因）")

    pairs = build_pairs(outputs, refs)
    needed = [p for pair in pairs for p in (pair["out_rel"], pair["ref_rel"])]
    embeds = embed_all(needed, out_dir / "embeddings.json", force=force)
    scored = score_pairs(pairs, embeds)
    results = analyze(scored)

    # 落盘：逐条分数 + 结果 + 报告（证据，可入库）
    csv_path = out_dir / "pairs.csv"
    cols = ["role", "exp", "input", "out_rel", "ref_id", "ref_rel",
            "target_speaker", "ref_speaker", "qwen_cos", "cam_cos"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored)
    results["inputs"] = {k: str(v.relative_to(ROOT)) for k, v in inputs.items()}
    results["targets"] = [_jsonable(t) for t in targets]
    (out_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    report = write_report(results, out_dir / "report.md")
    print(f"[calib] 报告：{report}", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RVC 变声验收 emb_sim 项的双空间标定")
    ap.add_argument("--reuse", action="store_true", help="已产出的变声输出直接复用（不推理）")
    ap.add_argument("--force", action="store_true", help="忽略嵌入缓存，全部重算")
    args = ap.parse_args(argv)
    try:
        res = run(reuse=args.reuse, force=args.force)
    except Exception as e:  # noqa: BLE001 —— CLI 给可读错误，不裸崩
        print(f"[calib] 失败：{e}", file=sys.stderr, flush=True)
        return 1
    d = res["legacy_decision"]
    print(f"[calib] 同人 {res['n_same']} / 异人 {res['n_cross']}", flush=True)
    print(f"[calib] 判定：{d['verdict']}（余量 {d['margin_low']:+.4f} / "
          f"{d['margin_high']:+.4f}）", flush=True)
    for key, ruler in res["rulers"].items():
        print(f"[calib] {key}: AUC={ruler['auc']:.4f} 同人 "
              f"{ruler['same']['min']:.4f}–{ruler['same']['max']:.4f} 异人 "
              f"{ruler['cross']['min']:.4f}–{ruler['cross']['max']:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

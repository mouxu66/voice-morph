"""公开许可语料的抓取与拼接（评测用，2026-10-08）。

为什么有这个模块
    真人小样本试跑的最长一环是"找人 + 录 65 秒"，而大创截止在即。公开许可语料能
    顶上这一环 —— 但**它是"合规"最容易翻车的地方**，所以这里把合规做成**机器判据**，
    而不是文档里的一句"已确认可用"：

    1. **许可白名单**：只接受 Apache-2.0 / CC0 / CC-BY / 公有领域。
       `--manifest` 里写别的（尤其 `CC-BY-NC-ND`）**直接拒绝执行**。
       为什么卡 ND：ST-CMDS 就是 `CC BY-NC-ND 4.0`，而本项目要用它的音频做
       **切档 + 拼接**（= 演绎），ND 与 NC 两条全部踩中 —— 这种"看起来是免费语料"
       的许可最容易被当成可用。白名单比逐个人工判断可靠。
    2. **不得进训练集**（AGENTS.md 袋鼠音素材铁律）：输出目录命中
       `clips / rvc_dataset / dataset_raw` 一类关键词就拒绝 —— 公开语料是评测素材，
       不是训练素材，这条界线以前只写在文档里。
    3. **逐条验证再拼接**：读不出来 / 削波 / 静音 的条**剔除并记录**，
       不静默拼进去（一条爆音的音频会让 reference 的 HNR/jitter 整个废掉）。

    拼接是**有代价的妥协**，不要藏：协议 §4.2 要求"一次连续录完"，而公开语料没有
    单条 65s 的素材。同一说话人、同一次录制的多条拼接，保住了"同一说话人/同一麦/
    同一房间"，丢掉的是"同一条 take" —— 所以产物只能支撑**链路与趋势验证**，
    不能当被试数据用（见 `--print-manifest` 输出的 usage_limits）。

用法
    # 现成清单（AISHELL-3，Apache-2.0，3 个说话人）见 tools/voicebank_public.aishell3.json
    python tools/voicebank_fetch_public.py --manifest tools/voicebank_public.aishell3.json \\
        --out media/vocals/public_pilot            # 真下（逐条落盘，可中断续跑）
    python tools/voicebank_fetch_public.py --manifest ... --out ... --dry-run
    python tools/voicebank_fetch_public.py --write-example tools/my_manifest.json   # 生成模板
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 只认这几条 —— 见模块注释第 1 条。键是**规范化后的小写许可名**。
ALLOWED_LICENSES = {
    "apache-2.0": "允许商用与演绎，署名即可",
    "cc0-1.0": "公有领域奉献，无义务",
    "cc-by-4.0": "允许商用与演绎，需署名",
    "public-domain": "公有领域",
}
# 明确点名的拒绝项（写清楚理由，比"不在白名单里"更能教育下一个人）
KNOWN_DENIED = {
    "cc-by-nc-nd-4.0": "NC（禁商用）+ ND（禁演绎）—— 切档与拼接就是演绎，两条都踩",
    "cc-by-nc-4.0": "NC（禁商用）",
    "cc-by-nd-4.0": "ND（禁演绎）",
    "research-only": "仅限研究用途，需逐案确认发布场景",
}
# 袋鼠音素材铁律：公开语料是**评测**素材，绝不允许落进训练集目录
FORBIDDEN_OUT_TOKENS = ("clips", "rvc_dataset", "dataset_raw", "dataset")

UA = {"User-Agent": "voice-morph-research/1.0 (local research use)"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_license(s: str) -> str:
    return str(s or "").strip().lower().replace(" ", "-").replace("_", "-")


def check_license(manifest: dict) -> dict:
    """许可门禁。返回 {ok, license, reason}；不抛。"""
    lic = _norm_license(manifest.get("license"))
    if lic in ALLOWED_LICENSES:
        return {"ok": True, "license": lic, "reason": ALLOWED_LICENSES[lic]}
    if lic in KNOWN_DENIED:
        return {"ok": False, "license": lic, "reason": KNOWN_DENIED[lic]}
    return {"ok": False, "license": lic,
            "reason": "不在许可白名单里（只用 Apache-2.0 / CC0 / CC-BY / 公有领域）"}


def check_out_dir(out: Path) -> dict:
    """输出目录不得是训练集目录（袋鼠音素材铁律）。"""
    parts = [p.lower() for p in Path(out).parts]
    for tok in FORBIDDEN_OUT_TOKENS:
        if any(tok in p for p in parts):
            return {"ok": False,
                    "reason": (f"输出目录命中「{tok}」—— 公开许可语料只能作评测素材，"
                               f"不得进训练集（AGENTS.md 袋鼠音素材铁律）")}
    return {"ok": True, "reason": ""}


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _inspect(path: Path) -> dict:
    """单条音频体检：读不出 / 削波 / 过短 / 近乎静音 都记原因（绝不抛）。"""
    try:
        import numpy as np
        import soundfile as sf

        info = sf.info(str(path))
        y, sr = sf.read(str(path), dtype="float32", always_2d=True)
        y = y[:, 0]
        peak = float(np.abs(y).max()) if y.size else 0.0
        rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
        return {"ok": True, "duration_s": round(info.frames / info.samplerate, 3),
                "samplerate": info.samplerate, "peak": round(peak, 4),
                "rms_db": round(20 * np.log10(rms + 1e-12), 1),
                "clipped": peak >= 0.999}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def fetch_speaker(spk: dict, manifest: dict, out_dir: Path, *, dry_run: bool = False,
                  timeout: int = 120, log=print) -> dict:
    """下一个说话人的 clips_per_speaker 条 → 拼成一条连续 wav。返回统计（不抛）。"""
    import numpy as np
    import soundfile as sf

    sid = str(spk["id"])
    base = str(manifest["base_url"]).rstrip("/")
    split = str(manifest.get("split") or "train")
    n_want = int(manifest.get("clips_per_speaker") or 20)
    gap_ms = int(manifest.get("gap_ms") or 300)
    start = int(spk.get("start_index") or 1)
    stem = str(spk.get("stem") or f"{sid}{{i:04d}}.wav")
    target_dir = Path(out_dir) / sid
    raw_dir = target_dir / "raw"

    res: dict = {"speaker": sid, "note": spk.get("note") or "", "url_dir": f"{base}/{split}/wav/{sid}",
                 "clips_ok": 0, "clips_skipped": [], "error": None, "error_stage": None,
                 "out": None, "total_s": None, "files": []}
    if dry_run:
        res["files"] = [f"{split}/wav/{sid}/" + stem.format(i=i) for i in range(start, start + n_want)]
        return res

    target_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    pieces: list = []
    sr_ref: int | None = None
    for i in range(start, start + n_want):
        name = stem.format(i=i)
        dest = raw_dir / name
        if not dest.exists():
            try:
                dest.write_bytes(_get(f"{base}/{split}/wav/{sid}/{name}", timeout=timeout))
            except urllib.error.HTTPError as e:
                res["clips_skipped"].append({"file": name, "reason": f"HTTP {e.code}"})
                continue
            except Exception as e:  # noqa: BLE001
                res["clips_skipped"].append({"file": name, "reason": f"{type(e).__name__}"})
                continue
        info = _inspect(dest)
        if not info.get("ok"):
            res["clips_skipped"].append({"file": name, "reason": f"读不出：{info.get('reason')}"})
            continue
        if info["clipped"]:
            res["clips_skipped"].append({"file": name, "reason": f"削波 peak={info['peak']}"})
            continue
        if info["duration_s"] < 0.5:
            res["clips_skipped"].append({"file": name, "reason": f"过短 {info['duration_s']}s"})
            continue
        if info["rms_db"] < -45:
            res["clips_skipped"].append({"file": name, "reason": f"近乎静音 rms={info['rms_db']}dB"})
            continue
        if sr_ref is None:
            sr_ref = info["samplerate"]
        elif info["samplerate"] != sr_ref:
            res["clips_skipped"].append(
                {"file": name, "reason": f"采样率不一致 {info['samplerate']}≠{sr_ref}（同一次录制应为同一设备）"})
            continue
        y, _ = sf.read(str(dest), dtype="float32", always_2d=True)
        pieces.append(y[:, 0])
        res["clips_ok"] += 1
        res["files"].append({"file": name, "duration_s": info["duration_s"],
                             "rms_db": info["rms_db"], "sha256": _sha256(dest)})

    if not pieces:
        res.update(error="没有一条可用音频", error_stage="抓取",
                   error_hint=f"先确认 {base}/{split}/wav/{sid}/ 存在且命名匹配 {stem}")
        return res

    gap = np.zeros(int(sr_ref * gap_ms / 1000), dtype="float32")
    stitched = np.concatenate([np.concatenate([p, gap]) for p in pieces])
    out = target_dir / "source.wav"
    sf.write(str(out), stitched, sr_ref, subtype="PCM_16")
    res["out"] = str(out)
    res["total_s"] = round(len(stitched) / sr_ref, 2)
    res["samplerate"] = sr_ref
    res["gap_ms"] = gap_ms
    log(f"[fetch] {sid}：{res['clips_ok']}/{n_want} 条可用 → {res['total_s']}s"
        f"（剔除 {len(res['clips_skipped'])} 条）")
    return res


# 写进 SOURCES.json 的字段（全量 res 里有 error_hint 这类运行期信息，不必入档）
SOURCE_SPEAKER_KEYS = ("speaker", "note", "url_dir", "clips_ok", "clips_skipped",
                       "total_s", "samplerate", "files")


def _sources_speaker(s: dict) -> dict:
    return {k: s[k] for k in SOURCE_SPEAKER_KEYS if k in s}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


USAGE_LIMITS = [
    "可用于：链路验证与趋势观察（评测矩阵能跑满、指标能测出）。",
    "不可用于：当作**被试数据** —— 没有知情同意流程，说话人也不是职业用嗓人群。",
    "不可用于：ABX 相似度对照（语料里没有本项目复现文本的真实录音，缺 truth/）。",
    "不可用于：任何训练集（袋鼠音素材铁律），也不得再分发原始音频。",
    "报结论时必须写明来源与许可，并标注「公开许可语料，非被试」。",
]


def run(manifest_path: Path, out: Path, *, dry_run: bool = False,
        log=print) -> dict:
    """整轮抓取。许可/目录门禁在下载之前 —— 不合规就一条都不下。"""
    res: dict = {"ok": False, "dry_run": dry_run, "licenses": None, "out_dir": str(out),
                 "speakers": [], "error": None, "error_stage": None, "at": _now(),
                 "usage_limits": USAGE_LIMITS}
    try:
        manifest = json.loads(Path(manifest_path).read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        res.update(error=f"manifest 读取失败: {type(e).__name__}: {e}", error_stage="读 manifest")
        return res

    lic = check_license(manifest)
    res["licenses"] = lic
    if not lic["ok"]:
        res.update(error=f"许可未通过：{lic['license'] or '(空)'} —— {lic['reason']}",
                   error_stage="许可门禁")
        return res
    dir_ok = check_out_dir(out)
    if not dir_ok["ok"]:
        res.update(error=dir_ok["reason"], error_stage="目录门禁")
        return res

    speakers = manifest.get("speakers") or []
    if not speakers:
        res.update(error="manifest 里没有 speakers", error_stage="读 manifest")
        return res

    log(f"[fetch] 语料 {manifest.get('corpus')} · 许可 {lic['license']}（{lic['reason']}）"
        f" · 署名 {manifest.get('attribution')}")
    for spk in speakers:
        got = fetch_speaker(spk, manifest, out, dry_run=dry_run, log=log)
        res["speakers"].append(got)
        if got.get("error") and res["error"] is None:
            res.update(error=got["error"], error_stage=f"抓取 {got['speaker']}")

    if not dry_run:
        sources = {
            # `license` 存**原样**（人工会拿它去对来源页），`license_id` 存规范化 id
            # （机器比对上白名单的那一个）。两者都留，审计时才不用猜。
            "corpus": manifest.get("corpus"), "license": manifest.get("license"),
            "license_id": lic["license"], "license_note": lic["reason"],
            "license_source": manifest.get("license_source"),
            "attribution": manifest.get("attribution"),
            "base_url": manifest.get("base_url"), "split": manifest.get("split"),
            "retrieved_at": _now(), "clips_per_speaker": manifest.get("clips_per_speaker"),
            "gap_ms": manifest.get("gap_ms"),
            "speakers": [_sources_speaker(s) for s in res["speakers"]],
            "note": ("同一说话人、同一次录制的多条拼接 —— 保住同一说话人/麦/房间，"
                     "丢掉'同一条 take'，故只能作链路与趋势验证"),
            "usage_limits": USAGE_LIMITS,
        }
        p = Path(out) / "SOURCES.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        res["sources"] = str(p)

    res["ok"] = res["error"] is None
    return res


def _example_manifest() -> dict:
    return {
        "corpus": "AISHELL-3",
        "license": "Apache-2.0",
        "license_source": "https://www.openslr.org/93/",
        "attribution": ("AISHELL-3: A Multi-speaker Mandarin TTS Corpus and the Baselines "
                        "(Shi et al., 2020), Beijing Shell Shell Technology Co.,Ltd."),
        "base_url": "https://hf-mirror.com/datasets/AISHELL/AISHELL-3/resolve/main",
        "split": "train",
        "clips_per_speaker": 26,
        "gap_ms": 300,
        "speakers": [
            {"id": "SSB0005", "note": "示例：按 spk-info.txt 选不同性别/年龄"},
            {"id": "SSB0033", "note": "示例"},
            {"id": "SSB1072", "note": "示例"},
        ],
    }


def main() -> int:
    import contextlib

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description="公开许可语料抓取与拼接（许可白名单门禁 + 不得进训练集）")
    p.add_argument("--manifest", help="manifest json（--write-example 可生成模板）")
    p.add_argument("--out", help="输出目录（如 media/vocals/public_pilot）")
    p.add_argument("--dry-run", action="store_true", help="只列将要下载的文件名")
    p.add_argument("--write-example", metavar="PATH",
                   help="写一份示例 manifest 后退出（不下载）")
    a = p.parse_args()

    if a.write_example:
        Path(a.write_example).write_text(
            json.dumps(_example_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[fetch] 已写示例 manifest: {a.write_example}")
        return 0
    if not a.manifest or not a.out:
        p.error("需要 --manifest 与 --out（或用 --write-example）")

    res = run(Path(a.manifest), Path(a.out), dry_run=a.dry_run)
    if res.get("error"):
        print(f"[fetch] 失败（{res.get('error_stage')}）：{res['error']}")
        return 1
    if res.get("dry_run"):
        for s in res["speakers"]:
            print(f"  {s['speaker']}: {len(s['files'])} 条 ← {s['url_dir']}")
        return 0
    print(f"[fetch] 完成：{len(res['speakers'])} 个说话人；来源记录 {res.get('sources')}")
    for lim in USAGE_LIMITS:
        print(f"[fetch] 边界：{lim}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

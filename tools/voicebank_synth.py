"""嗓音保存评测 · 批量合成（大创研究层，2026-10-08）。

为什么有这个模块
    评测协议 §5 要求「对矩阵里每一格生成一个样本」→
    `samples/<说话人>/<时长档>/<条件>.wav`；工具链里 `plan / split / measure /
    analyze / listen` 都在，**唯独"合成"这一步没有任何工具** —— 它只能靠人在
    界面上手工点。3 说话人 × 4 档 × 4 条件 = 48 格，手工点会带来两个安静的错：

      · **挑错参考音**：把 10s 档的样本放进 30s 档目录，自变量标签就是错的，
        而 `analyze` 照常出报告 —— 错的是结论，报错的是没人报错；
      · **漏一格**：矩阵缺格 → `analyze` 报"数据不足以判定"，
        错因指向统计而实际在采集。

    与 `voicepreserve.export_experiment` 同一个动机：**手工搬运产生的是静默错误**，
    所以这里按 `matrix.json` 逐格合成，参考音与文本都从矩阵里现读，不靠人记。

做法（不新开一条推理路径）
    1. 读 `<exp>/matrix.json` 的每一格（`text` / `ref` / `sample` 三个字段都在里面）；
    2. 把该格的参考音注册成一个**临时音色**（`media/voicebank/<tmp>/reference.wav`）
       —— 这正是 `common.voice_ref()` 与界面音色库读的同一份磁盘契约；
    3. `POST /api/tts`（app 自己的现役链路）→ 从返回的 `url` 取回音频字节 → 写目标；
    4. 无论成败**都删掉临时音色**（`finally`）—— 否则音色库会被 48 个实验音色污染；
    5. 落 `provenance.json`：每格记引擎 / 参考音 / 文本 / 实际时长 / 时间。

三条不做的（都是"做错了也不会报错"的地方）
    · **不伪造 ref_text**：ICL 需要与参考音**逐字对应**的文字稿。参考音是录音脚本的
      前缀截取，谁也不知道第 3 秒停在哪句中间 —— 猜出来的稿子与音频不符，
      只会把"复刻质量"这个因变量污染掉。所以默认**不带**（走纯声纹 x-vector），
      要带就由操作者用 `--ref-texts` 显式给每档一份 txt。
    · **不静默跳过失败**：一格失败就记一条，整轮结束打印 `N/M` 与逐条原因，
      有任何失败则以退出码 1 结束（半套矩阵比明显报错危险得多）。
    · **不写空文件占位**：拿不到音频就**不落盘** —— 空文件会让矩阵"看起来是满的"。

用法
    # 0) 前置：后端在跑（app 或 `python m2_server/server.py`），且已跑过 plan
    python tools/voicebank_synth.py --exp vb01 --dry-run          # 先看会合成哪些格
    python tools/voicebank_synth.py --exp vb01                    # 真的合成（可重复跑，断点续跑）
    python tools/voicebank_synth.py --exp vb01 --conditions same   # 只跑一个条件（冒烟）
    python tools/voicebank_synth.py --exp vb01 --ref-texts ref_texts/
    # 然后：measure --with-emb --with-asr → listen → analyze
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 让 `import config` 在「从仓库根直接跑 python tools/voicebank_synth.py」时也能work
# （与 voicebank_eval.py 同一段引导代码 —— 少了它就只有从 m2_server/ 目录跑才通）。
ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 临时音色前缀：跑完可以按它一眼找出漏删的（`ls media/voicebank | grep ^vbsyn`）
TMP_VOICE_PREFIX = "vbsyn"
DEFAULT_API = os.environ.get("VM_API_BASE", "http://127.0.0.1:8000")
# 首次合成要等 worker 加载 4.9G 模型（实测 1–3 分钟），超时给足；
# 之后的每格只要几秒 —— 这个默认值防的是"第一次就超时失败"那种假故障。
DEFAULT_TIMEOUT = 600


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


# ---------------- 目录（与 voicebank_eval 同一套约定） ----------------

def _default_root() -> Path:
    """默认落盘根 = `<OUTPUTS_DIR>/voicebank`（与 `voicebank_eval._default_root` 同源）。

    ⚠️ **延迟 import config**：config 在导入时读环境变量定路径，而测试靠
    `m2_server/conftest.py` 的 `VM_OUTPUTS_DIR` 做隔离（早绑定的坑，见
    `docs/犯错档案-工程.md` §8.36）。延迟 import 让测试永远不必碰真实 outputs/。
    """
    import config as cfg

    return Path(cfg.OUTPUTS_DIR) / "voicebank"


def _default_media_dir() -> Path:
    """音色库所在（临时音色要装进**后端认的那个** media 目录）。

    后端从源码跑（`python m2_server/server.py`）时 = 项目 `media/`；安装版是
    `%APPDATA%\\voice-morph-desktop\\media`。两者不一致时 TTS 会报
    「音色不存在」—— 所以 `--media-dir` 是显式参数，不猜。
    """
    import config as cfg

    return Path(cfg.MEDIA_DIR)


# ---------------- HTTP（只用 urllib，零新依赖，与 voicebank_eval 同风格） ----------------

def _post_json(url: str, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _http_detail(e: Exception) -> str:
    """把 HTTP 错误体里的中文 detail 提出来（FastAPI 的报错都写在里面）。"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = json.loads(e.read())
            detail = body.get("detail") or ""
            if isinstance(detail, list):           # 422 校验错误的形状
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            if detail:
                return f"HTTP {e.code}: {detail}"
        except Exception:  # noqa: BLE001
            pass
        return f"HTTP {e.code}"
    return f"{type(e).__name__}: {e}"


# ---------------- 选择格子 ----------------

def _wav_seconds(path: Path) -> float | None:
    """读 wav 时长；读不出来返回 None（**绝不抛**，与 voice_metrics 同一约定）。"""
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return round(float(info.frames) / float(info.samplerate), 2)
    except Exception:  # noqa: BLE001
        return None


def load_cells(exp: str, *, root: Path | None = None,
               speakers: list[str] | None = None,
               durations: list[str] | None = None,
               conditions: list[str] | None = None) -> dict:
    """读 matrix.json 并套过滤。返回 {matrix, cells, error, error_stage}（不抛）。"""
    base = (Path(root) if root else _default_root()) / exp
    mpath = base / "matrix.json"
    if not mpath.exists():
        return {"error": f"矩阵不存在: {mpath}（先跑 plan：python tools/voicebank_eval.py "
                         f"plan --exp {exp} --speakers ...）",
                "error_stage": "读矩阵", "cells": [], "base": base}
    try:
        matrix = _read_json(mpath)
    except Exception as e:  # noqa: BLE001
        return {"error": f"矩阵读取失败: {type(e).__name__}: {e}",
                "error_stage": "读矩阵", "cells": [], "base": base}

    cells = list(matrix.get("matrix") or [])
    if not cells:
        return {"error": f"{mpath} 里没有格子（matrix 为空）", "error_stage": "读矩阵",
                "cells": [], "base": base, "matrix": matrix}

    if speakers:
        want = {s.strip() for s in speakers if s.strip()}
        cells = [c for c in cells if c.get("speaker") in want]
    if durations:
        want = {d.strip() for d in durations if d.strip()}
        cells = [c for c in cells if c.get("duration_tag") in want]
    if conditions:
        want = {c.strip() for c in conditions if c.strip()}
        cells = [c for c in cells if c.get("condition") in want]
    return {"matrix": matrix, "cells": cells, "base": base, "error": None,
            "error_stage": None}


# ---------------- 临时音色 ----------------

def tmp_voice_id(exp: str, speaker: str, tag: str) -> str:
    """临时音色 id。必须匹配 `common._VOICE_ID_RE`（`^[A-Za-z0-9_-]+$`）。

    三段都要过一遍白名单：实验名可能带点号，说话人 id 可能是中文
    —— 而 `str.isalnum()` 对中文返回 **True**，只按它过滤会做出一个后端
    直接判非法的 id（`is_valid_voice_id` 用 `re.ASCII` 之外的 `[A-Za-z0-9_-]`），
    表现为合成时 404「音色不存在」，排查方向全指错。所以这里按 ASCII 判。
    """

    def _safe(s: str) -> str:
        return "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "_"
                       for ch in str(s))

    return f"{TMP_VOICE_PREFIX}_{_safe(exp)}_{_safe(speaker)}_{_safe(tag)}".lower()


def install_tmp_voice(media_dir: Path, voice_id: str, ref: Path,
                      ref_text: str = "") -> dict:
    """把一档参考音装成临时音色（app 读的磁盘契约：voicebank/<id>/reference.wav）。

    `ref_text` 为空时**不写** `ref_text.txt` —— 后端据此回退纯声纹模式，
    而不是拿一份猜出来的稿子去走 ICL（见模块注释第二段）。
    """
    if not ref.exists():
        return {"error": f"参考音不存在: {ref}", "error_stage": "装临时音色"}
    d = Path(media_dir) / "voicebank" / voice_id
    try:
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref, d / "reference.wav")
        rt = d / "ref_text.txt"
        if ref_text:
            rt.write_text(ref_text, encoding="utf-8")
        elif rt.exists():
            rt.unlink()          # 上一个实验留下的稿子不能串到这一档
    except Exception as e:  # noqa: BLE001
        return {"error": f"临时音色安装失败: {type(e).__name__}: {e}",
                "error_stage": "装临时音色"}
    return {"ok": True, "dir": str(d)}


def remove_tmp_voice(media_dir: Path, voice_id: str) -> dict:
    d = Path(media_dir) / "voicebank" / voice_id
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        return {"error": f"临时音色清理失败: {type(e).__name__}: {e}"}
    return {"ok": not d.exists()}


# ---------------- 单格合成 ----------------

def synth_cell(api: str, text: str, voice_id: str, *,
               timeout: int = DEFAULT_TIMEOUT, language: str = "zh",
               log=print) -> dict:
    """一格：`POST /api/tts` → 取回字节。返回 {wav_bytes, url, duration_s} 或 {error}。

    ★ 走 HTTP 而不是直接 import 后端模块，为的是**沿用 app 的现役链路**：
      参考音解析、ref_text 有无、worker 的懒启动与降级，全都在后端那一侧，
      这里再实现一遍就会慢慢和界面行为分叉。
    """
    try:
        data = _post_json(f"{api.rstrip('/')}/api/tts",
                          {"text": text, "voice_id": voice_id,
                           "text_language": language}, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        detail = _http_detail(e)
        if "404" in detail and "不存在" in detail:
            detail += ("（临时音色没装到**后端认的** media 目录 —— 检查 --media-dir；"
                       "源码模式是项目 media/，安装版是 %APPDATA%\\voice-morph-desktop\\media）")
        return {"error": f"合成失败: {detail}", "error_stage": "POST /api/tts"}
    url = str(data.get("url") or "")
    if not url:
        return {"error": f"合成响应里没有 url: {data}", "error_stage": "POST /api/tts"}
    try:
        blob = _get_bytes(f"{api.rstrip('/')}{url}", timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"error": f"取回音频失败: {_http_detail(e)}", "error_stage": "取回音频"}
    if not blob:
        return {"error": "取回音频为空（0 字节）", "error_stage": "取回音频"}
    return {"wav_bytes": blob, "url": url,
            "duration_s": data.get("duration_s")}


def _check_wav_bytes(blob: bytes) -> dict:
    """落盘前先验字节确实是一段能读的 wav —— 写坏文件比不写更危险。"""
    try:
        import io

        import soundfile as sf

        info = sf.info(io.BytesIO(blob))
        if info.frames <= 0:
            return {"error": "音频为 0 帧", "error_stage": "验字节"}
        return {"ok": True, "duration_s": round(float(info.frames) / info.samplerate, 2),
                "samplerate": info.samplerate}
    except Exception as e:  # noqa: BLE001
        return {"error": f"不是可读的 wav: {type(e).__name__}: {e}", "error_stage": "验字节"}


# ---------------- 主流程 ----------------

def run(exp: str, *, root: Path | None = None, media_dir: Path | None = None,
        api: str = DEFAULT_API, speakers: list[str] | None = None,
        durations: list[str] | None = None, conditions: list[str] | None = None,
        ref_texts_dir: Path | None = None, force: bool = False,
        dry_run: bool = False, keep_voices: bool = False,
        timeout: int = DEFAULT_TIMEOUT, log=print) -> dict:
    """整轮合成。返回 {ok, synthesized, skipped, failed, failures, provenance, ...}。

    可重复跑：已存在且非 `--force` 的样本直接跳过（断点续跑）—— 一轮 48 格要跑
    十几分钟，中途断了不该从头再来。
    """
    picked = load_cells(exp, root=root, speakers=speakers,
                        durations=durations, conditions=conditions)
    if picked.get("error"):
        return {"ok": False, **{k: picked[k] for k in ("error", "error_stage")},
                "synthesized": 0, "skipped": 0, "failed": 0, "failures": []}
    base: Path = picked["base"]
    cells: list[dict] = picked["cells"]
    mdir = Path(media_dir) if media_dir else _default_media_dir()

    res: dict = {"ok": True, "exp": exp, "root": str(base), "media_dir": str(mdir),
                 "api": api, "cells": len(cells), "synthesized": 0, "skipped": 0,
                 "failed": 0, "failures": [], "provenance": None, "dry_run": bool(dry_run),
                 "error": None, "error_stage": None, "notes": []}

    if not cells:
        res.update(ok=False, error="过滤后没有格子（检查 --speakers/--durations/--conditions）",
                   error_stage="选格")
        return res

    # 目标路径：matrix 里的 `sample` 是相对 <exp>/ 的路径；没有就按约定拼
    plan: list[tuple[dict, Path]] = []
    for c in cells:
        rel = c.get("sample") or f"samples/{c['speaker']}/{c['duration_tag']}/{c['condition']}.wav"
        plan.append((c, base / rel))

    todo = [(c, p) for c, p in plan if force or not p.exists()]
    res["skipped"] = len(plan) - len(todo)
    if res["skipped"]:
        res["notes"].append(f"{res['skipped']} 格已有样本，跳过（--force 可重合成）")

    if dry_run:
        res["plan"] = [{"sample_id": c.get("sample_id"), "target": str(p),
                        "tmp_voice": tmp_voice_id(exp, c["speaker"], c["duration_tag"]),
                        "ref": c.get("ref"), "exists": p.exists()}
                       for c, p in plan]
        log(f"[synth] dry-run：{len(plan)} 格（其中待合成 {len(todo)}，已存在 {res['skipped']}）")
        return res

    if not todo:
        log("[synth] 没有待合成的格（全部已存在）")
        return res

    # 按 (说话人, 时长档) 分组：同一档只装一次临时音色，减少切换
    groups: dict[tuple[str, str], list[tuple[dict, Path]]] = {}
    for c, p in todo:
        groups.setdefault((c["speaker"], c["duration_tag"]), []).append((c, p))

    prov_path = base / "provenance.json"
    prov = {}
    if prov_path.exists():
        try:
            prov = _read_json(prov_path)
        except Exception:  # noqa: BLE001
            res["notes"].append("provenance.json 损坏，本轮重建（旧内容不保留）")

    log(f"[synth] 待合成 {len(todo)} 格 / {len(groups)} 个（说话人×档）组合；"
        f"音色库 {mdir}；后端 {api}")
    log("[synth] 首次合成要等 worker 加载模型（1–3 分钟），属正常现象")

    cells_prov = prov.get("cells") or {}
    for (spk, tag), items in sorted(groups.items()):
        vid = tmp_voice_id(exp, spk, tag)
        sample = items[0][0]
        ref = base / str(sample.get("ref") or f"refs/{spk}/{tag}.wav")
        ref_text = ""
        if ref_texts_dir:
            rt = Path(ref_texts_dir) / spk / f"{tag}.txt"
            if rt.exists():
                ref_text = rt.read_text("utf-8").strip()
            else:
                res["notes"].append(f"缺 ref_text（{rt}），该档按纯声纹合成")
        inst = install_tmp_voice(mdir, vid, ref, ref_text)
        if inst.get("error"):
            for c, _ in items:
                res["failed"] += 1
                res["failures"].append({"sample_id": c.get("sample_id"),
                                        "error": inst["error"],
                                        "error_stage": inst["error_stage"]})
            continue
        log(f"[synth] {spk}/{tag}：参考音 {ref.name}"
            f"（{_wav_seconds(ref) if ref.exists() else '缺失'}s，"
            f"{'ICL + 提供稿' if ref_text else '纯声纹'}）→ 音色 {vid}")
        try:
            for c, target in items:
                t0 = time.time()
                got = synth_cell(api, str(c.get("text") or ""), vid,
                                 timeout=timeout, log=log)
                if got.get("error"):
                    res["failed"] += 1
                    res["failures"].append({"sample_id": c.get("sample_id"),
                                            "error": got["error"],
                                            "error_stage": got.get("error_stage")})
                    log(f"[synth]   ✗ {c.get('sample_id')}: {got['error']}")
                    continue
                chk = _check_wav_bytes(got["wav_bytes"])
                if chk.get("error"):
                    res["failed"] += 1
                    res["failures"].append({"sample_id": c.get("sample_id"),
                                            "error": chk["error"],
                                            "error_stage": chk["error_stage"]})
                    log(f"[synth]   ✗ {c.get('sample_id')}: {chk['error']}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(got["wav_bytes"])
                res["synthesized"] += 1
                cells_prov[str(c.get("sample_id"))] = {
                    "sample_id": c.get("sample_id"), "speaker": spk, "duration_tag": tag,
                    "condition": c.get("condition"),
                    # 一律正斜杠：matrix.json 与评测报告都是 POSIX 风格路径，
                    # 混进 Windows 反斜杠会让"同一份记录在 Linux/CI 上对不上"
                    "ref": ref.relative_to(base).as_posix(),
                    "ref_actual_s": _wav_seconds(ref),
                    "ref_text": ref_text,                      # 空串 = 纯声纹（如实记录）
                    "text": c.get("text"), "style": c.get("style"),
                    "voice_id": vid, "target": target.relative_to(base).as_posix(),
                    "audio_s": chk["duration_s"], "elapsed_s": round(time.time() - t0, 2),
                    "at": _now(),
                }
                log(f"[synth]   ✓ {c.get('sample_id')}  "
                    f"{chk['duration_s']}s（{round(time.time() - t0, 1)}s）")
        finally:
            # 无论中间哪一格炸了都清 —— 半路留下临时音色会在音色库里挂一周没人认领
            if not keep_voices:
                rm = remove_tmp_voice(mdir, vid)
                if rm.get("error"):
                    res["notes"].append(rm["error"])

    prov.update({
        "exp": exp, "engine": "qwen3-tts（app 现役链路 POST /api/tts）",
        "api": api, "seed": None,           # 该链路不暴露随机种子 —— 如实标注，不编
        "ref_text_mode": ("每档 txt（ICL）" if ref_texts_dir else
                          "无 ref_text（纯声纹 x-vector）"),
        "first_run_at": prov.get("first_run_at") or _now(),
        "updated_at": _now(),
        "cells": cells_prov,
    })
    runs = prov.get("runs") or []
    runs.append({"at": _now(), "synth": res["synthesized"], "skip": res["skipped"],
                 "fail": res["failed"], "cells_total": res["cells"]})
    prov["runs"] = runs[-20:]
    res["provenance"] = str(_write_json(prov_path, prov).relative_to(base.parent))

    if res["failed"]:
        res["ok"] = False
        res["error"] = (f"{res['failed']} 格失败（成功 {res['synthesized']}）—— "
                        f"不静默放过：半套矩阵会让 analyze 把错因指向统计")
        res["error_stage"] = "合成"
    log(f"[synth] 完成：成功 {res['synthesized']} / 跳过 {res['skipped']} / "
        f"失败 {res['failed']}（共 {res['cells']} 格）")
    return res


def main() -> int:
    import contextlib

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description="按 matrix.json 批量合成评测样本（走 app 现役 TTS 链路）")
    p.add_argument("--exp", required=True, help="实验名（outputs/voicebank/<exp>/）")
    p.add_argument("--root", help="落盘根（默认 <OUTPUTS_DIR>/voicebank）")
    # ⚠️ help 字符串会被 argparse 做 `%` 格式化：这里必须写 `%%APPDATA%%`，
    # 否则 `--help` 自己就崩（ValueError: unsupported format character 'A'）。
    p.add_argument("--media-dir",
                   help="音色库所在 media 目录（源码模式=项目 media/，安装版=%%APPDATA%% 下）")
    p.add_argument("--api", default=DEFAULT_API, help=f"后端地址（默认 {DEFAULT_API}）")
    p.add_argument("--speakers", help="说话人 id，逗号分隔（默认全部）")
    p.add_argument("--durations", help="时长档标签，逗号分隔（如 3s,10s）")
    p.add_argument("--conditions", help="条件，逗号分隔（默认全部）")
    p.add_argument("--ref-texts", help="每档文字稿目录：<dir>/<说话人>/<档>.txt（可选）")
    p.add_argument("--force", action="store_true", help="重合成已存在的样本")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不动任何文件")
    p.add_argument("--keep-voices", action="store_true",
                   help="保留临时音色（默认跑完即删）")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单次请求超时秒")
    a = p.parse_args()

    def _split(v):
        return [x.strip() for x in v.split(",") if x.strip()] if v else None

    res = run(a.exp, root=Path(a.root) if a.root else None,
              media_dir=Path(a.media_dir) if a.media_dir else None,
              api=a.api, speakers=_split(a.speakers), durations=_split(a.durations),
              conditions=_split(a.conditions),
              ref_texts_dir=Path(a.ref_texts) if a.ref_texts else None,
              force=a.force, dry_run=a.dry_run, keep_voices=a.keep_voices,
              timeout=a.timeout)
    for n in res.get("notes") or []:
        print(f"[synth] 注意：{n}")
    if res.get("error"):
        print(f"[synth] 失败（{res.get('error_stage')}）：{res['error']}")
        return 1
    if res.get("dry_run"):
        for it in res.get("plan") or []:
            mark = "已存在" if it["exists"] else "待合成"
            print(f"  [{mark}] {it['sample_id']}  ref={it['ref']}  →  {it['target']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

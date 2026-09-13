# -*- coding: utf-8 -*-
"""音色入库自动质检：数据集预检 + 变声验收，结果落盘 outputs/qc/<exp>.json。

用法（跑在主 .venv，与 8000 服务同环境；变声验收依赖 worker 8001）：
    # 数据集预检：切片数/总时长/时长分布(max/median)/响度(max_dBFS)/语速（超 6 字/秒记警告）
    python tools/voice_qc.py --dataset D:\\RVC\\dataset\\<音色名>

    # 变声验收：测试音频 → 离线变声 → 时长比/f0偏移/ASR重合/声纹余弦 → score 0-100
    python tools/voice_qc.py --voice <音色名>

    # 两种模式可同时跑（同名时合并写进同一个 <exp>.json）
    python tools/voice_qc.py --dataset D:\\RVC\\dataset\\<音色名> --voice <音色名>

变声验收口径：
    - 测试输入优先用「其他音色」的参考音频（跨音色转换才检验泛化），音色库只有
      一个音色时回退该音色自己的数据集样本（自转换，记 self_convert=true）；
    - RVC 推理走 offline_vc_infer.py（与离线变声页同一文件级链路，pitch=0）；
    - 声纹余弦 = 变声输出 vs 目标音色参考（voicebank reference.wav，缺失回退数据集
      样本），阈值 ≥0.95；ASR 字符重合度阈值 ≥0.6（whisper 逐次转写有噪声，与
      tools/cascade_acceptance.py 同口径）。
任何一步失败都会把 error 写进 JSON、绝不裸崩（rvc_live 训练完成回调依赖此约定）。
"""
import argparse
import json
import math
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
import config as cfg  # noqa: E402

WORKER = "http://127.0.0.1:8001"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
INFER_PY = ROOT / "m2_server" / "offline_vc_infer.py"
QC_DIR = cfg.OUTPUTS_DIR / "qc"

# 语速警告阈值（字/秒）：素材语速过快会直接劣化模型可懂度（作者踩过的坑）
SPEED_WARN = 6.0


def _get_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _transcribe(path: Path, timeout: int = 300) -> str:
    """worker /transcribe。vad_filter=False：切片已按静音切好，whisper 内部 VAD
    会吞短片段（级联链路踩过的坑，分不清"没说话"和"被吃掉"）。"""
    data = _post_json(WORKER + "/transcribe",
                      {"path": str(path), "vad_filter": False}, timeout=timeout)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return str(data.get("text") or "")


def _emb(path: Path) -> list:
    data = _post_json(WORKER + "/emb", {"path": str(path)}, timeout=300)
    if data.get("error") or not data.get("emb"):
        raise RuntimeError(f"声纹提取失败: {data.get('error')}")
    return data["emb"]


_PUNCT = set("，。！？；：、·…—“”‘’（）《》〈〉【】,.!?;:()[]{}<>\"'`~@#$%^&*_+=/\\|- \t\r\n")


def _char_count(text: str) -> int:
    """语速分子：去掉标点/空白后的字数。"""
    return sum(1 for ch in text if ch not in _PUNCT)


def _char_overlap(text_a: str, text_b: str) -> float:
    """字符重合度（与 tools/cascade_acceptance.py 同口径：字符集合交/并，去逗号）。"""
    a = set(text_a.replace("，", "").replace(",", ""))
    b = set(text_b.replace("，", "").replace(",", ""))
    return len(a & b) / max(len(a | b), 1)


def _wav_info(path: Path) -> tuple[float, float]:
    """返回 (时长秒, max_dBFS)。"""
    import numpy as np
    import soundfile as sf

    d, sr = sf.read(str(path))
    if d.ndim > 1:
        d = d.mean(-1)
    dur = len(d) / sr
    peak = float(np.abs(d).max()) if len(d) else 0.0
    dbfs = 20.0 * math.log10(peak) if peak > 0 else -120.0
    return dur, dbfs


_F0_SCRIPT = (
    "import json, sys\n"
    "import numpy as np\n"
    "import parselmouth\n"
    "snd = parselmouth.Sound(sys.argv[1])\n"
    "pitch = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=800)\n"
    "f0 = pitch.selected_array['frequency']\n"
    "v = f0[f0 > 0]\n"
    "print(json.dumps(float(np.median(v)) if len(v) else 0.0))\n"
)


def _f0_median(path: Path) -> float:
    """voiced 段 f0 中位数（参数与 tools/measure_f0_ab.py 一致）；无浊音段返回 0。

    parselmouth 缺失时回退用 RVC venv 的 python 子进程算（那边带 RVC 整合包依赖），
    保证质检脚本无论被哪个 python 拉起（安装版后端不一定装了 parselmouth）都能算。
    """
    try:
        import numpy as np
        import parselmouth

        snd = parselmouth.Sound(str(path))
        pitch = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=800)
        f0 = pitch.selected_array["frequency"]
        voiced = f0[f0 > 0]
        return float(np.median(voiced)) if len(voiced) else 0.0
    except ImportError:
        if not RVC_VENV_PY.exists():
            raise RuntimeError("parselmouth 不可用（当前环境未装且 RVC venv 缺失）")
        r = subprocess.run([str(RVC_VENV_PY), "-c", _F0_SCRIPT, str(path)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"RVC venv f0 计算失败: {r.stderr.strip()[:200]}")
        return float(r.stdout.strip().splitlines()[-1])


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------- 数据集预检 ----------------

def run_dataset(dataset_dir: Path) -> dict:
    res = {"dir": str(dataset_dir), "clips": 0, "total_s": 0.0,
           "duration_max_s": None, "duration_median_s": None,
           "max_dbfs": None, "dbfs_median": None,
           "speech_rate": None, "chars": 0, "transcribed": 0,
           "transcribe_errors": 0, "warnings": [], "error": None,
           "error_stage": None}
    if not dataset_dir.exists():
        res["error"] = f"数据集目录不存在: {dataset_dir}"
        res["error_stage"] = "数据集目录"
        return res
    wavs = sorted(dataset_dir.glob("*.wav"))
    res["clips"] = len(wavs)
    if not wavs:
        res["error"] = f"数据集目录没有 wav: {dataset_dir}"
        res["error_stage"] = "数据集读取"
        return res

    durs, dbfs, bad = [], [], 0
    for f in wavs:
        try:
            dur, db = _wav_info(f)
        except Exception:
            bad += 1
            continue
        durs.append(dur)
        dbfs.append(db)
    if bad:
        res["warnings"].append(f"{bad} 个文件读取失败")
    if not durs:
        res["error"] = "没有可读取的 wav"
        res["error_stage"] = "数据集读取"
        return res
    durs.sort()
    dbfs.sort()
    res["total_s"] = round(sum(durs), 1)
    res["duration_max_s"] = round(durs[-1], 2)
    res["duration_median_s"] = round(durs[len(durs) // 2], 2)
    res["max_dbfs"] = round(dbfs[-1], 1)
    res["dbfs_median"] = round(dbfs[len(dbfs) // 2], 1)

    # 语速 = 转写字数 ÷ 总时长
    print(f"[dataset] 逐条转写 {len(wavs)} 个切片估语速…", flush=True)
    for i, f in enumerate(wavs, 1):
        try:
            res["chars"] += _char_count(_transcribe(f))
            res["transcribed"] += 1
        except Exception:
            res["transcribe_errors"] += 1
        if i % 10 == 0 or i == len(wavs):
            print(f"[dataset] {i}/{len(wavs)}（转写失败 {res['transcribe_errors']}）", flush=True)
    if res["transcribe_errors"] == len(wavs):
        res["warnings"].append("全部转写失败（worker 8001 未启动？），语速未评估")
    elif res["total_s"] > 0 and res["chars"] > 0:
        rate = res["chars"] / res["total_s"]
        res["speech_rate"] = round(rate, 2)
        if rate > SPEED_WARN:
            res["warnings"].append(
                f"语速过快 {rate:.1f} 字/秒（阈值 {SPEED_WARN}），素材本身语速快会直接劣化模型可懂度")
    return res


# ---------------- 变声验收 ----------------

def _find_infer_pth(voice_id: str) -> Path | None:
    """找可推理权重（与 offline_vc._find_infer_pth 同规则，复制过来以免 import 链
    拉起 rvc_live 的声卡自检）：assets/weights/<id>.pth 优先，缺失时从 G_*.pth 提取。"""
    infer_pth = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
    if infer_pth.exists():
        return infer_pth
    log_dir = cfg.RVC_ROOT / "logs" / voice_id
    ckpt = next(iter(sorted(log_dir.glob("G_*.pth"))), None)
    if ckpt is None:
        return None
    try:
        import os
        import shutil

        sys.path.insert(0, str(cfg.RVC_ROOT))
        os.environ["PYTHONPATH"] = str(cfg.RVC_ROOT)
        from train.process_ckpt import extract_small_model

        (cfg.RVC_ROOT / "assets" / "weights").mkdir(parents=True, exist_ok=True)
        extract_small_model(str(ckpt), voice_id, "48k", 1, f"{voice_id} RVC v2 48k", "v2")
        if not infer_pth.exists():
            return None
        shutil.copy2(infer_pth, log_dir / f"{voice_id}.pth")
        return infer_pth
    except Exception:
        return None


def _pick_test_input(exp: str) -> tuple[Path | None, bool]:
    """选变声验收的测试输入：其他音色的参考音频（跨音色泛化）→ media/clips 最长
    切片 → 自身数据集样本（自转换）。返回 (path, self_convert)。"""
    bank = cfg.MEDIA_DIR / "voicebank"
    if bank.exists():
        for d in sorted(bank.iterdir()):
            if d.name == exp or not (d / "reference.wav").exists():
                continue
            try:
                dur, _ = _wav_info(d / "reference.wav")
            except Exception:
                continue
            if 3.0 <= dur <= 30.0:
                return d / "reference.wav", False
    for cand_dir in (cfg.MEDIA_DIR / "clips", cfg.RVC_ROOT / "dataset" / exp):
        if not cand_dir.exists():
            continue
        best, best_dur = None, 0.0
        for f in sorted(cand_dir.glob("*.wav")):
            try:
                dur, _ = _wav_info(f)
            except Exception:
                continue
            if 3.0 <= dur <= 30.0 and dur > best_dur:
                best, best_dur = f, dur
        if best is not None:
            return best, cand_dir.name == exp
    return None, False


def _pick_emb_ref(exp: str, test_in: Path, self_convert: bool) -> Path | None:
    """声纹对比的「目标音色」参考：自转换时直接用输入；否则 voicebank 参考音频 →
    数据集最长样本（优先 ≤20s 的）。"""
    if self_convert:
        return test_in
    ref = cfg.MEDIA_DIR / "voicebank" / exp / "reference.wav"
    if ref.exists():
        return ref
    ds = cfg.RVC_ROOT / "dataset" / exp
    if not ds.exists():
        return None
    cands: list[tuple[float, Path]] = []
    for f in sorted(ds.glob("*.wav")):
        try:
            dur, _ = _wav_info(f)
        except Exception:
            continue
        cands.append((dur, f))
    if not cands:
        return None
    pool = [c for c in cands if c[0] <= 20.0] or cands
    return max(pool, key=lambda c: c[0])[1]


def run_voice(exp: str, ref: str = "") -> dict:
    res = {"items": {}, "score": None, "pass": None, "input": "", "output": "",
           "emb_ref": "", "self_convert": False, "error": None,
           "error_stage": None, "hint": None}

    try:
        _get_json(WORKER + "/health", timeout=10)
    except Exception as e:
        res["error"] = f"声纹/转写服务（worker 8001）未启动（{e}），ASR/声纹指标不可用"
        res["error_stage"] = "声纹·转写服务"
        res["hint"] = "请在「离线变声」或「级联」页确认 8001 worker 已启动，再触发质检"
        return res

    pth = _find_infer_pth(exp)
    if pth is None:
        res["error"] = (f"音色 [{exp}] 没有可推理的 RVC 权重"
                        f"（assets/weights/{exp}.pth 缺失且无法从 G_*.pth 提取）")
        res["error_stage"] = "RVC 权重"
        res["hint"] = "先完成该音色的 RVC 训练，或确认 assets/weights 下的权重文件存在"
        return res
    log_dir = cfg.RVC_ROOT / "logs" / exp
    index = next(iter(sorted(log_dir.glob("added_*.index"))), None)

    test_in, self_convert = _pick_test_input(exp)
    if test_in is None:
        res["error"] = "找不到测试音频（其他音色参考/切片/自身数据集都没有 3~30s 的 wav）"
        res["error_stage"] = "测试音频"
        res["hint"] = "为该音色准备一段 3~30 秒的参考/测试音频（voicebank 参考、clips 或数据集切片）"
        return res
    emb_ref = _pick_emb_ref(exp, test_in, self_convert)
    if ref:
        ref_p = Path(ref)
        if not ref_p.exists():
            res["error"] = f"显式参考音频不存在: {ref}"
            res["error_stage"] = "参考音频"
            return res
        emb_ref = ref_p
    res.update(input=str(test_in), self_convert=self_convert,
               emb_ref=str(emb_ref) if emb_ref else "")

    QC_DIR.mkdir(parents=True, exist_ok=True)
    in_16k = QC_DIR / f"{exp}_vc_in_16k.wav"
    out_path = QC_DIR / f"{exp}_vc_out.wav"

    # 预处理：统一 16k 单声道（与离线变声页同一处理）
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(test_in),
         "-af", "aresample=16000", "-ac", "1", str(in_16k)],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not in_16k.exists():
        res["error"] = f"ffmpeg 预处理失败: {r.stderr.strip()[:300]}"
        res["error_stage"] = "音频预处理"
        res["hint"] = "确认 ffmpeg 已安装并加入系统 PATH"
        return res

    # RVC 离线推理：整段文件级链路（RVC venv 子进程），pitch=0 保持源音高
    print(f"[voice] RVC 推理：{test_in.name} -> {out_path.name}（pitch=0）", flush=True)
    r = subprocess.run(
        [str(RVC_VENV_PY), str(INFER_PY), "--pth", str(pth),
         "--index", str(index) if index else "",
         "--input", str(in_16k), "--output", str(out_path),
         "--pitch", "0", "--index-rate", "0.5"],
        capture_output=True, text=True, timeout=1800,
        encoding="utf-8", errors="replace", cwd=str(cfg.RVC_ROOT))
    if r.returncode != 0 or not out_path.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        res["error"] = "RVC 推理失败: " + " | ".join(tail)[-400:]
        res["error_stage"] = "RVC 推理"
        res["hint"] = "查看 RVC 训练日志，确认权重未损坏、显存充足，且推理链路无异常"
        return res
    res["output"] = str(out_path)

    items: dict[str, dict] = {}

    def _item(key: str, value, ok: bool, score_f: float, detail: str):
        items[key] = {"value": None if value is None else round(float(value), 4),
                      "pass": bool(ok), "score": round(25.0 * score_f, 1), "detail": detail}

    # 1) 时长比（输出/输入）：RVC 忠实保速，偏离 1 说明引擎或素材异常
    dur_in, _ = _wav_info(in_16k)
    dur_out, _ = _wav_info(out_path)
    ratio = dur_out / dur_in if dur_in > 0 else None
    if ratio is None:
        _item("duration_ratio", None, False, 0.0, f"输入时长异常 {dur_in:.2f}s")
    else:
        f = 1.0 - _clamp01((abs(ratio - 1.0) - 0.05) / 0.40)
        _item("duration_ratio", ratio, 0.75 <= ratio <= 1.25, f,
              f"输出/输入 = {ratio:.2f}（{dur_out:.1f}s / {dur_in:.1f}s）")

    # 2) f0 中位数偏移（半音）：pitch=0 时输出应跟随源音高，偏移大即伪影/跑调
    try:
        f0_in, f0_out = _f0_median(in_16k), _f0_median(out_path)
        if f0_in > 0 and f0_out > 0:
            shift = 12.0 * math.log2(f0_out / f0_in)
            f = 1.0 - _clamp01((abs(shift) - 1.0) / 5.0)
            _item("f0_shift", shift, abs(shift) <= 3.0, f,
                  f"{shift:+.1f} 半音（{f0_in:.0f}Hz → {f0_out:.0f}Hz）")
        else:
            _item("f0_shift", None, False, 0.0,
                  f"浊音段不足（f0: {f0_in:.0f}/{f0_out:.0f}Hz）")
    except Exception as e:
        _item("f0_shift", None, False, 0.0, f"parselmouth 计算失败: {e}")

    # 3) ASR 字符重合度：可懂度/伪影的最敏感指标（电音伪影会推高错字率）
    try:
        tr_in, tr_out = _transcribe(in_16k), _transcribe(out_path)
        ov = _char_overlap(tr_in, tr_out)
        f = _clamp01((ov - 0.3) / 0.5)
        _item("asr_overlap", ov, ov >= 0.6, f,
              f"字符重合度 {ov:.2f}（源: {tr_in[:24]}｜出: {tr_out[:24]}）")
    except Exception as e:
        _item("asr_overlap", None, False, 0.0, f"转写失败: {e}")

    # 4) 声纹余弦：变声输出 vs 目标音色参考，阈值 ≥0.95
    try:
        if emb_ref is None:
            raise RuntimeError("没有目标音色参考音频（voicebank/数据集都缺）")
        import numpy as np

        v1, v2 = np.asarray(_emb(out_path)), np.asarray(_emb(emb_ref))
        sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
        f = _clamp01((sim - 0.90) / 0.07)
        ref_name = "输入自身" if self_convert else Path(emb_ref).name
        _item("emb_sim", sim, sim >= 0.95, f, f"声纹余弦 {sim:.4f}（vs {ref_name}）")
    except Exception as e:
        _item("emb_sim", None, False, 0.0, f"声纹对比失败: {e}")

    res["items"] = items
    res["score"] = round(sum(it["score"] for it in items.values())) if items else None
    res["pass"] = all(it["pass"] for it in items.values()) if items else False
    return res


# ---------------- 落盘与汇总 ----------------

def _qc_file(exp: str) -> Path:
    return QC_DIR / f"{exp}.json"


def _load_or_init(exp: str) -> dict:
    f = _qc_file(exp)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"exp": exp}


def _write_section(exp: str, section: str, payload: dict) -> Path:
    """把某个模式的结果合并进 outputs/qc/<exp>.json（两种模式同名时共存一文件）。"""
    data = _load_or_init(exp)
    data.update(exp=exp, created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    data[section] = payload
    if section == "voice":
        data["score"] = payload.get("score")
        data["pass"] = payload.get("pass")
    QC_DIR.mkdir(parents=True, exist_ok=True)
    _qc_file(exp).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return _qc_file(exp)


def _print_summary(exp: str, data: dict):
    print(f"\n=== 音色质检 {exp} ===")
    ds = data.get("dataset")
    if ds:
        if ds.get("error"):
            print(f"数据集预检失败（{ds.get('error_stage') or '未知'}）: {ds['error']}")
        else:
            print(f"数据集: {ds.get('clips')} 条 / {ds.get('total_s')}s，"
                  f"时长 max {ds.get('duration_max_s')}s · median {ds.get('duration_median_s')}s，"
                  f"响度 max {ds.get('max_dbfs')}dBFS，语速 {ds.get('speech_rate')} 字/秒")
            for w in ds.get("warnings", []):
                print(f"  警告: {w}")
    v = data.get("voice")
    if v:
        if v.get("error"):
            print(f"变声验收失败（{v.get('error_stage') or '未知'}）: {v['error']}")
            if v.get("hint"):
                print(f"  → 排查建议: {v['hint']}")
        else:
            for k, it in v.get("items", {}).items():
                print(f"  {k}: {it['detail']}  ->  {'PASS' if it['pass'] else 'FAIL'}")
            print(f"  score {v.get('score')}/100  ->  {'PASS' if v.get('pass') else 'FAIL'}")


def main():
    p = argparse.ArgumentParser(description="音色入库自动质检")
    p.add_argument("--dataset", help="数据集预检：切片数/时长分布/响度/语速")
    p.add_argument("--voice", help="变声验收：测试音频经离线变声后四项指标 + score")
    p.add_argument("--ref", help="显式指定声纹对比的目标音色参考音频（缺省自动查找 voicebank/数据集）")
    args = p.parse_args()
    if not args.dataset and not args.voice:
        p.error("至少指定 --dataset 或 --voice 之一")

    exps: list[str] = []
    failed = False
    if args.dataset:
        exp = Path(args.dataset).name
        print(f"[dataset] 预检 {args.dataset}", flush=True)
        try:
            payload = run_dataset(Path(args.dataset))
        except Exception as e:
            payload = {"error": f"{type(e).__name__}: {e}", "error_stage": "未知", "warnings": []}
        if payload.get("error"):
            failed = True
        _write_section(exp, "dataset", payload)
        exps.append(exp)
    if args.voice:
        exp = args.voice
        print(f"[voice] 验收音色 {exp}", flush=True)
        try:
            payload = run_voice(exp, args.ref or "")
        except Exception as e:
            payload = {"error": f"{type(e).__name__}: {e}", "error_stage": "未知",
                       "hint": "质检脚本意外崩溃，查看控制台堆栈定位", "items": {},
                       "score": None, "pass": None}
        if payload.get("error"):
            failed = True
        _write_section(exp, "voice", payload)
        if exp not in exps:
            exps.append(exp)

    for exp in exps:
        _print_summary(exp, _load_or_init(exp))
        print(f"结果: {_qc_file(exp)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

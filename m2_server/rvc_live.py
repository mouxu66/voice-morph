"""实时变声一键能力：RVC RealtimeVST + 训练触发 + 声卡自动切换/还原。

实时链路：
  麦克风阵列(真麦) → RVC 实时变声 → CABLE Input → CABLE Output → 微信/游戏等(录音= CABLE Output)
一键 start 会：
  1. 校验目标模型就绪（logs/<exp>/<exp>.pth + index）
  2. 把 RVC 实时配置( configs/config.json )预填为该模型 + 麦克风阵列 → CABLE Input
  3. 调用 audio_config.ps1 apply：录音默认切到 CABLE Output（自动备份原设备）
  4. 后台拉起 realtime_gui.py（新控制台），并守护进程 —— 退出后自动还原声卡

实验名(exp)即音色 ID：训练接口可传任意 exp/dataset，不再锁定单一音色。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    import config as cfg
except ImportError:  # 兜底：直接以模块方式运行时
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config as cfg

API_PREFIX = "/api"

ROOT = Path(__file__).resolve().parent.parent
RVC_ROOT = cfg.RVC_ROOT
CONFIG_JSON = RVC_ROOT / "configs" / "config.json"
REALTIME_PY = RVC_ROOT / "realtime_gui.py"
# 本机 RVC 环境没有 runtime 目录，Python 解释器在 .venv（训练驱动脚本亦如此）
RUNTIME_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
# 通用训练驱动（任意音色），train_meituan_rat.py 仅是其兼容包装
TRAIN_PY = RVC_ROOT / "train_rvc_voice.py"
AUDIO_PS1 = ROOT / "m2_server" / "audio_config.ps1"

# 真实麦克风与虚拟声卡（由 audio_config.ps1 list 探测确定）
INPUT_DEVICE = os.environ.get("VM_LIVE_INPUT_DEVICE", "麦克风阵列")
OUTPUT_DEVICE = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")

CREATE_NEW_CONSOLE = 0x00000010

DEFAULT_EPOCHS = 40


def _active_exp() -> str:
    """当前生效的 RVC 实验名（音色 ID）：最近一次启动的训练，否则回退默认。"""
    return _state["train"].get("exp") or cfg.RVC_DEFAULT_EXP


def _find_pth(exp: str, log_dir: Path) -> Path | None:
    """找到该实验可用的 RVC 最终权重：优先 <exp>.pth，否则回退 G_<...>.pth。

    RVC 训练脚本落盘文件名是 G_2333333.pth / D_2333333.pth（并非 <exp>.pth），
    而实时加载、前端 status 都按 <exp>.pth 判定，两者命名不一致会误报“未训练”。
    这里优先用标准名，缺失时回退到训练自动产出的 G_*.pth，避免手动复制。
    """
    p = log_dir / f"{exp}.pth"
    if p.exists():
        return p
    return next(log_dir.glob("G_*.pth"), None)


def _ensure_standard_pth(exp: str, log_dir: Path) -> Path | None:
    """确保拿到可实时推理的权重（rtrvc.get_synthesizer 只认含 weight 键的推理格式）。

    优先 assets/weights/<exp>.pth（推理格式，训练时自动提取）；缺失时从训练
    检查点 G_*.pth 用 train.process_ckpt.extract_small_model 提取（幂等缓存）。
    注意：logs/<exp>/<exp>.pth 若只是 G_*.pth 的拷贝（训练格式，键 model/optimizer…），
    实时加载会 KeyError('weight')，绝不能直接当推理权重用。
    """
    infer_pth = RVC_ROOT / "assets" / "weights" / f"{exp}.pth"
    if infer_pth.exists():
        return infer_pth
    ckpt = next(iter(sorted(log_dir.glob("G_*.pth"))), None)
    if ckpt is None:
        return None
    try:
        sys.path.insert(0, str(RVC_ROOT))
        os.environ["PYTHONPATH"] = str(RVC_ROOT)
        os.environ["weight_root"] = str(RVC_ROOT / "assets" / "weights")
        from train.process_ckpt import extract_small_model

        (RVC_ROOT / "assets" / "weights").mkdir(parents=True, exist_ok=True)
        extract_small_model(str(ckpt), exp, "48k", 1, f"{exp} RVC v2 48k", "v2")
        if not infer_pth.exists():
            return None
        shutil.copy2(infer_pth, log_dir / f"{exp}.pth")
        return infer_pth
    except Exception:
        return None


def _exp_dirs(exp: str | None = None) -> tuple[str, Path, Path]:
    """返回 (exp, log_dir, dataset_dir)。"""
    name = exp or _active_exp()
    log_dir, dataset_dir = cfg.rvc_exp_dirs(name)
    return name, log_dir, dataset_dir

_state = {
    "live": {"running": False, "pid": None, "audio_switched": False, "error": ""},
    # exp=当前训练/生效的音色 ID；total_epochs 用于进度百分比计算
    "train": {"running": False, "pid": None, "rc": None,
              "exp": cfg.RVC_DEFAULT_EXP, "total_epochs": DEFAULT_EPOCHS},
}
# realtime_gui 进程探测缓存（1s TTL，见 _find_realtime_pids）
_pid_cache: dict = {"ts": None, "pids": []}


def _audio(action: str) -> dict:
    """调用 audio_config.ps1 并解析 JSON 输出。

    apply 会首次自动备份原设备；restore 还原并删除备份；reset 强制恢复真实设备。
    脚本失败（ok=false 或无输出）时抛 RuntimeError，避免前端误报"已恢复"。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(AUDIO_PS1), "-action", action],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"audio_config {action} 执行超时(120s)")
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        raise RuntimeError(f"audio_config {action} 无输出: {err[:300]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"audio_config {action} 输出非 JSON: {out[:300]}")
    if not data.get("ok"):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            raise RuntimeError(f"audio_config {action} 失败: {', '.join(str(e) for e in errs)}")
        raise RuntimeError(f"audio_config {action} 失败: {data.get('error') or data}")
    return data


def _find_realtime_pids() -> list[int]:
    """按命令行找出所有 realtime_gui.py 进程的 PID（存活检测与兜底查杀共用）。

    带 1s 缓存：status 每 3s 被前端轮询，每次都起 PowerShell 进程太重。
    """
    now = time.time()
    cached = _pid_cache["ts"]
    if cached is not None and now - cached < 1.0:
        return _pid_cache["pids"]
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'realtime_gui' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        pids = [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except Exception:
        pass
    _pid_cache.update(ts=now, pids=pids)
    return pids


def _realtime_alive() -> bool:
    """检测 RVC 实时变声窗口（realtime_gui.py）是否仍在运行。"""
    return bool(_find_realtime_pids())


def _find_train_pids() -> list[int]:
    """按命令行找出所有训练驱动进程（覆盖服务重启后内存态丢失的场景）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'train_rvc_voice|train_meituan_rat' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _reset_audio():
    """强制把音频设备恢复为真实默认设备（兜底：无论有无备份都生效）。

    返回 (ok, detail)。reset 失败时保留备份供前端/后续重试。
    """
    try:
        data = _audio("reset")
        return True, data
    except Exception as e:
        return False, str(e)


def _auto_clean():
    """服务器启动时清理上次异常残留的声卡切换：有备份但实时未运行时自动还原。

    restore 成功会删除备份；若残留或抛错则走 reset 兜底（返回失败信息到日志）。
    """
    backup = Path(os.environ.get("LOCALAPPDATA", "")) / "rvc_audio_backup.txt"
    if backup.exists() and not _realtime_alive():
        try:
            _audio("restore")
        except Exception as e:
            print(f"[auto_clean] restore 失败，尝试 reset 兜底: {e}", flush=True)
            ok, detail = _reset_audio()
            if not ok:
                print(f"[auto_clean] reset 兜底也失败: {detail}", flush=True)
            else:
                print("[auto_clean] reset 兜底成功，声卡已还原", flush=True)
        else:
            if backup.exists():
                print("[auto_clean] restore 后备份残留，走 reset 兜底", flush=True)
                _reset_audio()
            else:
                print("[auto_clean] restore 成功，声卡已还原", flush=True)
        _state["live"].update(running=False, pid=None, audio_switched=False)


def _model_status(exp: str | None = None) -> bool | str:
    _, log_dir, _ = _exp_dirs(exp)
    name = exp or _active_exp()
    pth = _find_pth(name, log_dir)
    idx = next(log_dir.glob("added_*.index"), None) if log_dir.exists() else None
    if not (log_dir.exists() and pth is not None and idx is not None):
        return (f"缺少 RVC 音色模型（logs/{name}/ 下没有 .pth 与 index），"
                f"请先训练该音色")
    return True


def _voicebank_dir() -> Path:
    return cfg.MEDIA_DIR / "voicebank"


def _exp_snapshot(exp: str) -> dict:
    """某个实验（音色 ID）的训练产物快照：权重/索引/语料/训练时间。"""
    log_dir, dataset_dir = cfg.rvc_exp_dirs(exp)
    pth = _find_pth(exp, log_dir)
    idx = next(log_dir.glob("added_*.index"), None) if log_dir.exists() else None
    mtime = pth.stat().st_mtime if pth is not None and pth.exists() else 0.0
    return {
        "pth_exists": pth is not None,
        "index_exists": idx is not None,
        "model_ready": pth is not None and idx is not None,
        "dataset_count": len(list(dataset_dir.glob("*.wav"))) if dataset_dir.exists() else 0,
        "trained_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "weights_dir": str(log_dir),
    }


# 各阶段在日志中的标记 → (阶段名, 权重%)。顺序即执行顺序，取"最后命中"的阶段。
_TRAIN_STAGE_MARKERS = [
    ("数据切分", "预处理切分", 10),
    ("F0提取", "提取音高(F0)", 35),
    ("HuBERT特征", "提取特征", 55),
    ("轮次", "模型训练", 85),
    ("索引训练", "建立索引", 97),
]


def _train_progress(exp_name: str | None = None) -> dict:
    """解析 logs/<exp>/train_run.log 给出当前阶段/百分比/最近日志行，供前端展示。

    done/errored 由日志标记判定；进程退出码记录在 _state["train"]["rc"]。
    传 exp_name 时看指定音色的训练日志，否则看当前生效实验。
    """
    exp = exp_name or _active_exp()
    res = {
        # running 取"内存态 OR 进程存活"，服务重启后仍能追踪到外部启动的训练
        "running": bool(_state["train"]["running"] and _state["train"].get("exp") == exp)
                   or bool(_find_train_pids()),
        "rc": _state["train"].get("rc") if _state["train"].get("exp") == exp else None,
        "done": False, "error": "",
        "stage": "", "stage_index": -1, "total_stages": len(_TRAIN_STAGE_MARKERS),
        "percent": 0.0, "message": "", "log_tail": [],
        "exp": exp,
    }
    total_epochs = int(_state["train"].get("total_epochs") or DEFAULT_EPOCHS)
    _, log_dir, _ = _exp_dirs(exp)
    train_log = log_dir / "train_run.log"
    if not train_log.exists():
        return res
    try:
        lines = train_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return res

    # 只看最近一次启动之后的内容
    starts = [i for i, ln in enumerate(lines) if ln.startswith("=== TRAIN START")]
    seg = lines[starts[-1] + 1:] if starts else lines

    for ln in reversed(seg):
        if "[完成]" in ln:
            res.update(done=True, stage="完成", stage_index=len(_TRAIN_STAGE_MARKERS), percent=100.0,
                       message="模型训练完成")
            break
        if "[失败]" in ln:
            res.update(error=ln.strip(), message="训练失败")
            break
        hit = next(((kw, name, w) for kw, name, w in _TRAIN_STAGE_MARKERS if kw in ln), None)
        if hit:
            kw, name, weight = hit
            import re as _re
            m = _re.search(r"(\d+)\s*/\s*(\d+)", ln)
            percent = weight
            if kw == "轮次":
                em = _re.search(r"轮次[:：]\s*(\d+)", ln)
                if em:
                    percent = min(weight, float(em.group(1)) / total_epochs * weight)
                    res["message"] = f"{name} · 第 {em.group(1)}/{total_epochs} 轮"
                else:
                    res["message"] = name
            elif m and int(m.group(2)) > 0:
                cur, total = int(m.group(1)), int(m.group(2))
                base = _TRAIN_STAGE_MARKERS[max(0, [x[0] for x in _TRAIN_STAGE_MARKERS].index(kw) - 1)]
                prev_w = base[2] if [x[0] for x in _TRAIN_STAGE_MARKERS].index(kw) > 0 else 0
                percent = prev_w + (cur / total) * (weight - prev_w)
                res["message"] = f"{name} {cur}/{total}"
            else:
                res["message"] = name
            res.update(stage=name, stage_index=[x[0] for x in _TRAIN_STAGE_MARKERS].index(kw),
                       percent=round(min(100.0, max(res["percent"], percent)), 1))
            break
    res["log_tail"] = [ln.rstrip() for ln in seg[-14:] if ln.strip()]
    if res["rc"] is None and not res["running"] and not res["done"] and not res["error"]:
        res["message"] = res["message"] or "未运行（可能被中断）"
    return res


def _resolve_device_names() -> tuple[str, str] | None:
    """用 RVC 环境的 sounddevice 枚举 MME 设备，按关键词模糊匹配出精确全名。

    配置里必须写完整枚举名（如 "CABLE Input (VB-Audio Virtual Cable)"），
    写短名会导致 GUI 内匹配失败而回退到默认设备（曾导致输出落到真实扬声器）。
    """
    try:
        out = subprocess.run(
            [str(VENV_PY), "-c",
             "import sounddevice as sd, json\n"
             "items = []\n"
             "for i, d in enumerate(sd.query_devices()):\n"
             "    items.append({'name': d['name'], 'api': sd.query_hostapis(d['hostapi'])['name'],\n"
             "                  'in': d['max_input_channels'], 'out': d['max_output_channels']})\n"
             "print(json.dumps(items))"],
            capture_output=True, text=True, timeout=60,
            cwd=str(RVC_ROOT),
        )
        items = json.loads(out.stdout.strip().splitlines()[-1])
        mme = [d for d in items if d.get("api") == "MME"]
        inp = next((d["name"] for d in mme if d["in"] > 0 and INPUT_DEVICE.lower() in d["name"].lower()), None)
        outp = next((d["name"] for d in mme if d["out"] > 0 and OUTPUT_DEVICE.lower() in d["name"].lower()), None)
        if inp and outp:
            return inp, outp
    except Exception:
        pass
    return None


def _apply_model_config() -> bool:
    """把 RVC 实时配置预填为当前音色模型；设备字段每次都用枚举出的精确全名覆盖。"""
    name, log_dir, _ = _exp_dirs()
    cfg_json = {}
    if CONFIG_JSON.exists():
        try:
            cfg_json = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        except Exception:
            cfg_json = {}
    idx = next(log_dir.glob("added_*.index"), None)
    if idx is None:
        return False
    pth = _ensure_standard_pth(name, log_dir)
    if pth is None:
        return False
    cfg_json["pth_path"] = str(pth).replace("\\", "/")
    cfg_json["index_path"] = str(idx).replace("\\", "/")
    cfg_json["f0method"] = "rmvpe"
    cfg_json["sr_type"] = "sr_model"
    # 设备必须用枚举出的精确全名；解析失败时保留旧值（GUI 至少能用上次可用的配置）
    resolved = _resolve_device_names()
    if resolved:
        cfg_json["sg_input_device"], cfg_json["sg_output_device"] = resolved
    CONFIG_JSON.write_text(json.dumps(cfg_json, ensure_ascii=False), encoding="utf-8")
    return True


def _live_proc_alive() -> bool:
    """实时变声是否运行中：直接探测 realtime_gui 进程（同时覆盖服务器重启后内存 pid 丢失的情况）。"""
    return _realtime_alive()


def _live_waiter(proc: subprocess.Popen):
    proc.wait()
    # RVC 窗口关闭后自动还原声卡；restore 失败则 reset 兜底，并记录状态供 status 反馈
    error = ""
    try:
        _audio("restore")
    except Exception as e:
        error = f"自动还原声卡失败: {e}"
        print(f"[live_waiter] {error}，尝试 reset 兜底", flush=True)
        ok, detail = _reset_audio()
        if not ok:
            error = f"自动还原声卡失败: {e}；reset 兜底也失败: {detail}"
            print(f"[live_waiter] {error}", flush=True)
        else:
            error = ""
    _state["live"].update(running=False, pid=None, audio_switched=False, error=error)


router = APIRouter(prefix=API_PREFIX)


@router.get("/rvc/live/status")
def rvc_live_status(exp_name: str | None = None):
    # 传 exp_name 时按指定音色查询（前端选了哪个音色就看哪个）；
    # 不传则沿用当前生效的实验（最近一次训练/启动的音色）
    model = _model_status(exp_name)
    exp, log_dir, dataset_dir = _exp_dirs(exp_name)
    idx = next(log_dir.glob("added_*.index"), None) if log_dir.exists() else None
    return {
        "ok": True,
        "exp": exp,
        "model_ok": model is True,
        "model_detail": model if model is True else None,
        "pth_exists": _find_pth(exp, log_dir) is not None,
        "index_exists": idx is not None,
        "dataset_count": len(list(dataset_dir.glob("*.wav"))) if dataset_dir.exists() else 0,
        "live_running": _live_proc_alive(),
        "audio_switched": _state["live"]["audio_switched"],
        "last_error": _state["live"].get("error", ""),
        "train_running": _state["train"]["running"],
        "output_device": OUTPUT_DEVICE,
        "input_device": INPUT_DEVICE,
    }


@router.get("/rvc/voices")
def rvc_voices():
    """实时变声可选音色清单（合并两个来源）：

    1. 音色库 media/voicebank/<id>/reference.wav —— 能生成语料、能训练的音色；
    2. RVC 整合包 logs/<exp>/ 下训练出权重+索引的实验 —— 能直接实时变声的模型。

    两者以「音色 ID == 实验名」对齐，前端据此展示每个音色走到哪一步
    （未生成语料 / 语料就绪 / 模型就绪），并可用它启动对应模型的实时变声。
    """
    items: dict[str, dict] = {}

    bank = _voicebank_dir()
    if bank.exists():
        for d in bank.iterdir():
            if not d.is_dir() or not (d / "reference.wav").exists():
                continue
            display = d.name
            meta = d / "meta.json"
            if meta.exists():
                try:
                    display = str(json.loads(meta.read_text(encoding="utf-8")).get("display_name") or d.name)
                except Exception:
                    pass
            items[d.name] = {"id": d.name, "display_name": display,
                             "has_reference": True, **_exp_snapshot(d.name)}

    logs = cfg.RVC_ROOT / "logs"
    if logs.exists():
        for d in logs.iterdir():
            if not d.is_dir() or d.name in items:
                continue
            snap = _exp_snapshot(d.name)
            # 音色库里没有、又没训练产物也没语料的目录属于噪音，不展示
            if not (snap["pth_exists"] or snap["index_exists"] or snap["dataset_count"]):
                continue
            items[d.name] = {"id": d.name, "display_name": d.name,
                             "has_reference": False, **snap}

    voices = sorted(items.values(),
                    key=lambda v: (not v["model_ready"], not v["has_reference"], v["id"]))
    return {
        "voices": voices,
        "active_exp": _active_exp(),
        "default_exp": cfg.RVC_DEFAULT_EXP,
        "rvc_root": str(cfg.RVC_ROOT),
        "rvc_ready": cfg.RVC_ROOT.exists() and VENV_PY.exists(),
    }


@router.post("/rvc/live/start")
def rvc_live_start(exp_name: str | None = None):
    if _live_proc_alive():
        return JSONResponse({"ok": True, "already_running": True, "pid": _state["live"]["pid"]})
    # 允许切换到指定音色的模型（校验其权重与索引都存在后才写配置）
    if exp_name:
        if _model_status(exp_name) is not True:
            raise HTTPException(status_code=400, detail=f"音色 {exp_name} 的 RVC 模型未就绪")
        _state["train"]["exp"] = exp_name
    model = _model_status()
    if model is not True:
        raise HTTPException(status_code=400, detail=model)
    if not RUNTIME_PY.exists():
        raise HTTPException(status_code=500, detail=f"未找到 RVC 运行时: {RUNTIME_PY}")
    if not _apply_model_config():
        raise HTTPException(status_code=500, detail="RVC 音色索引缺失")

    # 切声卡：录音默认 → CABLE Output（首次自动备份原设备）
    try:
        _audio("apply")
        _state["live"]["audio_switched"] = True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切换虚拟声卡失败: {e}")

    # 注意：不能用 -I 隔离模式 —— realtime_gui.py 依赖脚本同目录下的 tools/ 模块，
    # -I 不会把脚本目录加入 sys.path，会导致 `from tools.file_io import read_text` 失败。
    # 这里依赖 cwd=RVC_ROOT + 解释器把脚本所在目录加入 sys.path[0]。
    _, log_dir, _ = _exp_dirs()
    log_dir.mkdir(parents=True, exist_ok=True)
    gui_log = open(log_dir / "realtime_gui.log", "ab")
    try:
        proc = subprocess.Popen(
            [str(RUNTIME_PY), str(REALTIME_PY), "--auto-start"],
            cwd=str(RVC_ROOT), creationflags=CREATE_NEW_CONSOLE,
            stdout=gui_log, stderr=subprocess.STDOUT,
        )
        _pid_cache["ts"] = None  # 清缓存，确保秒退检测能探到新进程
    except Exception as e:
        gui_log.close()
        _state["live"].update(running=False, pid=None, audio_switched=False)
        ok, detail = _reset_audio()
        msg = f"拉起 RVC 实时窗口失败: {e}"
        if not ok:
            msg += f"；自动还原声卡也失败({detail})，请点「一键恢复音频」"
        raise HTTPException(status_code=500, detail=msg)

    # 秒退检测：窗口要经历 import(约5~7s) 才到 argparse/GUI，窗口期给足 10s
    deadline = time.time() + 10
    while time.time() < deadline:
        if not _realtime_alive():
            gui_log.close()
            _state["live"].update(running=False, pid=None, audio_switched=False)
            ok, detail = _reset_audio()
            msg = "RVC 实时窗口启动即退出（日志见 logs/realtime_gui.log）"
            if not ok:
                msg += f"；自动还原声卡失败({detail})，请点「一键恢复音频」"
            raise HTTPException(status_code=500, detail=msg)
        time.sleep(1)
    # 子进程已继承自己的句柄副本，父进程侧可安全关闭，日志仍持续写入
    gui_log.close()

    _state["live"].update(running=True, pid=proc.pid, error="")
    threading.Thread(target=_live_waiter, args=(proc,), daemon=True).start()
    return JSONResponse({
        "ok": True, "pid": proc.pid, "audio_switched": True,
        "output_device": OUTPUT_DEVICE,
        "hint": "已把系统录音设备切到 CABLE Output（微信等应用会用变身后的声音），RVC 窗口已自动开始变声。关闭窗口或点「停止变声」会自动还原声卡",
    })


@router.post("/rvc/live/stop")
def rvc_live_stop():
    # 优先按命令行查杀 realtime_gui 进程（覆盖服务器重启后内存 pid 丢失的场景）；
    # 查不到时才回退到记录的 pid，避免误杀复用了同号 PID 的无关进程
    targets = set(_find_realtime_pids())
    if not targets and _state["live"]["pid"]:
        targets.add(_state["live"]["pid"])
    for p in targets:
        try:
            subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"], capture_output=True, timeout=30)
        except Exception:
            pass
    _pid_cache["ts"] = None  # 清缓存，stop 后 status 立即反映真实状态
    _state["live"].update(running=False, pid=None, audio_switched=False)
    # 还原声卡：restore 失败则 reset 兜底，并真实反馈
    try:
        _audio("restore")
        _state["live"]["audio_switched"] = False
        _state["live"]["error"] = ""
        return JSONResponse({"ok": True, "restored": True})
    except Exception as e:
        ok, detail = _reset_audio()
        _state["live"]["audio_switched"] = False
        if ok:
            _state["live"]["error"] = ""
            return JSONResponse({"ok": True, "restored": True, "note": f"restore 失败({e})，已用 reset 兜底恢复"})
        _state["live"]["error"] = f"还原声卡失败: {e}；reset 兜底失败: {detail}"
        return JSONResponse({"ok": False, "error": _state["live"]["error"], "fallback_failed": True})


@router.post("/rvc/live/reset")
def rvc_live_reset():
    """强制把音频设备恢复为真实默认设备（兜底：无论有无备份都生效）。"""
    ok, detail = _reset_audio()
    _state["live"].update(running=False, pid=None, audio_switched=False)
    if ok:
        _state["live"]["error"] = ""
        return JSONResponse({"ok": True, "reset": True})
    return JSONResponse({"ok": False, "error": f"恢复音频设备失败: {detail}"})


@router.get("/rvc/train/status")
def rvc_train_status(exp_name: str | None = None):
    model = _model_status(exp_name)
    _, log_dir, dataset_dir = _exp_dirs(exp_name)
    return {
        "ok": True,
        "model_ok": model is True,
        "model_detail": ("" if model is True else model),
        **_train_progress(exp_name),
        "dataset_count": len(list(dataset_dir.glob("*.wav"))) if dataset_dir.exists() else 0,
        "log_dir": str(log_dir),
    }


class TrainStartReq(BaseModel):
    """训练启动参数：不传时使用当前生效音色与其默认数据集。"""
    exp_name: str | None = None
    dataset_dir: str | None = None
    epochs: int | None = None


@router.post("/rvc/train/start")
def rvc_train_start(req: TrainStartReq | None = None):
    if _state["train"]["running"]:
        return JSONResponse({"ok": False, "already_running": True})
    body = req or TrainStartReq()
    exp = body.exp_name or cfg.RVC_DEFAULT_EXP
    epochs = int(body.epochs or DEFAULT_EPOCHS)
    _, log_dir, default_dataset = _exp_dirs(exp)
    dataset = Path(body.dataset_dir) if body.dataset_dir else default_dataset
    if not VENV_PY.exists() or not TRAIN_PY.exists():
        raise HTTPException(status_code=500, detail="未找到 RVC 训练脚本（train_rvc_voice.py）")
    if not dataset.exists():
        raise HTTPException(status_code=400, detail=f"训练集目录不存在: {dataset}")

    proc = subprocess.Popen(
        [str(VENV_PY), str(TRAIN_PY), "--exp", exp, "--dataset", str(dataset),
         "--epochs", str(epochs)],
        cwd=str(RVC_ROOT), creationflags=CREATE_NEW_CONSOLE,
    )

    def _waiter(p):
        rc = p.wait()
        _state["train"].update(running=False, pid=None, rc=rc)

    _state["train"].update(running=True, pid=proc.pid, rc=None,
                           exp=exp, total_epochs=epochs)
    threading.Thread(target=_waiter, args=(proc,), daemon=True).start()
    return JSONResponse({"ok": True, "started": True, "pid": proc.pid,
                         "exp": exp, "epochs": epochs,
                         "log_dir": str(log_dir)})


# 服务器启动时，清理上次异常残留的声卡切换（有备份但实时未运行 → 自动还原）
_auto_clean()
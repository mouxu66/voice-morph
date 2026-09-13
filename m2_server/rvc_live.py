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
import logging
import os
import subprocess
import sys
import threading
import time
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
import live_settings
from rvc_common import (ensure_infer_pth, exp_display_name, exp_snapshot, exp_source, find_pth, _find_pids_by_cmdline, _kill_pids)

logger = logging.getLogger(__name__)

API_PREFIX = "/api"

ROOT = Path(__file__).resolve().parent.parent
RVC_ROOT = cfg.RVC_ROOT
CONFIG_JSON = RVC_ROOT / "configs" / "config.json"
REALTIME_PY = RVC_ROOT / "realtime_gui.py"
# 无头运行器（不弹窗口/控制台，推理逻辑与 realtime_gui.py 同源）
HEADLESS_PY = RVC_ROOT / "rvc_headless.py"
# 自我监听：把 CABLE Output（变声后的音频）软回环到真实输出设备，让自己听得到
MONITOR_PY = RVC_ROOT / "rvc_monitor.py"
MONITOR_LOG = cfg.OUTPUTS_DIR / "live_monitor.log"
MONITOR_ENABLED = os.environ.get("VM_LIVE_MONITOR", "1") != "0"
MONITOR_GAIN = float(os.environ.get("VM_LIVE_MONITOR_GAIN", "0.8"))
# 本机 RVC 环境没有 runtime 目录，Python 解释器在 .venv（训练驱动脚本亦如此）
RUNTIME_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
# 通用训练驱动（任意音色），train_meituan_rat.py 仅是其兼容包装
TRAIN_PY = RVC_ROOT / "train_rvc_voice.py"
AUDIO_PS1 = ROOT / "m2_server" / "audio_config.ps1"
# 实时转写子进程（桌宠字幕）：复用级联的分块+ASR，只采真麦，无声卡操作
STREAM_PY = ROOT / "m2_server" / "cascade_stream.py"
ASR_STATE_FILE = cfg.OUTPUTS_DIR / "live_asr_state.json"
ASR_RUN_LOG = cfg.OUTPUTS_DIR / "live_asr_run.log"
# 音色入库自动质检：tools/voice_qc.py 的落盘位置与在跑去重
QC_PY = ROOT / "tools" / "voice_qc.py"
QC_DIR = cfg.OUTPUTS_DIR / "qc"
_QC_INFLIGHT: set[str] = set()

# 真实麦克风与虚拟声卡（由 audio_config.ps1 list 探测确定）
INPUT_DEVICE = os.environ.get("VM_LIVE_INPUT_DEVICE", "麦克风阵列")
OUTPUT_DEVICE = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")

CREATE_NEW_CONSOLE = 0x00000010
# 无头模式用：起进程但不分配控制台，避免每次开始变声都弹黑框
CREATE_NO_WINDOW = 0x08000000
# 无头运行器就绪标记（rvc_headless.py 开流后打印）
_STREAM_READY_MARK = "[headless] STREAM_UP"

DEFAULT_EPOCHS = 40

# 实时变声基线参数。每次「开始变声」都会写入 RVC 的 config.json，保证起点一致、可复现；
# GUI 里的滑杆仍可在本轮会话内热调（realtime_gui 支持热更新），下次启动回到基线。
#
# 为什么需要它：config.json 会持久化 GUI 上次的滑杆值，实验性的坏值会一直留着。
# 实测踩到的组合是 block_time=0.13 / crossfade_length=0.08 —— 淡入淡出占分块的 62%，
# 而 RVC 默认值是 0.05/0.25 = 20%。占比过高会产生明显的重叠涂抹（重影、相位感）；
# 同时 block_time 只有默认的一半，hubert/f0 上下文不足，音色更容易发飘发怪。
# rms_mix_rate 取 0.25 是为了和离线链路 offline_vc_infer.py 对齐——离线在 0.25 下听感最好。
REALTIME_TUNING = {
    "block_time": 0.25,        # RVC 默认；分块越长上下文越足，artifacts 越少
    "crossfade_length": 0.05,  # RVC 默认；占 block_time 的 20%，避免重叠涂抹
    "extra_time": 2.5,         # RVC 默认；额外推理上下文
    "rms_mix_rate": 0.25,      # 与离线链路一致（离线 0.25 时听感最佳）
    "index_rate": 0.5,         # 与离线 A/B 一致
    "threhold": -60.0,
    "sr_type": "sr_model",
    "f0method": "rmvpe",
}

# 性能档位参数表：每次 start 前由 _apply_model_config 写进 RVC config.json。
# 注意：balanced/game 都基于基线复制，绝不污染 REALTIME_TUNING 本身；
# fp32（is_half=False）与 f0method=rmvpe 是全项目铁律，档位不覆盖。
PROFILE_TUNING = {
    "balanced": dict(REALTIME_TUNING),
    "game": {
        **REALTIME_TUNING,
        # 分块加长 → 推理频次更低、更省；上下文与索引权重下调 → 显存/算力更省。
        # 代价：单句延迟略增、音质略降（面向边打游戏边变声的场景）。
        "block_time": 0.35,
        "extra_time": 1.5,
        "index_rate": 0.3,
    },
}
PROFILE_DESC = {"balanced": "均衡·音质优先", "game": "游戏低占用"}

# GPU 显存探测缓存（1s TTL）：status 轮询较频繁，避免反复 spawn nvidia-smi
_gpu_cache: dict = {"ts": 0.0, "used": None, "total": None, "proc": None}


def _nvidia_smi(query: str) -> list[str] | None:
    """跑一次 nvidia-smi（CSV 无头）；nvidia-smi 缺失/非 0 返回码一律返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return None
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return None


def _gpu_used_mb() -> int | None:
    rows = _nvidia_smi("memory.used")
    if not rows:
        return None
    try:
        return int(rows[0].split(",")[0])
    except ValueError:
        return None


def _gpu_total_mb() -> int | None:
    rows = _nvidia_smi("memory.total")
    if not rows:
        return None
    try:
        return int(rows[0].split(",")[0])
    except ValueError:
        return None


def _live_proc_vram_mb() -> int | None:
    """实时变声子进程的显存占用（compute-apps 只列有 CUDA 上下文的进程）。"""
    rows = _nvidia_smi("pid,used_memory")
    if not rows:
        return None
    targets = set(_find_realtime_pids())
    if _state["live"].get("pid"):
        targets.add(_state["live"]["pid"])
    for row in rows:
        parts = row.replace(",", " ").split()
        if len(parts) < 2:
            continue
        pid, mem = parts[0], parts[-1]
        if pid.isdigit() and int(pid) in targets and mem.lstrip("-").isdigit():
            return int(mem)
    # 进程还没拿到 CUDA 上下文不视为失败
    return None


def _gpu_snapshot() -> dict:
    """1s TTL 缓存的三连查结果，status/profile 接口共用。"""
    now = time.time()
    if now - _gpu_cache["ts"] >= 1.0:
        _gpu_cache.update(ts=now, used=_gpu_used_mb(), total=_gpu_total_mb(),
                          proc=_live_proc_vram_mb())
    return {"gpu_total_mb": _gpu_cache["total"], "gpu_used_mb": _gpu_cache["used"],
            "live_proc_vram_mb": _gpu_cache["proc"]}


def _active_exp() -> str:
    """当前生效的 RVC 实验名（音色 ID）：最近一次启动的训练，否则回退默认。"""
    return _state["train"].get("exp") or cfg.RVC_DEFAULT_EXP


def _exp_dirs(exp: str | None = None) -> tuple[str, Path, Path]:
    """返回 (exp, log_dir, dataset_dir)。"""
    name = exp or _active_exp()
    log_dir, dataset_dir = cfg.rvc_exp_dirs(name)
    return name, log_dir, dataset_dir

_state = {
    "live": {"running": False, "pid": None, "audio_switched": False, "error": "",
             "headless": False, "monitor": False, "monitor_gain": None},
    # exp=当前训练/生效的音色 ID；total_epochs 用于进度百分比计算
    "train": {"running": False, "pid": None, "rc": None,
              "exp": cfg.RVC_DEFAULT_EXP, "total_epochs": DEFAULT_EPOCHS},
}
# realtime_gui 进程探测缓存（1s TTL，见 _find_realtime_pids）
_pid_cache: dict = {"ts": None, "pids": []}
# asr-only 转写子进程探测缓存（3s TTL，status/pet 轮询较频繁）
_asr_pid_cache: dict = {"ts": None, "alive": False}


def _find_asr_pids() -> list[int]:
    return _find_pids_by_cmdline("cascade_stream.+asr-only")


def _asr_proc_alive() -> bool:
    now = time.time()
    if _asr_pid_cache["ts"] is None or now - _asr_pid_cache["ts"] > 3.0:
        _asr_pid_cache.update(ts=now, alive=bool(_find_asr_pids()))
    return _asr_pid_cache["alive"]


def _stop_asr_proc():
    """查杀实时转写子进程（按命令行匹配，覆盖 pid 丢失场景）。"""
    _asr_pid_cache["ts"] = None
    _kill_pids(_find_asr_pids(), "asr")


def _asr_state() -> dict:
    """读取转写子进程状态文件；running 以进程存活为准（文件可能残留）。"""
    res = {"running": False, "stage": "", "last_text": "", "chunks": 0,
           "error": "", "updated_at": ""}
    if ASR_STATE_FILE.exists():
        try:
            data = json.loads(ASR_STATE_FILE.read_text(encoding="utf-8"))
            res.update({k: data.get(k, res[k]) for k in res})
        except Exception as e:
            logger.debug("[asr] 读取转写状态文件失败（用进程存活兜底）: %s", e)
    res["running"] = _asr_proc_alive()
    return res


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
        raise RuntimeError(f"audio_config {action} 无输出: {err[:1500]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"audio_config {action} 输出非 JSON: {out[:1500]}")
    if not data.get("ok"):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            raise RuntimeError(f"audio_config {action} 失败: {', '.join(str(e) for e in errs)}")
        raise RuntimeError(f"audio_config {action} 失败: {data.get('error') or data}")
    return data


def _find_realtime_pids() -> list[int]:
    """按命令行找出所有实时变声进程的 PID（存活检测与兜底查杀共用）。

    同时匹配无头运行器 rvc_headless.py 与官方 GUI realtime_gui.py，
    两种模式共用同一套存活判断与停止逻辑。

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
             "Where-Object { $_.CommandLine -match 'realtime_gui|rvc_headless' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        pids = [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except Exception as e:
        logger.warning("[realtime] 枚举实时变声进程失败（状态可能失真）: %s", e)
    _pid_cache.update(ts=now, pids=pids)
    return pids


def _realtime_alive() -> bool:
    """检测 RVC 实时变声窗口（realtime_gui.py）是否仍在运行。"""
    return bool(_find_realtime_pids())


def _find_train_pids() -> list[int]:
    """按命令行找出所有训练驱动进程（覆盖服务重启后内存态丢失的场景）。"""
    return _find_pids_by_cmdline("train_rvc_voice|train_meituan_rat")


def _reset_audio():
    """强制把音频设备恢复为真实默认设备（兜底：无论有无备份都生效）。

    返回 (ok, detail)。reset 失败时保留备份供前端/后续重试。
    """
    try:
        data = _audio("reset")
        return True, data
    except Exception as e:
        logger.warning("[audio] 重置音频设备失败: %s", e)
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
            logger.warning("[auto_clean] restore 失败，尝试 reset 兜底: %s", e)
            ok, detail = _reset_audio()
            if not ok:
                logger.error("[auto_clean] reset 兜底也失败: %s", detail)
            else:
                logger.info("[auto_clean] reset 兜底成功，声卡已还原")
        else:
            if backup.exists():
                logger.warning("[auto_clean] restore 后备份残留，走 reset 兜底")
                _reset_audio()
            else:
                logger.info("[auto_clean] restore 成功，声卡已还原")
        _state["live"].update(running=False, pid=None, audio_switched=False)


def _model_status(exp: str | None = None) -> bool | str:
    _, log_dir, _ = _exp_dirs(exp)
    name = exp or _active_exp()
    pth = find_pth(name, log_dir)
    idx = next(log_dir.glob("added_*.index"), None) if log_dir.exists() else None
    if not (log_dir.exists() and pth is not None and idx is not None):
        return (f"缺少 RVC 音色模型（logs/{name}/ 下没有 .pth 与 index），"
                f"请先训练该音色")
    return True


def _voicebank_dir() -> Path:
    return cfg.MEDIA_DIR / "voicebank"


def _read_qc(exp: str):
    """读取该音色的质检结果（outputs/qc/<exp>.json）；没有或损坏时返回 None。"""
    f = QC_DIR / f"{exp}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[qc] 读取质检结果 %s 失败: %s", f, e)
        return None


def _read_source(exp: str) -> str:
    """读取该音色的来源标记（logs/<exp>/source.json → "market"/""）。

    市场安装的 RVC 模型由 market_install 落 source.json；自训/本地导入无标记。
    实现统一走 rvc_common.exp_source，避免两处规则漂移。
    """
    return exp_source(exp)


def _maybe_run_qc(exp: str, log_dir: Path):
    """训练完成后自动触发音色质检（后台线程，失败静默、绝不影响训练状态）。

    触发条件：质检结果不存在，或旧于最新训练产物（G_*.pth / <exp>.pth / index /
    推理权重任一更新过都算重训）。质检脚本自身保证"任何失败也写 JSON"，因此
    失败重试天然被 mtime 比较挡住，不会反复拉起。
    """
    try:
        cands = list(log_dir.glob("G_*.pth")) + list(log_dir.glob("added_*.index")) + [
            log_dir / f"{exp}.pth", RVC_ROOT / "assets" / "weights" / f"{exp}.pth"]
        model_mtime = max((p.stat().st_mtime for p in cands if p.exists()), default=0.0)
        if model_mtime <= 0:
            return
        qc_file = QC_DIR / f"{exp}.json"
        if qc_file.exists() and qc_file.stat().st_mtime >= model_mtime:
            return
        if exp in _QC_INFLIGHT:
            return
        _QC_INFLIGHT.add(exp)

        def _job():
            try:
                subprocess.run(
                    [sys.executable, str(QC_PY), "--voice", exp],
                    capture_output=True, timeout=1800)
            except Exception as e:
                logger.warning("[qc] 音色质检 %s 执行失败（已忽略）: %s", exp, e)
            finally:
                _QC_INFLIGHT.discard(exp)

        threading.Thread(target=_job, daemon=True).start()
    except Exception as e:
        logger.warning("[qc] 触发音色质检 %s 失败（已忽略）: %s", exp, e)


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
    # 训练完成 → 自动音色质检（后台线程静默跑，见 _maybe_run_qc）
    if res["done"]:
        _maybe_run_qc(exp, log_dir)
    if res["rc"] is None and not res["running"] and not res["done"] and not res["error"]:
        res["message"] = res["message"] or "未运行（可能被中断）"
    return res


def _device_matches(dev_name: str, want: str) -> bool:
    """设备名模糊匹配。

    MME 会把设备名截断到 31 字符（"CABLE Input (VB-Audio Virtual C"），
    配置里写完整名就永远匹配不上，所以两边都按前缀比较。
    """
    a, b = (dev_name or "").lower(), (want or "").lower()
    if not a or not b:
        return False
    return b in a or a in b or a[:28] in b or b[:28] in a


def _system_default_input() -> str | None:
    """系统当前默认录音设备的名字（MME 视角）。

    插上耳机后 Windows 会把默认录音切到耳机麦，拔掉又切回内置阵列，
    所以每次启动实时读一次，就能自动跟随用户实际在用的麦克风。
    """
    try:
        out = subprocess.run(
            [str(VENV_PY), "-c",
             "import sounddevice as sd, json\n"
             "print(json.dumps(sd.query_devices(kind='input')['name']))"],
            capture_output=True, text=True, timeout=60, cwd=str(RVC_ROOT),
        ).stdout
        return json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        logger.warning("[device] 读取系统默认录音设备失败: %s", e)
        return None


def _resolve_device_names() -> tuple[str, str] | None:
    """用 RVC 环境的 sounddevice 枚举 MME 设备，按关键词模糊匹配出精确全名。

    配置里必须写完整枚举名（如 "CABLE Input (VB-Audio Virtual Cable)"），
    写短名会导致 GUI 内匹配失败而回退到默认设备（曾导致输出落到真实扬声器）。

    输入设备候选优先级（A7 输入设备选择器）：
      1. live_settings.input_device —— 用户在界面上显式选的（手机当麦克风/USB 麦）
      2. 系统默认录音设备 —— 未显式选择时跟随（插耳机用耳机麦、拔了回内置麦）
      3. VM_LIVE_INPUT_DEVICE 环境变量 —— 旧的锁定手段，兼容保留
      4. "Microsoft 声音映射器" —— 兜底
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
        outp = next((d["name"] for d in mme
                     if d["out"] > 0 and _device_matches(d["name"], OUTPUT_DEVICE)), None)
        explicit = live_settings.get()["input_device"]
        candidates = [explicit, _system_default_input(), INPUT_DEVICE,
                      "Microsoft 声音映射器"]
        inp = None
        for want in candidates:
            if not want:
                continue
            inp = next((d["name"] for d in mme
                        if d["in"] > 0 and _device_matches(d["name"], want)), None)
            if inp:
                break
        if inp and outp:
            return inp, outp
    except Exception as e:
        logger.warning("[device] 枚举/匹配音频设备失败，沿用旧配置: %s", e)
    return None


def _apply_model_config() -> bool:
    """把 RVC 实时配置预填为当前音色模型 + 基线参数；设备字段用枚举出的精确全名覆盖。"""
    name, log_dir, _ = _exp_dirs()
    cfg_json = {}
    if CONFIG_JSON.exists():
        try:
            cfg_json = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[config] 读取 RVC config.json 失败，重置为空白配置: %s", e)
            cfg_json = {}
    idx = next(log_dir.glob("added_*.index"), None)
    if idx is None:
        return False
    pth = ensure_infer_pth(name)
    if pth is None:
        return False
    cfg_json["pth_path"] = str(pth).replace("\\", "/")
    cfg_json["index_path"] = str(idx).replace("\\", "/")
    # 基线参数统一覆盖：否则 GUI 上次遗留的实验性滑杆值会一直生效，
    # 而"实时听起来怪"绝大多数是这些参数导致的，不是模型问题。
    # 按性能档位取参数表（balanced=现状 / game=更低占用）。
    cfg_json.update(PROFILE_TUNING[live_settings.get()["perf_profile"]])
    # A8 输入降噪：live_settings.denoise（默认开）写入 RVC 自带 I_noise_reduce。
    # RVC 内置输入降噪 = 帧级 crossfade 降噪，代价约 +40ms 延迟（min(crossfade, 0.04)）；
    # 无头模式下 GUI 复选框永远 False，所以必须在每次启动时显式写入。
    cfg_json["I_noise_reduce"] = bool(live_settings.get()["denoise"])
    # 设备必须用枚举出的精确全名；解析失败时保留旧值（GUI 至少能用上次可用的配置）
    resolved = _resolve_device_names()
    if resolved:
        cfg_json["sg_input_device"], cfg_json["sg_output_device"] = resolved
    CONFIG_JSON.write_text(json.dumps(cfg_json, ensure_ascii=False), encoding="utf-8")
    return True


def _live_stream_ready() -> bool:
    """无头模式下音频流是否真的起来了（进程活着 ≠ 模型加载完）。

    rvc_headless.py 开流后会打印 STREAM_UP，这里扫日志判断，
    前端据此显示「加载中」而不是一上来就告诉用户「已在变声」。
    """
    try:
        _, log_dir, _ = _exp_dirs()
        path = log_dir / "realtime_gui.log"
        if not path.exists():
            return False
        # 只读尾部，避免每次 status 扫描整个大日志
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")
        return _STREAM_READY_MARK in tail
    except Exception as e:
        logger.debug("[stream] 读取实时日志判断流就绪失败: %s", e)
        return False


def _find_monitor_pids() -> list[int]:
    """按命令行找出自我监听回环进程（rvc_monitor.py）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'rvc_monitor' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except Exception as e:
        logger.warning("[monitor] 枚举监听回环进程失败: %s", e)
        return []


def _kill_monitor():
    """停止自我监听回环（变声停止时必须一起收掉，否则耳机里一直是自己的声音）。"""
    for pid in _find_monitor_pids():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except Exception as e:
            logger.debug("[monitor] 停止监听进程 %s 失败（可忽略）: %s", pid, e)


def _start_monitor(gain: float) -> bool:
    """拉起自我监听回环进程。失败只影响「自己听到」，不影响变声本身。"""
    if not MONITOR_PY.exists():
        return False
    _kill_monitor()  # 换音量/重启时先收掉旧的
    try:
        MONITOR_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(MONITOR_LOG, "ab")
        subprocess.Popen(
            [str(VENV_PY), str(MONITOR_PY),
             "--gain", str(gain), "--wait", "3.0"],
            cwd=str(RVC_ROOT), creationflags=CREATE_NO_WINDOW,
            stdout=log, stderr=subprocess.STDOUT,
        )
        log.close()
        return True
    except Exception as e:
        logger.warning("[live] 自我监听拉起失败（不影响变声）: %s", e)
        return False


def _live_input_device() -> str | None:
    """当前 RVC 配置里实际使用的输入设备名，供前端显示「正在用哪个麦」。"""
    try:
        data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        return data.get("sg_input_device")
    except Exception as e:
        logger.debug("[live] 读当前输入设备失败: %s", e)
        return None


def _live_proc_alive() -> bool:
    """实时变声是否运行中：直接探测 RVC 进程（同时覆盖服务器重启后内存 pid 丢失的情况）。"""
    return _realtime_alive()


def _live_waiter(proc: subprocess.Popen):
    proc.wait()
    # RVC 窗口关闭后自动还原声卡；restore 失败则 reset 兜底，并记录状态供 status 反馈
    _stop_asr_proc()  # 实时转写子进程跟随实时变声一起退出
    _kill_monitor()   # 自我监听回环同样跟随退出
    error = ""
    try:
        _audio("restore")
    except Exception as e:
        error = f"自动还原声卡失败: {e}"
        logger.warning("[live_waiter] %s，尝试 reset 兜底", error)
        ok, detail = _reset_audio()
        if not ok:
            error = f"自动还原声卡失败: {e}；reset 兜底也失败: {detail}"
            logger.error("[live_waiter] %s", error)
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
    running = _live_proc_alive()
    return {
        "ok": True,
        "exp": exp,
        "model_ok": model is True,
        "model_detail": model if model is True else None,
        "pth_exists": find_pth(exp, log_dir) is not None,
        "index_exists": idx is not None,
        "dataset_count": len(list(dataset_dir.glob("*.wav"))) if dataset_dir.exists() else 0,
        "live_running": running,
        # 进程活着 ≠ 能出声：无头模式下要等模型加载完、音频流起来才算就绪
        "live_ready": running and (_live_stream_ready() or not _state["live"].get("headless", True)),
        "headless": bool(_state["live"].get("headless", False)),
        "audio_switched": _state["live"]["audio_switched"],
        "last_error": _state["live"].get("error", ""),
        "train_running": _state["train"]["running"],
        "output_device": OUTPUT_DEVICE,
        # 实际生效的输入设备（跟随系统默认录音设备，插拔耳机会变）
        "input_device": _live_input_device() or INPUT_DEVICE,
        "monitor_on": bool(_find_monitor_pids()),
        "monitor_gain": _state["live"].get("monitor_gain"),
        # 性能档位 + GPU 显存占用（无 GPU/nvidia-smi 不可用时 gpu_* 为 null）
        "perf_profile": live_settings.get()["perf_profile"],
        "perf_profile_desc": PROFILE_DESC.get(live_settings.get()["perf_profile"], ""),
        **_gpu_snapshot(),
        # 实时转写（桌宠字幕）：running=转写子进程存活；stage/last_text 供桌宠渲染
        **{f"asr_{k}": v for k, v in _asr_state().items()},
    }


def _mme_input_devices() -> list[dict]:
    """枚举 MME 主机 API 下的输入设备（与 RVC 实时链路同视角），同名去重。

    MME 会把同一物理设备列出多次（如重复的"麦克风阵列"），且名字截断到 31 字符；
    去重保序，附 is_default 标记（系统默认录音设备）。sounddevice 缺失时返回空列表。
    """
    try:
        import sounddevice as sd
    except Exception as e:  # pragma: no cover - 环境缺库时前端显示空列表
        logger.warning("[devices] sounddevice 不可用: %s", e)
        return []
    try:
        apis = sd.query_hostapis()
        mme_idx = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
        default_name = ""
        try:
            default_name = sd.query_devices(kind="input")["name"] or ""
        except Exception:
            pass
        seen: dict[str, dict] = {}
        for d in sd.query_devices():
            if d["hostapi"] != mme_idx or d["max_input_channels"] <= 0:
                continue
            name = (d["name"] or "").strip()
            if not name or name in seen:
                continue
            seen[name] = {"name": name, "is_default": name == default_name}
        return list(seen.values())
    except Exception as e:
        logger.warning("[devices] 枚举输入设备失败: %s", e)
        return []


class LiveDevicesPayload(BaseModel):
    input_device: str | None = None
    denoise: bool | None = None


class LiveProfilePayload(BaseModel):
    profile: str


@router.get("/rvc/live/profile")
def rvc_live_profile_get():
    """当前性能档位 + GPU 显存占用（供前端展示显存条）。"""
    p = live_settings.get()["perf_profile"]
    return {"ok": True, "profile": p,
            "profile_desc": PROFILE_DESC.get(p, ""), **_gpu_snapshot()}


@router.post("/rvc/live/profile")
def rvc_live_profile_set(payload: LiveProfilePayload):
    """切换性能档位（balanced/game）。

    变声运行中切换 = 自动重启生效：stop（已还原声卡、清理字幕/监听）→ start 同一音色。
    game 档重启后字幕与自我监听默认全关（start 内按档位处理）。
    重启任一步失败时声卡已被还原，前端应提示用户点「一键恢复音频」。
    """
    profile = payload.profile.strip()
    if profile not in PROFILE_TUNING:
        raise HTTPException(status_code=400,
                            detail=f"未知性能档位：{profile}（可选 balanced / game）")
    changed = profile != live_settings.get()["perf_profile"]
    if changed:
        live_settings.update(perf_profile=profile)
    restarted = False
    if changed and _live_proc_alive():
        # stop 尽力执行：失败也不阻断 start（声卡 restore/apply 幂等）
        try:
            rvc_live_stop()
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.warning("[profile] 切换档位停止旧变声异常（继续重启）: %s", e)
        rvc_live_start(exp_name=_active_exp(), monitor=(profile == "balanced"))
        restarted = True
    return {"ok": True, "restarted": restarted, "profile": profile,
            "profile_desc": PROFILE_DESC.get(profile, "")}


@router.get("/rvc/live/audio_devices")
def rvc_live_audio_devices():
    """实时输入设备清单 + 当前设置（A7）。

    items 供前端下拉；explicit=用户显式选择（空串=跟随系统默认）；
    current=上次变声实际用的输入设备（RVC config.json）；denoise=输入降噪开关。
    """
    return {
        "ok": True,
        "items": _mme_input_devices(),
        "explicit": live_settings.get()["input_device"],
        "current": _live_input_device(),
        "denoise": live_settings.get()["denoise"],
        "running": _live_proc_alive(),
    }


@router.post("/rvc/live/audio_devices")
def rvc_live_audio_devices_set(payload: LiveDevicesPayload):
    """保存实时输入设备/降噪设置（A7/A8）。

    input_device 传空串 = 跟随系统默认录音设备；非空按关键词匹配（MIME 截断名也
    能对上，见 _device_matches）。变声运行中修改不中断，但设备切换需重启变声生效
    （needs_restart=true 由前端提示）；降噪开关同样在下次启动时写入 RVC config。
    """
    if payload.input_device is not None:
        want = payload.input_device.strip()
        if want:
            items = _mme_input_devices()
            if not items:
                raise HTTPException(status_code=503, detail="无法枚举音频设备，稍后重试")
            if not any(_device_matches(d["name"], want) for d in items):
                raise HTTPException(status_code=400, detail=f"找不到输入设备：{want}")
        data = live_settings.update(input_device=want)
    else:
        data = live_settings.get()
    if payload.denoise is not None:
        data = live_settings.update(denoise=payload.denoise)
    return {
        "ok": True,
        "input_device": data["input_device"],
        "denoise": data["denoise"],
        "running": _live_proc_alive(),
        "needs_restart": _live_proc_alive(),
    }


@router.post("/rvc/live/monitor")
def rvc_live_monitor(on: bool = True, gain: float | None = None):
    """开关自我监听（把变声后的声音回放到耳机）。

    变声运行中可随时开/关与调音量，不影响变声本身，也不用重启。
    """
    if not MONITOR_PY.exists():
        raise HTTPException(status_code=500, detail=f"未找到监听脚本: {MONITOR_PY}")
    g = MONITOR_GAIN if gain is None else float(gain)
    if not on:
        _kill_monitor()
        _state["live"].update(monitor=False)
        return JSONResponse({"ok": True, "monitor_on": False})
    if not _live_proc_alive():
        raise HTTPException(status_code=409, detail="实时变声未运行，先点开始变声")
    ok = _start_monitor(g)
    _state["live"].update(monitor=ok, monitor_gain=g)
    return JSONResponse({"ok": ok, "monitor_on": ok, "monitor_gain": g if ok else None})


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
                    display = str(json.loads(meta.read_text(encoding="utf-8")).get("display_name")
                                  or exp_display_name(d.name))
                except Exception as e:
                    logger.debug("[voices] 读取音色 %s 的 display_name 失败（回退目录名）: %s", d.name, e)
            items[d.name] = {"id": d.name, "display_name": display,
                             "has_reference": True, "qc": _read_qc(d.name),
                             "source": _read_source(d.name), **exp_snapshot(d.name)}

    logs = cfg.RVC_ROOT / "logs"
    if logs.exists():
        for d in logs.iterdir():
            if not d.is_dir() or d.name in items:
                continue
            snap = exp_snapshot(d.name)
            # 音色库里没有、又没训练产物也没语料的目录属于噪音，不展示
            if not (snap["pth_exists"] or snap["index_exists"] or snap["dataset_count"]):
                continue
            items[d.name] = {"id": d.name, "display_name": exp_display_name(d.name),
                             "has_reference": False, "qc": _read_qc(d.name),
                             "source": _read_source(d.name), **snap}

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
def rvc_live_start(exp_name: str | None = None, monitor: bool | None = None,
                   monitor_gain: float | None = None):
    """启动实时变声。

    monitor: 是否开启自我监听（把变声后的声音回环到耳机，让自己听得到）。
             不传时取环境变量 VM_LIVE_MONITOR（默认开；game 性能档默认关）。
    monitor_gain: 监听音量，默认 0.8。
    """
    # 性能档位：game 档默认不开自我监听与实时字幕（两个伴随进程是最吃 CPU/占用的部分）
    profile = live_settings.get()["perf_profile"]
    # game 档默认关监听；显式传 monitor=True 仍可手动开
    monitor = (profile != "game" and MONITOR_ENABLED) if monitor is None else bool(monitor)
    monitor_gain = MONITOR_GAIN if monitor_gain is None else float(monitor_gain)
    # 级联变声与实时变声互斥：两者抢 GPU 且都要占 CABLE（反向检查在 cascade.start）
    from cascade import _cascade_alive
    if _cascade_alive():
        raise HTTPException(status_code=409,
                            detail="级联变声正在运行，请先停止（两者抢 GPU 且都占 CABLE）")
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
    gui_log_path = log_dir / "realtime_gui.log"
    gui_log = open(gui_log_path, "ab")
    # 优先无头：没有窗口、没有控制台，推理逻辑与官方 GUI 同源；
    # 脚本缺失时回退到官方 GUI（此时只能继续弹窗口）。
    headless = HEADLESS_PY.exists()
    script = HEADLESS_PY if headless else REALTIME_PY
    cmd = [str(RUNTIME_PY), str(script)]
    if headless:
        # 无头进程没有窗口，不能再开控制台。
        # 监听不在这里传参 —— 已拆成独立进程 rvc_monitor.py，由 _start_monitor 管理。
        flags = CREATE_NO_WINDOW
    else:
        cmd.append("--auto-start")
        flags = CREATE_NEW_CONSOLE
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(RVC_ROOT), creationflags=flags,
            stdout=gui_log, stderr=subprocess.STDOUT,
        )
        _pid_cache["ts"] = None  # 清缓存，确保秒退检测能探到新进程
    except Exception as e:
        gui_log.close()
        _state["live"].update(running=False, pid=None, audio_switched=False)
        ok, detail = _reset_audio()
        msg = f"拉起 RVC 实时变声失败: {e}"
        if not ok:
            msg += f"；自动还原声卡也失败({detail})，请点「一键恢复音频」"
        raise HTTPException(status_code=500, detail=msg)

    # 秒退检测：无头模式要装 torch 再加载模型，窗口期给足 20s；
    # GUI 模式窗口要经历 import(约5~7s) 才到 argparse，10s 足够。
    deadline = time.time() + (20 if headless else 10)
    while time.time() < deadline:
        if not _realtime_alive():
            gui_log.close()
            _state["live"].update(running=False, pid=None, audio_switched=False)
            ok, detail = _reset_audio()
            msg = f"RVC 实时变声启动即退出（日志见 logs/{gui_log_path.name}）"
            if not ok:
                msg += f"；自动还原声卡失败({detail})，请点「一键恢复音频」"
            raise HTTPException(status_code=500, detail=msg)
        if _live_stream_ready():
            break
        time.sleep(1)
    # 子进程已继承自己的句柄副本，父进程侧可安全关闭，日志仍持续写入
    gui_log.close()

    _state["live"].update(running=True, pid=proc.pid, error="",
                          headless=headless, monitor=monitor)
    threading.Thread(target=_live_waiter, args=(proc,), daemon=True).start()

    # 拉起实时转写子进程（桌宠字幕）：只采真麦 + ASR，无声卡操作，失败不影响变声
    # game 档不自动拉起（whisper 最吃 CPU），需要时可在状态面板手动开
    asr_started = False
    try:
        if profile != "game" and STREAM_PY.exists():
            ASR_RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            asr_log = open(ASR_RUN_LOG, "ab")
            subprocess.Popen(
                [str(VENV_PY), str(STREAM_PY), "--asr-only",
                 "--state-path", str(ASR_STATE_FILE), "--out-dir", str(cfg.OUTPUTS_DIR)],
                stdout=asr_log, stderr=subprocess.STDOUT,
            )
            asr_log.close()
            asr_started = True
    except Exception as e:
        logger.warning("[live] 实时转写子进程拉起失败（桌宠字幕不可用）: %s", e)

    # 自我监听（独立进程）：把 CABLE Output 回环到耳机，让自己听得到变声
    monitor_started = False
    if monitor:
        monitor_started = _start_monitor(monitor_gain)

    return JSONResponse({
        "ok": True, "pid": proc.pid, "audio_switched": True,
        "asr_subtitle": asr_started,
        "headless": headless,
        "monitor": monitor_started,
        "monitor_gain": monitor_gain if monitor_started else None,
        "output_device": OUTPUT_DEVICE,
        "input_device": _live_input_device(),
        "hint": ("变声已在后台运行（无窗口）。系统录音已切到 CABLE Output，"
                 "微信等应用会用变身后的声音"
                 + ("；自我监听已开，你可以在耳机里听到自己。" if monitor_started else "")),
    })


@router.post("/rvc/live/stop")
def rvc_live_stop():
    # 优先按命令行查杀 realtime_gui 进程（覆盖服务器重启后内存 pid 丢失的场景）；
    # 查不到时才回退到记录的 pid，避免误杀复用了同号 PID 的无关进程
    targets = set(_find_realtime_pids())
    if not targets and _state["live"]["pid"]:
        targets.add(_state["live"]["pid"])
    _kill_pids(list(targets), "stop")
    _pid_cache["ts"] = None  # 清缓存，stop 后 status 立即反映真实状态
    _state["live"].update(running=False, pid=None, audio_switched=False,
                          monitor=False)
    _stop_asr_proc()  # 实时转写子进程跟随实时变声一起退出
    _kill_monitor()   # 自我监听回环同理，否则耳机里一直有自己的声音
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
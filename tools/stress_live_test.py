# -*- coding: utf-8 -*-
"""打游戏高负载场景下「实时变声」压测（本机实测脚本）。

模拟游戏占用：CPU 忙循环多进程 + 显存驻留张量 + GPU matmul 满载。
四档压力：none / med / high / extreme。

两层验证：
  probe    Phase A 真实链路：走后端 API 启/停 RVC（game 档），验证高负载下
           启动是否超时/秒退、进程是否 OOM/崩溃、status 是否异常。
  latency  Phase B 延迟量化：独立拉起 rvc_headless.py，注入「连续音 + 脉冲标记」
           到 CABLE → RVC 变声 → 扬声器 → WASAPI loopback 录音，
           测端到端延迟，并从 RVC 日志抽取每块「推理耗时」序列。

用法：
  python tools/stress_live_test.py            # 四档压力全自动（默认）
  python tools/stress_live_test.py --exp kangaroo_v2
  python tools/stress_live_test.py audit-restore   # 应急恢复（杀 RVC / 还原 config / 还原声卡）

输出：outputs/stress_live/ 下每档一个 JSON + 汇总 report.txt。
注意：Phase B 会把变声后的测试音播到物理扬声器（会出声）；Phase A 会把系统
默认录音设备切到 CABLE Output（测完自动还原）。
"""
import argparse
import json
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
RVC_PY = Path("D:/RVC/.venv/Scripts/python.exe")
RVC_DIR = Path("D:/RVC")
RVC_CONFIG = RVC_DIR / "configs" / "config.json"
RVC_EXP = "kangaroo_v2"
REPORT_DIR = ROOT / "outputs" / "stress_live"
BACKEND_URL = "http://127.0.0.1:8000"
LIVE_LOG = REPORT_DIR / "phase_b_live.log"
STREAM_READY = "[headless] STREAM_UP"
INFER_RE = re.compile(r"推理耗时：([\d.]+)秒")

# ---------------------------------------------------------------- 压力档位
PROFILES = {
    "none":     dict(cpu=0,  vram_mb=None, gpu_burn=False, label="无压力(基线)"),
    "med":      dict(cpu=12, vram_mb=3072, gpu_burn=False, label="中压:12核+3G显存"),
    "high":     dict(cpu=23, vram_mb=6144, gpu_burn=True,  label="高压:23核+6G显存+GPU算力"),
    "extreme":  dict(cpu=23, vram_mb=6656, gpu_burn=True,  label="极限:23核+6.5G显存+GPU算力"),
}


def http_json(method: str, path: str, timeout: float = 60.0, body: dict | None = None):
    import urllib.request
    url = BACKEND_URL + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "_http_status": e.code,
                "detail": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ---------------------------------------------------------------- 压力源
class Stressors:
    """CPU/显存/GPU 算力压力源（全部 subprocess 起独立 python，便于 kill）。"""

    STRESS_PY = r"""
import sys, time, hashlib
mode = sys.argv[1]
if mode == "cpu":
    buf = b"x" * 1048576
    while True:
        hashlib.sha256(buf * 4).digest()
elif mode == "vram":
    import torch, json, os
    # argv[2] = "目标MB|档位标签|状态文件目录"；目标一次申请失败（如 8GB 卡上 extreme
    # 请求 6656MB 余量吃紧）时降级为「能占多少占多少」，并把实际占用落盘上报，
    # 避免「档位名不副实」静默发生（2026-09-15 实测 extreme 曾 23/25 压力进程静默退出）。
    # ⚠️ 分隔符不能用 ":" —— Windows 盘符路径里自带冒号，会把目录切碎。
    mb, tag, report_dir = sys.argv[2].split("|")
    target_n = int(int(mb) * 1024 * 1024 / 4)
    RESERVE_BYTES = 384 * 1024 * 1024        # 给系统/其他进程留 ≤384MB
    status_path = os.path.join(report_dir, f"stress_vram_{tag}.json")
    info = {"status": "failed", "reason": "unknown", "target_mb": int(mb)}
    try:
        torch.cuda.init()
        free0, _ = torch.cuda.mem_get_info()
        x = torch.ones(target_n, dtype=torch.float32, device="cuda")
        info = {"status": "ok", "target_mb": int(mb),
                "alloc_mb": x.numel() * 4 // (1024 * 1024),
                "free_mb_at_start": free0 // (1024 * 1024)}
    except Exception as e:                   # CUDA OOM → 降级为尽量多占
        info["reason"] = type(e).__name__
        try:
            free, _ = torch.cuda.mem_get_info()
            n2 = max(0, (free - RESERVE_BYTES)) // 4
            if n2 > 0:
                x = torch.ones(n2, dtype=torch.float32, device="cuda")
                info = {"status": "degraded", "target_mb": int(mb),
                        "alloc_mb": x.numel() * 4 // (1024 * 1024),
                        "reason": "CUDA OOM -> 实占",
                        "free_mb_at_start": free0 // (1024 * 1024)}
        except Exception as e2:
            info = {"status": "failed", "reason": type(e2).__name__, "target_mb": int(mb)}
    os.makedirs(report_dir, exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    while True:
        time.sleep(3600)
elif mode == "gpu":
    import torch
    a = torch.randn(3072, 3072, device="cuda")
    while True:
        a = torch.matmul(a, a)
        torch.cuda.synchronize()
"""

    def __init__(self):
        self.procs: list = []

    def _spawn(self, mode: str, arg: str | None = None, timeout: float = None):
        cmd = [str(VENV_PY), "-c", self.STRESS_PY, mode]
        if arg:
            cmd.append(arg)
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def start(self, cpu: int, vram_mb: int | None, gpu_burn: bool, wait_vram: float = 90.0,
              tag: str = "hot"):
        self.stop()
        # 清掉上一轮/旧档遗留的状态文件，summary() 只反映当前档实际载荷
        for f in REPORT_DIR.glob("stress_vram_*.json"):
            try:
                f.unlink()
            except OSError:
                pass
        for _ in range(cpu):
            self._spawn("cpu")
        if vram_mb:
            # 先占显存、后起 GPU 算力：算力进程自带的 torch 上下文会挤掉大张量分配余量
            self._spawn("vram", f"{vram_mb}|{tag}|{REPORT_DIR}")
        if gpu_burn:
            self._spawn("gpu")
        if vram_mb:
            # 轮询等状态文件落盘（vram 子进程 import torch 在 23 核满载下实测 ~24s，
            # 文件落盘 = 分配已定局，之后 phase_a 的启动判定才可信）。一旦生成立即返回。
            deadline = time.time() + wait_vram
            status_file = REPORT_DIR / f"stress_vram_{tag}.json"
            while time.time() < deadline and not status_file.exists():
                time.sleep(0.2)
            if not status_file.exists():
                print(f"⚠️ [stress] vram 压力源 {tag} 在 {wait_vram:.0f}s 内未落盘状态文件，"
                      f"档位载荷未知", flush=True)
        else:
            time.sleep(2)

    def stop(self):
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        self.procs = []

    def summary(self) -> dict:
        alive = sum(1 for p in self.procs if p.poll() is None)
        s = {"spawned": len(self.procs), "alive": alive}
        # 携带每档 vram 压力源的实际占用（ok=达标 / degraded=实占<目标 / failed=没占上）
        for f in sorted(REPORT_DIR.glob("stress_vram_*.json")):
            tag = f.stem[len("stress_vram_"):]
            try:
                s[f"vram_{tag}"] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return s


# ---------------------------------------------------------------- Phase A
def phase_a(profile: str, exp: str, gather: dict) -> dict:
    """真实链路：后端 API 启动游戏档实时变声 → 观测 60s → 停止。"""
    p = PROFILES[profile]
    out = {"profile": profile, "label": p["label"], "start": None, "ready_at": None,
           "live_ready_seen": False, "crashed": False, "last_error": "", "status_log": [],
           "duration": None}

    # 切 game 档（贴近打游戏场景；字幕/自我监听默认全关）
    http_json("POST", "/api/rvc/live/profile", body={"profile": "game"})

    t0 = time.perf_counter()
    r = http_json("POST", f"/api/rvc/live/start?exp_name={exp}", timeout=60)
    out["start"] = r
    start_el = time.perf_counter() - t0
    out["start_elapsed"] = round(start_el, 2)

    deadline = time.time() + 60
    while time.time() < deadline:
        st = http_json("GET", "/api/rvc/live/status")
        if not st.get("ok"):
            continue
        row = {k: st.get(k) for k in ("live_running", "live_ready", "audio_switched",
                                      "last_error", "gpu_used_mb", "gpu_total_mb",
                                      "live_proc_vram_mb", "headless", "perf_profile")}
        out["status_log"].append({"t": round(time.perf_counter() - t0, 1), **row})
        if st.get("live_ready") and out["ready_at"] is None:
            out["ready_at"] = round(time.perf_counter() - t0, 1)
            out["live_ready_seen"] = True
        if not st.get("live_running"):
            break
        time.sleep(2)

    # 结束时若进程已不在 → 视为运行中崩溃
    st = http_json("GET", "/api/rvc/live/status")
    if not (st.get("ok") and st.get("live_running")):
        out["crashed"] = True
    err = st.get("last_error") or (out["status_log"][-1]["last_error"]
                                   if out["status_log"] else "")
    out["last_error"] = err or ""
    if out["status_log"]:
        out["duration"] = out["status_log"][-1]["t"]

    http_json("POST", "/api/rvc/live/stop", timeout=60)
    gather["phase_a"][profile] = out
    return out


# ---------------------------------------------------------------- Phase B
def _pick_devices():
    """RVC 输入=CABLE Output（从 CABLE In 播测试音去采）、输出=默认扬声器。"""
    import sounddevice as sd
    mme_index = next(i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "MME")
    default_out = sd.query_devices(kind="output")["name"]   # 如 "扬声器 (Senary Audio)"
    ins, outp = [], None
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != mme_index:
            continue
        name = d["name"].strip()
        if d["max_input_channels"] > 0 and "CABLE Output" in name and name not in ins:
            ins.append(name)
        elif d["max_output_channels"] > 0 and name == default_out:
            outp = name
    return (next(iter(ins), None), outp)


def _write_rvc_config(inp: str, outp: str):
    """写入压测用 RVC 配置（输入=CABLE Output、输出=扬声器），返回原始配置用于还原。"""
    original = dict(json.loads(RVC_CONFIG.read_text(encoding="utf-8"))) if RVC_CONFIG.exists() else {}
    base = {"block_time": 0.25, "crossfade_length": 0.05, "extra_time": 2.5,
            "rms_mix_rate": 0.25, "index_rate": 0.5, "threhold": -60.0,
            "sr_type": "sr_model", "f0method": "rmvpe", "sg_hostapi": "MME",
            "I_noise_reduce": False}
    original.update(base)   # 只压测是临时写，还原时用 original（含用户既有值）
    cfg = dict(original)
    cfg["sg_input_device"] = inp
    cfg["sg_output_device"] = outp
    RVC_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return original


def _wait_stream_ready(log_path: Path, deadline: float) -> bool:
    while time.time() < deadline:
        if log_path.exists():
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-8192:]
                if STREAM_READY in tail:
                    return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def _synth_signal(sr: int, dur: float):
    """连续 500Hz 低幅 + 每 2s 一个 150ms 3kHz 脉冲（用于延迟标记）。"""
    n = int(sr * dur)
    t = np.arange(n) / sr
    sig = 0.15 * np.sin(2 * np.pi * 500 * t)
    for k in range(1, int(dur // 2)):
        start = int(sr * (k * 2.0))
        end = min(n, start + int(sr * 0.15))
        if end > start:
            tt = np.arange(end - start) / sr
            sig[start:end] += 0.6 * np.sin(2 * np.pi * 3000 * tt) * np.hanning(end - start)
    return sig


def _detect_pulses(x: np.ndarray, sr: int, n_pulses: int, first_at: float = 2.0,
                   period: float = 2.0, span: float = 0.7):
    """能量包络检测脉冲位置（帧索引）。自适应跟踪：后一个脉冲在上一命中的±span窗口内找。

    RVC 会频谱迁移哔声，相关法不可靠；播放/录音各自起停有随机延迟，
    分脉冲独立搜索窗口（而非全局期望）才不会整串错位。
    """
    env = np.abs(x).astype(np.float64)
    win = int(sr * 0.05)
    if win > 1:
        kernel = np.ones(win) / win
        env = np.convolve(env, kernel, mode="same")
    hits = []
    prev = None
    for k in range(n_pulses):
        center = prev if prev is not None else int(sr * first_at)
        lo = max(0, int(center - sr * span))
        hi = min(len(env), int(center + sr * span))
        if lo >= hi:
            break
        seg = env[lo:hi]
        hits.append(lo + int(np.argmax(seg)))
        prev = int(center + sr * period)
    return hits


def phase_b(profile: str, gather: dict, peak_vram_from_a: int | None) -> dict:
    """延迟量化：注入测试音 → CABLE → RVC → 扬声器 → loopback 录音。"""
    p = PROFILES[profile]
    out = {"profile": profile, "label": p["label"], "stream_ready": False,
           "infer_samples": [], "latency_ms": [], "ready_at_s": None, "ok": False}

    inp, outp = _pick_devices()
    if not inp or not outp:
        out["error"] = f"设备枚举失败 inp={inp} outp={outp}"
        return out
    original = _write_rvc_config(inp, outp)
    # 磁盘备份兜底：异常中断后可用 audit-restore 恢复
    (REPORT_DIR / "rvc_config_backup.json").write_text(
        json.dumps(original, ensure_ascii=False), encoding="utf-8")

    LIVE_LOG.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [str(RVC_PY), str(RVC_DIR / "rvc_headless.py"), "--log", str(LIVE_LOG)],
        cwd=str(RVC_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ok = _wait_stream_ready(LIVE_LOG, deadline=time.time() + 90)
    out["stream_ready"] = ok
    alive = proc.poll() is None
    if not ok or not alive:
        out["error"] = f"RVC 未就绪 stream_ready={ok} alive={alive}"
        _kill_now(proc)
        _restore(original)
        return out

    # 录 30s 默认输出 loopback；播放端同步注入
    import pyaudiowpatch as paudio
    import sounddevice as sd
    sr_play = 48000
    dur = 30.0
    first_at, period = 2.0, 2.0       # 与 _synth_signal 的脉冲位置(k*2.0s)对齐
    n_pulses = int((dur - first_at) // period)
    signal = _synth_signal(sr_play, dur)
    out["n_pulses"] = n_pulses

    # 找注入端点（WASAPI 输出名）
    play_dev = next((d["name"] for d in sd.query_devices()
                     if d["max_output_channels"] > 0 and "CABLE" in (d["name"] or "")), None)
    if not play_dev:
        out["error"] = "找不到 CABLE 播放端点"
        _kill_now(proc)
        _restore(original)
        return out

    audio = paudio.PyAudio()
    try:
        default_out_name = audio.get_default_output_device_info()["name"]
    except Exception:
        out["error"] = "获取默认输出设备失败"
        audio.terminate()
        _kill_now(proc)
        _restore(original)
        return out
    # 双轨 loopback：CABLE 总线（注入原声）= 基准轨；默认扬声器 = RVC 输出轨。
    cable_lb = senary_lb = None
    for d in audio.get_loopback_device_info_generator():
        n = d["name"]
        if n.startswith("扬声器 (VB-Audio Virtual Cable)") and cable_lb is None:
            cable_lb = d
        elif default_out_name in n and senary_lb is None:
            senary_lb = d
    if cable_lb is None or senary_lb is None:
        out["error"] = f"loopback 不全 cable={cable_lb is not None} senary={senary_lb is not None}"
        audio.terminate()
        _kill_now(proc)
        _restore(original)
        return out

    chunk = 2048
    rates = {"cable": int(cable_lb["defaultSampleRate"]),
             "senary": int(senary_lb["defaultSampleRate"])}
    rec: dict[str, list] = {"cable": [], "senary": []}
    stop_flag = {"v": False}

    def recorder(key: str, dev):
        with audio.open(format=paudio.paInt16, channels=2, rate=rates[key],
                        input=True, input_device_index=dev["index"],
                        frames_per_buffer=chunk) as s:
            while not stop_flag["v"]:
                try:
                    rec[key].append(s.read(chunk, exception_on_overflow=False))
                except Exception:
                    break

    import threading
    t_cable = threading.Thread(target=recorder, args=("cable", cable_lb), daemon=True)
    t_senary = threading.Thread(target=recorder, args=("senary", senary_lb), daemon=True)
    t_cable.start()
    t_senary.start()
    time.sleep(0.5)

    play_dev_long = play_dev
    with sd.OutputStream(device=play_dev_long, samplerate=sr_play, channels=2,
                         latency="low") as outp_sd:
        total_frames = int(sr_play * dur)
        w = np.zeros(total_frames, dtype=np.float32)
        w[: len(signal)] = signal.astype(np.float32)
        for off in range(0, total_frames, 4096):
            sm = np.stack([w[off:off + 4096], w[off:off + 4096]], axis=1)
            outp_sd.write(np.ascontiguousarray(sm, dtype=np.float32))
            time.sleep(0.012)            # 维持实时节拍，避免一次灌完失真

    time.sleep(1.5)                      # 播完再补录尾部，覆盖设备缓冲
    stop_flag["v"] = True
    time.sleep(0.3)
    audio.terminate()
    audio = None
    t_cable.join(timeout=5)
    t_senary.join(timeout=5)

    def _save(key: str) -> np.ndarray | None:
        frames = b"".join(rec[key])
        if not frames:
            return None
        with wave.open(str(REPORT_DIR / f"{key}_rec_{profile}.wav"), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(rates[key])
            wf.writeframes(frames)
        x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return x.reshape(-1, 2).mean(axis=1) if x.ndim > 1 else x

    xc, xs = _save("cable"), _save("senary")
    if xc is None or xs is None:
        out["error"] = f"双轨录音缺轨 cable={xc is not None} senary={xs is not None}"
        _kill_now(proc)
        _restore(original)
        return out
    hits_c = _detect_pulses(xc, rates["cable"], n_pulses, first_at, period)
    hits_s = _detect_pulses(xs, rates["senary"], n_pulses, first_at, period)
    # 端到端延迟 = RVC 输出轨脉冲到达时刻 - CABLE 基准轨脉冲到达时刻（自对齐，无需墙钟）
    lat_ms = []
    for hc, hs in zip(hits_c, hits_s):
        lat_ms.append(round(hs / rates["senary"] * 1000 - hc / rates["cable"] * 1000, 1))
    out["latency_ms"] = lat_ms
    out["n_hits"] = {"cable": len(hits_c), "senary": len(hits_s)}

    # 从 RVC 日志抽推理耗时
    try:
        text = LIVE_LOG.read_text(encoding="utf-8", errors="replace")
        out["infer_samples"] = [round(float(v) * 1000, 1) for v in INFER_RE.findall(text)]
    except OSError:
        pass

    # 判定
    ok_final = True
    notes = []
    if out["infer_samples"]:
        s = np.array(out["infer_samples"])
        out["infer_stats"] = {"n": int(len(s)), "mean": round(float(s.mean()), 1),
                              "p50": round(float(np.percentile(s, 50)), 1),
                              "p95": round(float(np.percentile(s, 95)), 1),
                              "max": round(float(s.max()), 1)}
        if out["infer_stats"]["p95"] > 230:
            notes.append("推理耗时 P95 接近/超过 block_time(250ms)，有断流风险")
            ok_final = False
    if not hits_c or not hits_s:
        notes.append("脉冲未检出（链路可能静音，或带宽/丢轨）")
        ok_final = False
    if proc.poll() is not None:
        notes.append("运行期间 RVC 进程退出")
        ok_final = False
        out["crashed"] = True
    out["ok"] = ok_final
    out["notes"] = notes

    _kill_now(proc)
    _restore(original)
    gather["phase_b"][profile] = out
    return out


def _kill_now(proc):
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _restore(original: dict):
    """把压测前的 RVC 配置写回 configs/config.json（每条失败路径都要调用）。"""
    RVC_CONFIG.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")


def audit_restore():
    """应急恢复：杀残留 RVC 进程、用过滤后的备份配置还原 config、还原默认录音设备。"""
    print("[audit-restore] 结束所有 rvc_headless/realtime_gui 进程 ...")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'rvc_headless|realtime_gui' } | "
                    "ForEach-Object { taskkill /PID $_.ProcessId /T /F }"],
                   capture_output=True, timeout=60)
    # 还原 config：读压测期间写的磁盘备份
    buf = REPORT_DIR / "rvc_config_backup.json"
    if buf.exists():
        try:
            data = json.loads(buf.read_text(encoding="utf-8"))
            if data:
                RVC_CONFIG.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                print(f"[audit-restore] 还原 configs/config.json <- {buf.name}")
        except Exception:
            print("[audit-restore] 备份损坏，跳过 config 还原（无法自动恢复）")
    print("[audit-restore] 还原默认录音设备 ...")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "m2_server" / "audio_config.ps1"),
                    "-action", "reset"], capture_output=True, timeout=120)


# ---------------------------------------------------------------- 主流程
def run_ramp(exp: str):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    gather = {"phase_a": {}, "phase_b": {}, "stressors": {}}

    # 已有健康后端（桌面端托管）→ 复用；否则自己拉一个
    backend = None
    if not http_json("GET", "/api/health", timeout=10).get("status") == "ok":
        print("=== 启动后端(8000) ===", flush=True)
        backend = subprocess.Popen(
            [str(VENV_PY), "server.py"],
            cwd=str(ROOT / "m2_server"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if http_json("GET", "/api/health", timeout=10).get("status") == "ok":
                break
            time.sleep(1)
        else:
            print("[FAIL] 后端未就绪", flush=True)
            if backend:
                backend.terminate()
            sys.exit(1)

    # 记录并暂存原性能档位（压测统一切 game 档，结束还原）
    orig_profile = http_json("GET", "/api/rvc/live/profile").get("profile") or "balanced"
    try:
        stress = Stressors()
        for profile, p in PROFILES.items():
            print(f"\n=== 压力档: {profile} ({p['label']}) ===", flush=True)
            stress.start(cpu=p["cpu"], vram_mb=p["vram_mb"], gpu_burn=p["gpu_burn"],
                         tag=profile)
            gather["stressors"][profile] = stress.summary()
            vram_stat = gather["stressors"][profile].get(f"vram_{profile}")
            if vram_stat:
                if vram_stat.get("status") == "ok":
                    print(f"[stress] {profile} 显存压力占用 {vram_stat.get('alloc_mb')}MB"
                          f" (目标 {vram_stat.get('target_mb')}MB)", flush=True)
                else:
                    print(f"⚠️ [stress] {profile} 显存压力{('未达标' if vram_stat.get('status') == 'degraded' else '启动失败')}: "
                          f"{vram_stat} —— 结果请按实际载荷解读", flush=True)
            time.sleep(1)
            try:
                a = phase_a(profile, exp, gather)
                print(f"[A:{profile}] start_ok={bool(a.get('start',{}).get('ok'))} "
                      f"start_elapsed={a.get('start_elapsed')}s ready_at={a.get('ready_at')}s "
                      f"crashed={a.get('crashed')} err={a.get('last_error','')!r}", flush=True)
            except Exception as e:
                print(f"[A:{profile}] EXC {e}", flush=True)
            time.sleep(3)
            try:
                b = phase_b(profile, gather, None)
                print(f"[B:{profile}] stream_ready={b.get('stream_ready')} "
                      f"infer={b.get('infer_stats')} lat_first5={b.get('latency_ms', [])[:5]} "
                      f"ok={b.get('ok')} notes={b.get('notes')}", flush=True)
                if b.get("latency_ms") is not None:
                    print(f"[B:{profile}] 轨文件 {REPORT_DIR / ('cable_rec_' + profile + '.wav')} + "
                          f"{REPORT_DIR / ('senary_rec_' + profile + '.wav')}", flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[B:{profile}] EXC {e}", flush=True)
        stress.stop()
        http_json("POST", "/api/rvc/live/profile", body={"profile": orig_profile})
    finally:
        if backend:
            backend.terminate()

    rep = REPORT_DIR / "report.json"
    rep.write_text(json.dumps(gather, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {rep}", flush=True)


def main():
    # 输出编码不是装饰：Windows 下 stdout 被重定向（管道/文件）时按 ANSI(cp936) 编码，
    # 而压力测试报告里的 `✓`/`⚠️` 在 GBK 之外 → UnicodeEncodeError + 报告断掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=RVC_EXP)
    ap.add_argument("--only", choices=list(PROFILES) + [None], default=None)
    args = ap.parse_args()
    if args.only:
        for k in list(PROFILES):
            if k != args.only:
                PROFILES.pop(k)
    run_ramp(args.exp)


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""音频溯源水印（`sound.watermark` 插件核心）：Meta AudioSeal（MIT）的本地封装。

两个动作，够用就好：
  · **embed**   —— 给一段音频嵌一个隐形的 16 位溯源码（4 个 hex 字符，如 `a1b2`），
                   听感不变、文件时长不变；转码成 mp3 后仍可检出（步骤0 实测 prob=1.000）。
  · **detect**  —— 给一段音频，返回「是否带我们的水印 / 置信度 / 码」。

★ 口径红线（写文案时同样要守）
  只说「**可检出 / 可溯源**」，不说「防伪 / 无法去除」—— 隐形水印是概率性的取证
  信号，不是加密签名；把它宣传成"防伪"是在向用户承诺做不到的事。

★ 七个环境坑（步骤0 实测，2026-10-05，详见 `.workbuddy-ai/memory/2026-10-05.md`）
  1. `NO_TORCH_COMPILE=1` 必须在**任何** torch/audioseal 导入之前 setdefault
     （audioseal 的 `@torch_compile_lazy` 在装饰期读 env；Windows 无 Triton，
      不设 → 首次 embed 崩在 inductor `TritonMissing`，设了 14.9s → 0.63s）。
     本模块的 torch/audioseal 导入**全部在函数里**，所以模块顶部这一行就是"最早时点"。
  2. 模型输入必须 3D `(batch, channels, samples)`；2D 会进 dynamo 报 permute 错。
  3. 从**裸 .pth** 加载必须显式 `nbits=16`（裸 checkpoint 的 xp.cfg 缺该字段 →
     OmegaConf MissingMandatoryValue）。
  4. 本 venv 的 `torchaudio.load` 需要 torchcodec（未装）→ 统一 soundfile 读 +
     `torchaudio.functional.resample` 重采样。
  5. 重采样有 ±1~2 样本舍入 → 拼回原长时按 min 对齐裁剪。
  6. 推理包 `torch.no_grad()`（否则结果张量 requires_grad，`.numpy()` 直接报错）。
  7. 权重下载：huggingface.co 本机常不通，`https://hf-mirror.com` 可用 ——
     复用 `market_download.DownloadManager`（域名白名单本就含这两家 + 断点续传 +
     sha256 校验 + torch 文件头校验），不再另写一份下载器。

★ 采样率与音质：为什么非 16k 的音频走「水印信号叠加」而不是整段降采样
  AudioSeal 模型固定吃 16kHz。TTS 产物是 24kHz —— 若"降到 16k 加水印再升回来"，
  高于 8kHz 的内容全没了（听感发闷），绝不能挂在合成链路上（哪怕是可选开关）。
  所以 embed 对 sr≠16k 的输入：降到 16k 让模型生成**水印信号**（`wm = gen(x16) - x16`），
  把 wm 升回原采样率叠加到原音频上 —— 原音频一个采样都不动，只加了水印能量；
  检测端再把 24k 降到 16k 时，0-8kHz 频带里的水印还在（e2e 验证）。

★ 设备：一律 CPU
  主进程今天**没有** CUDA context（TTS 在独立 venv worker、RVC 在 D:/RVC 的 venv）。
  一旦这里碰 GPU：① 留下 ~500MB context 常驻，会把「实时变声」的 2GB 显存预检
  顶到红线上；② 显存紧张时 CUDA alloc 失败的报错面全落到水印头上。
  AudioSeal 模型很小（generator 56MB），CPU 上单文件 embed 亚秒级，够用。

★ 输出与状态
  · 页面产物写 `outputs/.session/`（`session_out`，退出即删）；
  · 「合成自动加标识」的开关 + 溯源码存 `media/watermark_auto.json`（持久），
    挂在 `tts_api.synth_wav` 出口，**纯旁路**：任何失败都吞掉，绝不影响合成。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

import config as cfg

# ★ 坑 1：必须在任何 torch/audioseal 导入之前（本模块顶层只 import 标准库 + config，
#   torch/audioseal 全在函数内，所以这行就是它们的"最早时点"）。
os.environ.setdefault("NO_TORCH_COMPILE", "1")

#: 模型权重文件名 / 校验值（步骤0 从 HF facebook/audioseal 下载后实测记录）。
#: 下载与校验交给 `market_download.DownloadManager`，这里只提供参数。
GENERATOR_FILE = "generator_base.pth"
DETECTOR_FILE = "detector_base.pth"
GENERATOR_SHA256 = "7a845b5fbe9364a63a3909d8ab3fe064d13a76ae4c2e983573e08c69b7b51748"
DETECTOR_SHA256 = "8a78e8a83584113523e161fc599fcab10fd0e94c04d2eb9d2fa1e9ec91ab69d9"
GENERATOR_BYTES = 58_805_980
DETECTOR_BYTES = 34_667_641

#: HF 官方 + 镜像（两台都在 market_download 的域名白名单里）。
_BASE_URL = "https://huggingface.co/facebook/audioseal/resolve/main"
_MIRROR_URL = "https://hf-mirror.com/facebook/audioseal/resolve/main"

#: AudioSeal 的工作采样率与消息位数（模型卡固定值，不可改）。
SR = 16000
NBITS = 16

#: 检出判定阈值：detector 输出的逐帧概率取平均前，AudioSeal 官方默认 0.5。
DETECT_THRESHOLD = 0.5

#: 自动加标识的开关/溯源码落盘处。media/ 是持久用户数据（outputs 会被清）。
AUTO_STATE_FILE = "watermark_auto.json"


class WatermarkError(Exception):
    """水印层的业务错误，`status` 供 API 层映射 HTTP 状态码。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- 基础小件


def models_dir() -> Path:
    """权重目录（每次现读 cfg —— 测试要 monkeypatch，模块级早绑定会静默失效）。"""
    return Path(cfg.WATERMARK_MODELS_DIR)


def code_to_bits(code: str) -> list[int]:
    """4 个 hex 字符 → 16 个 0/1 位。非法输入抛 400。"""
    c = (code or "").strip().lower()
    if len(c) != 4 or any(ch not in "0123456789abcdef" for ch in c):
        raise WatermarkError(f"溯源码必须是 4 位十六进制（如 a1b2），收到：{code!r}")
    return [(int(ch, 16) >> (3 - i)) & 1 for ch in c for i in range(4)]


def bits_to_code(bits) -> str:
    """16 个 0/1 位 → 4 位 hex（检测结果的展示格式）。"""
    v = 0
    for b in bits:
        v = (v << 1) | (1 if b else 0)
    return f"{v:04x}"


def random_code() -> str:
    """随机一个 4 位 hex 溯源码。"""
    return secrets.token_hex(2)


def _state_path() -> Path:
    return Path(cfg.MEDIA_DIR) / AUTO_STATE_FILE


def _read_state() -> dict:
    try:
        raw = json.loads(_state_path().read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def auto_enabled() -> bool:
    """「合成自动加标识」是否开着（默认关）。"""
    return bool(_read_state().get("enabled"))


def auto_code() -> str:
    """自动加标识用的溯源码（首次取用时生成并固定，用户要在页面上能看到它）。"""
    st = _read_state()
    code = str(st.get("code") or "")
    if code_to_bits_ok(code):
        return code
    return random_code()


def code_to_bits_ok(code: str) -> bool:
    try:
        code_to_bits(code)
        return True
    except WatermarkError:
        return False


def set_auto(enabled: bool, code: str = "") -> dict:
    """写开关与溯源码。开而不给码 → 沿用已有码或新生成一个（并落盘固定）。"""
    st = _read_state()
    new_code = (code or "").strip().lower()
    if new_code and not code_to_bits_ok(new_code):
        raise WatermarkError(f"溯源码必须是 4 位十六进制（如 a1b2），收到：{code!r}")
    if not new_code:
        new_code = st.get("code") if code_to_bits_ok(str(st.get("code") or "")) else random_code()
    payload = {"enabled": bool(enabled), "code": new_code}
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    tmp.replace(p)
    return payload


# ---------------------------------------------------------------- 权重：装/下


def installed() -> bool:
    """generator + detector 两个权重都在位（只看文件存在，sha256 由下载器保证）。"""
    d = models_dir()
    return (d / GENERATOR_FILE).is_file() and (d / DETECTOR_FILE).is_file()


def _download_tasks() -> dict:
    """两个权重各自的下载任务状态（沿用市场下载器的进度结构）。"""
    from market_download import get_manager

    mgr = get_manager()
    return {
        "generator": mgr.task_status("audioseal_generator"),
        "detector": mgr.task_status("audioseal_detector"),
    }


def status() -> dict:
    """给 `GET /api/watermark/status` 的总览：权重在不在 / 下载进度 / 开关。"""
    return {
        "models_dir": str(models_dir()),
        "installed": installed(),
        "size_hint_mb": round((GENERATOR_BYTES + DETECTOR_BYTES) / 1024 / 1024),
        "download": _download_tasks(),
        "auto": {"enabled": auto_enabled(), "code": auto_code() if auto_enabled() else ""},
    }


def start_download() -> dict:
    """启动权重下载（幂等：已装好直接回 done；在下的任务拒绝重复由下载器管）。

    复用 `market_download.DownloadManager`：断点续传、主源失败回退 hf-mirror、
    sha256 强校验、.pth 文件头校验（防 404 HTML 当权重落盘）都是现成的。
    """
    if installed():
        return {"status": "done", "installed": True}
    from market_download import MarketError, get_manager

    mgr = get_manager()
    # 下载完成 ≠ 可用：产物还躺在 outputs/market/，先补一次搬运（覆盖"上次下完了
    # 但服务重启，安装线程没赶上"的场景），再起监听线程跟住本次下载。
    install_from_downloads(mgr)
    if installed():
        return {"status": "done", "installed": True}
    jobs = [
        ("audioseal_generator", GENERATOR_FILE, GENERATOR_SHA256, GENERATOR_BYTES),
        ("audioseal_detector", DETECTOR_FILE, DETECTOR_SHA256, DETECTOR_BYTES),
    ]
    states: dict[str, dict] = {}
    for name, filename, sha, size in jobs:
        if (models_dir() / filename).is_file():
            states[name] = {"status": "done"}
            continue
        try:
            states[name] = mgr.start(
                name,
                url=f"{_BASE_URL}/{filename}",
                mirror_url=f"{_MIRROR_URL}/{filename}",
                sha256=sha,
                expected_size=size,
                filename=filename,
            )
        except MarketError as e:  # 同名任务在下 → 如实回报
            states[name] = mgr.task_status(name) or {"status": "downloading", "error": str(e)}
    _start_installer(mgr)
    return {"status": "downloading", "tasks": states}


_INSTALL_LOCK = threading.Lock()
_INSTALLER: dict = {}


def install_from_downloads(mgr=None) -> dict:
    """把下载器落在 `outputs/market/` 的权重搬进 `models_dir()`。

    ★ 为什么必须有这一步（2026-10-05 隔离实例实测抓到，修前必错）：
      `market_download.DownloadManager` 的产物**恒定**落在它的 `download_dir`
      （= `cfg.OUTPUTS_DIR/market`）—— `start()` 没有 dest 参数，它不知道调用方
      想把文件放哪。而本模块的 `installed()` / `_load_models()` 只看 `models_dir()`
      （= `tts_models/audioseal`）。
      少了搬运，「下载完成」与「可用」之间就永远差一步：两个 .pth 都 `done`、
      sha256 与文件头校验都过了，`POST /api/watermark/embed` 仍一律 **409
      「水印模型还没下载」**；页面上那个按钮只能反复重下 —— 用户侧的观感就是
      "下载了但装不上"。修复前的实测证据：`outputs/market/{generator,detector}_base.pth`
      在位（58.8MB + 34.7MB），`tts_models/audioseal/` 连目录都不存在。

    幂等：已在位 / 目标已存在 → 不搬不覆盖；只搬状态为 `done` 的任务。
    `mgr` 可注入（测试不必真起下载器）。
    """
    with _INSTALL_LOCK:
        if installed():
            return {"installed": True, "moved": []}
        if mgr is None:
            from market_download import get_manager

            mgr = get_manager()
        d = models_dir()
        moved: list[str] = []
        for name, fn in (
            ("audioseal_generator", GENERATOR_FILE),
            ("audioseal_detector", DETECTOR_FILE),
        ):
            dest = d / fn
            if dest.is_file():
                continue
            st = mgr.task_status(name) or {}
            if st.get("status") != "done":
                continue
            src = Path(st.get("dest") or (Path(mgr.download_dir) / fn))
            if not src.is_file():
                continue
            d.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append(fn)
        return {"installed": installed(), "moved": moved}


def _start_installer(mgr, timeout_s: int = 1800) -> None:
    """后台盯住两个下载任务，能装就装（下载可能经历镜像回退 + 断点续传，慢）。

    只在 `start_download()` 里起，且**去重** —— 页面按钮可以反复点，但不能攒出
    一堆轮询线程。线程是 daemon：进程退出不等它。

    ★ `mgr` 由调用方**捕获后传进来**，线程内不再重新 `get_manager()`：
      否则测试里 monkeypatch 掉的假下载器会在 monkeypatch 撤销后"复活"成真下载器，
      而真下载器会去读 `outputs/market/` 的历史完成态 —— 那意味着一个测试能偷偷
      把权重搬进真目录。捕获实例同时也让「测试用假件、运行用真件」这条边界是硬的。
    """
    with _INSTALL_LOCK:
        th = _INSTALLER.get("thread")
        if th is not None and th.is_alive():
            return
        th = threading.Thread(
            target=_install_when_ready, args=(mgr, timeout_s), name="wm-installer", daemon=True
        )
        _INSTALLER["thread"] = th
    th.start()


def _install_when_ready(mgr, timeout_s: int = 1800) -> None:
    """轮询到「装好」为止；两个任务都不再活跃时提前收工（失败/取消就没什么可等的）。"""
    names = ("audioseal_generator", "audioseal_detector")
    end = time.time() + timeout_s
    while time.time() < end:
        if install_from_downloads(mgr).get("installed"):
            return
        active = any(
            (mgr.task_status(n) or {}).get("status") in ("downloading", "idle") for n in names
        )
        if not active:
            return
        time.sleep(2)


# ---------------------------------------------------------------- 模型单例

_MODELS: dict[str, tuple] = {}
_MODELS_LOCK = threading.Lock()


def _load_models(device: str = "cpu"):
    """懒加载 generator + detector（进程内单例，按设备缓存）。

    `nbits=16` 必须显式给（坑 3）；导入全在函数里（坑 1 的另一半：让本模块
    在没装 audioseal 的机器上也能被 import —— 插件门禁要求 router 可容错加载）。
    """
    with _MODELS_LOCK:
        hit = _MODELS.get(device)
        if hit is not None:
            return hit
        try:
            from audioseal import AudioSeal  # noqa: F401  torch 随之进环境；此处只验可导入
        except ImportError as e:
            raise WatermarkError(
                "音频水印组件未安装（audioseal / torch）。请在能力管理里重装该能力的依赖，"
                "或执行 pip install audioseal。",
                status=503,
            ) from e
        d = models_dir()
        gen_p, det_p = d / GENERATOR_FILE, d / DETECTOR_FILE
        missing = [p.name for p in (gen_p, det_p) if not p.is_file()]
        if missing:
            raise WatermarkError(
                f"水印模型还没下载（缺 {'、'.join(missing)}）。请在「音频水印」页先下载模型（约 90MB）。",
                status=409,
            )
        gen = AudioSeal.load_generator(str(gen_p), nbits=NBITS, device=device)
        det = AudioSeal.load_detector(str(det_p), nbits=NBITS, device=device)
        gen.eval()
        det.eval()
        _MODELS[device] = (gen, det, device)
        return _MODELS[device]


# ---------------------------------------------------------------- 读音频 / 重采样


def _read_mono(path: Path) -> tuple:
    """soundfile 读成单声道 float32（坑 4：不用 torchaudio.load）。多声道取均值。

    soundfile 读不了的容器（mp3/m4a…）用 ffmpeg 解成临时 wav 再读 —— 检测/植入
    的输入恰恰最常见就是 mp3（用户拿到的分享件），这步兜底不能省。
    """
    import numpy as np
    import soundfile as sf

    p = Path(path)
    try:
        x, sr = sf.read(str(p), dtype="float32", always_2d=False)
    except Exception:
        x, sr = _read_mono_via_ffmpeg(p)
    if getattr(x, "ndim", 1) > 1:
        x = x.mean(axis=1)
    x = np.ascontiguousarray(x, dtype=np.float32)
    if x.size == 0:
        raise WatermarkError("这段音频是空的")
    return x, int(sr)


def _read_mono_via_ffmpeg(p: Path) -> tuple:
    """ffmpeg 解码任意音频容器 → 临时 wav → soundfile 读。解不了抛 400（带原格式提示）。"""
    import subprocess
    import tempfile

    import soundfile as sf

    from common import find_ffmpeg

    try:
        exe = find_ffmpeg()
    except Exception as e:
        raise WatermarkError(f"读不出这段音频（{p.suffix or '未知格式'}），且本机没有 ffmpeg 可兜底解码：{e}") from e
    tmp = Path(tempfile.mkdtemp(prefix="wm_dec_")) / "dec.wav"
    try:
        r = subprocess.run(
            [exe, "-y", "-i", str(p), "-ac", "1", str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not tmp.is_file():
            tail = (r.stderr or "").strip().splitlines()[-1:]
            raise WatermarkError(
                f"读不出这段音频（{p.suffix or '未知格式'}）—— ffmpeg 也解不了"
                f"{'：' + tail[0] if tail else ''}"
            )
        x, sr = sf.read(str(tmp), dtype="float32", always_2d=False)
        return x, int(sr)
    except WatermarkError:
        raise
    except Exception as e:
        raise WatermarkError(f"读不出这段音频（{p.suffix or '未知格式'}）：{e}") from e
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except Exception:
            pass


def _resample_t(x, orig_sr: int, target_sr: int):
    """torchaudio 单重采样（坑 4/5：sinc 插值，比线性插值保真；长度按 min 对齐）。"""
    import torchaudio.functional as AF

    y = AF.resample(x, orig_sr, target_sr)
    return y


def _as_3d(x):
    """(T,) 或 (C,T) → (1,C,T) 的 float32 tensor（坑 2：模型只认 3D，且只吃 tensor）。"""
    import numpy as np
    import torch

    if isinstance(x, torch.Tensor):
        t = x.float()
    else:
        t = torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))
    if t.ndim == 1:
        t = t[None, None, :]
    elif t.ndim == 2:
        t = t[None, :, :]
    return t


# ---------------------------------------------------------------- embed / detect


def embed_file(src: Path, dst: Path, code: str = "", alpha: float = 1.0) -> dict:
    """给 `src` 嵌入溯源码，写出 `dst`（覆盖语义由调用方管）。返回 {code, sr, seconds}。

    码留空 → 随机生成（返回值里带回，用户要能记下它）。
    音频本身 ≤1 路单声道处理；写盘固定 PCM_16（会话产物与既有链路同口径）。
    """
    import numpy as np
    import soundfile as sf
    import torch

    bits = code_to_bits(code) if (code or "").strip() else None
    use_code = (code or "").strip().lower() if bits else random_code()
    bits = code_to_bits(use_code)

    x, sr = _read_mono(Path(src))
    gen, _det, _dev = _load_models("cpu")
    msg = torch.tensor([bits], dtype=torch.float32)

    with torch.no_grad():
        if sr == SR:
            x16 = _as_3d(x)
            y = gen(x16, sample_rate=SR, message=msg)[0, 0].numpy()
            y = y[: x.size]  # 坑 5：seanet 补零裁回原长
        else:
            # 非 16k：只生成水印信号再叠加（见模块注释「采样率与音质」）。
            x16_t = _resample_t(torch.from_numpy(x), sr, SR)
            x16 = _as_3d(x16_t.numpy().astype(np.float32))
            wm16 = gen(x16, sample_rate=SR, message=msg)[0, 0] - x16[0, 0]
            wm = _resample_t(wm16, SR, sr)[: x.size]
            n = min(wm.numel(), x.size)
            y = x.copy()
            y[:n] += alpha * wm[:n].numpy().astype(np.float32)

    # 防削波：叠加/生成后峰值超 0.97 就整体等比回缩（与 sfx_lib._peak_norm 同策略）
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0.97:
        y = y * (0.97 / peak)

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), y, sr, subtype="PCM_16")
    return {"code": use_code, "sr": sr, "seconds": round(y.size / sr, 2)}


def detect_file(src: Path) -> dict:
    """检出：返回 {watermarked, prob, code, threshold}。

    输入不管什么采样率，一律降到 16k 再进 detector（模型只认 16k）。
    prob 是 AudioSeal 的逐帧检出概率（0~1）；≥0.5 判"带水印"。
    """
    import torch

    x, sr = _read_mono(Path(src))
    _gen, det, _dev = _load_models("cpu")
    x16 = _as_3d(_resample_t(torch.from_numpy(x), sr, SR).numpy().astype("float32"))
    with torch.no_grad():
        prob_t, msg_t = det.detect_watermark(x16, sample_rate=SR)
    prob = float(prob_t[0])
    code = bits_to_code(msg_t[0].int().tolist())
    return {
        "watermarked": prob >= DETECT_THRESHOLD,
        "prob": round(prob, 3),
        "code": code,
        "threshold": DETECT_THRESHOLD,
    }


# ---------------------------------------------------------------- 合成链路旁路


def auto_mark_if_enabled(path: Path) -> dict | None:
    """「合成自动加标识」的旁路钩子：开着就给这条产物原地嵌码，**任何失败都吞掉**。

    为什么吞错：这是挂在整个合成出口上的可选增强 —— 水印挂了绝不能把一次本来
    成功的合成变成失败（用户等着发微信/试听，不是等着收水印的报错）。
    返回嵌入结果（调试/测试用），没开或失败返回 None。
    """
    if not auto_enabled():
        return None
    tmp = Path(path).with_name(Path(path).stem + ".wm.tmp.wav")
    try:
        info = embed_file(Path(path), tmp, code=auto_code())
        tmp.replace(Path(path))
        return info
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None

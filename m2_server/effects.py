"""变声效果器链：对任意合成/转换输出做 DSP 后处理（混响/回声/EQ/变调等）。

设计：
- 每个效果是独立函数 (float32 mono, sr, params) -> float32 mono，可异常回退直通
- 效果链 = [{type, params}, ...] 依序应用，单一效果失败跳过并记录（不中断整链）
- 纯 numpy/scipy/librosa 本地计算：不占 GPU，不与 ASR/TTS/RVC 抢资源

用途：离线变声、级联输出、TTS 产物的「最后一公里」加工
（例：目标音色 + 教堂混响 / 老电话 / 机器人 / 广播电台感）。
"""
import math
from pathlib import Path

import numpy as np
from scipy import signal

try:  # librosa 仅 pitch 用到；缺了则该效果降级为直通
    import librosa
except ImportError:
    librosa = None


# ---------------- 基础工具 ----------------

def _mono(x: np.ndarray) -> np.ndarray:
    if x.ndim > 1:
        x = x[:, 0]
    return np.ascontiguousarray(x, dtype=np.float32)


def _clamp01(v, lo=0.0, hi=1.0) -> float:
    return float(min(hi, max(lo, v)))


def _f(params: dict, key: str, default: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, float(params.get(key, default)))))


def _norm(x: np.ndarray, peak: float = 0.97) -> np.ndarray:
    """防削波：整体峰值超限时等比回缩（不做响度归一，保留效果本身动态）。"""
    m = float(np.max(np.abs(x))) if len(x) else 0.0
    if m > peak:
        x = x * (peak / m)
    return x


# ---------------- 各效果实现 ----------------

def fx_reverb(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """Schroeder 混响：4 comb + 2 allpass。room 越大衰减越慢、湿声越多。"""
    room = _f(p, "room", 0.5, 0.05, 0.95)      # 房间尺寸 → 衰减/预延迟
    wet = _f(p, "wet", 0.35, 0.0, 1.0)
    damp = _f(p, "damp", 0.3, 0.0, 0.9)        # 高频阻尼（软墙面感）
    rt60 = 0.3 + room * 4.2                    # 0.3s~4.5s

    # comb：按 rt60 反推反馈系数 g = 10^(-3*L/(sr*rt60))
    delays_ms = [29.7, 37.1, 41.1, 43.7]
    out = np.zeros_like(x)
    for d_ms in delays_ms:
        L = max(1, int(sr * d_ms / 1000))
        g = 10 ** (-3.0 * (L / sr) / rt60)
        buf = np.zeros(L, dtype=np.float64)
        y = np.zeros_like(x, dtype=np.float64)
        idx = 0
        # 阻尼一阶低通系数（comb 内反馈环）
        damp_g = damp * 0.7
        last = 0.0
        for i in range(len(x)):
            v = buf[idx] * (1 - damp_g) + last * damp_g
            last = v
            y[i] = v
            buf[idx] = x[i] + g * v
            idx = (idx + 1) % L
        out += y
    out /= len(delays_ms)

    # allpass：抹平梳状染色
    for d_ms in (5.0, 1.7):
        L = max(1, int(sr * d_ms / 1000))
        buf = np.zeros(L, dtype=np.float64)
        y = np.zeros_like(x, dtype=np.float64)
        idx = 0
        g = 0.7
        for i in range(len(x)):
            v = buf[idx]
            y[i] = -g * x[i] + v
            buf[idx] = x[i] + g * v
            idx = (idx + 1) % L
        out = out * 0.7 + y * 0.3

    # 预延迟 20ms 防止湿声糊死干声
    pre = int(sr * 0.02)
    wet_sig = np.concatenate([np.zeros(pre, dtype=np.float32), out.astype(np.float32)])
    if len(wet_sig) < len(x):
        wet_sig = np.pad(wet_sig, (0, len(x) - len(wet_sig)))
    return _norm(x * (1 - wet) + wet_sig[: len(x)] * wet * 1.2)


def fx_echo(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """回声：单 tap 延迟 + 反馈衰减。"""
    delay_s = _f(p, "delay_s", 0.25, 0.02, 2.0)
    feedback = _f(p, "feedback", 0.35, 0.0, 0.9)
    wet = _f(p, "wet", 0.4, 0.0, 1.0)
    n = int(sr * delay_s)
    y = x.astype(np.float64).copy()
    buf = np.zeros(len(x) + n * 12, dtype=np.float64)
    buf[: len(x)] = x
    tap = n
    fb = feedback
    while tap < len(buf) and fb > 0.01:
        src = tap - n
        seg = buf[src: src + (len(buf) - tap)] * fb
        buf[tap:] += seg
        tap += n
        fb *= feedback
    out = (x * (1 - wet) + buf[: len(x)] * wet).astype(np.float32)
    return _norm(out)


def fx_eq(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """三段 EQ：低架（百叶箱感）/ 中峰（鼻音/电话感）/ 高架（空气感）。dB 可负。"""
    low_db = _f(p, "low_db", 0.0, -18.0, 18.0)
    mid_db = _f(p, "mid_db", 0.0, -18.0, 18.0)
    mid_hz = _f(p, "mid_hz", 1200.0, 200.0, 6000.0)
    high_db = _f(p, "high_db", 0.0, -18.0, 18.0)
    y = x.astype(np.float64)
    if abs(low_db) > 0.1:
        sos = _shelf(sr, 250, low_db, "low")
        y = signal.sosfilt(sos, y)
    if abs(mid_db) > 0.1:
        sos = _peak(sr, mid_hz, 0.8, mid_db)
        y = signal.sosfilt(sos, y)
    if abs(high_db) > 0.1:
        sos = _shelf(sr, 3500, high_db, "high")
        y = signal.sosfilt(sos, y)
    return _norm(y.astype(np.float32))


def _shelf(sr: int, fc: float, gain_db: float, kind: str):
    """Orfanidis 架子的简化实现：用 peaking Q 常数近似 shelf。"""
    # peaking 滤波器，Q 取 0.9，中心频率在截止处近似 shelf 响应
    return _peak(sr, fc, 0.9, gain_db * 0.85)


def _peak(sr: int, fc: float, q: float, gain_db: float):
    """RBJ peaking EQ biquad，返回 sos。"""
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * fc / sr
    alpha = math.sin(w0) / (2 * q)
    b0 = 1 + alpha * A
    b1 = -2 * math.cos(w0)
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * math.cos(w0)
    a2 = 1 - alpha / A
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def fx_pitch(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """变调（半音）：librosa 相位声码器。无 librosa 时直通。"""
    if librosa is None:
        return x
    semitones = _f(p, "semitones", 0.0, -12.0, 12.0)
    if abs(semitones) < 0.05:
        return x
    y = librosa.effects.pitch_shift(y=x.astype(np.float32), sr=sr,
                                    n_steps=semitones)
    return _norm(np.asarray(y, dtype=np.float32))


def fx_speed(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """变速不变调：时间拉伸。"""
    if librosa is None:
        return x
    rate = _f(p, "rate", 1.0, 0.5, 2.0)
    if abs(rate - 1.0) < 0.02:
        return x
    y = librosa.effects.time_stretch(y=x.astype(np.float32), rate=rate)
    return _norm(np.asarray(y, dtype=np.float32))


def fx_telephone(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """电话音：300~3400Hz 带通 + 轻失真 + 压缩，老座机/收音机感。"""
    drive = _f(p, "drive", 0.4, 0.0, 1.0)
    sos = signal.butter(4, [300 / (sr / 2), 3400 / (sr / 2)], btype="band", output="sos")
    y = signal.sosfilt(sos, x.astype(np.float64))
    y = np.tanh(y * (1 + drive * 7)) * 0.85  # 软削波失真
    y = y / max(0.3, float(np.max(np.abs(y)))) * 0.7  # 电话感的强压缩
    return _norm(y.astype(np.float32))


def fx_robot(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """机器人音：环形调制（载波乘法）+ 少量混响融合，机械单调感。"""
    freq = _f(p, "freq", 55.0, 20.0, 400.0)
    mix = _f(p, "mix", 0.85, 0.0, 1.0)
    t = np.arange(len(x), dtype=np.float64) / sr
    carrier = np.sign(np.sin(2 * math.pi * freq * t))  # 方波载波更"金属"
    ring = x.astype(np.float64) * carrier
    y = x * (1 - mix) + ring * mix
    return _norm(y.astype(np.float32))


def fx_tremolo(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """颤音：振幅 LFO 调制（老式电台/警笛感）。"""
    rate = _f(p, "rate", 5.0, 0.5, 20.0)
    depth = _f(p, "depth", 0.6, 0.0, 1.0)
    t = np.arange(len(x), dtype=np.float64) / sr
    lfo = 0.5 * (1 + depth) + 0.5 * (1 - depth) * np.sin(2 * math.pi * rate * t)
    return _norm((x * lfo).astype(np.float32))


def fx_chorus(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """合唱：多路微延迟 + LFO 调制叠加，单声源变厚（群感）。"""
    depth_ms = _f(p, "depth_ms", 6.0, 1.0, 20.0)
    rate = _f(p, "rate", 0.8, 0.1, 5.0)
    voices = int(_f(p, "voices", 3, 1, 6))
    y = x.astype(np.float64) / (voices + 1)
    t = np.arange(len(x), dtype=np.float64) / sr
    for i in range(voices):
        base_ms = depth_ms * (1 + i * 0.6)
        lfo = np.sin(2 * math.pi * rate * t + i * 2.1)
        d = (base_ms + depth_ms * lfo) / 1000 * sr
        # 逐样本变延迟（线性插值）
        idx_f = np.clip(d, 1, len(x) - 1).astype(np.float64)
        i0 = np.floor(idx_f).astype(np.int64)
        frac = idx_f - i0
        i0c = np.clip(i0, 0, len(x) - 2)
        delayed = x[i0c] * (1 - frac) + x[i0c + 1] * frac
        y += delayed * 0.8 / (voices + 1)
    return _norm(y.astype(np.float32))


def fx_limiter(x: np.ndarray, sr: int, p: dict) -> np.ndarray:
    """限幅器：tanh 软削波，控制链尾整体电平。"""
    ceiling = _f(p, "ceiling", 0.95, 0.5, 1.0)
    gain = _f(p, "gain_db", 0.0, -12.0, 12.0)
    y = x.astype(np.float64) * (10 ** (gain / 20))
    y = np.clip(np.tanh(y * 1.2), -ceiling, ceiling)
    return y.astype(np.float32)


# ---------------- 效果目录（前端参数面板渲染依据） ----------------

CATALOG: dict = {
    "reverb": {
        "name": "混响", "icon": "reverb",
        "desc": "空间感（房间到大教堂）",
        "params": [
            {"key": "room", "label": "房间尺寸", "min": 0.05, "max": 0.95, "step": 0.05, "default": 0.5},
            {"key": "wet", "label": "湿声比例", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.35},
            {"key": "damp", "label": "高频阻尼", "min": 0.0, "max": 0.9, "step": 0.05, "default": 0.3},
        ],
        "fx": fx_reverb,
    },
    "echo": {
        "name": "回声", "icon": "echo",
        "desc": "山谷/体育场的重复回声",
        "params": [
            {"key": "delay_s", "label": "延迟(秒)", "min": 0.02, "max": 2.0, "step": 0.02, "default": 0.25},
            {"key": "feedback", "label": "反馈", "min": 0.0, "max": 0.9, "step": 0.05, "default": 0.35},
            {"key": "wet", "label": "湿声比例", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.4},
        ],
        "fx": fx_echo,
    },
    "eq": {
        "name": "均衡器", "icon": "eq",
        "desc": "三段 EQ：低/中/高频增减",
        "params": [
            {"key": "low_db", "label": "低频(dB)", "min": -18, "max": 18, "step": 1, "default": 0},
            {"key": "mid_db", "label": "中频(dB)", "min": -18, "max": 18, "step": 1, "default": 0},
            {"key": "mid_hz", "label": "中频点(Hz)", "min": 200, "max": 6000, "step": 100, "default": 1200},
            {"key": "high_db", "label": "高频(dB)", "min": -18, "max": 18, "step": 1, "default": 0},
        ],
        "fx": fx_eq,
    },
    "pitch": {
        "name": "变调", "icon": "pitch",
        "desc": "升降温润度/卡通感（半音）",
        "params": [
            {"key": "semitones", "label": "半音", "min": -12, "max": 12, "step": 0.5, "default": 0},
        ],
        "fx": fx_pitch,
    },
    "speed": {
        "name": "变速", "icon": "speed",
        "desc": "语速快慢（不变调）",
        "params": [
            {"key": "rate", "label": "速度倍率", "min": 0.5, "max": 2.0, "step": 0.05, "default": 1.0},
        ],
        "fx": fx_speed,
    },
    "telephone": {
        "name": "电话音", "icon": "telephone",
        "desc": "老座机/对讲机质感",
        "params": [
            {"key": "drive", "label": "失真度", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.4},
        ],
        "fx": fx_telephone,
    },
    "robot": {
        "name": "机器人", "icon": "robot",
        "desc": "环形调制机械音",
        "params": [
            {"key": "freq", "label": "载波(Hz)", "min": 20, "max": 400, "step": 5, "default": 55},
            {"key": "mix", "label": "混合比", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.85},
        ],
        "fx": fx_robot,
    },
    "tremolo": {
        "name": "颤音", "icon": "tremolo",
        "desc": "振幅波动（警笛/老电台）",
        "params": [
            {"key": "rate", "label": "频率(Hz)", "min": 0.5, "max": 20.0, "step": 0.5, "default": 5.0},
            {"key": "depth", "label": "深度", "min": 0.0, "max": 1.0, "step": 0.05, "default": 0.6},
        ],
        "fx": fx_tremolo,
    },
    "chorus": {
        "name": "合唱", "icon": "chorus",
        "desc": "单声源变厚（群感）",
        "params": [
            {"key": "depth_ms", "label": "深度(ms)", "min": 1, "max": 20, "step": 1, "default": 6},
            {"key": "rate", "label": "摆速(Hz)", "min": 0.1, "max": 5.0, "step": 0.1, "default": 0.8},
            {"key": "voices", "label": "声部数", "min": 1, "max": 6, "step": 1, "default": 3},
        ],
        "fx": fx_chorus,
    },
    "limiter": {
        "name": "限幅器", "icon": "limiter",
        "desc": "电平控制（放链尾防爆音）",
        "params": [
            {"key": "ceiling", "label": "上限", "min": 0.5, "max": 1.0, "step": 0.01, "default": 0.95},
            {"key": "gain_db", "label": "增益(dB)", "min": -12, "max": 12, "step": 1, "default": 0},
        ],
        "fx": fx_limiter,
    },
}

# 无参目录（给前端的纯净版，不含函数）
def catalog_meta() -> list[dict]:
    out = []
    for k, v in CATALOG.items():
        out.append({"type": k, "name": v["name"], "icon": v["icon"],
                    "desc": v["desc"], "params": v["params"]})
    return out


def apply_chain(x: np.ndarray, sr: int, chain: list[dict]) -> tuple[np.ndarray, list[str]]:
    """依序应用效果链。单个效果异常/未知类型 → 跳过并记录，绝不中断整链。"""
    x = _mono(x)
    skipped: list[str] = []
    for step in chain or []:
        if not isinstance(step, dict):
            continue
        t = str(step.get("type", ""))
        meta = CATALOG.get(t)
        if meta is None:
            skipped.append(f"未知效果 {t}")
            continue
        params = step.get("params") or {}
        try:
            x = meta["fx"](x, sr, params)
        except Exception as e:
            skipped.append(f"{meta['name']} 失败: {type(e).__name__}")
    return _norm(x), skipped


# ---------------- FastAPI router ----------------

from fastapi import APIRouter, File, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import config as cfg  # noqa: E402
import soundfile as sf  # noqa: E402

router = APIRouter(prefix="/api/effects")


@router.get("/catalog")
def effects_catalog():
    """效果目录：前端参数面板的渲染依据。"""
    return {"ok": True, "effects": catalog_meta()}


@router.post("/apply")
async def effects_apply(file: UploadFile = File(...), chain: str = "[]"):
    """上传音频 + 效果链 JSON 字符串 → 处理后的 wav 文件。

    chain 形如 [{"type":"reverb","params":{"room":0.7,"wet":0.4}}, ...]，
    空链直接返回原文件（前端预览直通用）。任一效果失败只跳过该环节。
    """
    import json as _json
    try:
        steps = _json.loads(chain or "[]")
        if not isinstance(steps, list):
            raise ValueError("chain 必须是数组")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"chain 解析失败: {e}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    try:
        import io
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"音频读取失败: {e}")

    out, skipped = await _run_chain_threadpool(data, sr, steps)

    cfg.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(file.filename or "audio").stem
    out_path = cfg.OUTPUTS_DIR / f"fx_{stem}.wav"
    sf.write(str(out_path), out, sr, subtype="PCM_16")

    headers = {"X-Fx-Skipped": "; ".join(skipped) or "0"}
    return FileResponse(str(out_path), media_type="audio/wav", headers=headers)


async def _run_chain_threadpool(data, sr, steps):
    """DSP 在线程池跑，避免长音频阻塞事件循环（对齐 worker 的坑 5 教训）。"""
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(apply_chain, data, sr, steps)

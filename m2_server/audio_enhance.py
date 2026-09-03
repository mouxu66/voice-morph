# -*- coding: utf-8 -*-
"""音频增强后处理：DeepFilterNet3 模型级降噪/增强（升级替代 afftdn）。

覆盖三处入口（ROADMAP P2-5）：
  1. 离线变声输入   offline_vc._ovc_worker：denoise=True 时模型增强，不可用回退 afftdn
  2. TTS 参考音频   server.create_voicebank：enhance=1 时对切片增强后再合并
  3. 麦克风录入     cascade_stream：实时流式增强（增强后再 VAD/ASR）

性能与延迟：48k 帧长 512 → 算法延迟约 10.7ms；CPU 推理 RTF ≈ 0.07x，可实时。
许可与离线：DeepFilterNet 模型 Apache-2.0（代码 MIT/Apache 双许可）；ONNX 资产
首次自动下载到本地缓存（%LOCALAPPDATA%\\deepfilter-stream\\...\\dfn3-512-v1），
之后完全离线可用；也支持 DEEPFILTER_STREAM_MODEL_DIR 显式指向模型目录。

统一封装：
  - enhance_file(src, dst)：整段文件增强（写 wav，采样率跟随输入），供离线/TTS 入口
  - StreamEnhancer：实时流式增强器（单流单线程），供麦克风入口
  - available()：探测模型可用性（不可用返回 False，调用方回退/关闭增强）
"""
import threading
from pathlib import Path

import numpy as np

_model = None
_model_lock = threading.Lock()

# 降噪强度档位（P2-5 验收发现：默认不限压制会把弱人声/环境声当噪声压掉，
# 导致语音断裂——t1-2s 的 f0 直接归零。atten_lim_db = 最大压制 dB 上限，
# 实现：out = lim*dry + (1-lim)*enhanced，lim = 10^(-atten_lim/20)。
# 越小越保守（弱人声几乎不伤、噪声残留多）；None = 不限（压得最狠）。
ATTEN_LIM_PRESETS: dict[str, float | None] = {
    "light": 6.0,      # 轻 · 保弱声：最多压 6dB，弱人声/远场声安全
    "standard": 12.0,  # 标准：最多压 12dB（默认档）
    "strong": None,    # 强力：不限，模型默认行为（原"一刀切"效果）
}


def resolve_atten_lim(level: str | None) -> float | None:
    """把档位名解析成 atten_lim_db；未知/空值回退 standard。"""
    if level not in ATTEN_LIM_PRESETS:
        level = "standard"
    return ATTEN_LIM_PRESETS[level]


def _get_model():
    """全局共享 DeepFilterModel（onnx session 线程安全，可派生多流）。"""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from deepfilter_stream import DeepFilterModel
                _model = DeepFilterModel()
    return _model


def available() -> bool:
    """探测 DeepFilterNet 是否可用（包已装且模型资产就绪）。"""
    try:
        _get_model()
        return True
    except Exception:
        return False


def enhance_file(src, dst, atten_lim_db: float | None = None) -> Path:
    """整段文件增强。src/dst 均为 wav 路径，返回 dst。

    采样率跟随输入（内部自动重采样到 48k 推理再回采），输出长度可能比输入
    短约一个模型帧（10.7ms trim），对后续推理/合成无影响。
    """
    import soundfile as sf

    src = Path(src)
    dst = Path(dst)
    data, sr = sf.read(str(src), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = np.ascontiguousarray(data, dtype=np.float32)

    den = _get_model().new_stream(atten_lim_db=atten_lim_db)
    chunk = den.frame_size * 8  # 每块喂若干帧，摊薄 Python 开销
    outs = []
    for i in range(0, len(data), chunk):
        y = den.process(data[i:i + chunk], sr)
        if y.size:
            outs.append(y)
    tail = den.flush()
    if tail.size:
        outs.append(tail)
    y = np.concatenate(outs) if outs else np.zeros(0, dtype=np.float32)

    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), y, sr)
    return dst


class StreamEnhancer:
    """实时流式增强器：持有单个 Denoiser（单流单线程，勿跨线程使用）。

    process() 输出为变长（内部按 48k 帧缓冲，首块约 10.7ms 后才有输出），
    需要配合 FrameRealigner 重新对齐成定长帧再喂下游 VAD/ASR。
    """

    def __init__(self, atten_lim_db: float | None = None):
        self._den = _get_model().new_stream(atten_lim_db=atten_lim_db)

    def process(self, pcm: np.ndarray, sr: int) -> np.ndarray:
        return self._den.process(np.ascontiguousarray(pcm, dtype=np.float32), sr)

    def reset(self) -> None:
        self._den.reset()


class FrameRealigner:
    """把增强器的变长输出重新对齐成定长 frame_n 帧流（保留不足一帧的尾）。"""

    def __init__(self, enh: StreamEnhancer, sr: int, frame_n: int):
        self.enh = enh
        self.sr = sr
        self.frame_n = frame_n
        self.buf = np.zeros(0, dtype=np.float32)

    def feed(self, pcm: np.ndarray) -> list[np.ndarray]:
        y = self.enh.process(pcm, self.sr)
        if y.size == 0:
            return []
        self.buf = np.concatenate([self.buf, y])
        n = len(self.buf) // self.frame_n
        if n == 0:
            return []
        frames = np.array_split(self.buf[: n * self.frame_n], n)
        self.buf = self.buf[n * self.frame_n:]
        return frames

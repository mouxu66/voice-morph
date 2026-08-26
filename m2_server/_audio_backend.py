"""音频后端兼容层：统一的 torchaudio/torchcodec monkey-patch。

背景：
    torchaudio 2.9 的 load 强制依赖 torchcodec，但 torchcodec 在本环境
    （torch 2.9.1 + cu128）因 C++ ABI 不兼容无法加载；同时 transformers
    在 import 时会查询 torchcodec 的包元数据版本号，未安装会导致整体
    import 失败。

方案：
    1. patch_torchcodec_metadata()  伪造 torchcodec 版本号，仅绕过 metadata 查询
       （实际 torchcodec 不会被调用，音频加载已由 soundfile 接管）。
    2. patch_torchaudio_load()      用 soundfile 后端替换 torchaudio.load，
       功能等价，且同时兼容 buffer / channels_first / normalize 等参数。

本模块被 m2_server 下多个入口复用（demo_convert / gptsovits_tts），
避免多处重复、签名不一致的 monkey-patch。
"""
from __future__ import annotations

import importlib.metadata


def patch_torchcodec_metadata() -> None:
    """伪造 torchcodec 的包版本号，绕过 transformers 的 metadata 查询。"""
    if getattr(importlib.metadata.version, "_patched_torchcodec", False):
        return
    _orig = importlib.metadata.version

    def _version(name, *args, **kwargs):
        if name == "torchcodec":
            return "0.0.0"
        return _orig(name, *args, **kwargs)

    _version._patched_torchcodec = True
    importlib.metadata.version = _version


def patch_torchaudio_load() -> None:
    """用 soundfile 替换 torchaudio.load，绕过 torchcodec。

    幂等：重复调用不会叠加 patch。
    """
    import io

    import soundfile as sf
    import torch
    import torchaudio as ta

    if getattr(ta.load, "_patched_by_soundfile", False):
        return

    def _load(path, frame_offset=0, num_frames=-1, normalize=True,
              channels_first=True, format=None, buffer=None, num_channels=None,
              **_kw):
        # num_frames<=0 表示读全部；soundfile 约定用 -1（不能传 None）
        frames = int(num_frames) if num_frames is not None and num_frames > 0 else -1
        if buffer is not None:
            data, sr = sf.read(io.BytesIO(buffer), start=int(frame_offset),
                               frames=frames, dtype="float32", always_2d=True)
        else:
            data, sr = sf.read(str(path), start=int(frame_offset),
                               frames=frames, dtype="float32", always_2d=True)
        wav = torch.from_numpy(data.T.copy())
        if not channels_first:
            wav = wav.T
        if not normalize:
            wav = (wav * 32768.0).round().clamp(-32768, 32767).to(torch.int16)
        return wav, sr

    _load._patched_by_soundfile = True
    ta.load = _load

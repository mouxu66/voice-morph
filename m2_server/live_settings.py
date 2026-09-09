# -*- coding: utf-8 -*-
"""实时变声本地设置：输入设备选择 + 输入降噪开关。

为什么要独立设置文件
--------------------
输入设备原来写死在环境变量（VM_LIVE_INPUT_DEVICE）/默认值里，用户没法在界面上
换麦克风（手机当麦克风、USB 麦克风、调音台都接不进实时链路）。降噪开关
（RVC 自带 I_noise_reduce）原来只在 RVC GUI 里有，无头模式下永远 False。

设置落在 outputs/live_settings.json，三方共用：
  - rvc_live._resolve_device_names：输入设备候选的第一优先级
  - rvc_live._apply_model_config：每次启动变声把 denoise 写进 RVC config.json
  - cascade_stream：采集输入设备关键词（级联链路同样要能换麦）

约定：
  - input_device 为空字符串 = 跟随系统默认录音设备（插耳机自动切耳机麦）
  - 非空 = 设备名关键词（大小写不敏感的包含匹配，见 rvc_live._device_matches）
  - 文件损坏/缺键一律回退默认值，绝不阻塞变声启动
"""
import json
import os
import tempfile
import threading
from pathlib import Path

# 项目根下的 outputs 目录（与 ASR 状态、cascade 状态同处）。
# 独立推导而非 import config：cascade_stream 在 RVC venv 里跑，
# 路径必须自包含，避免 import 链条的 cwd 差异。
_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(
    os.environ.get("VM_LIVE_SETTINGS", _ROOT / "outputs" / "live_settings.json")
)

DEFAULTS = {"input_device": "", "denoise": True}

_lock = threading.Lock()


def _read_raw() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 损坏的设置文件等价于没有设置：回退默认值，下次保存会覆盖
        return {}


def get() -> dict:
    """读当前设置（缺键补默认值；返回副本，调用方修改不影响存储）。"""
    raw = _read_raw()
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    # denoise 必须是 bool；input_device 必须是 str
    out["denoise"] = bool(out["denoise"])
    out["input_device"] = str(out["input_device"] or "").strip()
    return out


def update(input_device: str | None = None, denoise: bool | None = None) -> dict:
    """合并写设置；未传的字段保持原值。返回写入后的完整设置。"""
    with _lock:
        data = get()
        if input_device is not None:
            data["input_device"] = str(input_device).strip()
        if denoise is not None:
            data["denoise"] = bool(denoise)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再替换，避免并发读到大半个 JSON
        fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, SETTINGS_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return data

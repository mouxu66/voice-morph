# -*- coding: utf-8 -*-
"""翻唱链路的**神经带宽扩展**（UniverSR）：把 RVC 干声缺掉的 8–12k 及以上"生成"出来。

为什么需要（2026-10-19 实测，见 `docs/product/research/m1q_*.md`）
    现役后期链只有 `treble=g=…:f=9000` —— 它只能放大**已有**的高频。而 RVC 输出的
    8–12k 相对 0–4k 是 **−55.3dB**（源是 −24.4dB）：放大造不出不存在的东西。
    与"抄"（源高频移植 / 谱折叠）相比，UniverSR 是**生成**：复数谱流匹配，
    同一张对照表（论文，16k 输入≈8k 截止）音乐 LSD-HF 0.93 vs AudioSR/FlashSR 1.48。

口径与边界
    · `input_sr=16000` 表示**有效带宽** 8kHz（Nyquist=input_sr/2），这是 RVC 干声的实况；
      **不是**文件的采样率（链路是 40k）。改这个值要连同实测一起改（`VM_COVER_BWE_SR`）。
    · 输出 48k → 落回输入采样率（>20k 按预期丢掉）。`seed` 固定：不固定就是每次重采样样，
      同一段素材两次结果不同，A/B 与"翻唱可复现"都失去意义。
    · **默认关**：这是实验能力，要显式打开（`bwe=True` 或 `VM_COVER_BWE=1`）。
      它换来的听感收益还没过用户耳朵裁决（m1q 的 listen.html 就是那一步）。
    · 走**子进程**跑 `tools/universr_enhance.py`：模型栈（timm/torchdiffeq/57M 权重）
      不进后端进程，显存也不会被后端长期占住。

环境（一次性）：主 `.venv` 里 `pip install --no-deps timm==1.0.25 torchdiffeq==0.2.5`
并在 `universr_repo/` 里 `pip install -e . --no-deps`；权重放
`universr_repo/ckpts/universr-audio/{config.yaml,pytorch_model.bin}`。
**不许装 requirements.txt 里钉的 torch 2.7.1**（会顶掉生产 torch 2.9.1+cu128，5060 只认 cu128）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config as cfg

#: 跑手与项目根（用 `__file__` 定位，不早绑定 OUTPUTS_DIR 之类的可变配置）
ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tools" / "universr_enhance.py"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
WEIGHTS = ROOT / "universr_repo" / "ckpts" / "universr-audio"
MODEL_ID = "woongzip1/universr-audio"

#: 有效带宽（Hz）：16000 → Nyquist 8k = RVC 干声的实况（8–12k 实测 −55dB）
INPUT_SR = 16000
#: 推理随机源：固定，保证同一段素材可复现（见模块头）
SEED = 1234
#: 超时：25s 音频实测推理几秒；留足冷启动（模型加载）+ 慢机器
TIMEOUT_S = 900.0


def enabled(requested: bool = False) -> bool:
    """是否真的要做这一步。

    `VM_COVER_BWE=0/1` 可强制关/开（A/B 两版都听时用），否则以请求参数为准（默认关）。
    """
    env = os.environ.get("VM_COVER_BWE", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    return bool(requested)


def _importable(mod: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def available() -> dict:
    """能力探测：返回 `{available, missing: [...]}`（**只读**，不下载、不动 GPU）。"""
    missing: list[str] = []
    if not VENV_PY.exists():
        missing.append(f"缺少 venv 解释器（{VENV_PY}）")
    if not RUNNER.exists():
        missing.append(f"缺少跑手脚本（{RUNNER}）")
    for f in ("config.yaml", "pytorch_model.bin"):
        if not (WEIGHTS / f).exists():
            missing.append(f"缺少权重 {f}（{WEIGHTS}）")
    for mod in ("torch", "torchaudio", "timm", "torchdiffeq", "universr"):
        if not _importable(mod):
            missing.append(f"缺依赖 {mod}（pip install --no-deps …，别动 torch 版本）")
    if missing and (WEIGHTS / "pytorch_model.bin").exists():
        # 权重在、依赖缺 = 安装没做完；这一条最容易漏（跑了 --no-deps 却没装依赖）
        pass
    return {"available": not missing, "missing": missing, "weights": str(WEIGHTS),
            "model": MODEL_ID, "input_sr": INPUT_SR}


def _to_sr(src: Path, dst: Path, sr: int) -> Path:
    """48k → 链路采样率（ffmpeg；用项目自己的 find_ffmpeg，别依赖 PATH）。"""
    from common import find_ffmpeg

    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"重采样失败：{(r.stderr or '')[-200:]}")
    return dst


def _sr_of(path: Path) -> int:
    import soundfile as sf

    return int(sf.info(str(path)).samplerate)


def enhance_in_place(wav: Path, *, input_sr: int | None = None, seed: int = SEED) -> dict:
    """把 `wav` 就地替换成"补过高频"的版本，返回跑手的 info（失败抛异常，调用方决定降级）。

    ★ 就地替换前先写 `<name>.pre_bwe.wav` 备份：这一步是**实验能力**，
      出问题时用户手里还得有干净的那一份（同目录，随会话/产物一起被清理）。
    """
    st = available()
    if not st["available"]:
        raise RuntimeError("UniverSR 不可用：" + "、".join(st["missing"]))
    wav = Path(wav)
    sr = _sr_of(wav)
    up48 = wav.with_name(wav.stem + ".bwe48.wav")
    cmd = [str(VENV_PY), str(RUNNER), "--input", str(wav), "--output", str(up48),
           "--input-sr", str(int(input_sr or INPUT_SR)), "--seed", str(int(seed))]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=TIMEOUT_S, cwd=str(ROOT))
    if r.returncode != 0:
        up48.unlink(missing_ok=True)
        raise RuntimeError(f"UniverSR 失败：{(r.stderr or r.stdout or '')[-300:]}")
    line = [ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")]
    info = json.loads(line[-1]) if line else {}
    backup = wav.with_suffix(".pre_bwe.wav")
    shutil.copy2(wav, backup)
    _to_sr(up48, wav, sr)
    up48.unlink(missing_ok=True)
    return {**info, "sr": sr, "backup": str(backup)}


def main() -> int:
    """手工跑一次（排障用）：`python m2_server/cover_bwe.py status|run <wav>`。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    argv = sys.argv[1:]
    if not argv or argv[0] == "status":
        print(json.dumps(available(), ensure_ascii=False, indent=1))
        return 0
    if argv[0] == "run" and len(argv) > 1:
        p = Path(argv[1])
        if not p.is_absolute():
            p = cfg.OUTPUTS_DIR / p.name
        print(json.dumps(enhance_in_place(p), ensure_ascii=False, indent=1))
        return 0
    print("用法：cover_bwe.py status | cover_bwe.py run <wav>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

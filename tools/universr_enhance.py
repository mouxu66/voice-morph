# -*- coding: utf-8 -*-
"""UniverSR 单文件超分跑手（用主 `.venv` 的 python 跑，但**永远以子进程方式**被调用）。

为什么不去建一个独立 venv（原计划如此，2026-10-19 实测改主意）
    UniverSR 的 `requirements.txt` 钉 torch 2.7.1 —— 装进主 `.venv` 会顶掉生产
    torch 2.9.1+cu128（5060 = sm_120 只认 cu128）。但独立 venv 要重下 1.3GB 的
    cu128 轮子，而 **download.pytorch.org 在这条线路上只有 ~217KB/s ≈ 100 分钟**
    （实测；清华 TUNA 的 pytorch-wheels 镜像 403，hf 镜像同样 ~0.4MB/s）。
    所以改成：主 venv 已有同版本 torch → 只补 3 个**纯 Python** 依赖
    （`timm` / `torchdiffeq` / `-e universr_repo`，全部 `--no-deps`，绝不碰 torch），
    模型代码仍走**子进程**（后端不把 timm 的 import 代价与显存带进来）。
    这条边界由 `m2_server/tests/test_cover_bwe_universr.py` 钉住：
    实验脚本不得在模块级 import universr/torch。

本脚本是那条边界上唯一的接口：进 = 一个 wav 路径 + 有效带宽；出 = 一个 48k wav + 一行 JSON。

`--input-sr` 是**有效带宽**不是文件的采样率（README 的口径）：
    Nyquist = input_sr/2 → `16000` 表示"内容只到 8kHz"。我们的 RVC 干声正是这种
    （8–12k 实测 −55dB，等于空的），所以默认 16000；源高频移植那条臂才需要 24000。

用法（由 `experiments/cover_bwe_universr.py` 调用，也可手跑）：
    D:/变声/.venv-universr/Scripts/python.exe tools/universr_enhance.py \
        --input media/covers/song2/hf8k/rvc_dry.wav --output /tmp/bwe_d.wav --input-sr 16000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HF_ID = "woongzip1/universr-audio"
#: 本地权重（`universr_repo/ckpts/universr-audio`）优先 —— 与项目其它本地模型一样，
#: 下过一次就不再联网（HF 在这条线路上只有 ~0.4MB/s，每次现拉不现实）。
def _default_model() -> str:
    local = Path(__file__).resolve().parent.parent / "universr_repo" / "ckpts" / "universr-audio"
    return str(local) if (local / "pytorch_model.bin").exists() else HF_ID


DEFAULT_MODEL = _default_model()


def _f0(x, sr: int) -> float:
    """前 3 秒的自相关基频（静音返回 0）。只用来抓"整段被拉慢"这类离谱错误。"""
    import numpy as np

    seg = np.asarray(x, dtype=np.float64)[: sr * 3]
    if seg.size < sr // 2 or float(np.sqrt((seg ** 2).mean())) < 1e-3:
        return 0.0
    seg = seg - seg.mean()
    ac = np.correlate(seg, seg, "full")[seg.size - 1:]
    ac = ac / max(ac[0], 1e-12)
    lo, hi = int(sr / 1000), min(int(sr / 60), ac.size - 1)
    if hi <= lo:
        return 0.0
    return float(sr / (lo + int(np.argmax(ac[lo:hi]))))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="UniverSR 单文件超分（独立 venv）")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--input-sr", type=int, default=16000, choices=[8000, 12000, 16000, 24000],
                    help="有效带宽（Nyquist = 该值/2）；不是文件采样率")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"本地目录或 HF repo id（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--ode-method", default="midpoint", choices=["euler", "midpoint", "rk4"])
    ap.add_argument("--ode-steps", type=int, default=4)
    ap.add_argument("--guidance", type=float, default=3.5,
                    help="★ 默认值很关键：README 的 1.5 在我们的干声上**几乎不产高频**"
                         "（4s 实测 8–11k 只从 −60 到 −59），3.0 → −35.5，4.0 → −26.3 但峰值 2.0。"
                         "取 3.5 并在超过 0.99 时整体缩波，避免削波。")
    ap.add_argument("--seed", type=int, default=1234,
                    help="★ 默认播种：seed=None 时每次采样新噪声，A/B 就不是单变量了")
    ap.add_argument("--lowpass", action="store_true",
                    help="先按 input_sr 做一次低通（给\"源高频移植后\"这种带宽更高的输入用）")
    #: ★ 必须分块：模型内部是**全长度**复数谱 + UNet，25s 一次前向实测要 5.7GB
    #: （8GB 的 5060 直接 OOM，见 docs/product/research/m1q_*.md 的排障记录）。
    ap.add_argument("--chunk-s", type=float, default=5.0, help="分块长度（秒）")
    ap.add_argument("--overlap-s", type=float, default=0.3, help="块间交叠（秒），线性交叉淡入淡出")
    a = ap.parse_args()

    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio

    import universr

    src, sr = sf.read(str(a.input), dtype="float32", always_2d=True)
    mono = src.mean(axis=1)
    if a.lowpass:
        from scipy.signal import butter, sosfiltfilt

        ny = sr / 2.0
        sos = butter(8, min(a.input_sr / 2.0, ny * 0.99) / ny, btype="low", output="sos")
        mono = sosfiltfilt(sos, mono.astype(np.float64)).astype("float32")

    t0 = time.time()
    model = universr.UniverSR.from_pretrained(a.model, device=a.device)
    t_load = time.time() - t0

    # ① ★ 必须先降到 `input_sr`：模型对**张量**入口的语义是 `file_sr == input_sr`
    #    （见 universr/model.py 的 `_load_audio`：张量直接返回 input_sr，随后
    #    `resample(input_sr → 48000)`）。2026-10-19 曾把 48k 张量配 input_sr=16000
    #    喂进去 → 模型当成 16k 信号**再升采 3 倍** → 整段被拉慢 3 倍（质心 190Hz、
    #    4–8k 掉到 −62dB，指标全绿得像"没补"，其实输入本身就是错的）。
    #    正确做法：先真正降到 16k（内容只到 8k，无损），再交给模型自己升到 48k。
    in_sr = int(a.input_sr)
    wav = torch.from_numpy(mono.astype(np.float32)).unsqueeze(0)
    if sr != in_sr:
        wav = torchaudio.functional.resample(wav, orig_freq=sr, new_freq=in_sr)
    ratio = 48000.0 / in_sr

    # ② 分块 + 交叠交叉淡入淡出（在**输入域**切块，输出域按 ratio 对齐）
    total_in = wav.shape[-1]
    total = int(round(total_in * ratio))
    chunk = max(int(a.chunk_s * in_sr), 4096)
    hop = max(chunk - int(a.overlap_s * in_sr), 1)
    t1 = time.time()
    acc = torch.zeros(1, total)
    wsum = torch.zeros(1, total)
    tried = 0
    pos = 0
    while pos < total_in:
        seg = wav[..., pos:pos + chunk]
        with torch.inference_mode():
            try:
                out = model.enhance(seg, input_sr=in_sr, ode_method=a.ode_method,
                                    ode_steps=a.ode_steps, guidance_scale=a.guidance,
                                    seed=a.seed + tried)
            except torch.OutOfMemoryError as e:            # 显存不够就当场缩块重试
                raise SystemExit(
                    f"分块 {a.chunk_s}s 仍 OOM：请自降 --chunk-s（原始报错：{e}）")
        got = out.squeeze(0).detach().cpu()
        p_out = int(round(pos * ratio))
        n = min(got.shape[-1], total - p_out)
        if n <= 0:
            break
        got = got[..., :n]
        ramp = torch.linspace(0, 1, n) if n > 1 else torch.ones(1)
        acc[..., p_out:p_out + n] += got * ramp
        wsum[..., p_out:p_out + n] += ramp
        tried += 1
        if pos + chunk >= total_in:
            break
        pos += hop
    t_run = time.time() - t1
    out = (acc / wsum.clamp_min(1e-6)).clamp(-1.0, 1.0)
    peak = float(out.abs().max())
    if peak > 0.99:                                       # guidance 高时能量会整体上去
        out = out * (0.99 / peak)

    # ③ 自检：时长/音高对不对（挡住上面那类"拉慢 3 倍"的假绿）
    f0_in = _f0(mono, sr)
    f0_out = _f0(out.squeeze(0).numpy(), 48000)
    if f0_in > 0 and f0_out > 0:
        octave = float(np.log2(f0_out / f0_in))
        if abs(octave) > 0.5:
            raise SystemExit(
                f"输出音高偏移 {octave:+.2f} 八度（输入 {f0_in:.0f}Hz → 输出 {f0_out:.0f}Hz）"
                f"——极可能是采样率/带宽口径不一致，别拿这条结果做 A/B")
    else:
        octave = 0.0
    secs_out = float(out.shape[-1] / 48000)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(a.output), out.squeeze(0).numpy(), 48000, subtype="PCM_16")
    print(json.dumps({"ok": True, "input": str(a.input), "output": str(a.output),
                      "input_sr_effective": in_sr, "file_sr": int(sr),
                      "seconds_in": round(len(mono) / sr, 2),
                      "seconds_out": round(secs_out, 2), "out_sr": 48000,
                      "f0_in": round(f0_in, 1), "f0_out": round(f0_out, 1),
                      "octave_shift": round(octave, 3),
                      "peak": round(peak, 4), "load_s": round(t_load, 2), "run_s": round(t_run, 2),
                      "chunks": tried, "chunk_s": a.chunk_s, "overlap_s": a.overlap_s,
                      "ode": f"{a.ode_method}/{a.ode_steps}", "seed": a.seed,
                      "guidance": a.guidance}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

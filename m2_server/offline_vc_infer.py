# -*- coding: utf-8 -*-
"""离线变声推理 CLI（由 m2_server/offline_vc.py 用 D:\\RVC\\.venv 的 python 子进程调用）。

走 RVC 文件级推理链路 infer/vc（Pipeline 自动按静音切块拼接，支持任意长度音频）；
不用 rtrvc.RVC——那是实时块式设计，cache_pitch 固定 1024 帧，整段超 10s 会溢出报错。
用法：
    python offline_vc_infer.py --pth <模型.pth> --index <added_*.index> \
        --input <16k以下任意wav> --output <48k wav> [--pitch 0] [--index-rate 0.5]
"""
import argparse
import os
import sys


def main():
    # 从 VM_RVC_ROOT 读取（与 config.py 一致），未设置回退 D:\RVC，保证换机可移植。
    # （放在 main 内：模块级 chdir 会污染 pytest 导入环境，且便于对 postprocess 做单测）
    rvc_root = os.environ.get("VM_RVC_ROOT", r"D:\RVC")
    sys.path.insert(0, rvc_root)
    os.environ["PYTHONPATH"] = rvc_root
    os.chdir(rvc_root)
    # infer/vc/pipeline.py 加载 rmvpe 等资源依赖 webui 启动时设置的环境变量
    os.environ.setdefault("RVC_CUDA_GRAPH", "0")
    os.environ.setdefault("weight_root", "assets/weights")
    os.environ.setdefault("index_root", "logs")
    os.environ.setdefault("rmvpe_root", "assets/rmvpe")

    p = argparse.ArgumentParser()
    p.add_argument("--pth", required=True)
    p.add_argument("--index", default="")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--pitch", type=int, default=0, help="变调半音数，男转女 +12")
    p.add_argument("--index-rate", type=float, default=0.5)
    args = p.parse_args()
    # RVC configs/config.py 在 import 时会 parser.parse_args() 严格解析 sys.argv，
    # 见到我们的 --pth/--input 会直接报错退出；解析完自己的参数后清空 argv 再 import。
    sys.argv = [sys.argv[0]]

    import numpy as np
    import soundfile as sf
    import torch

    from configs.config import Config
    from infer.module.models import (
        SynthesizerTrnMs256NSFsid,
        SynthesizerTrnMs256NSFsid_nono,
        SynthesizerTrnMs768NSFsid,
        SynthesizerTrnMs768NSFsid_nono,
    )
    from infer.vc.modules import VC
    from infer.vc.pipeline import Pipeline
    from infer.vc.utils import load_hubert

    config = Config()
    config.device = torch.device("cuda")
    config.is_half = True

    # 手动填充 VC 实例（get_vc 依赖 weight_root 环境变量与 Gradio 回调，绕开它）
    cpt = torch.load(args.pth, map_location="cpu")
    tgt_sr = cpt["config"][-1]
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk
    if_f0 = cpt.get("f0", 1)
    version = cpt.get("version", "v1")

    synth_cls = {
        ("v1", 1): SynthesizerTrnMs256NSFsid,
        ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
        ("v2", 1): SynthesizerTrnMs768NSFsid,
        ("v2", 0): SynthesizerTrnMs768NSFsid_nono,
    }[(version, if_f0)]
    net_g = synth_cls(*cpt["config"], is_half=config.is_half)
    del net_g.enc_q
    net_g.load_state_dict(cpt["weight"], strict=False)
    net_g.eval().to(config.device)
    net_g = net_g.half() if config.is_half else net_g.float()

    vc = VC(config)
    vc.cpt, vc.tgt_sr, vc.if_f0, vc.version = cpt, tgt_sr, if_f0, version
    vc.net_g = net_g
    vc.pipeline = Pipeline(tgt_sr, config)
    vc.hubert_model = load_hubert(config)

    index = args.index if (args.index and os.path.exists(args.index)) else ""
    from infer.audio import load_audio

    audio_in = load_audio(args.input, 16000)
    audio_max = np.abs(audio_in).max() / 0.95
    if audio_max > 1:
        audio_in /= audio_max
    audio_out = vc.pipeline.pipeline(
        vc.hubert_model, vc.net_g, 0, audio_in, [0.0, 0.0, 0.0], args.pitch,
        "rmvpe", index, args.index_rate if index else 0.0, vc.if_f0, vc.tgt_sr,
        0, 0.25, vc.version, 0.33,
    )

    y = postprocess_audio(audio_out.astype("float32"), vc.tgt_sr)
    sf.write(args.output, y, vc.tgt_sr)
    print("OK %.2fs @%dHz" % (len(y) / vc.tgt_sr, vc.tgt_sr))


def postprocess_audio(y, out_sr):
    """RVC 输出后处理：RMS 锚定 + 去刺耳高频 + 峰值安全化。

    历史坑：结尾原来是 np.clip(y, -0.99, 0.99) 硬削波——RVC 输出瞬态 + IIR
    低通过冲后大量样本顶到限幅沿（实测 0.17% 样本被削），听感刺耳、NatScore
    直接掉到负值区。改为「只衰减」的峰值归一到 -1.5 dBFS：够响的段不动，
    超限的段整体缩，绝不再产生平顶削波。
    """
    import numpy as np
    from scipy.signal import butter, sosfilt

    y = np.asarray(y, dtype=np.float32)
    # RMS 归一到 -18 dBFS（响度锚点）
    rms = float(np.sqrt((y ** 2).mean()))
    if rms > 1e-9:
        y = y / rms * (10 ** (-18 / 20))
    # 轻微低通去刺耳高频；低采样率模型（如 22.05k）时截止不得贴 Nyquist
    cutoff = min(11000.0, out_sr * 0.45)
    sos = butter(4, cutoff, fs=out_sr, btype="lowpass", output="sos")
    y = sosfilt(sos, y)
    # 峰值安全化：只衰减到 -1.5 dBFS，不放大（保留 RMS 锚定的响度关系）
    peak = float(np.abs(y).max())
    target = 10 ** (-1.5 / 20)
    if peak > target:
        y = y / peak * target
    return y.astype(np.float32)


if __name__ == "__main__":
    main()

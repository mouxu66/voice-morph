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

RVC_ROOT = r"D:\RVC"
sys.path.insert(0, RVC_ROOT)
os.environ["PYTHONPATH"] = RVC_ROOT
os.chdir(RVC_ROOT)
# infer/vc/pipeline.py 加载 rmvpe 等资源依赖 webui 启动时设置的环境变量
os.environ.setdefault("RVC_CUDA_GRAPH", "0")
os.environ.setdefault("weight_root", "assets/weights")
os.environ.setdefault("index_root", "logs")
os.environ.setdefault("rmvpe_root", "assets/rmvpe")


def main():
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
    from scipy.signal import butter, sosfilt

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
    audio = audio_out
    out_sr = vc.tgt_sr

    y = audio.astype("float32")
    # RMS 归一到 -18 dBFS
    rms = float(np.sqrt((y ** 2).mean()))
    y = y / (rms + 1e-9) * (10 ** (-18 / 20))
    # 轻微低通 11kHz 去刺耳高频
    sos = butter(4, 11000, fs=out_sr, btype="lowpass", output="sos")
    y = sosfilt(sos, y)
    y = np.clip(y, -0.99, 0.99).astype("float32")

    sf.write(args.output, y, out_sr)
    print("OK %.2fs @%dHz" % (len(y) / out_sr, out_sr))


if __name__ == "__main__":
    main()

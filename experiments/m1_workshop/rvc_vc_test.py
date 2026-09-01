"""文件级 RVC VC 验证：把任意人声用已训好的 meituan_rat 模型转成袋鼠声。
用法：
  D:/RVC/.venv/Scripts/python.exe rvc_vc_test.py [输入wav] [输出wav]
不传参默认：输入=你的录音 my_voice_1/reference.wav，输出=outputs/rvc_vc_test/myvoice_to_rat.wav
"""
import os, sys, glob
RVC_ROOT = r"D:\RVC"
sys.path.insert(0, RVC_ROOT)
os.environ["PYTHONPATH"] = RVC_ROOT
os.chdir(RVC_ROOT)

import argparse
import numpy as np
import soundfile as sf
import torch
from scipy.signal import butter, sosfilt
from configs.config import Config
from infer.rtrvc import RVC

EXP = "meituan_rat"
PTH = os.path.join(RVC_ROOT, "logs", EXP, f"{EXP}.pth")
IDX = sorted(glob.glob(os.path.join(RVC_ROOT, "logs", EXP, "added_*.index")))[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=r"D:/变声/media/voicebank/my_voice_1/reference.wav")
    ap.add_argument("dst", nargs="?", default=r"D:/变声/outputs/rvc_vc_test/myvoice_to_rat.wav")
    ap.add_argument("--index_rate", type=float, default=0.75)
    args = ap.parse_args()

    print(">> 显存:", torch.cuda.get_device_properties(0).total_memory/1024**3, "GB | 空闲",
          torch.cuda.mem_get_free_memory if hasattr(torch.cuda,'mem_get_free_memory') else '',
          flush=True)
    config = Config()
    config.device = torch.device("cuda")
    config.is_half = False
    rvc = RVC(0, 0.0, PTH, IDX, index_rate=args.index_rate, config=config)
    print(">> 模型加载完成:", os.path.basename(PTH), flush=True)

    audio, sr = sf.read(args.src, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(1)
    if sr != 16000:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    # 切块推理：每块<=8s（f0帧~800<1024)，绕过 RVC infer 固定 1024 帧缓存限制
    BLOCK = 16000 * 8
    out_chunks = []
    for i in range(0, len(audio), BLOCK):
        seg = audio[i:i+BLOCK]
        x = torch.from_numpy(seg.astype("float32")).to("cuda")
        with torch.no_grad():
            # return_length 必须传"帧数"(采样数//160)，不能传采样数：
            # 该值会作为 dec 的 n_res，传采样数(如128000)会让 z 被拉伸到十几万帧 -> OOM。
            o = rvc.infer(x, x.shape[0], 0, x.shape[0] // 160, "rmvpe")
        y = o.cpu().numpy()[: x.shape[0] * 3].astype("float32")
        out_chunks.append(y)
    y = np.concatenate(out_chunks)
    # RMS 归一到 -18 dBFS + 轻微低通去刺耳
    rms = float(np.sqrt((y ** 2).mean()))
    y = y / (rms + 1e-9) * (10 ** (-18 / 20))
    sos = butter(4, 11000, fs=48000, btype="lowpass", output="sos")
    y = sosfilt(sos, y)
    y = np.clip(y, -0.99, 0.99).astype("float32")
    os.makedirs(os.path.dirname(args.dst), exist_ok=True)
    sf.write(args.dst, y, 48000)
    print(">> 生成 %.2fs @48k | 输出 %s" % (len(y)/48000, args.dst), flush=True)

if __name__ == "__main__":
    main()

import os, sys
RVC_ROOT = r"D:\RVC"
sys.path.insert(0, RVC_ROOT); os.environ["PYTHONPATH"] = RVC_ROOT; os.chdir(RVC_ROOT)
import torch, numpy as np
from configs.config import Config
from infer.rtrvc import RVC

config = Config(); config.device = torch.device("cpu"); config.is_half = False
rvc = RVC(0, 0.0, r"D:/RVC/logs/meituan_rat/meituan_rat.pth",
          r"D:/RVC/logs/meituan_rat/added_IVF182_Flat_nprobe_1_meituan_rat_v2.index",
          index_rate=0.75, config=config)
print("模型加载完成(CPU)。net_g 类型:", type(rvc.net_g).__name__, flush=True)
print("upsample_rates:", getattr(rvc.net_g, "upsample_rates", "N/A"), flush=True)

orig = rvc.net_g.infer
def wrapped(phone, lengths, coarse, continuous, speaker, *a, **k):
    print(">> net_g.infer phone.shape=", tuple(phone.shape),
          "coarse.shape=", tuple(coarse.shape) if torch.is_tensor(coarse) else coarse,
          "lengths=", tuple(lengths.shape) if torch.is_tensor(lengths) else lengths, flush=True)
    return orig(phone, lengths, coarse, continuous, speaker, *a, **k)
rvc.net_g.infer = wrapped

import soundfile as sf, librosa
audio, sr = sf.read(r"D:/变声/media/voicebank/my_voice_1/reference.wav", dtype="float32")
if audio.ndim > 1: audio = audio.mean(1)
if sr != 16000: audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
seg = audio[:16000*3]
x = torch.from_numpy(seg.astype("float32"))
print("input_wav len:", x.shape[0], "-> p_len 应=", x.shape[0]//160, flush=True)
try:
    with torch.no_grad():
        o = rvc.infer(x, x.shape[0], 0, x.shape[0], "rmvpe")
    print("OK out", tuple(o.shape), flush=True)
except Exception as e:
    print("FAIL:", repr(e), flush=True)

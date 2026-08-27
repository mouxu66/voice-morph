import sys
sys.path.insert(0, r"D:\变声\m2_server")
import glob
import torch
from qwen_tts import Qwen3TTSModel
import qwen3_tts_service as q
q.MODEL = Qwen3TTSModel.from_pretrained(r"D:/变声/tts_models/qwen3-tts-1.7b-base", device_map="cuda:0", dtype=torch.bfloat16)
p = sorted(glob.glob(r"D:\变声\media\clips\*.wav"))[0]
try:
    e = q._speaker_embedding(p)
    print("OK shape=", e.shape)
except Exception as ex:
    print("FAIL:", ex)

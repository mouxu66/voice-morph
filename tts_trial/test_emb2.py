import sys
sys.path.insert(0, r"D:\变声\m2_server")
import glob
import torch
import soundfile as sf
import librosa
from qwen_tts import Qwen3TTSModel

m = Qwen3TTSModel.from_pretrained(r"D:/变声/tts_models/qwen3-tts-1.7b-base", device_map="cuda:0", dtype=torch.bfloat16)
p = sorted(glob.glob(r"D:\变声\media\clips\*.wav"))[0]
wav, sr = sf.read(p, dtype="float32")
if wav.ndim > 1:
    wav = wav.mean(axis=1)
tsr = m.model.speaker_encoder_sample_rate
if sr != tsr:
    wav = librosa.resample(y=wav, orig_sr=int(sr), target_sr=tsr)
e = m.model.extract_speaker_embedding(audio=wav, sr=tsr)
lines = [f"type={type(e)}"]
if torch.is_tensor(e):
    lines.append(f"is_tensor dtype={e.dtype} shape={e.shape} device={e.device}")
    e2 = e.detach().float().cpu().numpy()
    lines.append(f"converted ok shape={e2.shape}")
else:
    lines.append(f"NOT tensor, repr={repr(e)[:200]}")
open(r"D:\变声\tts_trial\emb_type.txt", "w").write("\n".join(lines))

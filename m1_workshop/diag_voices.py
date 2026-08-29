# 诊断：切片里到底有几种声音？锚点参考音(reference_24k)和它们像不像？
import os, glob, json
import numpy as np
import torch, librosa
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF  = "D:/变声/media/voicebank/merg_004/reference_24k.wav"
CLIPS= "D:/变声/media/clips"

qwen3tts = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16,
                                          attn_implementation="sdpa")
base = qwen3tts.model
tgt_sr = base.speaker_encoder_sample_rate

def emb(path):
    a, sr = librosa.load(path, sr=None, mono=True)
    if int(sr) != int(tgt_sr):
        a = librosa.resample(y=a.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt_sr))
    e = base.extract_speaker_embedding(audio=a.astype(np.float32), sr=int(tgt_sr))
    return e.detach().cpu().float().numpy()

def cos(a, b):
    a, b = a/np.linalg.norm(a), b/np.linalg.norm(b)
    return float(np.dot(a, b))

ref_emb = emb(REF)
print(f"[ref] {os.path.basename(REF)} norm={np.linalg.norm(ref_emb):.2f}")

files = sorted(glob.glob(os.path.join(CLIPS, "*.wav")))
# 取前 40 段 + 每段视频的代表，抽样
sample = files[:40]
print(f"[clips] 抽样 {len(sample)} 段")
embs = []
names = []
for f in sample:
    try:
        e = emb(f)
        embs.append(e); names.append(os.path.basename(f))
    except Exception as ex:
        print("  skip", os.path.basename(f), ex)

embs = np.stack(embs)
sims = [cos(e, ref_emb) for e in embs]
print("\n=== 各切片与锚点参考音的余弦相似度（越高=越像同一个说话人）===")
for n, s in zip(names, sims):
    print(f"  {s:+.3f}  {n}")

# 切片两两相似度：看有几簇
S = np.array([[cos(embs[i], embs[j]) for j in range(len(embs))] for i in range(len(embs))])
# 用均值+阈值粗略分簇
flat = S[np.triu_indices(len(embs), 1)]
print(f"\n[聚类] 切片间两两相似度：mean={flat.mean():.3f} min={flat.min():.3f} max={flat.max():.3f}")
# 与锚点相似度分布
ss = np.array(sims)
print(f"[锚点] 与参考音相似度：mean={ss.mean():.3f} min={ss.min():.3f} max={ss.max():.3f}")
print("  低于0.3的(可能不同说话人)数量:", int((ss<0.3).sum()))
print("  高于0.6的(很可能同一个人)数量:", int((ss>0.6).sum()))

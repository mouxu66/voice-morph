# -*- coding: utf-8 -*-
"""零样本参考音时长扫描：同一个 base，参考音分别用 10s/30s/60s/120s/300s 前缀。

回答"不微调的话，参考音要多久才够"——与 12 分钟 LoRA 微调做对照。
目标声纹基准用 source 全长的 CAM++（避免循环：参考音本身就是 source 的前缀）。
"""
import argparse, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0, _HERE)
import numpy as np, soundfile as sf, torch
sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
from ab_evaluate import cam_emb
sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
SOURCE = os.path.join(PPT, "source_24k.wav")
REF10 = os.path.join(PPT, "ref_24k.wav")
OUT = os.path.join(PPT, "out_ab", "reflen_sweep")
os.makedirs(OUT, exist_ok=True)
SPK_ID, SPK = 3000, "spk"
TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]
DURATIONS = [10, 30, 60, 120, 300]


def log(m): print(m, flush=True)


def cos(x, y):
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def make_refs():
    """生成各时长前缀参考音；10s 用现成 ref_24k（它就是精挑的一段，可能优于朴素前缀）。"""
    a, sr = sf.read(SOURCE)
    paths = {}
    if os.path.exists(REF10):
        paths[10] = REF10
    for d in DURATIONS:
        if d == 10 and os.path.exists(REF10):
            continue
        p = os.path.join(OUT, f"ref_{d}s.wav")
        sf.write(p, a[: int(d * sr)], sr)
        paths[d] = p
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-gen", action="store_true", help="只算已有 wav 的 CAM++")
    args = ap.parse_args()

    refs = make_refs()
    # 目标基准：source 全长的声纹（分段平均）
    a, sr = sf.read(SOURCE)
    seg = 20 * sr
    vecs = []
    for s0 in range(0, min(len(a), 300 * sr), seg):
        seg_a = a[s0:s0 + seg]
        if len(seg_a) < 2 * sr:
            continue
        p = os.path.join(OUT, "_tgt.wav")
        sf.write(p, seg_a, sr)
        v = cam_emb(p)
        if v is not None:
            vecs.append(np.asarray(v, dtype=np.float32).reshape(-1))
    tgt = np.mean(vecs, axis=0)
    log(f"[target] source 前 300s 分段平均声纹 dim={tgt.shape}")

    if not args.skip_gen:
        from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
        import librosa
        for dur, rp in refs.items():
            log(f"\n### 参考音 {dur}s : {os.path.basename(rp)}")
            q = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16,
                                              attn_implementation="sdpa")
            b = q.model.to("cuda")
            b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
            b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
            b.speaker_encoder = b.speaker_encoder.to(torch.bfloat16)
            ra, rsr = librosa.load(rp, sr=None, mono=True)
            tgt_sr = b.speaker_encoder_sample_rate
            if int(rsr) != int(tgt_sr):
                ra = librosa.resample(y=ra.astype(np.float32), orig_sr=int(rsr), target_sr=int(tgt_sr))
            e = b.extract_speaker_embedding(audio=ra.astype(np.float32), sr=int(tgt_sr))
            e = e.unsqueeze(0) if e.dim() == 1 else e
            ce = b.talker.model.codec_embedding
            with torch.no_grad():
                ce.weight[SPK_ID] = e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)
            tc = b.config.talker_config
            tc.spk_id, tc.spk_is_dialect = {SPK: SPK_ID}, {SPK: False}
            b.supported_speakers = tc.spk_id.keys()
            for i, t in enumerate(TEXTS, 1):
                tk = q._tokenize_texts([q._build_assistant_text(t)])
                tk = [x.to("cuda") if torch.is_tensor(x) else torch.tensor(x, device="cuda") for x in tk]
                with torch.no_grad():
                    codes, _ = b.generate(input_ids=tk, languages=["Auto"], speakers=[SPK],
                                          non_streaming_mode=True, do_sample=True, temperature=0.8,
                                          top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
                c = codes[0]
                wavs, fs = b.speech_tokenizer.decode([{"audio_codes": c}])
                arr = np.asarray(wavs[0], dtype=np.float32)
                sf.write(os.path.join(OUT, f"d{dur}_{i}.wav"), arr, int(fs))
                log(f"  [d{dur}] {i}) peak={np.max(np.abs(arr)):.3f} dur={len(arr)/fs:.2f}s")
            del b
            import gc
            gc.collect(); torch.cuda.empty_cache()

    print(f"\n{'参考音时长':<14}{'CAM++ vs 全长基准':>18}")
    print("-" * 34)
    for dur in refs:
        sims = []
        for i in range(1, 6):
            p = os.path.join(OUT, f"d{dur}_{i}.wav")
            if not os.path.exists(p):
                continue
            v = cam_emb(p)
            if v is not None:
                sims.append(cos(np.asarray(v, dtype=np.float32).reshape(-1), tgt))
        if sims:
            print(f"{dur:<10}s{np.mean(sims):>16.4f}")


if __name__ == "__main__":
    main()

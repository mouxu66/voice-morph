# -*- coding: utf-8 -*-
"""微调效果判决：同路径、同批次、同参考音，唯一变量 = 挂不挂 LoRA（scale 0.5）。

一次加载 bf16 base，先跑 5 条 base，再挂 adapter(scale 0.5) 跑同 5 条，
最后统一用 CAM++ 算与目标参考的相似度，并给 ASR 可懂度。
"""
import argparse, gc, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0, _HERE)
import numpy as np, soundfile as sf, torch
sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
from ab_evaluate import cam_emb
sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
OUT = os.path.join(PPT, "out_ab", "ab_effect")
os.makedirs(OUT, exist_ok=True)
SPK_ID, SPK = 3000, "spk"
ADAPTER = os.path.join(PPT, "out_v2fix", "adapter-final")
TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]


def log(m): print(m, flush=True)


def cos(x, y):
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lora-scale", type=float, default=0.5)
    ap.add_argument("--skip-asr", action="store_true")
    args = ap.parse_args()

    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    from peft import PeftModel
    from qlora_quant_config import apply_lora_scale

    ref_v = cam_emb(REF)
    ref_v = np.asarray(ref_v, dtype=np.float32).reshape(-1)
    log(f"[ref] {REF} dim={ref_v.shape}")

    q = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    b.speaker_encoder = b.speaker_encoder.to(torch.bfloat16)
    import librosa
    a, sr = librosa.load(REF, sr=None, mono=True)
    tgt = b.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        a = librosa.resample(y=a.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    e = b.extract_speaker_embedding(audio=a.astype(np.float32), sr=int(tgt))
    e = e.unsqueeze(0) if e.dim() == 1 else e
    ce = b.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)
    tc = b.config.talker_config
    tc.spk_id, tc.spk_is_dialect = {SPK: SPK_ID}, {SPK: False}
    b.supported_speakers = tc.spk_id.keys()

    def gen_all(tag, model):
        out = []
        for i, t in enumerate(TEXTS, 1):
            tk = q._tokenize_texts([q._build_assistant_text(t)])
            tk = [x.to("cuda") if torch.is_tensor(x) else torch.tensor(x, device="cuda") for x in tk]
            with torch.no_grad():
                codes, _ = model.generate(input_ids=tk, languages=["Auto"], speakers=[SPK],
                                          non_streaming_mode=True, do_sample=True, temperature=0.8,
                                          top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
            c = codes[0]
            wavs, fs = b.speech_tokenizer.decode([{"audio_codes": c}])
            arr = np.asarray(wavs[0], dtype=np.float32)
            p = os.path.join(OUT, f"{tag}_{i}.wav")
            sf.write(p, arr, int(fs))
            v = np.asarray(cam_emb(p), dtype=np.float32).reshape(-1)
            out.append({"i": i, "path": p, "peak": float(np.max(np.abs(arr))),
                        "dur": round(len(arr) / fs, 2), "sim": cos(v, ref_v)})
            log(f"  [{tag}] {i}) sim={out[-1]['sim']:.4f} peak={out[-1]['peak']:.3f} dur={out[-1]['dur']}s")
        return out

    log("\n### A: base（同路径、无 LoRA）")
    base_rows = gen_all("base", b)

    log(f"\n### B: base + LoRA（scale={args.lora_scale}）")
    pm = PeftModel.from_pretrained(b, ADAPTER)
    n = apply_lora_scale(pm, args.lora_scale)
    log(f"  [lora] {n} 个模块 scaling -> {args.lora_scale}")
    pm.eval()
    lora_rows = gen_all("lora", pm)
    del pm
    gc.collect(); torch.cuda.empty_cache()

    sb = float(np.mean([r["sim"] for r in base_rows]))
    sl = float(np.mean([r["sim"] for r in lora_rows]))
    pb = float(np.mean([r["peak"] for r in base_rows]))
    pl = float(np.mean([r["peak"] for r in lora_rows]))
    log("\n" + "=" * 66)
    log(f"{'臂':<22}{'CAM++相似度':>13}{'平均peak':>10}{'帧/时长':>10}")
    log("-" * 66)
    log(f"{'base（无LoRA）':<22}{sb:>13.4f}{pb:>10.3f}")
    log(f"{f'+LoRA scale{args.lora_scale}':<22}{sl:>13.4f}{pl:>10.3f}   Δ={sl - sb:+.4f}")
    log("=" * 66)
    with open(os.path.join(OUT, "effect.json"), "w", encoding="utf-8") as f:
        import json
        json.dump({"lora_scale": args.lora_scale, "base": base_rows, "lora": lora_rows,
                   "mean_sim_base": sb, "mean_sim_lora": sl}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

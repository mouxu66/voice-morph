# -*- coding: utf-8 -*-
"""多 adapter 同批次评测：base 基线 + 若干 LoRA adapter，同 5 条文本、同参考音、同路径。

一次加载 base 生成 base 基线；之后每换一个 adapter 重新加载（PeftModel 不便热替换）。
CAM++ 在 .venv 侧另算（modelscope 只装在那）；本脚本只落盘 wav + peak/时长。
"""
import argparse, gc, json, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0, _HERE)
import numpy as np, soundfile as sf, torch
sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
OUT = os.path.join(PPT, "out_ab", "ab_effect_multi")
os.makedirs(OUT, exist_ok=True)
SPK_ID, SPK = 3000, "spk"
TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]


def log(m): print(m, flush=True)


def load_base():
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    import librosa
    q = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    b.speaker_encoder = b.speaker_encoder.to(torch.bfloat16)
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
    return q, b


def gen_all(q, b, model, tag):
    rows = []
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
        rows.append({"i": i, "path": p, "peak": round(float(np.max(np.abs(arr))), 3),
                     "dur": round(len(arr) / fs, 2)})
        log(f"  [{tag}] {i}) peak={rows[-1]['peak']} dur={rows[-1]['dur']}s")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapters", required=True,
                    help="形如 name1=path1,name2=path2；name 会作为输出文件名前缀")
    ap.add_argument("--lora-scale", type=float, default=0.5)
    args = ap.parse_args()
    from qlora_quant_config import apply_lora_scale
    from peft import PeftModel

    result = {}
    q, b = load_base()
    result["base"] = gen_all(q, b, b, "base")
    del b
    gc.collect(); torch.cuda.empty_cache()

    for spec in args.adapters.split(","):
        spec = spec.strip()
        if not spec:
            continue
        name, path = spec.split("=", 1)
        log(f"\n### {name}: {path} (scale={args.lora_scale})")
        q, b = load_base()
        pm = PeftModel.from_pretrained(b, path)
        n = apply_lora_scale(pm, args.lora_scale)
        log(f"  [lora] {n} 个模块 scaling -> {args.lora_scale}")
        pm.eval()
        result[name] = gen_all(q, b, pm, name)
        del pm, b
        gc.collect(); torch.cuda.empty_cache()

    with open(os.path.join(OUT, "multi.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"-> {os.path.join(OUT, 'multi.json')}")


if __name__ == "__main__":
    main()

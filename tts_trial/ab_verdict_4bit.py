# -*- coding: utf-8 -*-
"""★ 缺失格子的决定性判决：双修复 adapter 在【与训练一致的 4bit 底座】上推理。

背景（为什么之前三轮"修复"都没打到点上，见 qlora_quant_config.py 顶部与
docs/大创申报-2026-10/AB对照-12分钟PPT微调-2026-10-12.md）：
  - adapter out_v2fix 是在【两个 bug 都修好】的脚本下训出来的（总参 1202.9M/1.599%，
    长路径 skip 名单 + prefill 对齐）；
  - 但判决脚本 ab_verdict.py 加载底座时【没传 quantization_config】= 纯 bf16，
    于是三轮 LoRA 全是「NF4 训练 / bf16 推理」错配（8-31 满幅饱和指纹，peak=1.00）；
    在它里面改 skip 名单毫无作用（根本没量化）。
  - 本脚本第一次跑唯一没被测过的格子：v2fix adapter + NF4 + 与训练同源 skip 名单。

同 adapter、同锚点、同 generate 参数，唯一变量 = 底座量化方式：
  arm A: nf4_train_parity —— build_bnb_config()（= 训练配置）  【待判决】
  arm B: bf16_control     —— 不传 quant（复现已知崩坏，作阴性对照）
判据沿用 ab_verdict.py：peak<0.9 且 ASR 字符重合>0.5 = 可用。
"""
import argparse
import gc
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import librosa
import numpy as np
import soundfile as sf
import torch
from bitsandbytes.nn import Linear4bit
from peft import PeftModel

from qlora_quant_config import build_bnb_config

sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
ADAPTER = os.path.join(PPT, "out_v2fix", "adapter-final")
OUT = os.path.join(PPT, "out_ab", "verdict_4bit")
SPK_ID, SPK = 3000, "spk"

TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "这道题的关键是先判断函数的单调性。",
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def set_anchor(base, ref):
    base.speaker_encoder = base.speaker_encoder.to(getattr(base, "dtype", torch.bfloat16))
    a, sr = librosa.load(ref, sr=None, mono=True)
    tgt = base.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        a = librosa.resample(y=a.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    e = base.extract_speaker_embedding(audio=a.astype(np.float32), sr=int(tgt))
    e = e.unsqueeze(0) if e.dim() == 1 else e
    ce = base.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)


def probe_quant(base, tag):
    """生成前先坐实底座量化状态（训练 dry_run 口径）。"""
    ok = True
    for name in ("speaker_encoder", "talker.model.codec_embedding",
                 "talker.model.text_embedding", "talker.codec_head",
                 "talker.text_projection", "talker.code_predictor.lm_head"):
        mod = base.get_submodule(name)
        is4 = isinstance(mod, Linear4bit)
        dt = next(mod.parameters()).dtype
        bad = is4 or str(dt).lower().startswith("uint")
        log(f"  [{tag}] {name:38s} {'Linear4bit/uint8 BAD' if bad else f'{dt} OK'}")
        ok = ok and not bad
    qp = base.get_submodule("talker.model.layers.0.self_attn.q_proj")
    log(f"  [{tag}] talker q_proj 应被量化: Linear4bit={isinstance(qp, Linear4bit)}")
    total_m = sum(p.numel() for p in base.parameters()) / 1e6
    log(f"  [{tag}] base 总参数 {total_m:.1f}M（skip 正确时命门模块保 fp，数值更大）")
    return ok, total_m


def build(adapter, use_4bit, ref, lora_scale=2.0):
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    kw = dict(torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    if use_4bit:
        kw["quantization_config"] = build_bnb_config()
    q = Qwen3TTSModel.from_pretrained(BASE, **kw)
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    probe_quant(b, "4bit" if use_4bit else "bf16")
    set_anchor(b, ref)
    m = PeftModel.from_pretrained(b, adapter) if adapter else b
    # ★ LoRA 有效缩放 = lora_alpha/r=32/16=2.0 默认过冲（#343），≤1.0 才稳定。
    #   通过改各 lora 模块 scaling[active] 直接生效，无需重训/重存 adapter。
    if adapter is not None and lora_scale != 2.0:
        n = 0
        for mod in m.modules():
            if hasattr(mod, "scaling") and isinstance(getattr(mod, "scaling"), dict):
                for k in mod.scaling:
                    mod.scaling[k] = float(lora_scale)
                n += 1
        log(f"  [lora] 已把 {n} 个 lora 模块 scaling 覆盖为 {lora_scale}")
    m.eval()
    tc = b.config.talker_config
    tc.spk_id, tc.spk_is_dialect = {SPK: SPK_ID}, {SPK: False}
    b.supported_speakers = tc.spk_id.keys()
    return q, b, m


def overlap(a, b):
    import zhconv
    a, b = zhconv.convert(a or "", "zh-cn"), zhconv.convert(b or "", "zh-cn")
    A, B = set(a), set(b)
    return len(A & B) / max(len(A | B), 1) if A and B else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=ADAPTER)
    ap.add_argument("--arms", default="4bit,bf16",
                    help="逗号分隔：4bit=训练一致；bf16=阴性对照")
    ap.add_argument("--max-new-tokens", type=int, default=400)
    ap.add_argument("--ref", default=REF, help="参考音（决定锚点声纹）")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--texts", default="", help="逗号分隔，覆盖默认 3 条评测文本")
    ap.add_argument("--lora-scale", type=float, default=2.0,
                    help="LoRA 有效缩放（默认 alpha/r=2.0；#343 实测需 ≤1.0）")
    args = ap.parse_args()
    ref, out_dir = args.ref, args.out
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.exists(args.adapter):
        raise SystemExit(f"adapter 不存在：{args.adapter}")
    if not os.path.exists(ref):
        raise SystemExit(f"参考音不存在：{ref}")

    texts = [t.strip() for t in args.texts.split(",") if t.strip()] or TEXTS

    from faster_whisper import WhisperModel
    wm = WhisperModel("small", device="cuda", compute_type="float16")

    rows = []
    for arm in args.arms.split(","):
        arm = arm.strip()
        use_4bit = (arm == "4bit")
        tag = ("nf4" if use_4bit else "bf16") + f"_scale{args.lora_scale:g}"
        log(f"\n########## arm = {tag} ##########")
        q = b = m = None
        try:
            q, b, m = build(args.adapter, use_4bit, ref, lora_scale=args.lora_scale)
            for i, t in enumerate(texts, 1):
                try:
                    tk = q._tokenize_texts([q._build_assistant_text(t)])
                    tk = [x.to("cuda") if torch.is_tensor(x) else
                          torch.tensor(x, device="cuda") for x in tk]
                    codes, _ = m.generate(
                        input_ids=tk, languages=["Auto"], speakers=[SPK],
                        non_streaming_mode=True, do_sample=True, temperature=0.8,
                        top_p=0.95, repetition_penalty=1.1,
                        max_new_tokens=args.max_new_tokens)
                    c = codes[0]
                    wavs, fs = b.speech_tokenizer.decode([{"audio_codes": c}])
                    arr = np.asarray(wavs[0], dtype=np.float32)
                    p = os.path.join(out_dir, f"{tag}_{i}.wav")
                    sf.write(p, arr, int(fs))
                    segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
                    back = "".join(s.text for s in segs)
                    peak = float(np.max(np.abs(arr)))
                    clip = float(np.mean(np.abs(arr) >= 0.999))
                    zc = float(np.mean(np.diff(np.sign(arr)) != 0))
                    o = overlap(back, t)
                    verdict = "★可用" if (peak < 0.9 and o > 0.5) else (
                        "边缘" if o > 0.3 else "✗崩坏")
                    log(f"  {i}) dur={len(arr)/fs:5.1f}s peak={peak:.3f} clip={100*clip:.2f}% "
                        f"zcr={zc:.3f} 可懂度={o:.2f}  {verdict}")
                    log(f"      回译: {back}")
                    rows.append({"arm": tag, "i": i, "dur": round(len(arr)/fs, 2),
                                 "peak": round(peak, 3), "clip_pct": round(100*clip, 3),
                                 "zcr": round(zc, 4), "overlap": round(o, 3),
                                 "verdict": verdict, "back": back, "text": t})
                except Exception as e:
                    log(f"  {i}) 失败 {type(e).__name__}: {e}")
                    rows.append({"arm": tag, "i": i, "error": f"{type(e).__name__}: {e}"})
        finally:
            del m, b, q
            gc.collect()
            torch.cuda.empty_cache()

    log("\n" + "=" * 84)
    log(f"{'臂':<20}{'平均可懂度':>12}{'平均peak':>11}{'削波%':>9}  判读")
    log("-" * 84)
    for pref in ("nf4", "bf16"):
        rs = [r for r in rows if r["arm"].startswith(pref) and "overlap" in r]
        if not rs:
            continue
        tag = rs[0]["arm"]
        o = float(np.mean([r["overlap"] for r in rs]))
        pk = float(np.mean([r["peak"] for r in rs]))
        cl = float(np.mean([r["clip_pct"] for r in rs]))
        v = "★可用" if (pk < 0.9 and o > 0.5) else ("边缘" if o > 0.3 else "✗崩坏")
        log(f"{tag:<20}{o:>12.3f}{pk:>11.3f}{cl:>9.3f}  {v}")
    log("=" * 84)

    with open(os.path.join(out_dir, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    log(f"-> {os.path.join(out_dir, 'verdict.json')}")


if __name__ == "__main__":
    main()

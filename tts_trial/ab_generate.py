# -*- coding: utf-8 -*-
"""A/B 对照评测：A 臂（零样本克隆）vs B 臂（12Hz LoRA 微调）。

A 臂和 B 臂用**同一批测试文本**（从 12 分钟演讲之外另选，模拟课件旁白场景）。

关键工程约束（8 月踩过的坑）：
  - 推理**必须 bf16**，不能用 4-bit —— 4-bit 推理会崩（8 月记录）。
  - base 与 adapter **不合并**（合并会毁权重），用 peft 挂载。
  - 两臂分别独立跑进程，避免同驻超 8GB。

产出：
  out_ab/{A,B}/*.wav
  out_ab/ab_scores.json
"""
import argparse
import gc
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WORK = os.path.join(ROOT, "media", "ft_work", "ppt12")
AB = os.path.join(WORK, "out_ab")
BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF = os.path.join(WORK, "ref_24k.wav")

# 测试文本：课件旁白风格，*不在* 12 分钟训练文本里（避免"背题"）
TEST_TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]

sys.stdout.reconfigure(encoding="utf-8")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run_arm(arm, adapter=None, tag=""):
    """跑一个臂，生成所有测试文本。"""
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    out_dir = os.path.join(AB, arm)
    os.makedirs(out_dir, exist_ok=True)

    log(f"[{arm}] 加载模型 (adapter={adapter}) ...")
    if adapter:
        # 先载 base（bf16），再挂 LoRA 适配器 —— 不合并权重
        from peft import PeftModel
        model = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, device_map="cuda:0")
        inner = model.model
        inner = PeftModel.from_pretrained(inner, adapter)
        model.model = inner
        log(f"[{arm}] LoRA 已挂载: {adapter}")
    else:
        model = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, device_map="cuda:0")

    prompt = model.create_voice_clone_prompt(ref_audio=REF, ref_text=None,
                                            x_vector_only_mode=True)
    log(f"[{arm}] 参考音 prompt 就绪")

    results = []
    for i, text in enumerate(TEST_TEXTS, 1):
        t0 = time.time()
        try:
            wavs, sr = model.generate_voice_clone(
                text=text, language="Chinese", voice_clone_prompt=prompt,
                non_streaming_mode=True)
            a = np.asarray(wavs[0], dtype=np.float32)
            p = os.path.join(out_dir, f"{tag}{i:02d}.wav")
            sf.write(p, a, int(sr))
            el = time.time() - t0
            log(f"[{arm}] {i}/{len(TEST_TEXTS)} {len(a)/sr:.1f}s  {el:.1f}s  {text}")
            results.append({"arm": arm, "idx": i, "text": text, "path": p,
                            "dur": round(len(a) / sr, 2), "gen_s": round(el, 1)})
        except Exception as e:
            log(f"[{arm}] {i} 失败: {type(e).__name__}: {e}")
            results.append({"arm": arm, "idx": i, "text": text, "path": None,
                            "error": f"{type(e).__name__}: {e}"})

    del model
    gc.collect()
    torch.cuda.empty_cache()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["A", "B"])
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--out", default=os.path.join(AB, "gen_result.jsonl"))
    args = ap.parse_args()

    os.makedirs(AB, exist_ok=True)
    res = run_arm(args.arm, args.adapter, args.tag)
    with open(args.out, "a", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    n_ok = sum(1 for r in res if r.get("path"))
    log(f"[{args.arm}] 完成 {n_ok}/{len(res)}")


if __name__ == "__main__":
    main()

# coding=utf-8
# Qwen3-TTS QLoRA 微调 —— 训练分布对齐推理（aligned prefill）
#
# 为什么需要这个变体（与 sft_12hz_qlora.py 的关键区别）：
#   上一版（sft_12hz_qlora.py）复用了官方 sft_12hz.py 的"手写拼接"前向：
#       input_embeds = text_embedding + codec_embedding(group0) + speaker@6 + codec 1~15 组
#   但模型真正开口（generate）时，talker 的 prefill 是另一套：
#       role(tokens) + tts_pad×K + tts_bos + codec_prefill(nothink/think_bos/think_eos/speaker/codec_pad/codec_bos)
#       + text_projection 偏移 + trailing text + tts_pad + codec_bos
#   并且推理时 codec 段每个位置只喂第 0 码组（codec_embedding(codec0)），绝不是把 1~15 组也叠进输入。
#   4-bit 基座冻结、只动 transformer 的小 LoRA 扛不住这套"训练/推理输入分布错位"，
#   于是 LoRA 在错的分布上学，套回推理就吐噪声（loss 全程 ~ln(8192)≈9，即随机猜码）。
#
# 本脚本：把训练前向的"提示区"强制复刻 generate 的 non_streaming + speakers 分支数学
# （build_prefill_embeds 逐行对齐 modeling_qwen3_tts.py 的 generate 2079~2227 行），
# codec 段只喂第 0 码组（= 推理逐步输入），标签按 next-token 对齐。
# 这样 LoRA 在"推理真正会看到的分布"上学习 —— 基座本身就能在该分布上产出连贯语音，
# LoRA 只需把音色/韵律往袋鼠方向推，loss 从第一步就远低于随机，能稳定收敛。
#
# 运行（先冒烟，再正式）：
#   python sft_12hz_qlora_aligned.py --train_jsonl train_with_codes.jsonl \
#       --ref_audio media/voicebank/merg_004/reference_24k.wav --dry_run
#   python sft_12hz_qlora_aligned.py --train_jsonl train_with_codes.jsonl \
#       --ref_audio media/voicebank/merg_004/reference_24k.wav --num_epochs 3
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# 2026-10-12：repo 根下的 qwen_tts/ 副本用旧版 @check_model_inputs() 装饰器写法，
# 与当前 transformers 5.16.1 不兼容（TypeError: missing 1 required positional argument: 'func'）。
# 只插 _HERE（finetuning/，为了 dataset.py），不插 _ROOT；
# 需要覆盖时用 VM_QWEN_TTS_ROOT 显式指定（默认走 site-packages 的干净版本）。
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_override = os.environ.get("VM_QWEN_TTS_ROOT")
if _override and _override not in sys.path:
    sys.path.insert(0, _override)
# tts_trial/ 根放 sys.path【末尾】，只为导入训练/推理共用的量化配置单一来源
# qlora_quant_config（放末尾避免遮蔽 site-packages）。本脚本有两份同内容拷贝：
#   Qwen3-TTS/finetuning/（现役）与 finetuning_patches/（仓库内受版本管理副本），
# 两者目录深度不同，故从本文件位置向上查找含 qlora_quant_config.py 的目录，
# 而不是写死 dirname 层数 —— 这样两份逐字节相同、各自也都能直接运行。
def _find_shared_config_dir(start):
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, "qlora_quant_config.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


_shared_dir = _find_shared_config_dir(_HERE)
if _shared_dir and _shared_dir not in sys.path:
    sys.path.append(_shared_dir)

import librosa
import numpy as np
import torch
import torch.nn.functional as F
from bitsandbytes.nn import Linear4bit  # 仅 dry_run 判定
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from torch.optim import AdamW
from transformers import AutoConfig

# 训练/推理量化配置单一来源：skip 名单 + LoRA target + BitsAndBytesConfig 都从这里取，
# 禁止在本脚本另抄一份（否则改一处漏一处，已经造成三次 4bit/fp 错配事故）。
from qlora_quant_config import (
    LORA_TARGET_MODULES,
    MODULES_TO_NOT_CONVERT,
    build_bnb_config,
)

SPK_ID = 3000  # custom_voice 锚点落到的 codec_embedding 行


def compute_anchor(base, ref_audio_path: str) -> torch.Tensor:
    """用干净参考音频算一次袋鼠声纹锚点。"""
    audio, sr = librosa.load(ref_audio_path, sr=None, mono=True)
    tgt_sr = base.speaker_encoder_sample_rate
    if int(sr) != int(tgt_sr):
        audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt_sr))
    audio = audio.astype(np.float32)
    emb = base.extract_speaker_embedding(audio=audio, sr=int(tgt_sr))
    emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
    return emb.detach().to(torch.bfloat16)


def build_prefill_embeds(base, input_id, speaker_embed, non_streaming_mode=True):
    """逐行对齐 generate() 的 non_streaming + speakers（+非 icl）分支。
    输入 input_id: [1, T] = _build_assistant_text(text) 的 token。
    返回 talker_input_embed: [1, P, d]（纯提示区，不含 codec 目标）。
    """
    talker = base.talker
    cfg = base.config
    tcfg = cfg.talker_config
    dev = talker.device
    dt = input_id.dtype

    # tts bos/eos/pad 投影
    tts_bos_embed, tts_eos_embed, tts_pad_embed = talker.text_projection(
        talker.get_text_embeddings()(
            torch.tensor([[cfg.tts_bos_token_id, cfg.tts_eos_token_id, cfg.tts_pad_token_id]],
                        device=dev, dtype=dt)
        )
    ).chunk(3, dim=1)

    # codec prefill（language=auto：无 language_id）
    codec_prefill_list = [[tcfg.codec_nothink_id, tcfg.codec_think_bos_id, tcfg.codec_think_eos_id]]
    c0 = talker.get_input_embeddings()(
        torch.tensor(codec_prefill_list, device=dev, dtype=dt))              # [1,3,d]
    c1 = talker.get_input_embeddings()(
        torch.tensor([[tcfg.codec_pad_id, tcfg.codec_bos_id]], device=dev, dtype=dt))  # [1,2,d]
    codec_input_embedding = torch.cat(
        [c0, speaker_embed.view(1, 1, -1), c1], dim=1)                      # [1,6,d]

    # role 区
    role = talker.text_projection(talker.get_text_embeddings()(input_id[:, :3]))  # [1,3,d]
    # pad×K + bos，再叠加 codec_prefill[:, :-1]
    head = torch.cat(
        (tts_pad_embed.expand(-1, codec_input_embedding.shape[1] - 2, -1), tts_bos_embed),
        dim=1) + codec_input_embedding[:, :-1]                              # [1,5,d]
    talker_input_embed = torch.cat((role, head), dim=1)                      # [1,8,d]

    # 非 icl 分支
    # ★ 2026-10-12 修复：原先这里多截了一行 `[:, :-1]`，导致 head 只剩 7 行、
    #   丢掉了末尾的 (tts_bos + codec_pad)，训练 prefill 比推理短 1 行
    #   （实测 train P=20 vs infer P=21，余弦 0.9146 → LoRA 学到的修正套不上推理）。
    #   generate()（modeling_qwen3_tts.py:2209-2213）的真实流程是：
    #     role(3) + head(5) = 8 行 → 追加 text 首 token+codec_bos = 9 行
    #     → `[:, :-1]` 截掉**刚追加的那行** → 回到 8 行（末尾仍是 tts_bos+codec_pad）。
    #   所以这里必须**保留全部 8 行**，不能再截。
    text_part = torch.cat(
        (talker.text_projection(talker.get_text_embeddings()(input_id[:, 3:-5])), tts_eos_embed),
        dim=1) + talker.get_input_embeddings()(
            torch.tensor([[tcfg.codec_pad_id] * (input_id[:, 3:-5].shape[1] + 1)],
                        device=dev, dtype=dt))                               # [1, (T-8)+1, d]
    bos_part = tts_pad_embed + talker.get_input_embeddings()(
        torch.tensor([[tcfg.codec_bos_id]], device=dev, dtype=dt))          # [1,1,d]
    talker_input_embed = torch.cat([talker_input_embed, text_part, bos_part], dim=1)
    return talker_input_embed


def report_param_counts(peft_model):
    total = sum(p.numel() for p in peft_model.parameters())
    trainable = sum(p.numel() for p in peft_model.parameters() if p.requires_grad)
    print(f"[QLoRA] 总参数 {total/1e6:.1f}M | 可训练(LoRA) {trainable/1e6:.1f}M "
          f"({100*trainable/total:.3f}%)")


def dry_run_checks(base):
    ok = True
    for name in ("speaker_encoder", "talker.model.codec_embedding",
                 "talker.model.text_embedding", "talker.codec_head", "talker.text_projection"):
        try:
            mod = base.get_submodule(name)
            is_4bit = isinstance(mod, Linear4bit) or "4bit" in str(type(mod)).lower()
            if is_4bit:
                print(f"[check] {name:35s} 被量化 BAD"); ok = False
            else:
                print(f"[check] {name:35s} dtype={next(mod.parameters()).dtype} OK")
        except Exception as e:
            print(f"[check] {name:35s} 无法访问: {e}"); ok = False
    proj = base.get_submodule("talker.model.layers.0.self_attn.q_proj")
    print(f"[check] talker q_proj 量化4bit: {isinstance(proj, Linear4bit)}")
    cp = base.get_submodule("talker.code_predictor.model.layers.0.self_attn.q_proj")
    print(f"[check] code_predictor q_proj 量化4bit: {isinstance(cp, Linear4bit)}")
    print("[check] 精度正确" if ok else "[check] 异常")
    return ok


def build_sample(qwen3tts, item, max_codec_len):
    """把一条 jsonl 样本变成 (input_id[1,T], audio_codes[N,16])。"""
    text = item["text"]
    audio_codes = torch.tensor(item["audio_codes"], dtype=torch.long)
    if audio_codes.shape[0] > max_codec_len:
        audio_codes = audio_codes[:max_codec_len]
    input_id = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(text)])[0]
    return input_id, audio_codes


def train():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init_model_path", default="D:/变声/tts_models/qwen3-tts-1.7b-base")
    ap.add_argument("--output_model_path", default="output_qlora_aligned")
    ap.add_argument("--train_jsonl", required=True)
    ap.add_argument("--ref_audio", required=True, help="干净袋鼠参考音频，固定说话人锚点")
    ap.add_argument("--num_epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--batch_size", type=int, default=1)
    ap.add_argument("--grad_accum", type=int, default=8)
    ap.add_argument("--lora_r", type=int, default=16)
    ap.add_argument("--lora_alpha", type=int, default=32)
    ap.add_argument("--lora_dropout", type=float, default=0.05)
    ap.add_argument("--max_codec_len", type=int, default=750)
    ap.add_argument("--max_steps", type=int, default=-1, help=">0 时仅跑若干 step 做冒烟/验收")
    ap.add_argument("--attn_implementation", default="sdpa")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    device = "cuda"
    qwen3tts = Qwen3TTSModel.from_pretrained(
        args.init_model_path, torch_dtype=torch.bfloat16,
        attn_implementation=args.attn_implementation,
        quantization_config=build_bnb_config(),
    )
    base = qwen3tts.model

    # get_input_embeddings 补丁（PEFT 需要）
    def _gi(self): return self.talker.model.codec_embedding
    def _si(self, v): self.talker.model.codec_embedding = v
    base.get_input_embeddings = _gi.__get__(base)
    base.set_input_embeddings = _si.__get__(base)
    base = prepare_model_for_kbit_training(base, use_gradient_checkpointing=True)

    # 固定说话人锚点：写进 codec_embedding[SPK_ID]，训练/推理都从同一行查
    anchor = compute_anchor(base, args.ref_audio).to(device)
    with torch.no_grad():
        base.talker.model.codec_embedding.weight[SPK_ID] = anchor[0].to(
            base.talker.model.codec_embedding.weight.dtype)

    lora_config = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
        bias="none", target_modules=LORA_TARGET_MODULES, modules_to_save=[],
    )
    peft_model = get_peft_model(base, lora_config)
    report_param_counts(peft_model)

    if args.dry_run:
        dry_run_checks(base)
        # 额外验证 prefill 能跑通
        sid = torch.tensor([[SPK_ID]], device=device, dtype=torch.long)
        spk_emb = base.talker.get_input_embeddings()(sid)
        dummy = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text("测试一下。")])[0].to(device)
        pe = build_prefill_embeds(base, dummy, spk_emb)
        print(f"[dry_run] prefill shape={tuple(pe.shape)} OK；锚点 codec_embedding[{SPK_ID}] norm={anchor.norm():.3f}")
        return

    # 训练数据
    raw = [json.loads(l) for l in open(args.train_jsonl, encoding="utf-8")]
    samples = []
    skipped = 0
    for it in raw:
        try:
            iid, ac = build_sample(qwen3tts, it, args.max_codec_len)
            if iid.shape[1] < 9:
                skipped += 1; continue
            samples.append((iid, ac))
        except Exception:
            skipped += 1
    print(f"[data] 可用样本 {len(samples)} 条（跳过 {skipped}）")

    optimizer = AdamW([p for p in peft_model.parameters() if p.requires_grad],
                      lr=args.lr, weight_decay=0.01)
    inner = peft_model.get_base_model()  # 带 LoRA 的 Qwen3TTSForConditionalGeneration
    peft_model.train()
    inner.talker.train()

    sid = torch.tensor([[SPK_ID]], device=device, dtype=torch.long)
    amp_dtype = torch.bfloat16

    import random
    global_step = 0
    for epoch in range(args.num_epochs):
        random.shuffle(samples)
        optim_steps = 0
        peft_model.zero_grad()
        for iid, ac in samples:
            iid = iid.to(device)
            ac = ac.to(device)
            spk_emb = inner.talker.get_input_embeddings()(sid)  # 读锚点行
            with torch.autocast(device_type="cuda", dtype=amp_dtype):
                prefill = build_prefill_embeds(inner, iid, spk_emb)        # [1,P,d]
                codec0 = ac[:, 0]                                          # [N]
                codec_emb = inner.talker.get_input_embeddings()(codec0.unsqueeze(0))  # [1,N,d]
                input_embeds = torch.cat([prefill, codec_emb], dim=1)      # [1,P+N,d]
                P = prefill.shape[1]; N = codec0.shape[0]
                # ★ 2026-10-12 实验修正（#371/#278 double-shift）：labels 与输入【同位】，
                # 交给 talker 的 ForCausalLMLoss 内部做【唯一一次】 shift（logits[j]→labels[j+1]）。
                # 旧写法先手写 shift（labels[P-1]=c0[0]、labels[P:P+N-1]=c0[1:]）再被内部 shift
                # 一次 → 模型被教成预测 j+2。数据本身不含 EOS（81/81 末尾皆非 eos），停句靠 base 先验。
                labels = torch.full((1, P + N), -100, dtype=torch.long, device=device)
                labels[0, P:P + N] = codec0
                attn = torch.ones((1, P + N), dtype=torch.long, device=device)

                outputs = inner.talker(
                    inputs_embeds=input_embeds, attention_mask=attn,
                    labels=labels, output_hidden_states=True,
                )
                hidden = outputs.hidden_states[0][-1]                       # [1,P+N,d]
                talker_hidden = hidden[0, P:P + N]                          # [N,d] codec 区隐藏
                talker_codec_ids = ac                                        # [N,16] 与 codec 区 1:1 对应
                _, sub_loss = inner.talker.forward_sub_talker_finetune(talker_codec_ids, talker_hidden)
                loss = outputs.loss + 0.3 * sub_loss

            loss.backward()
            global_step += 1
            optim_steps += 1
            if optim_steps % args.grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(peft_model.parameters(), 1.0)
                optimizer.step()
                optimizer.zero_grad()

            if global_step % 10 == 0:
                print(f"Epoch {epoch} | Step {global_step} | Loss {loss.item():.4f} | "
                      f"codec0_n={N} prefill_p={P}", flush=True)
            if args.max_steps > 0 and global_step >= args.max_steps:
                break
        # 每个 epoch 结束保存适配器（非破坏式）
        out_dir = os.path.join(args.output_model_path, f"adapter-epoch-{epoch}")
        os.makedirs(out_dir, exist_ok=True)
        peft_model.save_pretrained(out_dir)
        print(f"[save] adapter -> {out_dir}", flush=True)
        if args.max_steps > 0 and global_step >= args.max_steps:
            break

    # 最终只保存最新适配器（推理用 base 4bit + adapter 不合并）
    final_dir = os.path.join(args.output_model_path, "adapter-final")
    os.makedirs(final_dir, exist_ok=True)
    peft_model.save_pretrained(final_dir)
    print(f"[done] 最终适配器 -> {final_dir} | global_step={global_step}")


if __name__ == "__main__":
    train()

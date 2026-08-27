# coding=utf-8
"""Qwen3-TTS 官方 sft_12hz.py 的 8GB 显存适配版，改动点：
1. flash_attention_2 -> sdpa（本机未装 flash-attn）
2. AdamW -> Adafactor（全参 AdamW 状态需 ~14GB，8GB 卡放不下）
3. 训练时逐 step 打显存峰值日志，写 ft_train_out.txt 供评估
其余逻辑与官方一致。
"""
import argparse
import json
import os
import shutil
import sys

import torch
from accelerate import Accelerator
sys.path.insert(0, r"D:\变声\tts_trial\Qwen3-TTS\finetuning")
from dataset import TTSDataset
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from safetensors.torch import save_file
from torch.optim import Adafactor
from torch.utils.data import DataLoader
from transformers import AutoConfig

target_speaker_embedding = None


def log(m):
    print(m, flush=True)


def _save_ckpt(output_dir: str, base_dir: str, args, accelerator, model):
    """最小化保存：差异文件只写两份（补丁 config + 训练权重），其余全部硬链接基座文件。

    硬链接同卷零额外磁盘占用；跨卷/失败时退回 copy2。基座的 model.safetensors
    与 config.json 跳过（前者被训练权重替换，后者要打 custom_voice 补丁）。
    """
    from pathlib import Path
    src, dst = Path(base_dir), Path(output_dir)
    dst.mkdir(parents=True, exist_ok=True)

    for f in src.rglob("*"):
        rel = f.relative_to(src)
        target = dst / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if rel.parent == Path(".") and f.name in ("model.safetensors", "config.json"):
            continue  # 仅顶层的这两个是差异文件；子目录里同名文件照常链接
        try:
            os.link(f, target)
        except OSError:
            shutil.copy2(f, target)

    # 1) 打补丁的 config.json（custom_voice + spk_id）
    with open(src / "config.json", "r", encoding="utf-8") as f:
        config_dict = json.load(f)
    config_dict["tts_model_type"] = "custom_voice"
    talker_config = config_dict.get("talker_config", {})
    talker_config["spk_id"] = {args.speaker_name: 3000}
    talker_config["spk_is_dialect"] = {args.speaker_name: False}
    config_dict["talker_config"] = talker_config
    with open(dst / "config.json", "w", encoding="utf-8") as f:
        json.dump(config_dict, f, indent=2, ensure_ascii=False)

    # 2) 训练出的 talker 权重（丢掉 speaker_encoder，嵌入写死进 codec_embedding[3000]）
    unwrapped_model = accelerator.unwrap_model(model)
    state_dict = {k: v.detach().to("cpu") for k, v in unwrapped_model.state_dict().items()}
    for k in [k for k in state_dict if k.startswith("speaker_encoder")]:
        del state_dict[k]
    weight = state_dict['talker.model.codec_embedding.weight']
    state_dict['talker.model.codec_embedding.weight'][3000] = \
        target_speaker_embedding[0].detach().to(weight.device).to(weight.dtype)
    save_file(state_dict, str(dst / "model.safetensors"))
    log(f"[save] {output_dir}")


def _prune_ckpts(root: str, keep: int):
    """只保留最近 keep 个 checkpoint-* 目录。"""
    from pathlib import Path
    if keep <= 0:
        return
    dirs = sorted(Path(root).glob("checkpoint-*"), key=lambda p: p.name)
    for d in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(d, ignore_errors=True)
        log(f"[prune] 删除旧检查点 {d.name}")


def train():
    global target_speaker_embedding

    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument("--init_model_path", type=str, default=r"D:/变声/tts_models/qwen3-tts-1.7b-base")
    parser.add_argument("--output_model_path", type=str, default=os.path.join(here, "ft_output"))
    parser.add_argument("--train_jsonl", type=str, required=True)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--num_epochs", type=int, default=3)
    parser.add_argument("--speaker_name", type=str, default="feidudu")
    # speaker 锚点参考音频：单说话人微调必须固定，否则随 batch 随机漂移
    parser.add_argument("--anchor_ref", type=str,
                        default=os.path.join(here, "ft_data", "feidudu_full.wav"))
    parser.add_argument("--keep_ckpts", type=int, default=2, help="只保留最近 N 个检查点")
    args = parser.parse_args()

    accelerator = Accelerator(gradient_accumulation_steps=4, mixed_precision="bf16")

    MODEL_PATH = args.init_model_path

    try:
        qwen3tts = Qwen3TTSModel.from_pretrained(
            MODEL_PATH, torch_dtype=torch.bfloat16, attn_implementation="sdpa")
        log("[attn] sdpa OK")
    except Exception as exc:
        log(f"[attn] sdpa 失败({exc})，退回 eager")
        qwen3tts = Qwen3TTSModel.from_pretrained(
            MODEL_PATH, torch_dtype=torch.bfloat16, attn_implementation="eager")

    config = AutoConfig.from_pretrained(MODEL_PATH)

    train_data = open(args.train_jsonl).readlines()
    train_data = [json.loads(line) for line in train_data]
    dataset = TTSDataset(train_data, qwen3tts.processor, config)
    train_dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True,
                                  collate_fn=dataset.collate_fn)

    # ---- speaker 锚点：训练前用专属参考音频固定算一次，不随 batch 漂移 ----
    anchor_idx = next((i for i, r in enumerate(train_data)
                       if os.path.normpath(r.get("audio", "")) == os.path.normpath(args.anchor_ref)), None)
    if anchor_idx is None:
        log(f"[anchor] 警告：{args.anchor_ref} 不在训练集里，退回用首个样本")
        anchor_idx = 0
    else:
        log(f"[anchor] 参考音频 {os.path.basename(args.anchor_ref)} (样本 #{anchor_idx})")
    with torch.inference_mode():
        anchor_batch = dataset.collate_fn([dataset[anchor_idx]])
        raw_model = qwen3tts.model
        ref_mels0 = anchor_batch['ref_mels'].to(raw_model.device).to(raw_model.dtype)
        target_speaker_embedding = raw_model.speaker_encoder(ref_mels0).detach()

    # torch 2.8 原生 Adafactor：二阶矩分解存储，状态只占常规 AdamW 的零头
    optimizer = Adafactor(qwen3tts.model.parameters(), lr=args.lr,
                          weight_decay=0.01)

    model, optimizer, train_dataloader = accelerator.prepare(
        qwen3tts.model, optimizer, train_dataloader)

    log(f"[mem] 初始已分配 {torch.cuda.memory_allocated()/2**30:.2f} GiB / "
        f"保留 {torch.cuda.memory_reserved()/2**30:.2f} GiB")

    num_epochs = args.num_epochs
    peak_mem = 0.0
    model.train()

    for epoch in range(num_epochs):
        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(model):
                input_ids = batch['input_ids']
                codec_ids = batch['codec_ids']
                ref_mels = batch['ref_mels']
                text_embedding_mask = batch['text_embedding_mask']
                codec_embedding_mask = batch['codec_embedding_mask']
                attention_mask = batch['attention_mask']
                codec_0_labels = batch['codec_0_labels']
                codec_mask = batch['codec_mask']

                speaker_embedding = model.speaker_encoder(
                    ref_mels.to(model.device).to(model.dtype)).detach()

                input_text_ids = input_ids[:, :, 0]
                input_codec_ids = input_ids[:, :, 1]

                input_text_embedding = model.talker.model.text_embedding(input_text_ids) * text_embedding_mask
                input_codec_embedding = model.talker.model.codec_embedding(input_codec_ids) * codec_embedding_mask
                input_codec_embedding[:, 6, :] = speaker_embedding

                input_embeddings = input_text_embedding + input_codec_embedding

                for i in range(1, 16):
                    codec_i_embedding = model.talker.code_predictor.get_input_embeddings()[i - 1](
                        codec_ids[:, :, i])
                    codec_i_embedding = codec_i_embedding * codec_mask.unsqueeze(-1)
                    input_embeddings = input_embeddings + codec_i_embedding

                outputs = model.talker(
                    inputs_embeds=input_embeddings[:, :-1, :],
                    attention_mask=attention_mask[:, :-1],
                    labels=codec_0_labels[:, 1:],
                    output_hidden_states=True
                )

                hidden_states = outputs.hidden_states[0][-1]
                talker_hidden_states = hidden_states[codec_mask[:, :-1]]
                talker_codec_ids = codec_ids[codec_mask]

                sub_talker_logits, sub_talker_loss = model.talker.forward_sub_talker_finetune(
                    talker_codec_ids, talker_hidden_states)

                loss = outputs.loss + 0.3 * sub_talker_loss

                accelerator.backward(loss)

                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(model.parameters(), 1.0)

                optimizer.step()
                optimizer.zero_grad()

            cur = torch.cuda.max_memory_allocated() / 2**30
            peak_mem = max(peak_mem, cur)
            log(f"[epoch {epoch} step {step}] loss={loss.item():.4f} "
                f"峰值显存={cur:.2f} GiB")

        if accelerator.is_main_process:
            output_dir = os.path.join(args.output_model_path, f"checkpoint-epoch-{epoch}")
            _save_ckpt(output_dir, MODEL_PATH, args, accelerator, model)
            _prune_ckpts(args.output_model_path, keep=args.keep_ckpts)

    log(f"[DONE] 全部完成，全程峰值显存 {peak_mem:.2f} GiB")


if __name__ == "__main__":
    train()

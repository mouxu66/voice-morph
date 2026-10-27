# coding=utf-8
"""Qwen3-TTS 官方 sft_12hz.py 的 8GB 显存适配版，改动点：
1. flash_attention_2 -> sdpa（本机未装 flash-attn）
2. AdamW -> Adafactor（全参 AdamW 状态需 ~14GB，8GB 卡放不下）
3. **lr 按优化器换算**（2026-10-13 修复，见下）
4. 训练时逐 step 打显存峰值日志，写 ft_train_out.txt 供评估

★ lr 为什么必须换（2026-10-13 定位，docs/交接-AI必读.md §9.7 第 170 条）：
  改动 2 换了优化器，却**沿用了官方的 lr=2e-5**，而两者步长语义不同：
      AdamW      绝对步长 ≈ lr
      Adafactor  绝对步长 ≈ RMS(θ) · min(lr, 1/√t)     （torch/optim/_adafactor.py:376）
  RMS(θ) 实测 0.001~0.2 ⇒ Adafactor 实际步长比 AdamW 小 1~2 个数量级。
  再叠加全参权重是 **bf16 存储**（8 位尾数，相对精度 2^-8≈3.9e-3），
  单步相对变化 2e-5 比它还小两个数量级 ⇒ **权重舍入后原地不动，loss 冻结**。
  实测：lr=2e-5 跑满 1940 步（=10 epoch）loss 从 14.56 只到 14.51；
        换成 Adafactor 原生尺度 lr=1e-2，1 epoch 就从 13.46 降到 8.47。
  详见 `tts_trial/finetune_lr.py`（唯一事实来源）与 `tts_trial/tests/test_finetune_lr.py`。
"""
import argparse
import json
import os
import shutil
import sys

import torch
from accelerate import Accelerator
sys.path.insert(0, r"D:\变声\tts_trial\Qwen3-TTS\finetuning")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 为了 import finetune_lr
from dataset import TTSDataset
from finetune_lr import ADAFACTOR_LR, check_lr
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

    ★ 2026-10-13 修复：铺文件改走 `ckpt_utils.link_base_tree()`。
      旧写法直接 `os.link` + `except OSError: shutil.copy2`，在**目标已存在**时
      （同一音色第 2 次训练 / C2 续训，输出目录里还留着上一轮的 checkpoint-epoch-*）
      会 FileExistsError → 落到 copy2 → Windows 报 WinError 32 → 异常冒出本函数
      ⇒ 训练进程 rc=1，尽管权重已训好，状态机（finetune.py:448）把整轮判成 error。
      复现脚本 `tts_trial/probe_ckpt_save.py`，回归测试 `tests/test_ckpt_utils.py`。
    """
    from pathlib import Path

    from ckpt_utils import link_base_tree

    src, dst = Path(base_dir), Path(output_dir)
    dst.mkdir(parents=True, exist_ok=True)
    link_base_tree(src, dst)

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
    # 8G 卡全参训练时系统内存压力大：先空缓存，再逐张搬运，避免 CPU 一次性分配爆掉
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    unwrapped_model = accelerator.unwrap_model(model)
    raw_sd = unwrapped_model.state_dict()
    state_dict = {}
    for k, v in raw_sd.items():
        if k.startswith("speaker_encoder"):
            continue  # 嵌入已写死进 codec_embedding，speaker_encoder 不需要保存
        state_dict[k] = v.detach().to("cpu")
    del raw_sd
    weight = state_dict['talker.model.codec_embedding.weight']
    state_dict['talker.model.codec_embedding.weight'][3000] = \
        target_speaker_embedding[0].detach().to(weight.device).to(weight.dtype)
    save_file(state_dict, str(dst / "model.safetensors"))
    del state_dict
    gc.collect()
    torch.cuda.empty_cache()
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
    # ★ 默认值 = Adafactor 的原生尺度（1e-2），**不是**官方的 AdamW 值 2e-5。
    #   8 月那次生产微调就是被 2e-5 冻住的（loss 10 epoch 只动 0.05），见文件头说明。
    parser.add_argument("--lr", type=float, default=ADAFACTOR_LR,
                        help=f"Adafactor 学习率；默认 {ADAFACTOR_LR:g}（其原生尺度）。"
                             f"官方 sft_12hz.py 的 2e-5 是 AdamW 尺度，配 Adafactor 会"
                             f"因步长被 bf16 舍入吃掉而完全不收敛。")
    parser.add_argument("--num_epochs", type=int, default=3)
    parser.add_argument("--speaker_name", type=str, default="feidudu")
    # speaker 锚点参考音频：单说话人微调必须固定，否则随 batch 随机漂移
    parser.add_argument("--anchor_ref", type=str,
                        default=os.path.join(here, "ft_data", "feidudu_full.wav"))
    parser.add_argument("--keep_ckpts", type=int, default=2, help="只保留最近 N 个检查点")
    args = parser.parse_args()

    # ★ 把「lr 与优化器量纲不匹配」显式打出来。历史上它不报错、不警告，
    #   只是 loss 不动 —— 10 epoch 白跑、42GB 磁盘白占，全靠人事后翻日志才发现。
    warn = check_lr(args.lr, "Adafactor")
    if warn:
        log(f"[lr][警告] {warn}")
    else:
        log(f"[lr] Adafactor lr={args.lr:g}（原生尺度 {ADAFACTOR_LR:g}）OK")

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

    train_data = open(args.train_jsonl, encoding="utf-8").readlines()
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
                    # ★ labels 与输入【同位】，交给 ForCausalLMLoss 内部做唯一一次 shift
                    #   （2026-10-13 修复）。官方 sft_12hz.py 这里写的是 `[:, 1:]`，
                    #   而 transformers 5.x 的 ForCausalLMLoss **内部已经 shift 一次**
                    #   （loss_utils.py:61-64 `labels[..., 1:]`）⇒ 两者叠加会教成预测
                    #   **j+2**（跳过 audio_codec_0[0]、整体错一格）。
                    #   数值复验见 tts_trial/probe_shift_check.py：同位 labels 的交叉熵
                    #   = 0.000000（与手工 next-token 参照一致），而 `[:, 1:]` = 60.0。
                    #   同族修复 2026-10-12 已在 QLoRA 脚本做过（见犯错指南 #160、
                    #   官方 issue #278/#371 —— 官方 sft 脚本也有这个双重 shift）。
                    labels=codec_0_labels[:, :-1],
                    output_hidden_states=True
                )

                hidden_states = outputs.hidden_states[0][-1]
                talker_hidden_states = hidden_states[codec_mask[:, :-1]]
                talker_codec_ids = codec_ids[codec_mask]

                # ★ 2026-10-14 修复：sub_talker 的损失**不能用 vendor 的返回值**。
                #   `forward_sub_talker_finetune()`（第三方包 modeling_qwen3_tts.py:1609）
                #   内部把 `labels=codec_ids[:, 1:]` 交给 self.loss_function，
                #   而 loss_function = ForCausalLMLoss **内部还会再 shift 一次**
                #   （loss_utils.py:61-64）⇒ 实际在教模型预测 **k+2**，错位一格。
                #
                #   用真模型当裁判实测（probe_subtalker_oracle.py，3 条真实样本）：
                #       对齐 A（logits[k] → codec_{k+1}）: 7.4167  ← 低于随机线 7.6246 ✅
                #       对齐 B（codec_{k+2}，= 现行双重 shift）: 9.2917
                #       对齐 C（codec_k）                    : 9.2500
                #       vendor 内部返回的 loss               : 9.2614 ≈ B ⇒ 确认错位
                #   所以这里只取 logits，自己按 A 算交叉熵。
                #   （talker 主损失那处已同步修，见上面 labels=codec_0_labels[:, :-1]。）
                sub_talker_logits, _ = model.talker.forward_sub_talker_finetune(
                    talker_codec_ids, talker_hidden_states)
                sub_logits_flat = sub_talker_logits.reshape(-1, sub_talker_logits.shape[-1])
                sub_targets = talker_codec_ids[:, 1:].reshape(-1)
                sub_talker_loss = torch.nn.functional.cross_entropy(
                    sub_logits_flat[: sub_targets.numel()], sub_targets
                )

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

# -*- coding: utf-8 -*-
"""QLoRA 量化配置 —— 训练与推理共用的【唯一事实来源】(single source of truth)。

为什么需要这个模块（同一个坑已经踩了三次）：
  「不量化名单」最初被手抄进训练脚本 + 4 个推理脚本（ab_generate_B / ab_control_test /
  ab_quant_test / ab_loss_diag）。transformers 在 4.57.3 → 5.16.1 之间把
  quantizers_utils.should_convert_module() 的匹配语义从【子串】改成了【前缀 / 结尾】：

      4.57.3："text_projection" 子串命中 talker.text_projection.linear_fc1  ✅
      5.16.1：必须全名前缀/结尾相等，裸名不再命中带子路径的模块            ❌

  于是 talker.text_projection.* 与 talker.code_predictor.lm_head.* 共 17 个命门
  Linear 被误 NF4 量化（dry_run 总参 1202.9M → 1183.0M）。第一次只修了训练脚本，
  推理脚本仍旧裸名单 → 反向错配；判决脚本 ab_verdict.py 又干脆用纯 bf16 加载底座，
  在它里面改名单完全不生效。为根治「改一处、漏多处」，所有训练 / 推理脚本一律从
  这里取名单与 BitsAndBytesConfig，并由
  tts_trial/tests/test_qlora_quant_parity.py 做一致性回归。

匹配规则实测（transformers 5.16.1，见 ab_skip_semantics.py）：
  名单条目按「模块全名的前缀或结尾」命中。speaker_encoder / codec_embedding /
  text_embedding / codec_head 在模型顶层，裸名即以其开头/结尾，仍命中；
  text_projection、lm_head 是子模块，必须写完整父路径。
"""

# 不做 NF4 量化、必须保持全精度的模块。
# 训练 dry_run 基线（8 月标定，2026-10-11 复核）：总参 1202.9M / 可训练 1.599%。
MODULES_TO_NOT_CONVERT = [
    "speaker_encoder",
    "codec_embedding",
    "text_embedding",
    "codec_head",
    "talker.text_projection",
    "talker.code_predictor.lm_head",
]

# LoRA 只挂 talker / code_predictor 的注意力与 MLP 投影（不碰命门模块）。
LORA_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]

# 训练侧 LoRA 超参（adapter_config.json 里就是 r=16 / alpha=32）。
LORA_R = 16
LORA_ALPHA = 32

# ★ 推理侧有效 LoRA 缩放（2026-10-12 实测定位，见 docs 犯错指南 #160）：
#   Peft 实际缩放 = lora_alpha / r = 32/16 = 2.0。在 Qwen3-TTS 这个「talker +
#   code_predictor」两级、逐帧自回归的自定义模型上，2.0 的 LoRA 增量在自由生成时
#   逐 token 累积，把 logits/波形推到满幅饱和（peak 钉死 1.00、跑到 token 上限、
#   ASR 全空）。teacher-forcing 只走一次前向不累积，所以 loss 好看（≈4.0）极具迷惑性。
#   把有效缩放降到 ≤1.0 立即恢复：实测 scale=0.5 时现成 adapter（无需重训/降级）
#   3/3 可懂度 0.87/0.79/0.94、peak 0.21~0.23、零削波；4.57.3 与 5.16.1 两栈一致。
#   社区同款：QwenLM/Qwen3-TTS issue #343（merge at lora_scale≈0.4）。
#   推理时用 apply_lora_scale() 覆盖各 lora 模块的 scaling[active] 即可，不必重存 adapter。
LORA_INFERENCE_SCALE = 0.5

# 8 月验收口径的参数量指纹（修复量化名单后必须回到这两个值）。
AUGUST_BASELINE_TOTAL_M = 1202.9
AUGUST_BASELINE_TRAINABLE_PCT = 1.599


def build_bnb_config():
    """构造与训练【逐字段一致】的 NF4 BitsAndBytesConfig。

    训练（sft_12hz_qlora_aligned.build_bnb_config）与推理必须共用本函数，
    任何一边改了量化类型 / 计算精度 / skip 名单都会造成「4-bit 训练 ↔ 全精度推理」
    错配（8-31 指纹：LoRA 增量套上去崩成电音、峰值顶到 1.00）。

    延迟导入 torch / transformers：让本模块在无 GPU 的 CI Python 里也能被导入，
    只读取上面的常量做结构一致性测试。
    """
    import torch
    from transformers import BitsAndBytesConfig

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        # transformers 4.57.3 只有 llm_int8_skip_modules 字段（无 modules_to_not_convert）。
        # 拷贝一份，避免调用方误改这里的全局名单。
        llm_int8_skip_modules=list(MODULES_TO_NOT_CONVERT),
    )


def apply_lora_scale(peft_model, scale=LORA_INFERENCE_SCALE):
    """把 PeftModel 里每个 lora 模块的有效缩放 scaling[active] 覆盖为 `scale`。

    必须在 PeftModel.from_pretrained() 之后、generate() 之前调用。返回被改写的模块数。
    默认 2.0（alpha/r）会让 Qwen3-TTS 自由生成满幅饱和；见模块顶部 LORA_INFERENCE_SCALE。
    只改运行时缩放，不动磁盘上的 adapter，因此无需重训/重存，且可随时扫描找最优值。
    """
    n = 0
    for mod in peft_model.modules():
        scaling = getattr(mod, "scaling", None)
        if isinstance(scaling, dict):
            for key in scaling:
                scaling[key] = float(scale)
            n += 1
    return n

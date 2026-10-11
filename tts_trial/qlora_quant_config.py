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

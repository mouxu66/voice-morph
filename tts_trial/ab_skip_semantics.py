# -*- coding: utf-8 -*-
"""验证 transformers 5.16.1 的 skip-module 匹配语义变化。

8 月脚本用裸名 ["...","text_projection","lm_head"]，在 transformers 4.57.3 下
（子串语义）能跳过 talker.text_projection.linear_fc1 等；
在 5.16.1 下改成前缀/后缀语义，裸名不再命中 → 这些模块被 NF4 量化。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import transformers
from transformers.quantizers.quantizers_utils import should_convert_module

BARE = ["speaker_encoder", "codec_embedding", "text_embedding",
        "codec_head", "text_projection", "lm_head"]
FULL = ["speaker_encoder", "codec_embedding", "text_embedding",
        "codec_head", "talker.text_projection", "talker.code_predictor.lm_head"]

print(f"transformers = {transformers.__version__}")

CASES = [
    "talker.text_projection.linear_fc1",
    "talker.text_projection.linear_fc2",
    "talker.code_predictor.lm_head.0",
    "talker.code_predictor.lm_head.14",
    "speaker_encoder.conv1",
    "talker.model.codec_embedding",
    "talker.codec_head",
    "talker.model.layers.0.self_attn.q_proj",     # 这个本来就该量化
]

print("\n" + "=" * 88)
print(f"{'模块名':<44}{'裸名(8月写法)':>20}{'全路径(修正)':>20}")
print("-" * 88)
for name in CASES:
    a = should_convert_module(name, BARE)
    b = should_convert_module(name, FULL)
    print(f"{name:<44}{('量化' if a else '跳过'):>20}{('量化' if b else '跳过'):>20}")
print("=" * 88)
print("\n期望：text_projection.* / lm_head.* 应「跳过」（它们是 prefill 与 sub-talker 的命门）")
print("      若裸名那列显示「量化」→ 确认 8 月的 skip 名单今天失效了")

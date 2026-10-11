# -*- coding: utf-8 -*-
"""回归测试：QLoRA「不量化名单」训练/推理必须同源、同语义。

背景（同一个坑踩了三次，详见 tts_trial/qlora_quant_config.py 顶部说明）：
  transformers 4.57.3 → 5.16.1 把 should_convert_module() 的匹配从【子串】
  改成【前缀/结尾】，裸名 "text_projection"/"lm_head" 不再命中
  talker.text_projection.linear_fc1、talker.code_predictor.lm_head.0 等 17 个命门
  Linear → 被误 NF4 量化（总参 1202.9M → 1183.0M）。名单又被手抄进 6 个脚本，
  修训练漏推理、判决脚本干脆纯 bf16 加载，三轮「修复」都没打到真正缺失的格子。

本测试不依赖 GPU：
  - 纯 AST 静态扫描，系统 Python（无 torch CUDA）即可跑；
  - should_convert_module 语义探针在装了 transformers≥5 的解释器上额外校验。
"""
import ast
import os

import pytest

import qlora_quant_config as Q

TTS_TRIAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 现役训练 / 推理脚本：一律不允许自带名单，必须从 qlora_quant_config 取。
#   - Qwen3-TTS/finetuning/... 是实际执行的训练脚本（Qwen3-TTS/ 被 .gitignore，
#     但文件在工作树里、是唯一在跑的那份）；
#   - finetuning_patches/... 是仓库内受版本管理的副本，必须与上者逐字节同源。
ACTIVE_SCRIPTS = [
    "ab_generate_B.py",
    "ab_control_test.py",
    "ab_quant_test.py",
    "ab_loss_diag.py",
    os.path.join("Qwen3-TTS", "finetuning", "sft_12hz_qlora_aligned.py"),
    os.path.join("finetuning_patches", "sft_12hz_qlora_aligned.py"),
]

# 旧语义下的裸名写法 —— 在 transformers ≥5 下会漏掉命门模块，任何活动脚本都不许再出现。
BARE_CRITICAL = ("text_projection", "lm_head")

# 有意保留裸名的文件：
#   - qlora_quant_config.py 本身是唯一事实来源（里面写的是长路径，无裸名条目）；
#   - ab_skip_semantics.py 是【故意的阴性对照探针】，就是要演示裸名失效，不能改。
SCAN_ALLOWLIST = {
    os.path.join(TTS_TRIAL, "qlora_quant_config.py"),
    os.path.join(TTS_TRIAL, "ab_skip_semantics.py"),
    os.path.abspath(__file__),
}


def _parse(rel):
    path = os.path.join(TTS_TRIAL, rel)
    with open(path, encoding="utf-8") as f:
        return path, ast.parse(f.read(), filename=path)


def _string_constants(tree):
    """所有字符串字面量（属性访问 talker.text_projection 不算，注释/docstring 外的才算）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _imports_shared_module(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "qlora_quant_config":
            return True
        if isinstance(node, ast.Import):
            if any(a.name == "qlora_quant_config" for a in node.names):
                return True
    return False


def test_shared_list_uses_long_paths():
    """唯一事实来源里，命门模块必须是长路径，且不得残留裸名条目。"""
    lst = Q.MODULES_TO_NOT_CONVERT
    assert "talker.text_projection" in lst
    assert "talker.code_predictor.lm_head" in lst
    for bare in BARE_CRITICAL:
        assert bare not in lst, f"名单里仍有裸名 {bare!r}，transformers≥5 下会漏过子模块"
    # 顶层模块裸名仍然有效（前缀即全名开头），保留。
    for top in ("speaker_encoder", "codec_embedding", "text_embedding", "codec_head"):
        assert top in lst


@pytest.mark.parametrize("rel", ACTIVE_SCRIPTS)
def test_active_scripts_import_shared_list(rel):
    path, tree = _parse(rel)
    assert os.path.exists(path), f"活动脚本缺失：{path}"
    assert _imports_shared_module(tree), (
        f"{rel} 没有从 qlora_quant_config 导入量化名单 —— 又要手抄漂移了。"
        "训练/推理必须共用 qlora_quant_config.MODULES_TO_NOT_CONVERT / build_bnb_config()。"
    )


@pytest.mark.parametrize("rel", ACTIVE_SCRIPTS)
def test_active_scripts_have_no_bare_critical_names(rel):
    _, tree = _parse(rel)
    offenders = [s for s in _string_constants(tree) if s in BARE_CRITICAL]
    assert not offenders, (
        f"{rel} 仍含裸名字面量 {offenders}；transformers≥5 下不命中带子路径的命门模块，"
        "请改用 qlora_quant_config.MODULES_TO_NOT_CONVERT（长路径）。"
    )


def test_tracked_copy_matches_active_training_script():
    """仓库内副本必须与实际执行的训练脚本逐字节一致（防止再出现「修一份漏一份」）。"""
    active = os.path.join(TTS_TRIAL, "Qwen3-TTS", "finetuning", "sft_12hz_qlora_aligned.py")
    tracked = os.path.join(TTS_TRIAL, "finetuning_patches", "sft_12hz_qlora_aligned.py")
    with open(active, encoding="utf-8") as f:
        a = f.read()
    with open(tracked, encoding="utf-8") as f:
        b = f.read()
    assert a == b, "finetuning_patches 副本与 Qwen3-TTS/finetuning 现役训练脚本不一致"


def test_no_other_script_reintroduces_bare_list():
    """兜底：tts_trial 根目录的活动 A/B 脚本都不许出现裸名清单条目。

    只扫 tts_trial/*.py（现役 A/B 脚本都在这一层）；不递归 venv、上游工作目录
    Qwen3-TTS/、归档 _archive 等（现役训练脚本由 test_active_scripts_* 单独覆盖）。
    """
    violations = []
    for fn in os.listdir(TTS_TRIAL):
        if not fn.endswith(".py"):
            continue
        path = os.path.abspath(os.path.join(TTS_TRIAL, fn))
        if path in SCAN_ALLOWLIST:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
        except SyntaxError:
            continue
        bad = [s for s in _string_constants(tree) if s in BARE_CRITICAL]
        if bad:
            violations.append(f"{fn}: {bad}")
    assert not violations, "发现裸名清单回潮：\n" + "\n".join(violations)


def test_bnb_config_matches_training():
    """build_bnb_config() 的量化字段必须与训练口径一致（NF4 + bf16 计算 + 同源 skip）。"""
    pytest.importorskip("transformers")
    pytest.importorskip("torch")
    cfg = Q.build_bnb_config()
    assert cfg.load_in_4bit is True
    assert cfg.bnb_4bit_quant_type == "nf4"
    assert cfg.bnb_4bit_use_double_quant is True
    # 关键：skip 名单必须就是唯一来源那份（拷贝，不共享可变对象）。
    assert list(cfg.llm_int8_skip_modules) == Q.MODULES_TO_NOT_CONVERT


def test_skip_semantics_under_transformers_v5():
    """在 transformers≥5 上实测匹配语义：长路径跳过命门模块、裸名漏过、q_proj 仍量化。"""
    transformers = pytest.importorskip("transformers")
    major = int(transformers.__version__.split(".")[0])
    if major < 5:
        pytest.skip("仅在 transformers≥5（前缀/结尾语义）下校验")
    from transformers.quantizers.quantizers_utils import should_convert_module

    # 长路径名单：命门模块必须「不转换」。
    for mod in ("talker.text_projection.linear_fc1",
                "talker.text_projection.linear_fc2",
                "talker.code_predictor.lm_head.0",
                "talker.code_predictor.lm_head.14"):
        assert should_convert_module(mod, Q.MODULES_TO_NOT_CONVERT) is False, mod
    # 注意力投影仍要量化。
    assert should_convert_module(
        "talker.model.layers.0.self_attn.q_proj", Q.MODULES_TO_NOT_CONVERT) is True

    # 阴性对照：旧裸名清单在 v5 下确实漏过（这就是本次事故根因）。
    bare = ["speaker_encoder", "codec_embedding", "text_embedding",
            "codec_head", "text_projection", "lm_head"]
    assert should_convert_module("talker.text_projection.linear_fc1", bare) is True
    assert should_convert_module("talker.code_predictor.lm_head.0", bare) is True

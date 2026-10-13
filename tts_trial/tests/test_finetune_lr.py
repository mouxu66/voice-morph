# -*- coding: utf-8 -*-
"""微调 lr 量纲与标签对齐的回归测试（2026-10-13）。

背景（两件事一起守，因为它们都是「不报错、不警告、只是训不出来」的静默失效）：

  A. **lr 量纲错配**：`tts_trial/sft_8gb.py` 把官方的 AdamW 尺度 lr=2e-5
     喂给了 Adafactor（相对步长算法，原生尺度 1e-2）。叠加 bf16 权重存储后，
     单步位移落到 bf16 的 ulp 之下 ⇒ 权重原地不动，10 epoch loss 只动 0.05。
  B. **labels 双重 shift**：`ForCausalLMLoss` 内部已经 shift 一次
     （loss_utils.py:61-64），而脚本传的 labels 又手移过一次 ⇒ 教成预测 j+2。

本测试**不需要 torch / GPU**（与 test_qlora_quant_parity.py 同口径），
只做逻辑断言 + AST 守卫 —— 防止「改一处漏一处」再次发生。
"""
import ast
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TTS_TRIAL = os.path.dirname(_HERE)
if _TTS_TRIAL not in sys.path:
    sys.path.insert(0, _TTS_TRIAL)

import finetune_lr as FL  # noqa: E402


# ---------------------------------------------------------------- lr 常量

def test_adafactor_lr_is_native_scale():
    """Adafactor 的默认 lr 必须是它的原生尺度（1e-2），不是 AdamW 的 2e-5。"""
    assert FL.ADAFACTOR_LR == pytest.approx(1e-2)
    assert FL.ADAFACTOR_LR > FL.MIN_LR_FOR_BF16


def test_adamw_lr_differs_from_adafactor_lr():
    """两者必须不同 —— 若被"统一"成一个值，这次修复就白做了。"""
    assert FL.ADAMW_LR < FL.MIN_LR_FOR_BF16
    assert FL.ADAFACTOR_LR != FL.ADAMW_LR


def test_min_lr_matches_bf16_relative_precision():
    """下限的物理依据：单步相对变化要盖过 bf16 的 1 ulp（2^-8≈3.9e-3）。"""
    assert FL.MIN_LR_FOR_BF16 == pytest.approx(5e-3)
    assert 2.0 ** -8 < FL.MIN_LR_FOR_BF16 < 2.0 ** -7


# ---------------------------------------------------------------- check_lr

def test_check_lr_warns_on_production_value():
    """生产历史值 2e-5 必须触发告警（这正是被冻住的那次）。"""
    warn = FL.check_lr(2e-5, "Adafactor")
    assert warn, "lr=2e-5 + Adafactor 必须告警"
    assert "2e-05" in warn or "2e-5" in warn
    assert "AdamW" in warn


def test_check_lr_silent_on_native_value():
    assert FL.check_lr(FL.ADAFACTOR_LR, "Adafactor") is None


def test_check_lr_silent_on_adamw():
    """AdamW 下 2e-5 是完全正常的（官方 sft_12hz.py 就是这么用的）。"""
    assert FL.check_lr(2e-5, "AdamW") is None
    assert FL.check_lr(1e-5, "adamw") is None


def test_check_lr_boundary():
    """恰好等于下限时不该告警；差一点点就该告警。"""
    assert FL.check_lr(FL.MIN_LR_FOR_BF16, "Adafactor") is None
    assert FL.check_lr(FL.MIN_LR_FOR_BF16 * 0.99, "Adafactor") is not None


# ---------------------------------------------------------------- AST 守卫

SFT = os.path.join(_TTS_TRIAL, "sft_8gb.py")


def _sft_tree():
    with open(SFT, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def test_sft_imports_shared_lr_module():
    """训练脚本必须从 finetune_lr 取 lr —— 不许再手写魔法数（同 skip 名单的教训）。"""
    tree = _sft_tree()
    imported = {
        a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "finetune_lr"
        for a in node.names
    }
    assert "ADAFACTOR_LR" in imported, "sft_8gb.py 必须从 finetune_lr 导入 ADAFACTOR_LR"
    assert "check_lr" in imported, "sft_8gb.py 必须调用 check_lr 做自检"


def test_sft_lr_default_comes_from_shared_constant():
    """--lr 的默认值必须是 ADAFACTOR_LR 这个名字，不能是字面量 2e-5。"""
    tree = _sft_tree()
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--lr":
            continue
        found = True
        default = next((kw.value for kw in node.keywords if kw.arg == "default"), None)
        assert default is not None, "--lr 必须有 default"
        assert isinstance(default, ast.Name) and default.id == "ADAFACTOR_LR", (
            f"--lr 的默认值必须是 ADAFACTOR_LR（现在写的是 "
            f"{ast.dump(default)[:60]}）—— 硬编码数值正是本次事故的成因"
        )
        # 顺便钉死：不许退回 AdamW 尺度
        assert not (isinstance(default, ast.Constant) and default.value == 2e-5)
    assert found, "没找到 --lr 参数定义（脚本结构变了？）"


def test_sft_calls_check_lr_at_startup():
    """必须在训练开始前调用 check_lr，否则警告永远不会出现。"""
    src = open(SFT, encoding="utf-8").read()
    assert "check_lr(" in src, "sft_8gb.py 必须调用 check_lr"
    # 调用点应当在 argparse 之后（拿得到 args.lr）
    assert src.index("check_lr(") > src.index("args = parser.parse_args()")


def test_sft_labels_are_same_position_not_pre_shifted():
    """★ labels 必须是同位（[:, :-1]），不能是 [:, 1:]。

    `ForCausalLMLoss` 内部会做一次 `labels[..., 1:]`（transformers 5.x，
    loss_utils.py:61-64）。若这里再手移一次，模型被教成预测 j+2 —— 数值复验见
    `tts_trial/probe_shift_check.py`：同位 labels 的交叉熵 = 0.000000（与手工
    next-token 参照一致），而 `[:, 1:]` = 60.0。
    """
    tree = _sft_tree()
    # 找到 model.talker(...) 调用里的 labels= 实参
    labels_arg = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "talker"):
            continue
        for kw in node.keywords:
            if kw.arg == "labels":
                labels_arg = kw.value
    assert labels_arg is not None, "没找到 model.talker(...) 的 labels= 实参"

    seg = ast.unparse(labels_arg)
    assert "codec_0_labels" in seg, f"labels 应当取自 codec_0_labels，实际是 {seg}"
    assert ":-1" in seg, (
        f"labels 必须是同位切片 [:, :-1]（实际 {seg}）—— 传 [:, 1:] 会与 "
        f"ForCausalLMLoss 内部的 shift 叠加，教成预测 j+2"
    )
    assert "[, 1:]" not in seg.replace(" ", ""), f"labels 不能是 [:, 1:]（实际 {seg}）"

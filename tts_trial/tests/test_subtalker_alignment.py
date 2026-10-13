# -*- coding: utf-8 -*-
"""sub_talker 损失对齐的回归测试（2026-10-14）。

守的坑：`sft_8gb.py` 原来直接用第三方包 `forward_sub_talker_finetune()` 返回的
`sub_talker_loss` 作为 `0.3 * sub_talker_loss` 项。而那个函数内部把
`labels=codec_ids[:, 1:]` 交给 `ForCausalLMLoss`，后者**内部还会再 shift 一次**
（transformers 5.x，`loss_utils.py:61-64`）⇒ 实际在教模型预测 **k+2**，错位一格。

真模型裁判实测（`probe_subtalker_oracle.py`，3 条真实样本）：
    对齐 A（logits[k] → codec_{k+1}）: 7.4167  ← 低于随机线 ln(2048)=7.6246 ✅
    对齐 B（codec_{k+2} = 双重 shift）: 9.2917
    vendor 内部返回                    : 9.2614 ≈ B ⇒ 确认错位
修复后验收（`verify_subtalker_fix.py`）：新版比 vendor 低 **−1.89**（4/4 条样本）。

本测试**不需要 torch / GPU**：用 AST + 纯逻辑断言钉住「不许再用 vendor 的返回值」。
"""
import ast
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TTS_TRIAL = os.path.dirname(_HERE)
if _TTS_TRIAL not in sys.path:
    sys.path.insert(0, _TTS_TRIAL)

SFT = os.path.join(_TTS_TRIAL, "sft_8gb.py")


def _tree():
    with open(SFT, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _sub_talker_assignment():
    """找到 `... = ...forward_sub_talker_finetune(...)` 那个赋值节点。"""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and fn.attr == "forward_sub_talker_finetune":
                return node
    return None


def _unpack_names(node) -> list[str]:
    """取 `a, b = f()` 这种元组解包里的名字；不是元组就返回 []。"""
    for t in node.targets:
        if isinstance(t, ast.Tuple):
            return [e.id for e in t.elts if isinstance(e, ast.Name)]
    return []


def test_sft_still_calls_forward_sub_talker_finetune():
    """基本前提：脚本仍走这个函数取 logits（结构没被删掉）。"""
    assert _sub_talker_assignment() is not None, (
        "没找到 forward_sub_talker_finetune 的调用（脚本结构变了？）"
    )


def test_does_not_take_vendor_sub_talker_loss():
    """★ 核心回归：不许把 vendor 返回的 loss 当成 sub_talker 损失。

    vendor 的返回值是**错位一格**的（内部被 ForCausalLMLoss 多 shift 一次）。
    必须只取 logits（约定用 `_` 丢弃第二个返回值），自己算交叉熵。
    """
    node = _sub_talker_assignment()
    assert node is not None
    names = _unpack_names(node)
    assert len(names) == 2, (
        f"应当是 `logits, _ = ...` 两元解包，实际解包到 {names or '（非元组）'}"
    )
    assert names[1] == "_", (
        f"第二个返回值必须是 `_`（显式丢弃 vendor 的错位 loss），实际是 `{names[1]}`"
        " —— 它在教模型预测 k+2，见模块 docstring"
    )


def test_sub_talker_loss_is_recomputed_with_cross_entropy():
    """必须自己按 `codec_ids[:, 1:]` 重算交叉熵（对齐 A）。"""
    src = open(SFT, encoding="utf-8").read()
    assert "cross_entropy" in src, "缺少自算的交叉熵 —— sub_talker 损失没被修正"
    # 目标必须取 [:, 1:]（对齐 A：logits[k] → codec_{k+1}）
    assert "talker_codec_ids[:, 1:]".replace(" ", "") in src.replace(" ", ""), (
        "没找到 `talker_codec_ids[:, 1:]` —— 对齐 A 的目标应当是 codec_{k+1}"
    )


def test_no_leftover_vendor_loss_multiplication():
    """不许再出现 `0.3 * sub_talker_loss` 直接来自 vendor 的形态。

    修复后 `sub_talker_loss` 只应来自自算的 cross_entropy。
    """
    tree = _tree()
    # 找到所有对 sub_talker_loss 的赋值
    assigns = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "sub_talker_loss":
                    assigns.append(node)
    assert assigns, "没找到 sub_talker_loss 的赋值"
    # 至少有一个来自 cross_entropy；且不应有任何来自 forward_sub_talker_finetune
    from_ce = [n for n in assigns
               if "cross_entropy" in ast.unparse(n.value)]
    from_vendor = [n for n in assigns
                   if "forward_sub_talker_finetune" in ast.unparse(n.value)]
    assert from_ce, "sub_talker_loss 必须由 cross_entropy 计算（而非 vendor 返回值）"
    assert not from_vendor, (
        "sub_talker_loss 仍直接取自 forward_sub_talker_finetune 的返回值 —— 错位未修"
    )


@pytest.mark.parametrize("bad", [
    "sub_talker_logits, sub_talker_loss = m.forward_sub_talker_finetune(a, b)",
])
def test_detector_recognises_the_bug(bad):
    """牙齿测试：把旧写法喂进来时，上面那条判据**必须能识别出来**。

    （防「规则写坏了两边都空 = 白绿」，见 docs/犯错档案-工程.md §8.21。）
    """
    tree = ast.parse(bad)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call))
    names = _unpack_names(node)
    # 旧写法：第二个名字是 sub_talker_loss，不是 "_" ⇒ 判定为"取用了 vendor loss"
    assert len(names) == 2, f"牙齿测试自身写错：解包到 {names}"
    assert names[1] != "_", "牙齿测试自身写错了"
    assert "forward_sub_talker_finetune" in ast.unparse(node.value)
    # 并确认新写法（修复后）会被**放过**
    good = "sub_talker_logits, _ = m.forward_sub_talker_finetune(a, b)"
    gnames = _unpack_names(next(n for n in ast.walk(ast.parse(good))
                                if isinstance(n, ast.Assign)))
    assert gnames[1] == "_", "修复后的写法不该被判红"

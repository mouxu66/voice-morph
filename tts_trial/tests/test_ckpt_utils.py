# -*- coding: utf-8 -*-
"""checkpoint 落盘的回归测试（2026-10-13）。

守的是 `sft_8gb.py::_save_ckpt()` 那条路上的一个静默崩溃：
    目标目录已有同名文件时，旧写法 `os.link` → `except OSError: shutil.copy2`
    会 FileExistsError 后落到 copy2，而 Windows 对「目标是 src 的硬链接」报
    `PermissionError: [WinError 32]`，异常冒出 ⇒ 训练进程 rc=1。
    触发条件：同一音色**第二次训练**（或 C2 续训）—— 输出目录里还留着上一轮的
    `checkpoint-epoch-*`，而新训练从 epoch-0 重新编号。

不需要 torch / GPU；用 tmp_path 造目录树。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TTS_TRIAL = os.path.dirname(_HERE)
if _TTS_TRIAL not in sys.path:
    sys.path.insert(0, _TTS_TRIAL)

import ckpt_utils as CU  # noqa: E402


# ---------------------------------------------------------------- place_file

def test_place_file_links_when_target_absent(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "out" / "dst.bin"

    how = CU.place_file(src, dst)

    assert dst.read_bytes() == b"payload"
    # 同卷应当走硬链接；万一文件系统不支持，copy 也可接受
    assert how in ("linked", "copied")


def test_place_file_overwrites_existing_target(tmp_path):
    """★ 核心回归：目标已存在时必须能安全覆盖（旧写法在这里 WinError 32）。"""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new-content")
    dst = tmp_path / "dst.bin"
    dst.write_bytes(b"stale-content-from-previous-run")

    CU.place_file(src, dst)

    assert dst.read_bytes() == b"new-content"


def test_place_file_overwrites_existing_hardlink(tmp_path):
    """★ 最锋利的一格：目标已是 src 的硬链接（= 重训时的真实状态）。

    旧写法在这里必崩：os.link 报 FileExistsError → except 里 copy2 → WinError 32。
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"v1")
    dst = tmp_path / "dst.bin"
    os.link(src, dst)                      # 造出「目标 = src 的硬链接」

    CU.place_file(src, dst)                # 修复后应当不抛

    assert dst.read_bytes() == b"v1"
    assert not dst.is_symlink()


def test_place_file_is_idempotent(tmp_path):
    """连放三次都要成功 —— 对应「同一个 epoch 因故重跑」。"""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dst = tmp_path / "out" / "dst.bin"

    for _ in range(3):
        CU.place_file(src, dst)

    assert dst.read_bytes() == b"x"


def test_place_file_creates_missing_parent(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dst = tmp_path / "a" / "b" / "c" / "dst.bin"

    CU.place_file(src, dst)

    assert dst.exists()


# ---------------------------------------------------------------- link_base_tree

def _build_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "config.json").write_text('{"tts_model_type":"base"}', encoding="utf-8")
    (base / "model.safetensors").write_bytes(b"BASE-WEIGHTS")
    (base / "vocab.json").write_text("{}", encoding="utf-8")
    (base / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    sub = base / "speech_tokenizer"
    sub.mkdir()
    (sub / "config.json").write_text('{"sub":1}', encoding="utf-8")
    (sub / "model.safetensors").write_bytes(b"SPEECH-TOK")
    return base


def test_link_base_tree_skips_only_top_level_diff_files(tmp_path):
    """顶层 model.safetensors / config.json 跳过；**子目录同名文件必须照常铺**。

    （第一版曾把子目录的一起跳过，导致 speech_tokenizer/ 下缺文件。）
    """
    base = _build_base(tmp_path)
    out = tmp_path / "out"

    CU.link_base_tree(base, out)

    assert not (out / "config.json").exists(), "顶层 config.json 应跳过（调用方自己写补丁版）"
    assert not (out / "model.safetensors").exists(), "顶层 model.safetensors 应跳过"
    assert (out / "vocab.json").exists(), "普通顶层文件要铺"
    assert (out / ".gitattributes").exists(), "顶层其它文件也要铺"
    assert (out / "speech_tokenizer" / "config.json").exists(), "子目录同名文件不该被跳过"
    assert (out / "speech_tokenizer" / "model.safetensors").exists()


def test_link_base_tree_survives_rerun(tmp_path):
    """★ 核心回归：同一个输出目录连铺两次（= 重训 / 续训）不能崩。"""
    base = _build_base(tmp_path)
    out = tmp_path / "out"

    CU.link_base_tree(base, out)
    CU.link_base_tree(base, out)        # 旧写法在这里 WinError 32

    assert (out / "vocab.json").exists()


def test_link_base_tree_reports_counts(tmp_path):
    base = _build_base(tmp_path)
    stats = CU.link_base_tree(base, tmp_path / "out")

    assert stats["linked"] + stats["copied"] == 4      # vocab + .gitattributes + 子目录 2 个
    assert stats["linked"] >= 1, "同卷应尽量用硬链接（省磁盘）"


def test_link_base_tree_content_is_correct(tmp_path):
    """铺完之后内容要逐字节一致（硬链接是共享数据块，不是空壳）。"""
    base = _build_base(tmp_path)
    out = tmp_path / "out"

    CU.link_base_tree(base, out)

    assert (out / "vocab.json").read_text("utf-8") == "{}"
    assert (out / "speech_tokenizer" / "model.safetensors").read_bytes() == b"SPEECH-TOK"


# ---------------------------------------------------------------- 真脚本守卫

SFT = os.path.join(_TTS_TRIAL, "sft_8gb.py")


def test_sft_uses_shared_ckpt_helper():
    """sft_8gb.py 必须走 ckpt_utils，不许再手写 `except OSError: shutil.copy2`。

    那个手写写法正是本次崩溃的成因；用 AST/文本双保险防止改回去。
    """
    src = open(SFT, encoding="utf-8").read()
    assert "link_base_tree" in src, "sft_8gb.py 必须调用 ckpt_utils.link_base_tree"
    assert "shutil.copy2(f, target)" not in src, (
        "sft_8gb.py 里还留着会崩的手写 copy2 兜底 —— 请改用 ckpt_utils.place_file/link_base_tree"
    )

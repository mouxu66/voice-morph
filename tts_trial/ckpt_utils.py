# -*- coding: utf-8 -*-
"""checkpoint 落盘的小工具（无 torch 依赖，便于单测）。

为什么单独一个模块（2026-10-13）：
  `tts_trial/sft_8gb.py::_save_ckpt()` 用「硬链接基座文件 + 只写差异文件」来让
  8GB 卡上的全参 checkpoint 不额外吃磁盘。但它在**目标已存在**时会崩：

      os.link(src, dst)      → dst 已存在 ⇒ FileExistsError（OSError 子类，被 except 接住）
      shutil.copy2(src, dst) → dst 是 src 的硬链接时，Windows 报
                               PermissionError: [WinError 32] 另一个程序正在使用此文件

  两者叠加 ⇒ 异常直接冒出 `_save_ckpt`，**训练进程 rc=1**，而权重其实已经训好了。
  生产后果（`m2_server/finetune.py:448`）：状态机只看 `rc == 0 and ck`，
  于是整轮被标成 `error` —— 哪怕 10 个 epoch 全跑完、loss 也降得很好。

  触发条件很常见：**同一个音色第二次训练**（或 C2 续训）时，输出目录里还留着上一轮的
  `checkpoint-epoch-*`，而新训练又从 `epoch-0` 开始编号 ⇒ 撞名。
  实测复现见 `tts_trial/probe_ckpt_save.py`（连调两次 `_save_ckpt`，第二次必崩）。

修法：落盘前先把已存在的目标删掉，再尝试硬链接、退回复制。三种情形都覆盖：
    目标不存在        → 直接 link（或 copy）
    目标存在且非硬链接 → unlink 后 link
    目标存在且是硬链接 → unlink 后 link（**不能**直接 copy2，那正是 WinError 32）
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def place_file(src: str | Path, dst: str | Path) -> str:
    """把 `src` 落到 `dst`：优先硬链接（同卷零占用），失败退回复制。

    目标已存在时**先删再放**（重训/续训/断点续跑的常规情形）。
    返回实际采用的方式：`"linked"` 或 `"copied"`。
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # ★ 必须先删已存在的目标：见模块顶部说明（FileExistsError → copy2 → WinError 32）
    if dst.exists() or dst.is_symlink():
        dst.unlink()

    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        # 跨卷 / 文件系统不支持硬链接 / 其它 —— 退回复制
        shutil.copy2(src, dst)
        return "copied"


def link_base_tree(
    base_dir: str | Path,
    out_dir: str | Path,
    skip_top: tuple[str, ...] = ("model.safetensors", "config.json"),
) -> dict[str, int]:
    """把基座目录树铺进输出目录（硬链接优先），返回 {"linked": n, "copied": n}。

    `skip_top`：**仅在顶层**跳过的文件名 —— 这两个是「差异文件」，由调用方自己写：
        model.safetensors 会被训练出的权重替换；config.json 要打 custom_voice 补丁。
    子目录里的同名文件照常链接（第一版曾把子目录的一起跳过，导致 speech_tokenizer
    下的文件缺失）。
    """
    base, out = Path(base_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    stats = {"linked": 0, "copied": 0}
    for f in sorted(base.rglob("*")):
        rel = f.relative_to(base)
        target = out / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if rel.parent == Path(".") and f.name in skip_top:
            continue
        stats[place_file(f, target)] += 1
    return stats

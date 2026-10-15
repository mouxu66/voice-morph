# -*- coding: utf-8 -*-
"""隔离环境侧 runner：用 audio-separator 从混声里抠出**主唱**。

为什么是独立脚本而不是直接 import（2026-10-15）：
    `audio-separator` 依赖较新的 torch（实测 PyPI CPU 2.14），装进项目主 venv
    会与在役的 torchaudio 2.9.1+cu128 冲突、并把 torch 顶成 CPU 版（本机实测
    import torchaudio 直接崩）。所以它必须活在**独立 venv** 里，由产品后端用
    子进程调用 —— 与 RVC 推理 venv（`D:/RVC/.venv`）同一套隔离思路。

这个脚本只在那个独立 venv 内运行（产品代码在 `m2_server/lead_sep.py`）：
    <sep_venv>/python.exe tools/lead_sep_runner.py <model_dir> <model_file> <input.wav> <out_dir>

产物约定（写死，供后端解析）：
    <out_dir>/lead.wav        —— 主唱（Vocals stem）
    <out_dir>/backing.wav    —— 其余（Instrumental stem，含和声与伴奏）
退出码：0 成功；2 参数/环境错；3 模型缺失；4 分离失败。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _last_stem(name: str) -> str:
    """复合文件名里**最后一个** stem 标记才算数。

    串联/重跑时文件名可能是 `x_(Vocals)_A_(Instrumental)_B.wav`，
    简单的 `"(Vocals)" in name` 会被前半段骗到 —— 比较两个标记谁更靠后。
    """
    iv = name.rfind("(Instrumental)")
    vv = name.rfind("(Vocals)")
    return "Instrumental" if iv > vv else "Vocals"


def main() -> int:
    if len(sys.argv) != 5:
        sys.stderr.write("用法: lead_sep_runner.py <model_dir> <model_file> <input> <out_dir>\n")
        return 2
    # ★ model_file 必须保持**字符串**：audio-separator 用 `filename == model_filename`
    # 做字符串相等匹配，包成 Path 后 `Path("x")=="x"` 为 False（2026-10-15 实测踩坑）。
    model_dir = Path(sys.argv[1])
    model_file = sys.argv[2]
    inp = Path(sys.argv[3])
    out_dir = Path(sys.argv[4])

    if not inp.exists():
        sys.stderr.write(f"输入不存在: {inp}\n")
        return 2
    model_path = model_dir / model_file
    if not model_path.exists():
        sys.stderr.write(f"模型缺失: {model_path}\n")
        return 3
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from audio_separator.separator import Separator
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"独立环境缺少 audio-separator: {e!r}\n")
        return 2

    try:
        sep = Separator(
            output_dir=str(out_dir),
            output_format="wav",
            model_file_dir=str(model_dir),
            use_autocast=True,
            log_level=40,  # ERROR：正常不刷日志
        )
        sep.load_model(model_file)
        produced = sep.separate(str(inp))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"主唱分离失败: {e!r}\n")
        return 4

    # audio-separator 返回的可能是**相对文件名**（相对其 cwd，而非 out_dir），
    # 不能直接拿来 Path.replace —— 统一按"文件名落在 out_dir"解析成绝对路径。
    def _resolve(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else out_dir / q.name

    voc = [_resolve(p) for p in produced if _last_stem(Path(p).name) == "Vocals"]
    inst = [_resolve(p) for p in produced if _last_stem(Path(p).name) == "Instrumental"]
    if not voc or not voc[0].exists():
        sys.stderr.write(f"分离结果里没有主唱轨: {produced}\n")
        return 4
    lead = out_dir / "lead.wav"
    voc[0].replace(lead)
    backing = out_dir / "backing.wav"
    if inst and inst[0].exists():
        inst[0].replace(backing)
    print(str(lead))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

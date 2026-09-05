# -*- coding: utf-8 -*-
"""Seed-VC 接口默认参数对齐测试。

背景（2026-09-05 优化项①）：experiments/seedvc_param_sweep.py 实测 + 前端默认
sim=0.5 时音色相似度最高（CAM++ 0.807）且漏源最低；后端 /seedvc/run 与
run_conversion 原默认 0.7，前端不传参时（post_seedvc 等场景）会用到与最优经验
不一致的值。本测试锁死「默认=0.5」契约，防止默认值回退或前后端再次漂移。
"""
import inspect
from pathlib import Path

import seed_vc


def _form_default(func, name):
    """取 FastAPI Form() 参数的实际默认值（Form 对象 → .default）。"""
    p = inspect.signature(func).parameters[name]
    return getattr(p.default, "default", p.default)


def test_seedvc_run_form_defaults_aligned():
    """/seedvc/run Form 默认 similarity_cfg_rate=0.5（与前端/实验最优对齐）。"""
    assert _form_default(seed_vc.seedvc_run, "similarity_cfg_rate") == 0.5


def test_run_conversion_defaults_aligned():
    """run_conversion 默认 similarity_cfg_rate=0.5（offline_vc post_seedvc 复用同一契约）。"""
    assert inspect.signature(seed_vc.run_conversion).parameters["similarity_cfg_rate"].default == 0.5


def test_run_conversion_cfm_ckpt_optional():
    """cfm_checkpoint_path 必须可选默认 None——offline_vc post_seedvc 不传时保持零样本行为不变。"""
    params = inspect.signature(seed_vc.run_conversion).parameters
    assert params["cfm_checkpoint_path"].default is None
    assert "cfm_checkpoint_path" in inspect.signature(seed_vc._seedvc_worker).parameters


def test_ft_ckpt_kangaroo_resolves():
    """kangaroo 音色必须命中 73 条自录切片微调的 CFM 检查点（不存在则测试环境不完整）。"""
    ck = seed_vc._ft_ckpt("kangaroo")
    assert ck is not None and isinstance(ck, Path), "kangaroo 微调产物缺失：需先跑 seed_vc_repo/train_v2.py"
    assert ck.name.startswith("CFM_") and ck.exists()


def test_ft_ckpt_unknown_silent():
    """未知音色/无微调目录时必须静默回落零样本（None），不抛错。"""
    assert seed_vc._ft_ckpt("no_such_voice_xyz") is None
    assert seed_vc._ft_ckpt("") is None


def test_run_conversion_passes_ckpt_to_cmd(monkeypatch, tmp_path):
    """传 cfm_checkpoint_path 时子进程 cmd 必须带 --cfm-checkpoint-path（接线锁定）。"""
    import subprocess

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        (tmp_path / "out.wav").write_bytes(b"x")  # 模拟推理产物
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ckpt = tmp_path / "fake_cfm.pth"
    seed_vc.run_conversion(tmp_path / "in_src.wav", tmp_path / "in_tgt.wav", tmp_path,
                           cfm_checkpoint_path=ckpt)
    assert str(ckpt) in captured["cmd"]
    assert "--cfm-checkpoint-path" in captured["cmd"]
    # 不传时保持零样本命令（无该参数）
    def fake_run_no_ckpt(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        (tmp_path / "out2.wav").write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_no_ckpt)
    seed_vc.run_conversion(tmp_path / "in_src.wav", tmp_path / "in_tgt.wav", tmp_path)
    assert "--cfm-checkpoint-path" not in captured["cmd"]


def test_ft_runs_bounded():
    """自定义微调音色数量受 SEEDVC_FT_MAX_RUNS 约束（防止误配膨胀）。"""
    assert 0 < len(seed_vc.SEEDVC_FT_RUNS) <= seed_vc.SEEDVC_FT_MAX_RUNS
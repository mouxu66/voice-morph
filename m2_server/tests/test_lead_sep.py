# -*- coding: utf-8 -*-
"""`m2_server/lead_sep.py`（主唱分离产品后端）的守卫。

重点不是分离质量（那要耳朵 + media 产物，见 m1k），而是**集成正确性**：
能力可选、缺环境/缺模型/失败三类错误分得清（前端要给不同提示）、子进程命令
把路径与模型名传对、成功后以 lead.wav 为准。任何一个错了产品侧都会静默走错分支。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lead_sep  # noqa: E402


# ------------------------------------------------------- 错误码分类（纯函数）

def test_classify_returncode_maps_each_rc():
    e = lead_sep.classify_returncode(lead_sep.RC_MODEL, "模型缺失: x")
    assert e.kind is lead_sep.LeadSepError.Kind.MODEL_MISSING

    e = lead_sep.classify_returncode(lead_sep.RC_ENV, "no audio-separator")
    assert e.kind is lead_sep.LeadSepError.Kind.ENV_MISSING

    e = lead_sep.classify_returncode(lead_sep.RC_SEP, "boom")
    assert e.kind is lead_sep.LeadSepError.Kind.FAILED
    assert "boom" in str(e)


def test_classify_returncode_unknown_is_failure():
    e = lead_sep.classify_returncode(99, "")
    assert e.kind is lead_sep.LeadSepError.Kind.FAILED
    assert "99" in str(e)


# ------------------------------------------------------- 环境探测

def test_status_reports_missing_pieces(monkeypatch, tmp_path):
    fake_root = tmp_path / "RVC"
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", fake_root, raising=False)
    # runner 真实存在（runtime.ROOT 指向仓库），只让 venv/模型缺失
    st = lead_sep.status("m.onnx")
    assert st["available"] is False
    assert "venv" in " ".join(st["missing"]) or "环境" in " ".join(st["missing"])
    assert st["model_exists"] is False


def test_status_ready_when_all_present(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    venv_py = root / ".venv_sep/Scripts/python.exe"
    model = root / "sep_models/m.onnx"
    venv_py.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    venv_py.write_text("x")
    model.write_text("y")
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)

    st = lead_sep.status("m.onnx")
    assert st["model_exists"] is True
    # runner 在真实仓库 tools/ 下；测试机上它应当存在 → 整体 ready
    assert st["available"] is lead_sep.runner_path().exists()


def test_require_ready_raises_by_kind(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)
    # 都没有 → ENV_MISSING
    with pytest.raises(lead_sep.LeadSepError) as ei:
        lead_sep.require_ready("m.onnx")
    assert ei.value.kind is lead_sep.LeadSepError.Kind.ENV_MISSING

    # 有 venv 没模型 → MODEL_MISSING
    py = root / ".venv_sep/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    with pytest.raises(lead_sep.LeadSepError) as ei:
        lead_sep.require_ready("m.onnx")
    assert ei.value.kind is lead_sep.LeadSepError.Kind.MODEL_MISSING


# ------------------------------------------------------- extract_lead（mock 子进程）

class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_extract_lead_builds_correct_command_and_returns_lead(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    py = root / ".venv_sep/Scripts/python.exe"
    model = root / "sep_models/m.onnx"
    py.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    py.write_text("x")
    model.write_text("x")
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)
    # runner 用真实仓库的（存在）
    assert lead_sep.runner_path().exists()

    src = tmp_path / "vocals.wav"
    src.write_text("audio")
    out = tmp_path / "leadout"

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        out.mkdir(parents=True, exist_ok=True)
        (out / "lead.wav").write_text("lead")  # runner 产出
        return _Proc(0)

    monkeypatch.setattr(lead_sep.subprocess, "run", fake_run)

    lead = lead_sep.extract_lead(src, out, model="m.onnx", timeout_s=123)
    assert lead == out / "lead.wav"
    # 命令：<sep venv py> <runner> <model_dir> <model 字符串名> <input> <out>
    assert captured["cmd"][0] == str(py)
    assert captured["cmd"][1].endswith("lead_sep_runner.py")
    assert captured["cmd"][2] == str(root / "sep_models")
    assert captured["cmd"][3] == "m.onnx"          # ★ 必须是 str，不能是 Path
    assert captured["cmd"][4] == str(src)
    assert captured["cmd"][5] == str(out)
    assert captured["cwd"] == str(root)            # 跟随 RVC_ROOT


def test_extract_lead_maps_runner_rc_to_error(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    py = root / ".venv_sep/Scripts/python.exe"
    model = root / "sep_models/m.onnx"
    py.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    py.write_text("x"); model.write_text("x")
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)

    monkeypatch.setattr(lead_sep.subprocess, "run",
                        lambda *a, **k: _Proc(lead_sep.RC_MODEL, stderr="模型缺失"))
    with pytest.raises(lead_sep.LeadSepError) as ei:
        lead_sep.extract_lead(tmp_path / "v.wav", tmp_path / "o", model="m.onnx")
    assert ei.value.kind is lead_sep.LeadSepError.Kind.MODEL_MISSING


def test_extract_lead_missing_output_is_failure(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    py = root / ".venv_sep/Scripts/python.exe"
    model = root / "sep_models/m.onnx"
    py.parent.mkdir(parents=True); model.parent.mkdir(parents=True)
    py.write_text("x"); model.write_text("x")
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)
    monkeypatch.setattr(lead_sep.subprocess, "run", lambda *a, **k: _Proc(0))  # rc=0 但没产物

    with pytest.raises(lead_sep.LeadSepError) as ei:
        lead_sep.extract_lead(tmp_path / "v.wav", tmp_path / "o", model="m.onnx")
    assert ei.value.kind is lead_sep.LeadSepError.Kind.FAILED


def test_extract_lead_timeout_is_failure(monkeypatch, tmp_path):
    root = tmp_path / "RVC"
    py = root / ".venv_sep/Scripts/python.exe"
    model = root / "sep_models/m.onnx"
    py.parent.mkdir(parents=True); model.parent.mkdir(parents=True)
    py.write_text("x"); model.write_text("x")
    monkeypatch.setattr(lead_sep.cfg, "RVC_ROOT", root, raising=False)

    def boom(*a, **k):
        raise lead_sep.subprocess.TimeoutExpired(k.get("cmd"), k.get("timeout"))

    monkeypatch.setattr(lead_sep.subprocess, "run", boom)
    with pytest.raises(lead_sep.LeadSepError) as ei:
        lead_sep.extract_lead(tmp_path / "v.wav", tmp_path / "o", model="m.onnx")
    assert ei.value.kind is lead_sep.LeadSepError.Kind.FAILED

# -*- coding: utf-8 -*-
"""`cover_bwe.py` 的守卫（不需要 GPU / 权重 / ffmpeg）。

这一步最贵的两类错都不会报错、只表现为"听感不对"或"白等"：
1. **默认被打开** → 每个翻唱都多花几秒且换了一版听感（实验能力必须显式开）；
2. **装到一半也照跑** → 权重在、依赖缺（跑了 `--no-deps` 却没补 timm/torchdiffeq）时
   子进程会在 import 就炸，而用户看到的是"翻唱失败"，不是"BWE 缺依赖"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cover_bwe as bwe  # noqa: E402


# ---------------------------------------------------------------- 开关

def test_default_is_off(monkeypatch):
    monkeypatch.delenv("VM_COVER_BWE", raising=False)
    assert bwe.enabled() is False
    assert bwe.enabled(False) is False


def test_request_turns_it_on(monkeypatch):
    monkeypatch.delenv("VM_COVER_BWE", raising=False)
    assert bwe.enabled(True) is True


@pytest.mark.parametrize("val", ["0", "false", "OFF", "no"])
def test_env_can_force_off_even_when_requested(monkeypatch, val):
    monkeypatch.setenv("VM_COVER_BWE", val)
    assert bwe.enabled(True) is False


@pytest.mark.parametrize("val", ["1", "true", "ON", "yes"])
def test_env_can_force_on_for_ab(monkeypatch, val):
    monkeypatch.setenv("VM_COVER_BWE", val)
    assert bwe.enabled(False) is True


def test_env_junk_falls_back_to_request(monkeypatch):
    monkeypatch.setenv("VM_COVER_BWE", "maybe")
    assert bwe.enabled(False) is False and bwe.enabled(True) is True


# ---------------------------------------------------------------- 能力探测

def _fake_env(monkeypatch, tmp_path: Path, *, weights: bool, deps: bool, runner: bool = True):
    """把探测面全部指向 tmp（真跑一次探测，不 mock 掉 available 本身）。"""
    monkeypatch.setattr(bwe, "WEIGHTS", tmp_path / "ckpts")
    monkeypatch.setattr(bwe, "VENV_PY", tmp_path / "python.exe")
    monkeypatch.setattr(bwe, "RUNNER", tmp_path / "runner.py")
    bwe.VENV_PY.write_text("", encoding="utf-8")
    if runner:
        bwe.RUNNER.write_text("", encoding="utf-8")
    if weights:
        bwe.WEIGHTS.mkdir(parents=True, exist_ok=True)
        (bwe.WEIGHTS / "config.yaml").write_text("", encoding="utf-8")
        (bwe.WEIGHTS / "pytorch_model.bin").write_bytes(b"x")
    monkeypatch.setattr(bwe, "_importable", lambda mod: deps)


def test_available_reports_every_missing_piece(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path, weights=False, deps=False, runner=False)
    st = bwe.available()
    assert st["available"] is False
    joined = " ".join(st["missing"])
    assert "pytorch_model.bin" in joined and "runner.py" in joined and "torchdiffeq" in joined


def test_available_true_when_everything_present(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path, weights=True, deps=True)
    st = bwe.available()
    assert st["available"] is True and st["missing"] == []
    assert st["input_sr"] == 16000  # 有效带宽口径：Nyquist 8k = RVC 干声实况


# ---------------------------------------------------------------- 执行

def test_enhance_refuses_when_unavailable_without_launching(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path, weights=False, deps=True)
    calls: list = []
    monkeypatch.setattr(bwe.subprocess, "run", lambda *a, **k: calls.append(a))
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF")
    with pytest.raises(RuntimeError) as e:
        bwe.enhance_in_place(wav)
    assert "不可用" in str(e.value)
    assert calls == [], "不可用时不许起子进程（会白等一次模型加载）"


def test_enhance_writes_backup_and_keeps_sample_rate(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path, weights=True, deps=True)
    wav = tmp_path / "vocal.wav"
    wav.write_bytes(b"OLD")
    monkeypatch.setattr(bwe, "_sr_of", lambda p: 40000)
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # 跑手：写 48k 产物 + 打印一行 JSON
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(b"NEW48")
        return type("R", (), {"returncode": 0, "stdout": '{"ok": true, "run_s": 3.5}\n',
                              "stderr": ""})()

    def fake_resample(src, dst, sr):
        seen["resample"] = (Path(src).name, Path(dst).name, sr)
        Path(dst).write_bytes(b"NEW40")
        return Path(dst)

    monkeypatch.setattr(bwe.subprocess, "run", fake_run)
    monkeypatch.setattr(bwe, "_to_sr", fake_resample)

    info = bwe.enhance_in_place(wav, input_sr=16000, seed=7)

    assert wav.read_bytes() == b"NEW40"                       # 就地替换
    assert (tmp_path / "vocal.pre_bwe.wav").read_bytes() == b"OLD"  # 备份在（可回退）
    assert seen["resample"] == ("vocal.bwe48.wav", "vocal.wav", 40000)
    cmd = seen["cmd"]
    assert "--input-sr" in cmd and cmd[cmd.index("--input-sr") + 1] == "16000"
    assert cmd[cmd.index("--seed") + 1] == "7"                # 播种必须传下去
    assert str(bwe.RUNNER) in cmd and str(bwe.VENV_PY) in cmd
    assert info["run_s"] == 3.5 and info["sr"] == 40000


def test_enhance_surfaces_runner_stderr(monkeypatch, tmp_path):
    """跑手失败时，报错里必须带上它的 stderr（否则排障只能靠猜）。"""
    _fake_env(monkeypatch, tmp_path, weights=True, deps=True)
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"OLD")
    monkeypatch.setattr(bwe, "_sr_of", lambda p: 40000)
    monkeypatch.setattr(bwe.subprocess, "run", lambda cmd, **kw: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError: torchdiffeq"})())
    with pytest.raises(RuntimeError) as e:
        bwe.enhance_in_place(wav)
    assert "torchdiffeq" in str(e.value)
    assert wav.read_bytes() == b"OLD", "失败时不许动原文件"

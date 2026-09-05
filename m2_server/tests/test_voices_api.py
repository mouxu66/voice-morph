"""voices_api.delete_voice 单测（删除音色：重试 / 真实报错 / selected 悬空清理）。

回归背景（2026-09-05）：旧实现 shutil.rmtree(d, True)（ignore_errors=True）在
Windows 文件被 worker 占用时静默失败仍返回 ok:true，用户在 UI 看到"删除失败"
却无任何错误原因。
"""
import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

import voices_api  # noqa: E402


@pytest.fixture()
def voicebank(tmp_path, monkeypatch):
    """构造临时 voicebank 并把 voices_api.VOICEBANK / cfg.MEDIA_DIR 指过去。"""
    vb = tmp_path / "voicebank"
    vb.mkdir()
    monkeypatch.setattr(voices_api, "VOICEBANK", vb)
    monkeypatch.setattr(voices_api.cfg, "MEDIA_DIR", tmp_path)
    return vb


def _make_voice(vb: Path, vid: str, *, selected: str | None = None):
    d = vb / vid
    (d / "ft_model").mkdir(parents=True)
    (d / "reference.wav").write_bytes(b"RIFFfake")
    (d / "ft_model" / "model.safetensors").write_bytes(b"x" * 32)
    if selected is not None:
        (vb / "selected_voice.json").write_text(
            json.dumps({"voice_id": selected}), encoding="utf-8")


# ---------------- 正常删除 ----------------

def test_delete_ok_removes_dir(voicebank):
    _make_voice(voicebank, "v1")
    resp = asyncio_run(voices_api.delete_voice("v1"))
    assert resp == {"ok": True}
    assert not (voicebank / "v1").exists()


def test_delete_clears_dangling_selected(voicebank):
    """删除当前选中音色时，selected_voice.json 必须被清掉（防悬空引用）。"""
    _make_voice(voicebank, "v1", selected="v1")
    assert (voicebank / "selected_voice.json").exists()
    asyncio_run(voices_api.delete_voice("v1"))
    assert not (voicebank / "selected_voice.json").exists()


def test_delete_keeps_selected_of_other_voice(voicebank):
    """删除非选中音色时，selected_voice.json 保持不动。"""
    _make_voice(voicebank, "v1")
    _make_voice(voicebank, "v2", selected="v2")
    asyncio_run(voices_api.delete_voice("v1"))
    assert json.loads((voicebank / "selected_voice.json").read_text("utf-8")) == {
        "voice_id": "v2"}


# ---------------- 异常路径 ----------------

def test_delete_404_when_missing(voicebank):
    with pytest.raises(HTTPException) as ei:
        asyncio_run(voices_api.delete_voice("ghost"))
    assert ei.value.status_code == 404


def test_delete_400_when_invalid_id(voicebank):
    with pytest.raises(HTTPException) as ei:
        asyncio_run(voices_api.delete_voice("../etc"))
    assert ei.value.status_code == 400


def test_delete_500_reports_locked_files(voicebank, monkeypatch):
    """文件被占用：rmtree 三次重试全失败 → 500 + 报错点名被锁文件（不再吞错）。"""
    _make_voice(voicebank, "v1")
    calls = {"n": 0}

    def _locked_rmtree(path, *a, **kw):
        calls["n"] += 1
        raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")

    monkeypatch.setattr(voices_api.shutil, "rmtree", _locked_rmtree)
    monkeypatch.setattr(voices_api.asyncio, "sleep", _noop_await)
    with pytest.raises(HTTPException) as ei:
        asyncio_run(voices_api.delete_voice("v1"))
    assert ei.value.status_code == 500
    assert calls["n"] == 3                       # 重试 3 次
    assert "model.safetensors" in ei.value.detail  # 点名被锁文件
    assert (voicebank / "v1").exists()           # 目录保留，不静默半删


def test_delete_recovers_after_transient_lock(voicebank, monkeypatch):
    """句柄短暂占用：第 2 次重试成功 → 返回 ok（体现重试的价值）。"""
    _make_voice(voicebank, "v1")
    real_rmtree = shutil.rmtree
    state = {"n": 0}

    def _flaky_rmtree(path, *a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise PermissionError(32, "暂时占用")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(voices_api.shutil, "rmtree", _flaky_rmtree)
    monkeypatch.setattr(voices_api.asyncio, "sleep", _noop_await)
    resp = asyncio_run(voices_api.delete_voice("v1"))
    assert resp == {"ok": True}
    assert state["n"] == 2
    assert not (voicebank / "v1").exists()


# ---------------- helpers ----------------

def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


async def _noop_await(*a, **kw):
    return None

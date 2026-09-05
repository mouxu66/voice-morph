"""voices_api.delete_voice 单测（删除音色：重试 / 真实报错 / selected 悬空清理）。

回归背景（2026-09-05）：旧实现 shutil.rmtree(d, True)（ignore_errors=True）在
Windows 文件被 worker 占用时静默失败仍返回 ok:true，用户在 UI 看到"删除失败"
却无任何错误原因。
"""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

import voices_api  # noqa: E402


@pytest.fixture()
def voicebank(tmp_path, monkeypatch):
    """整体替换 voices_api.cfg 为受控命名空间（含临时 RVC_ROOT）。

    不 patch 全局 config 模块的属性，避免其他测试 / 导入顺序对模块状态的干扰。"""
    vb = tmp_path / "voicebank"
    vb.mkdir()
    rvc_root = tmp_path / "rvc"
    rvc_root.mkdir()
    monkeypatch.setattr(voices_api, "VOICEBANK", vb)
    monkeypatch.setattr(voices_api, "cfg",
                        SimpleNamespace(MEDIA_DIR=tmp_path, RVC_ROOT=rvc_root))
    # common.selected_voice() 读全局 config.MEDIA_DIR——一并指向 tmp，
    # 否则它读真实 voicebank 的 selected_voice.json，清悬空逻辑测不到
    monkeypatch.setattr(sys.modules["config"], "MEDIA_DIR", tmp_path)
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
    assert resp == {"ok": True, "source": "voicebank"}
    assert not (voicebank / "v1").exists()


def test_delete_rvc_logs_entry(voicebank, monkeypatch):
    """音色列表合并了 RVC 实验目录来源（kind=rvc_model）：voicebank 里没有的
    条目（如旧模型 kangaroo_clean）必须路由到 RVC_ROOT/logs/<id> 实验目录删除，
    而不是 404"删除失败"。

    真建临时目录（让 exists() 检查通过）+ 记录式 rmtree（不真删），
    与环境的 safe-delete 钩子完全解耦。"""
    exp = voices_api.cfg.RVC_ROOT / "logs" / "old_exp"
    exp.mkdir(parents=True)
    calls: list[Path] = []

    def _rec_rmtree(path, *a, **kw):
        calls.append(Path(path))

    monkeypatch.setattr(voices_api.shutil, "rmtree", _rec_rmtree)
    resp = asyncio_run(voices_api.delete_voice("old_exp"))
    assert resp == {"ok": True, "source": "rvc_logs"}
    assert calls == [exp]
    assert voices_api.VOICEBANK not in calls[0].parents  # 没误指 voicebank


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
    assert resp["ok"] is True
    assert state["n"] == 2
    assert not (voicebank / "v1").exists()


# ---------------- helpers ----------------

def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


async def _noop_await(*a, **kw):
    return None

"""市场音色自动试听（A2）：状态机 / 源句缓存 / GPU 忙 skipped / 推理成功。

全部本地完成：源句 TTS 用 monkeypatch 桩，RVC 推理子进程用 stub 替代，
不碰真实网络、不用真实 GPU。
"""
import json
import time

import pytest

import config
import market_preview as mp


@pytest.fixture()
def market_dir(tmp_path, monkeypatch):
    """隔离 outputs/market：避免测试间通过模块级 MARKET_DIR 互相污染。"""
    d = tmp_path / "market"
    d.mkdir(parents=True)
    monkeypatch.setattr(mp, "MARKET_DIR", d)
    return d


@pytest.fixture()
def rvc_tmp(tmp_path, monkeypatch):
    """RVC 根目录 + 伪 .venv + 权重，落位一个可推理的市场音色。"""
    rvc = tmp_path / "rvc"
    (rvc / "assets" / "weights").mkdir(parents=True)
    idx = rvc / "logs" / "demo_voice" / "added_demo_voice.index"
    idx.parent.mkdir(parents=True)
    idx.write_bytes(b"\x00")
    pth = rvc / "assets" / "weights" / "demo_voice.pth"
    pth.write_bytes(b"\x80\x02" + b"\x00" * 64)
    (rvc / ".venv" / "Scripts").mkdir(parents=True)
    (rvc / ".venv" / "Scripts" / "python.exe").write_bytes(b"MZ")
    monkeypatch.setattr(config, "RVC_ROOT", rvc)
    monkeypatch.setattr(mp, "RVC_VENV_PY", rvc / ".venv" / "Scripts" / "python.exe")
    return rvc


def _wait_inflight(voice_id: str, timeout: float = 5.0):
    """等待后台生成线程结束（避免 daemon 线程与 monkeypatch 还原竞态）。"""
    deadline = time.time() + timeout
    while voice_id in mp._inflight and time.time() < deadline:
        time.sleep(0.05)


@pytest.fixture()
def fake_voicebank(tmp_path, monkeypatch):
    vb = tmp_path / "voicebank"
    d = vb / "vb_demo"
    d.mkdir(parents=True)
    (d / "reference.wav").write_bytes(b"RIFFfake")
    monkeypatch.setattr(mp, "VOICEBANK", vb)
    return vb


def test_status_missing_for_unknown_voice(rvc_tmp, market_dir):
    """未生成过、无 pth 的音色 → missing。"""
    st = mp.status("nope")
    assert st["status"] == "missing" and st["url"] == ""


def test_preview_no_rvc_venv_marks_failed(tmp_path, market_dir, monkeypatch):
    """RVC 运行环境缺失 → failed 且带可读原因。"""
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "no_python.exe")
    mp._do_generate("x_voice")
    st = mp.status("x_voice")
    assert st["status"] == "failed"
    assert "RVC" in st["error"]
    assert not (mp.MARKET_DIR / "x_voice_preview.wav").exists()


def test_preview_gpu_busy_marks_skipped(tmp_path, market_dir, monkeypatch):
    """实时/离线变声占用 RVC 环境 → skipped，不静默排队（前端可手动重试）。"""
    (tmp_path / "assets" / "weights").mkdir(parents=True)
    (tmp_path / "assets" / "weights" / "b_voice.pth").write_bytes(b"\x80\x02abc")
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "py.exe")
    monkeypatch.setattr(mp, "_gpu_busy", lambda: "离线变声任务正在运行")
    mp._do_generate("b_voice")
    st = mp.status("b_voice")
    assert st["status"] == "skipped"
    assert "离线变声任务正在运行" in st["error"]


def test_ensure_source_caches_tts_once(tmp_path, fake_voicebank, market_dir, monkeypatch):
    """源句缓存：首次用第一个 voicebank 参考合成并缓存，二次不重复调 TTS。"""
    calls = {"n": 0}

    def fake_tts(**kw):
        calls["n"] += 1
        import soundfile as sf
        import numpy as np
        p = tmp_path / "gen.wav"
        sf.write(str(p), np.zeros(16000, dtype=np.float32), 16000)
        return p.read_bytes()

    import qwen3_tts
    monkeypatch.setattr(qwen3_tts, "tts", fake_tts)

    src1 = mp._ensure_source()
    src2 = mp._ensure_source()
    assert src1 == src2 and src1.exists()
    assert calls["n"] == 1, "源句应只合成一次并缓存"
    assert src1.stat().st_size > 0


def test_ensure_source_fails_without_voicebank(tmp_path, market_dir, monkeypatch):
    """无任何 voicebank 且无缓存 → 可读原因，而不是裸异常。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(mp, "VOICEBANK", empty)
    with pytest.raises(RuntimeError, match="参考音色"):
        mp._ensure_source()


def test_generate_success_marks_ready(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """完整链路（stub 推理子进程）→ 试听落盘，status=ready 且 url 可访问。"""
    import soundfile as sf
    import numpy as np

    (mp.MARKET_DIR / "_preview_src.wav").write_bytes(b"RIFFsrc")  # 预置源句，避免触碰真实 TTS worker

    def fake_run(cmd, **kw):  # stub：模拟推理子进程写出 wav
        out = cmd[cmd.index("--output") + 1]
        sf.write(out, np.sin(np.linspace(0, 100, 16000 * 3)).astype(np.float32), 16000)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    st = mp.status("demo_voice")
    assert st["status"] == "ready"
    assert st["url"].endswith("demo_voice_preview.wav")
    assert st["url"].startswith("/api/media/")
    sc = json.loads(mp._sidecar("demo_voice").read_text("utf-8"))
    assert sc["status"] == "ready"


def test_generate_dedupe_via_inflight(rvc_tmp, market_dir):
    """同一音色重复触发：第二次直接返 generating，不重复起线程。"""
    mp._inflight.add("demo_voice")
    st = mp.generate("demo_voice")
    assert st["status"] == "generating"
    mp._inflight.discard("demo_voice")


def test_generate_ready_short_circuits_inflight(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """已 ready 的音色即使线程/重装也不重复触发。"""
    import soundfile as sf
    import numpy as np

    (mp.MARKET_DIR / "_preview_src.wav").write_bytes(b"RIFFsrc")

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        sf.write(out, np.zeros(16000, dtype=np.float32), 16000)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")           # 造 ready
    mp._inflight.add("demo_voice")
    st = mp.generate("demo_voice")          # ready 优先 → 直接返回，不进 inflight 分支
    assert st["status"] == "ready"
    mp._inflight.discard("demo_voice")


def test_try_auto_preview_never_raises(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """安装收尾触发不应抛异常（含异常路径）；线程快速终结不残留 inflight。"""
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "no_python.exe")  # 无环境 → 线程快速 failed
    mp.try_auto_preview("demo_voice")
    mp.try_auto_preview("")            # 空 id 也不抛
    _wait_inflight("demo_voice")
    _wait_inflight("")
    assert mp._inflight == set()
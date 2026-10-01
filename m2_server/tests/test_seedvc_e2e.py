"""Seed-VC HTTP 端到端：POST /api/seedvc/run → worker 线程 → GET /api/seedvc/status。

盘点缺口（docs/待办盘点-2026-10-01.md §六）：此前只有「参数签名对账」与
run_conversion/build_cmd 的纯函数单测，**没有一条从路由进去、走完 worker、
再从 status 读出结果的完整链路**。「接线」错误（worker 少传一个旋钮、状态机
没翻转、URL 拼错）恰好是单测拦不住的那类 —— 每一层单独看都是对的。

桩掉的只有两样**重外部依赖**（端到端不需要真跑模型）：
  · `_preprocess`     —— 会 spawn ffmpeg（ci-fidelity 刻意把 ffmpeg 摘出 PATH，
                          硬调就是 5ccb11c 修的那类假红）；
  · `run_conversion`  —— 会拉起真实推理子进程（几十秒 + 显卡）。
其余全真：TestClient 打**真实 app**（路由挂载、Form 解析、互斥检查、状态机、
会话目录、soundfile 读时长都是真的）—— 与 test_audition_api.py 同一手法。
"""

import time
import wave
from pathlib import Path

import pytest

import seed_vc

# ---------------- 素材与桩 ----------------


def _wav_bytes(seconds: float = 1.0, sr: int = 16000, fill: bytes = b"\x00\x00") -> bytes:
    """16-bit 单声道 PCM（标准库 wave，不 spawn ffmpeg —— 同 5ccb11c 的教训）。

    `fill` 给可辨识的样本值：让「参考音字节没流到 worker」这类回归能被
    内容断言抓住（而不是只看文件存在）。
    """
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(fill * int(sr * seconds))
    return buf.getvalue()


#: 上传参考音的样本填充 —— 与主文件（静音）可区分
_TGT_FILL = b"\x7f\x7f"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每条用例一份干净桩环境；SEEDVC_STATE 是模块级全局，进出都要复原。"""
    saved = dict(seed_vc.SEEDVC_STATE)
    seed_vc.SEEDVC_STATE.update(running=False, status="idle", error="", url="")

    venv_py = tmp_path / "fake_venv" / "python.exe"
    venv_py.parent.mkdir()
    venv_py.write_bytes(b"x")
    infer_v2 = tmp_path / "inference_v2.py"
    infer_v2.write_bytes(b"x")
    infer_f0 = tmp_path / "inference.py"
    infer_f0.write_bytes(b"x")
    monkeypatch.setattr(seed_vc, "SEEDVC_VENV_PY", venv_py)
    monkeypatch.setattr(seed_vc, "SEEDVC_INFER_V2", infer_v2)
    monkeypatch.setattr(seed_vc, "SEEDVC_INFER_F0", infer_f0)
    monkeypatch.setattr(seed_vc, "_live_running", lambda: False)
    monkeypatch.setattr(seed_vc, "_cascade_running", lambda: False)
    def _pp(src, dst, denoise):
        Path(dst).write_bytes(Path(src).read_bytes())

    monkeypatch.setattr(seed_vc, "_preprocess", _pp)

    captured: dict = {}

    def fake_run_conversion(in_src, in_tgt, out_dir, **kwargs):
        captured["in_src"] = in_src
        captured["in_tgt_path"] = in_tgt
        # ★ 此刻抓下参考音内容：worker 的 finally 会删掉 in_tgt 等中间件，
        #   等 status=done 再读就是 FileNotFoundError（实测踩过）。
        captured["in_tgt_bytes"] = Path(in_tgt).read_bytes()
        captured["kwargs"] = kwargs
        produced = Path(out_dir) / "converted.wav"
        produced.write_bytes(_wav_bytes(1.0))
        return produced

    monkeypatch.setattr(seed_vc, "run_conversion", fake_run_conversion)

    import server
    from fastapi.testclient import TestClient

    client = TestClient(server.app, raise_server_exceptions=False)
    yield client, captured
    seed_vc.SEEDVC_STATE.clear()
    seed_vc.SEEDVC_STATE.update(saved)


def _wait_done(client, timeout_s: float = 10.0) -> dict:
    """轮询到终态；超时 fail（别用 sleep 定长等待，快机器上白等、慢机器上假红）。"""
    deadline = time.time() + timeout_s
    st: dict = {}
    while time.time() < deadline:
        st = client.get("/api/seedvc/status").json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    pytest.fail(f"worker {timeout_s}s 内未到终态（status={st.get('status')}）—— 端到端断了")


# ---------------- 端到端（两条链路）----------------


def test_expressive_end_to_end_run_to_status_done(env):
    """expressive：POST → worker → status=done，URL 指向会话产物、时长真读出来。"""
    client, captured = env
    r = client.post(
        "/api/seedvc/run",
        files={
            "file": ("src.wav", _wav_bytes(), "audio/wav"),
            "target": ("tgt.wav", _wav_bytes(fill=_TGT_FILL), "audio/wav"),
        },
        data={"mode": "expressive"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["mode"] == "expressive" and body["target"] == "uploaded"

    st = _wait_done(client)
    assert st["status"] == "done", st.get("error") or st  # error 时把真因带出来
    assert st["url"].startswith("/api/media/outputs/.session/seedvc_")
    assert st["duration_s"] == pytest.approx(1.0, abs=0.1)  # soundfile 真读了产物
    name = st["url"].rsplit("/", 1)[-1]
    assert (seed_vc.session_out.session_dir() / name).is_file()

    # v2 分支的表单默认：10 步 / 1.0 倍速 / 零样本（与 test_seedvc.py 的签名对账互补）
    kw = captured["kwargs"]
    assert "f0_condition" not in kw  # f0 旗标只属于 singing 分支，expressive 不传
    assert kw["diffusion_steps"] == 10
    assert kw["length_adjust"] == 1.0
    assert kw["cfm_checkpoint_path"] is None
    assert kw["similarity_cfg_rate"] == 0.5

    # 上传参考音的**字节**必须流到 worker（路由内读完再传 —— 见 seed_vc.py 的星注）
    assert _TGT_FILL in captured["in_tgt_bytes"]


def test_singing_end_to_end_defaults_to_a_preset(env):
    """singing：不传任何旋钮 → A 档标定值从路由流到 worker（掉了就是静默念白）。"""
    client, captured = env
    r = client.post(
        "/api/seedvc/run",
        files={
            "file": ("src.wav", _wav_bytes(), "audio/wav"),
            "target": ("tgt.wav", _wav_bytes(fill=_TGT_FILL), "audio/wav"),
        },
        data={"mode": "singing"},
    )
    assert r.status_code == 200, r.text
    st = _wait_done(client)
    assert st["status"] == "done", st.get("error") or st

    kw = captured["kwargs"]
    assert kw["f0_condition"] is True
    assert kw["semi_tone_shift"] == 11  # ★ A 档（test_seedvc_f0.py 钉的值，端到端再钉一次）
    assert kw["diffusion_steps"] == 80
    assert kw["auto_f0_adjust"] is True
    assert kw["inference_cfg_rate"] == pytest.approx(1.2)
    assert kw["length_adjust"] == pytest.approx(1.0)
    assert _TGT_FILL in captured["in_tgt_bytes"]


def test_voicebank_target_branch_reuses_ref(env, tmp_path, monkeypatch):
    """target_voice_id 分支：路由解析 voice_ref，worker 直接用 ref（不再吃上传件）。"""
    client, captured = env
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes())
    monkeypatch.setattr(seed_vc, "voice_ref", lambda vid: (ref, vid))

    r = client.post(
        "/api/seedvc/run",
        files={"file": ("src.wav", _wav_bytes(), "audio/wav")},
        data={"mode": "expressive", "target_voice_id": "kangaroo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "kangaroo"
    st = _wait_done(client)
    assert st["status"] == "done", st.get("error") or st
    assert Path(captured["in_tgt_path"]) == ref  # 参考音原样进推理，不经上传落盘


# ---------------- 路由守卫（互斥与参数校验的接线）----------------


def test_route_rejects_unknown_mode(env):
    client, _ = env
    r = client.post(
        "/api/seedvc/run",
        files={"file": ("src.wav", _wav_bytes(), "audio/wav")},
        data={"mode": "karaoke"},
    )
    assert r.status_code == 400
    assert "expressive" in r.json()["detail"]


def test_route_rejects_missing_target(env):
    """既没选 voicebank 音色也没传参考音 → 400（在起 worker 之前拦住）。"""
    client, _ = env
    r = client.post(
        "/api/seedvc/run",
        files={"file": ("src.wav", _wav_bytes(), "audio/wav")},
        data={"mode": "expressive"},
    )
    assert r.status_code == 400
    assert "voicebank" in r.json()["detail"]


def test_route_rejects_when_already_running(env):
    client, _ = env
    seed_vc.SEEDVC_STATE["running"] = True
    r = client.post(
        "/api/seedvc/run",
        files={
            "file": ("src.wav", _wav_bytes(), "audio/wav"),
            "target": ("tgt.wav", _wav_bytes(), "audio/wav"),
        },
    )
    assert r.status_code == 409
    assert "已有转换任务" in r.json()["detail"]


def test_route_rejects_when_live_running(env, monkeypatch):
    """实时变声占着显卡 → 409（GPU 互斥的接线，单测只测过 _live_running 本身）。"""
    client, _ = env
    monkeypatch.setattr(seed_vc, "_live_running", lambda: True)
    r = client.post(
        "/api/seedvc/run",
        files={
            "file": ("src.wav", _wav_bytes(), "audio/wav"),
            "target": ("tgt.wav", _wav_bytes(), "audio/wav"),
        },
    )
    assert r.status_code == 409
    assert "实时变声" in r.json()["detail"]

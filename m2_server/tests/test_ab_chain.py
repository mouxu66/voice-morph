"""多链路对比评测（A4）：/api/ab/chain 编排 / 单链路失败隔离 / qwen3 无文本跳过 / 打分 / 单飞互斥。

全部本地完成：三条链路与打分全部 stub，不跑真实 RVC/Seed-VC/TTS 子进程，
不加载 CAM++/NatScore 模型、不碰网络。
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ab_chain as ac


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """isolated /ab/chain: stub 链路 + 打分 + 互斥锁。"""
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(ac, "OUT", out)

    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFref")
    monkeypatch.setattr(ac, "voice_ref", lambda vid: (ref, ""))

    monkeypatch.setattr(ac, "_live_proc_alive", lambda: False)
    monkeypatch.setattr(ac, "MAX_UPLOAD_BYTES", 64 * 1024 * 1024)

    def fake_pre(src, dst):
        dst.write_bytes(b"RIFF16k")     # 免真实 ffmpeg
    monkeypatch.setattr(ac, "_preprocess16k", fake_pre)

    def fake_score(wav, ref_):
        return {"secs": 0.9, "nats": 3.25, "duration_s": 3.0}
    monkeypatch.setattr(ac, "_score_metrics", fake_score)

    app = FastAPI()
    app.include_router(ac.router)
    return TestClient(app)


def _mk_link_via(tag: str, monkeypatch):
    """把 tag 对应的链路替换成「写一个文件即成功」的 stub。"""
    import soundfile as sf
    import numpy as np

    func_name = {"seed_vc": "_seedvc_link"}.get(tag, f"_{tag}_link")

    def fake_link(voice_id, src16k, out_wav):
        sf.write(str(out_wav),
                 np.sin(np.linspace(0, 100, 16000)).astype(np.float32), 16000)
    monkeypatch.setattr(ac, func_name, fake_link)


def _upload(voice_id: str = "kangaroo", text: str = "", name: str = "src.wav"):
    return {"file": (name, io.BytesIO(b"RIFFraw"))},\
           {"voice_id": voice_id, "text": text}


def test_chain_success_three_links(client, monkeypatch):
    """有文本 → 三条链路全跑，done + url + 客观分都齐。"""
    for t in ("rvc", "seed_vc", "qwen3"):
        _mk_link_via(t, monkeypatch)
    files, data = _upload(text="周末我们去河边散步。")
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["target_voice_id"] == "kangaroo"
    assert body["text"] == "周末我们去河边散步。"
    for tag in ("rvc", "seed_vc", "qwen3"):
        link = body["chains"][tag]
        assert link["status"] == "done", link
        assert link["url"].startswith("/api/media/outputs/")
        assert link["metrics"] == {"secs": 0.9, "nats": 3.25, "duration_s": 3.0}


def test_chain_qwen3_skipped_without_text(client, monkeypatch):
    """无文本 → Qwen3 skipped 且带提示；RVC/Seed-VC 照常 done。"""
    for t in ("rvc", "seed_vc"):
        _mk_link_via(t, monkeypatch)
    files, data = _upload()
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 200
    chains = r.json()["chains"]
    assert chains["rvc"]["status"] == "done"
    assert chains["seed_vc"]["status"] == "done"
    assert chains["qwen3"]["status"] == "skipped"
    assert "文本" in chains["qwen3"]["error"]


def test_chain_single_link_failure_isolated(client, monkeypatch):
    """RVC 失败只影响自身（failed+原因），Seed-VC 仍 done，Qwen3 跳过。"""
    _mk_link_via("seed_vc", monkeypatch)
    _mk_link_via("qwen3", monkeypatch)

    def boom(voice_id, src16k, out_wav):
        raise RuntimeError("RVC 推理失败: CUDA OOM")
    monkeypatch.setattr(ac, "_rvc_link", boom)

    files, data = _upload(text="说点什么。")
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 200
    chains = r.json()["chains"]
    assert chains["rvc"]["status"] == "failed"
    assert "CUDA OOM" in chains["rvc"]["error"]
    assert chains["rvc"]["metrics"] is None
    assert chains["seed_vc"]["status"] == "done"
    assert chains["qwen3"]["status"] == "done"


def test_chain_lock_busy_returns_409(client, monkeypatch):
    """已有链路对比在跑 → 409，而不是排队/叠加。"""
    import threading
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(ac, "_chain_lock", lock)
    files, data = _upload()
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 409
    assert "在跑" in r.json()["detail"]


def test_chain_gpu_busy_returns_409(client, monkeypatch):
    """实时变声占用 GPU → 409 带原因。"""
    monkeypatch.setattr(ac, "_live_proc_alive", lambda: True)
    files, data = _upload()
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 409
    assert "实时变声" in r.json()["detail"]


def test_chain_missing_voice_returns_400(client, monkeypatch):
    """未选音色 → 400。"""
    files, data = _upload(voice_id="")
    r = client.post("/api/ab/chain", files=files, data=data)
    assert r.status_code == 400


def test_run_one_success_and_failure(client, monkeypatch):
    """_run_one：成功产出 url，异常转 failed 且清理残留文件。"""
    def ok_link(dst):
        pass  # 不产出文件 → 视为失败
    r1 = ac._run_one("rvc", ok_link, 1)
    assert r1["status"] == "failed"

    import soundfile as sf
    import numpy as np

    def real_ok(dst):
        sf.write(str(dst), np.zeros(16000, dtype=np.float32), 16000)
    r2 = ac._run_one("rvc", real_ok, 2)
    assert r2["status"] == "done"
    assert r2["url"].endswith("ab_chain_rvc_2.wav")


def test_nats_scorer_missing_ckpt_raises(client, monkeypatch):
    """NatScore 权重缺失 → 可读原因，而不是裸 ImportError/裸加载。"""
    monkeypatch.setattr(ac, "NATSCORE_CKPT", __import__("pathlib").Path("no") / "such_ckpt.pt")
    with pytest.raises(RuntimeError, match="NatScore 权重缺失"):
        ac._nats_scorer()


def test_nats_scorer_missing_dependency_reports_readable_error(client, monkeypatch, tmp_path):
    """权重在、但依赖（torch）装不上时也要给可读原因。

    回归：原实现先 `from natscore_local import load_local` 再查权重，
    依赖缺失时抛裸 ModuleNotFoundError，绕过了本文件声称的「可读原因」约定。
    用 sys.modules 里塞 None 模拟 import 失败（无需真的卸载 torch）。
    """
    import sys

    ckpt = tmp_path / "final.pt"
    ckpt.write_bytes(b"stub")
    monkeypatch.setattr(ac, "NATSCORE_CKPT", ckpt)
    monkeypatch.setattr(ac, "_NATS", None)
    monkeypatch.setitem(sys.modules, "natscore_local", None)   # import → ImportError
    with pytest.raises(RuntimeError, match="NatScore 依赖缺失"):
        ac._nats_scorer()

# ---------------- RVC 失败信息的可读性（2026-09-18 试衣间实测）----------------


def test_fail_tail_prefers_real_error_over_warning():
    """回归：torch 的弃用警告曾把真错误挤出 tail，用户看到的是假原因。

    真机现场：RVC 子进程因 cuDNN 崩掉，但 stderr 末尾 3 行是
    `FutureWarning: torch.nn.utils.weight_norm is deprecated` + 源码片段，
    报出来的错就成了那条警告，完全指不到问题。
    """
    stderr = "\n".join([
        r"D:\RVC\.venv\Lib\site-packages\torch\nn\utils\weight_norm.py:143: FutureWarning:"
        r" `torch.nn.utils.weight_norm` is deprecated in favor of"
        r" `torch.nn.utils.parametrizations.weight_norm`.",
        "  WeightNorm.apply(module, name, dim)",
        "Traceback (most recent call last):",
        "RuntimeError: cuDNN error: CUDNN_STATUS_EXECUTION_FAILED",
    ])
    tail = ac._fail_tail(stderr, "")
    assert "cuDNN" in tail
    assert "FutureWarning" not in tail


def test_fail_tail_drops_warnings_and_caret_lines():
    """没有 error 字样时，退而求其次：去掉警告/源码片段/caret 再取末尾。"""
    stdout = "\n".join([
        "UserWarning: something deprecated",
        "  some_source_line()",
        "    ^^^^^^^^",
        "real problem line one",
        "real problem line two",
    ])
    tail = ac._fail_tail("", stdout, n=2)
    assert "real problem line two" in tail
    assert "UserWarning" not in tail
    assert "^^^^" not in tail


def test_fail_tail_handles_empty_output():
    assert ac._fail_tail("", "") == ""


def test_rvc_link_reports_readable_error_on_failure(tmp_path, monkeypatch):
    """子进程失败 → RuntimeError 里必须带真错误，而不是 torch 警告。"""
    pth = tmp_path / "v.pth"
    pth.write_bytes(b"\x00")
    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"

    class _R:
        returncode = 1
        stdout = ""
        stderr = ("x.py:1: FutureWarning: deprecated\n  warn()\n"
                  "RuntimeError: cuDNN error: CUDNN_STATUS_EXECUTION_FAILED")

    monkeypatch.setattr(ac.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError) as ei:
        ac._rvc_link("v1", src, out, pth=pth, index="")
    msg = str(ei.value)
    assert "cuDNN" in msg and "FutureWarning" not in msg


def test_rvc_link_accepts_weight_override_without_touching_resolver(tmp_path, monkeypatch):
    """权重覆盖是给「未安装市场音色用暂存权重」用的：不该再去解析本机产物。"""
    called = {"resolver": 0}

    def _resolver(_v):
        called["resolver"] += 1
        return None

    monkeypatch.setattr(ac, "ensure_infer_pth", _resolver)
    pth = tmp_path / "staged.pth"
    pth.write_bytes(b"\x00")
    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.wav"
    seen = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **_k):
        seen["cmd"] = cmd
        out.write_bytes(b"RIFF")
        return _R()

    monkeypatch.setattr(ac.subprocess, "run", _run)
    ac._rvc_link("market_voice", src, out, pth=pth, index="", pitch=3, index_rate=0.7)
    assert called["resolver"] == 0                 # 有覆盖就不碰解析器
    assert str(pth) in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--index") + 1] == ""
    assert seen["cmd"][seen["cmd"].index("--pitch") + 1] == "3"
    assert seen["cmd"][seen["cmd"].index("--index-rate") + 1] == "0.7"

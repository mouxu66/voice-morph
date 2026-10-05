# -*- coding: utf-8 -*-
"""`sound.watermark`（音频溯源水印）的测试 —— 2026-10-05 新增。

分两层：
  · **纯逻辑 / 文件层**：码↔位转换、开关状态落盘、embed/detect 的文件读写与
    错误映射 —— 模型用**假件**（monkeypatch `_load_models`），不碰真权重、不碰网络。
  · **API 层**：TestClient 打 `/api/watermark/*`，验证 HTTP 形状与错误码。

真模型的效果验证（roundtrip / mp3 后仍可检出）是**人工 e2e**，不进 pytest ——
它依赖 90MB 权重下载与 GPU/CUDA 初始化，CI 上既慢又脆。
"""

from __future__ import annotations

import re

import pytest

import audio_watermark as wm

torch = pytest.importorskip("torch", reason="假模型用 torch 张量搭，缺 torch 时整组跳过")


@pytest.fixture(autouse=True)
def _isolated_wm(tmp_path, monkeypatch):
    """每个用例：权重目录/开关状态都指向临时目录；模型缓存清空（防串味）。"""
    monkeypatch.setattr(wm.cfg, "WATERMARK_MODELS_DIR", tmp_path / "wm-models")
    monkeypatch.setattr(wm.cfg, "MEDIA_DIR", tmp_path / "media")
    wm._MODELS.clear()
    yield
    wm._MODELS.clear()


# ---------------------------------------------------------------- 码与位


def test_code_bits_roundtrip():
    for code in ("0000", "ffff", "a1b2", "3c9d"):
        assert wm.bits_to_code(wm.code_to_bits(code)) == code


def test_code_to_bits_values():
    assert wm.code_to_bits("8001") == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]


# 注意：大写 hex 会被归一化接受（lowercase），不算非法码 —— 别把它加进 bad 列表。
@pytest.mark.parametrize("bad", ["", "abc", "12345", "zzzz", "12 4", "0x12"])
def test_bad_code_rejected(bad):
    with pytest.raises(wm.WatermarkError) as ei:
        wm.code_to_bits(bad)
    assert ei.value.status == 400


def test_random_code_is_4_hex():
    assert re.fullmatch(r"[0-9a-f]{4}", wm.random_code())


# ---------------------------------------------------------------- 开关状态


def test_auto_default_off_and_code_fixed_on_enable():
    assert wm.auto_enabled() is False
    payload = wm.set_auto(True)
    assert payload["enabled"] is True
    assert re.fullmatch(r"[0-9a-f]{4}", payload["code"])
    # 再读一次：同一份状态（码固定，不是每次现摇）
    assert wm.auto_code() == payload["code"]
    wm.set_auto(False)
    assert wm.auto_enabled() is False


def test_auto_custom_code_and_rejection():
    assert wm.set_auto(True, "0ff1")["code"] == "0ff1"
    with pytest.raises(wm.WatermarkError):
        wm.set_auto(True, "nothex")


# ---------------------------------------------------------------- 下载编排


class _StubManager:
    """记录 start() 参数的假下载器（绝不发网络请求）。"""

    def __init__(self):
        self.calls = []

    def start(self, name, **kw):
        self.calls.append((name, kw))
        return {"name": name, "status": "downloading"}

    def task_status(self, name):
        return {"name": name, "status": "downloading"}


class _DoneManager:
    """已下完的假下载器：产物躺在 download_dir，task_status 报 done + dest。

    专门用来钉「下载完成 ≠ 可用」那道搬运步骤 —— 真实 bug 就是缺它。
    """

    def __init__(self, download_dir, files):
        self.download_dir = download_dir
        self._files = files

    def start(self, name, **kw):  # pragma: no cover - 本组不触发下载
        raise AssertionError("已 done 的任务不该再 start")

    def task_status(self, name):
        fn = self._files.get(name)
        if fn is None:
            return {"name": name, "status": "failed"}
        p = self.download_dir / fn
        return {"name": name, "status": "done", "dest": str(p)}


def test_start_download_starts_two_jobs(monkeypatch):
    stub = _StubManager()
    import market_download

    monkeypatch.setattr(market_download, "get_manager", lambda: stub)
    monkeypatch.setattr(wm, "_start_installer", lambda *a, **k: None)  # 本用例不测监听线程
    res = wm.start_download()
    assert res["status"] == "downloading" and set(res["tasks"]) == {
        "audioseal_generator", "audioseal_detector"}
    assert len(stub.calls) == 2
    names = {n for n, _ in stub.calls}
    assert names == {"audioseal_generator", "audioseal_detector"}
    for _n, kw in stub.calls:
        assert kw["sha256"] and len(kw["sha256"]) == 64
        assert kw["expected_size"] and kw["expected_size"] > 1_000_000
        assert kw["filename"] in (wm.GENERATOR_FILE, wm.DETECTOR_FILE)
        assert kw["mirror_url"].startswith("https://hf-mirror.com/"), "镜像兜底必须带上"


def test_install_from_downloads_moves_weights(tmp_path):
    """★ 回归：下完的权重必须被搬进 models_dir，否则 embed 永远 409。"""
    dl = tmp_path / "market"
    dl.mkdir()
    (dl / wm.GENERATOR_FILE).write_bytes(b"gen")
    (dl / wm.DETECTOR_FILE).write_bytes(b"det")
    mgr = _DoneManager(dl, {
        "audioseal_generator": wm.GENERATOR_FILE,
        "audioseal_detector": wm.DETECTOR_FILE,
    })

    assert wm.installed() is False, "前置：模型目录还是空的"
    res = wm.install_from_downloads(mgr)
    assert res["installed"] is True
    assert sorted(res["moved"]) == sorted([wm.GENERATOR_FILE, wm.DETECTOR_FILE])
    assert wm.installed() is True
    assert not (dl / wm.GENERATOR_FILE).exists(), "搬走（move），不是拷贝"
    assert (wm.models_dir() / wm.DETECTOR_FILE).read_bytes() == b"det"

    # 幂等：第二次什么都不搬，也不覆盖
    again = wm.install_from_downloads(mgr)
    assert again == {"installed": True, "moved": []}


def test_install_from_downloads_skips_unfinished(tmp_path):
    """没下完（failed / downloading）一律不搬 —— 半个文件搬过去只会换来一个假的"已安装"。"""
    dl = tmp_path / "market"
    dl.mkdir()
    (dl / wm.GENERATOR_FILE).write_bytes(b"gen")
    (dl / wm.DETECTOR_FILE).write_bytes(b"det")
    mgr = _DoneManager(dl, {"audioseal_generator": wm.GENERATOR_FILE})  # detector 报 failed
    res = wm.install_from_downloads(mgr)
    assert res["installed"] is False
    assert res["moved"] == [wm.GENERATOR_FILE], "只搬 done 的那一个"


def test_installer_loop_installs_then_exits(tmp_path):
    """监听线程的循环体：装好就返回（用同步调用验，不起线程）。"""
    dl = tmp_path / "market"
    dl.mkdir()
    (dl / wm.GENERATOR_FILE).write_bytes(b"gen")
    (dl / wm.DETECTOR_FILE).write_bytes(b"det")
    mgr = _DoneManager(dl, {
        "audioseal_generator": wm.GENERATOR_FILE,
        "audioseal_detector": wm.DETECTOR_FILE,
    })
    wm._install_when_ready(mgr, timeout_s=5)
    assert wm.installed() is True


def test_installer_loop_gives_up_when_nothing_active(tmp_path):
    """两个任务都不活跃且没装成 → 立刻收工，不空转到超时。"""
    dl = tmp_path / "market"
    dl.mkdir()
    mgr = _DoneManager(dl, {})  # 全部 failed
    t0 = __import__("time").time()
    wm._install_when_ready(mgr, timeout_s=30)
    assert __import__("time").time() - t0 < 3, "不该等到超时"


def test_start_download_idempotent_when_installed():
    d = wm.models_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / wm.GENERATOR_FILE).write_bytes(b"x")
    (d / wm.DETECTOR_FILE).write_bytes(b"x")
    assert wm.installed() is True
    assert wm.start_download() == {"status": "done", "installed": True}


def test_status_reports_missing_models():
    st = wm.status()
    assert st["installed"] is False
    assert st["auto"] == {"enabled": False, "code": ""}


# ---------------------------------------------------------------- embed / detect（假模型）


class _FakeGen:
    """假 generator：返回 x + 由消息位决定的微弱 pattern（保持形状契约：3D 进 3D 出）。"""

    def __call__(self, x, sample_rate=None, message=None, alpha=1.0):
        assert x.ndim == 3, "audioseal 只认 3D 输入 —— 这里守住坑 2 的契约"
        bits = (message[0] * 2 - 1) * 0.01  # 0/1 → ∓0.01
        # 水印信号与输入同长（真模型如此）：把 16 位 pattern 平铺到 T 再裁齐
        reps = (x.shape[2] + bits.numel() - 1) // bits.numel()
        pattern = bits.repeat(reps)[: x.shape[2]].reshape(1, 1, -1)
        return x + pattern


class _FakeDet:
    """假 detector：恒回"带水印 prob=0.98、码 0ff1"（形状契约与真 detect_watermark 一致）。"""

    def detect_watermark(self, x, sample_rate=None):
        assert x.ndim == 3
        prob = torch.full((1,), 0.98)
        msg = torch.tensor([wm.code_to_bits("0ff1")], dtype=torch.float32)  # (1, 16)
        return prob, msg

    __call__ = detect_watermark


@pytest.fixture()
def fake_models(monkeypatch):
    monkeypatch.setattr(wm, "_load_models", lambda device="cpu": (_FakeGen(), _FakeDet(), device))


def _write_wav(path, sr=24000, seconds=1.0):
    import numpy as np
    import soundfile as sf

    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    sf.write(str(path), x, sr, subtype="PCM_16")
    return x


def test_embed_non_16k_keeps_length_and_sr(fake_models, tmp_path):
    src = tmp_path / "in.wav"
    original = _write_wav(src, sr=24000, seconds=1.0)
    dst = tmp_path / "out.wav"
    info = wm.embed_file(src, dst, code="a1b2")
    assert info["code"] == "a1b2" and info["sr"] == 24000
    import soundfile as sf

    y, sr = sf.read(str(dst), dtype="float32")
    assert sr == 24000
    assert len(y) == len(original), "水印不许改变时长"
    # 水印能量微小但不为零（否则就是没嵌上）
    assert float(abs(y - original).max()) > 1e-4


def test_embed_blank_code_generates_one(fake_models, tmp_path):
    src = tmp_path / "in.wav"
    _write_wav(src)
    info = wm.embed_file(src, tmp_path / "out.wav", code="")
    assert re.fullmatch(r"[0-9a-f]{4}", info["code"])


def test_detect_reports_watermarked(fake_models, tmp_path):
    src = tmp_path / "a.wav"
    _write_wav(src)
    res = wm.detect_file(src)
    assert res["watermarked"] is True
    assert res["prob"] >= 0.5
    assert res["code"] == "0ff1"


def test_models_missing_maps_to_409(tmp_path):
    src = tmp_path / "a.wav"
    _write_wav(src)
    with pytest.raises(wm.WatermarkError) as ei:
        wm.embed_file(src, tmp_path / "o.wav", code="a1b2")
    assert ei.value.status == 409
    assert "下载" in str(ei.value)


def test_unreadable_audio_maps_to_400(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all" * 100)
    with pytest.raises(wm.WatermarkError) as ei:
        wm.detect_file(bad)  # 模型都没到那步 —— 读音频就先失败
    assert ei.value.status in (400, 503)  # 无 ffmpeg 环境兜底失败也算输入错误


# ---------------------------------------------------------------- 合成链路旁路


def test_auto_mark_off_is_noop(fake_models, tmp_path):
    src = tmp_path / "a.wav"
    _write_wav(src)
    assert wm.auto_mark_if_enabled(src) is None


def test_auto_mark_marks_in_place_and_cleans_tmp(fake_models, tmp_path):
    import soundfile as sf

    src = tmp_path / "a.wav"
    original = _write_wav(src)
    wm.set_auto(True, "0ff1")
    info = wm.auto_mark_if_enabled(src)
    assert info is not None and info["code"] == "0ff1"
    y, sr = sf.read(str(src), dtype="float32")
    assert sr == 24000 and len(y) == len(original)
    assert not list(tmp_path.glob("*.tmp.wav")), "临时文件必须被替换掉，不能残留"


def test_auto_mark_swallows_errors(tmp_path, monkeypatch):
    """旁路铁律：水印挂了绝不能拖垮合成 —— 出错返回 None、原文件原样。"""
    src = tmp_path / "a.wav"
    original = _write_wav(src)
    wm.set_auto(True, "0ff1")

    def _boom(*a, **kw):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(wm, "embed_file", _boom)
    assert wm.auto_mark_if_enabled(src) is None
    import soundfile as sf

    y, _sr = sf.read(str(src), dtype="float32")
    assert len(y) == len(original), "合成产物必须原样保留"


# ---------------------------------------------------------------- API 层


@pytest.fixture()
def client(fake_models):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import watermark_api

    app = FastAPI()
    app.include_router(watermark_api.router)
    return TestClient(app)


def test_api_status_shape(client):
    r = client.get("/api/watermark/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("installed", "models_dir", "download", "auto", "size_hint_mb"):
        assert key in body


def test_api_auto_roundtrip(client):
    assert client.get("/api/watermark/auto").json() == {"enabled": False, "code": ""}
    r = client.post("/api/watermark/auto", json={"enabled": True, "code": "0ff1"})
    assert r.status_code == 200 and r.json()["code"] == "0ff1"
    assert client.get("/api/watermark/auto").json()["enabled"] is True
    bad = client.post("/api/watermark/auto", json={"enabled": True, "code": "zz"})
    assert bad.status_code == 400


def test_api_embed_and_detect(client, tmp_path):
    import io

    import soundfile as sf

    import numpy as np

    t = np.linspace(0, 0.5, 12000, endpoint=False)
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, x, 24000, format="WAV", subtype="PCM_16")
    buf.seek(0)

    r = client.post(
        "/api/watermark/embed",
        files={"file": ("clip.wav", buf, "audio/wav")},
        data={"code": "a1b2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["code"] == "a1b2"
    assert body["url"].startswith("/api/media/outputs/.session/"), body

    # 检出：走 name=（会话裸名）这条输入路径
    d = client.post("/api/watermark/detect", data={"name": body["wav"]})
    assert d.status_code == 200, d.text
    assert d.json()["code"] == "0ff1" and d.json()["watermarked"] is True


def test_api_detect_unknown_name_400(client):
    assert client.post("/api/watermark/detect", data={"name": "nope.wav"}).status_code == 400


def test_api_embed_bad_code_400(client, tmp_path):
    import io

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, [0.0] * 2400, 24000, format="WAV", subtype="PCM_16")
    buf.seek(0)
    r = client.post(
        "/api/watermark/embed",
        files={"file": ("c.wav", buf, "audio/wav")},
        data={"code": "zzzz"},
    )
    assert r.status_code == 400

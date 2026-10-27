"""「边唱边说」三档后端单测（调研：`docs/交接-AI必读.md`）。

覆盖：
    A  `sfx_lib.mix_bgm` 垫底 DSP + `/api/soundboard/bgm/upload|premix`
    B  `/api/cover/clip` 翻唱直发的截 ≤54s 入口
    C  `/api/soundboard/bgm/start|stop|gain` + `board_worker` 垫底层协议

不打真声卡：C 的用例把 `sounddevice` 换成桩再 import `board_worker`
（与 `test_soundboard.py` 的真 worker 段同一手法）；端点用例把 `_spawn_worker`
换成按同一行协议应答的假 worker —— 验的是协议契约，不是自说自话。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import config as cfg  # noqa: E402
import cover_api  # noqa: E402
import session_out  # noqa: E402
import sfx_lib  # noqa: E402
import soundboard  # noqa: E402

SR = 44100


# --------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """outputs/素材目录/计数/worker 全按用例隔离（绝不写用户的真 media/outputs）。

    `session_out` 每次现读 `cfg.OUTPUTS_DIR`（§8.36/§8.37 的教训：不许早绑定），
    所以打 `cfg` 一处，会话目录就整体跟着走。
    """
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(sfx_lib, "IMPORT_DIR", tmp_path / "imported")
    monkeypatch.setattr(soundboard, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(soundboard, "_proc", None)
    yield
    monkeypatch.setattr(soundboard, "_proc", None)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(soundboard.router)
    app.include_router(cover_api.router)
    return TestClient(app, raise_server_exceptions=False)


class FakeWorker:
    """按真协议应答的假 worker（含垫底层：bgm → bgm_playing 等）。"""

    def __init__(self):
        self.commands: list[dict] = []
        self._out: list[str] = []
        self.alive = True
        self.stdin = self
        self.stdout = self

    def write(self, s: str) -> None:
        cmd = json.loads(s)
        self.commands.append(cmd)
        if cmd.get("bgm_stop"):
            self._out.append(json.dumps({"type": "bgm_stopped"}))
        elif "bgm_gain" in cmd:
            self._out.append(json.dumps({"type": "bgm_gain", "gain": cmd["bgm_gain"]}))
        elif cmd.get("bgm"):
            self._out.append(json.dumps({"type": "bgm_playing", "id": cmd.get("id"), "duration_s": 3.0}))
        elif cmd.get("stop"):
            self._out.append(json.dumps({"type": "stopped"}))
        else:
            self._out.append(json.dumps({"type": "playing", "id": cmd.get("id"), "duration_s": 1.4}))

    def flush(self) -> None: ...
    def close(self) -> None:
        self.alive = False

    def readline(self) -> str:
        return self._out.pop(0) + "\n" if self._out else ""

    def poll(self):
        return None if self.alive else 1

    def kill(self) -> None:
        self.alive = False


@pytest.fixture()
def worker(monkeypatch):
    w = FakeWorker()
    monkeypatch.setattr(soundboard, "_spawn_worker", lambda: w)
    return w


def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _session_wav(prefix: str, pcm: np.ndarray, sr: int = SR, subtype: str = "PCM_16") -> str:
    """往（隔离后的）会话目录写一条音频，返回裸名。"""
    p = session_out.new_path(prefix)
    sf.write(str(p), pcm, sr, subtype=subtype)
    return p.name


# --------------------------------------------------------------- A：mix_bgm DSP


def test_bgm_length_follows_the_voice_not_the_song():
    """垫底后长度 = 人声长度：歌更**长**时被截断（人声是时间轴）。"""
    voice = _tone(1.0)
    bgm = _tone(3.0, freq=220.0, sr=48000)  # 不同采样率，顺带覆盖重采样
    out, notes = sfx_lib.mix_bgm(voice, SR, bgm, 48000)
    assert out.size == voice.size
    assert notes == []
    # 中段（避开淡入淡出）应当真的有声
    mid = out[int(0.4 * SR) : int(0.6 * SR)]
    assert np.abs(mid).max() > 0.2


def test_bgm_shorter_than_voice_loops_and_notes():
    voice = _tone(2.0)
    bgm = _tone(0.5, freq=220.0, amp=0.5)
    out, notes = sfx_lib.mix_bgm(voice, SR, bgm, SR, fade_in_s=0.01, fade_out_s=0.01)
    assert out.size == voice.size, "歌短时循环补满，但不许改人声长度"
    assert any("循环补满" in n for n in notes)
    # 单点断言太脆：220Hz 正弦在 0.1s 的整数倍处恰好是零交叉（2π·220·0.2 = 88π），
    # 查 1.2s 起一小窗即可（第二遍从 1.0s 回绕，1.2s 落在第二遍 0.2s 处）。
    band = out[int(1.2 * SR) : int(1.25 * SR)]
    assert np.abs(band).max() > 0.1, "1.2s > 0.5s 歌长 → 应是第二遍"


def test_auto_balance_lands_on_lead_db():
    """两轨电平相同、lead_db=10 → BGM 的实际增益 ≈ 10^(-10/20)。"""
    voice = _tone(1.0, freq=440.0, amp=0.3)
    bgm = _tone(2.0, freq=220.0, amp=0.3)  # 同幅正弦 → RMS 相同
    out, notes = sfx_lib.mix_bgm(voice, SR, bgm, SR, fade_in_s=0.01, fade_out_s=0.01)
    assert notes == []
    used = bgm[: voice.size]
    band = slice(int(0.3 * SR), int(0.7 * SR))  # 避开淡变区
    gain_eff = np.median((out[band] - voice[band]) / used[band])
    assert abs(gain_eff - 10 ** (-10 / 20)) < 0.03


def test_silent_voice_falls_back_to_fixed_attenuation():
    """人声全静音（量不出电平）→ 退化为固定衰减并如实记 notes。"""
    voice = np.zeros(SR, dtype=np.float32)
    bgm = _tone(2.0, freq=220.0, amp=0.3)
    out, notes = sfx_lib.mix_bgm(voice, SR, bgm, SR, fade_in_s=0.01, fade_out_s=0.01)
    assert any("量不出" in n for n in notes)
    band = slice(int(0.3 * SR), int(0.7 * SR))
    ratio = np.sqrt(np.mean(out[band].astype(np.float64) ** 2)) / np.sqrt(
        np.mean(bgm[: voice.size][band].astype(np.float64) ** 2)
    )
    assert abs(ratio - 10 ** (-10 / 20)) < 0.03


def test_start_s_skips_the_intro():
    """start_s=0.5 → 垫进去的是歌的第 0.5s 起那段，不是从 0 重来。

    lead_db=0（人声全静音 → 走固定衰减分支，增益恰为 10^0=1）：
    断言才能直接对斜坡原值比，不用再乘一个增益系数。
    """
    voice = np.zeros(SR, dtype=np.float32)
    ramp = np.linspace(0.0, 0.8, SR * 2, dtype=np.float32)  # 线性斜坡：位置可反推
    out, _ = sfx_lib.mix_bgm(
        voice, SR, ramp, SR, lead_db=0, start_s=0.5, fade_in_s=0.01, fade_out_s=0.01
    )
    idx = int(0.4 * SR)
    assert abs(out[idx] - ramp[int(0.5 * SR) + idx]) < 0.02


def test_fades_land_on_the_voice_timeline():
    """淡入从垫入点开始、淡出恰好落在**人声结束**处（不是歌自己的结尾）。"""
    voice = np.zeros(2 * SR, dtype=np.float32)
    bgm = np.full(SR * 5, 0.5, dtype=np.float32)  # 常量歌，比人声长得多
    out, _ = sfx_lib.mix_bgm(voice, SR, bgm, SR, lead_db=0, fade_in_s=0.5, fade_out_s=0.5)
    assert out[0] == 0.0, "淡入起点应从零开始"
    assert abs(out[int(0.25 * SR)] - 0.25) < 0.02, "淡入中点应到一半"
    assert abs(out[int(1.0 * SR)] - 0.5) < 0.02, "淡变之外应是全量"
    assert abs(out[-1]) < 0.02, "人声结束处应淡出到零"
    assert abs(out[-int(0.25 * SR)] - 0.25) < 0.02


def test_start_beyond_song_length_raises():
    voice = _tone(0.5)
    bgm = _tone(2.0, freq=220.0)
    with pytest.raises(sfx_lib.SfxError):
        sfx_lib.mix_bgm(voice, SR, bgm, SR, start_s=5.0)


def test_empty_bgm_raises():
    with pytest.raises(sfx_lib.SfxError):
        sfx_lib.mix_bgm(_tone(0.5), SR, np.zeros(0, dtype=np.float32), SR)


def test_peak_norm_still_caps_the_mix():
    """配平后仍可能超峰（人声+歌同相），整体回缩的护栏必须还在。"""
    voice = np.full(SR, 0.9, dtype=np.float32)
    bgm = np.full(2 * SR, 0.9, dtype=np.float32)
    out, _ = sfx_lib.mix_bgm(voice, SR, bgm, SR, lead_db=0, fade_in_s=0.01, fade_out_s=0.01)
    assert np.abs(out).max() <= 0.97 + 1e-6


# --------------------------------------------------------------- A：/bgm/upload + /bgm/premix


def _wav_bytes(seconds: float = 1.5, freq: float = 220.0, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, _tone(seconds, freq=freq, sr=sr), sr, format="WAV")
    return buf.getvalue()


def test_bgm_upload_writes_a_session_wav(client):
    r = client.post(
        "/api/soundboard/bgm/upload",
        files={"file": ("我的歌.wav", io.BytesIO(_wav_bytes()), "audio/wav")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["name"].startswith("bgm_")
    assert body["url"].startswith("/api/media/outputs/")
    p = session_out.find(body["name"])
    assert p is not None and abs(sfx_lib.duration_s(p) - 1.5) < 0.05


def test_bgm_upload_rejects_junk_and_too_short(client):
    junk = client.post(
        "/api/soundboard/bgm/upload", files={"file": ("x.wav", io.BytesIO(b"not audio"), "audio/wav")}
    )
    assert junk.status_code == 400
    short = client.post(
        "/api/soundboard/bgm/upload",
        files={"file": ("x.wav", io.BytesIO(_wav_bytes(0.2)), "audio/wav")},
    )
    assert short.status_code == 400


def test_bgm_premix_produces_bgmmix_next_to_the_source(client):
    voice_name = _session_wav("tts", _tone(1.0))
    bgm_name = _session_wav("bgm", _tone(3.0, freq=220.0))
    r = client.post(
        "/api/soundboard/bgm/premix",
        json={"wav": voice_name, "bgm": bgm_name},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wav"] == f"bgmmix_{Path(voice_name).stem}.wav"
    assert abs(body["seconds"] - 1.0) < 0.02
    assert body["notes"] == []
    mixed, sr = sf.read(str(session_out.find(body["wav"])), dtype="float32")
    assert mixed.size == SR  # 长度跟人声走
    assert sr == SR


def test_bgm_premix_defaults_to_latest_tts(client):
    voice_name = _session_wav("tts", _tone(1.0))
    bgm_name = _session_wav("bgm", _tone(2.0, freq=220.0))
    r = client.post("/api/soundboard/bgm/premix", json={"bgm": bgm_name})
    assert r.status_code == 200
    assert r.json()["wav"] == f"bgmmix_{Path(voice_name).stem}.wav"


def test_bgm_premix_reports_loop_note(client):
    voice_name = _session_wav("tts", _tone(2.0))
    bgm_name = _session_wav("bgm", _tone(0.5, freq=220.0))
    r = client.post("/api/soundboard/bgm/premix", json={"wav": voice_name, "bgm": bgm_name})
    assert r.status_code == 200
    assert any("循环补满" in n for n in r.json()["notes"])


def test_bgm_premix_errors_are_explicit(client):
    bgm_name = _session_wav("bgm", _tone(2.0, freq=220.0))
    assert client.post("/api/soundboard/bgm/premix", json={"bgm": "nope.wav"}).status_code == 404
    assert client.post("/api/soundboard/bgm/premix", json={"bgm": "../x.wav"}).status_code == 400
    # 起点越过歌长：SfxError → 400，报错里能看见两个数
    voice_name = _session_wav("tts", _tone(1.0))
    r = client.post(
        "/api/soundboard/bgm/premix", json={"wav": voice_name, "bgm": bgm_name, "start_s": 99}
    )
    assert r.status_code == 400 and "起点" in r.json()["detail"]


# --------------------------------------------------------------- B：/cover/clip


def test_cover_clip_takes_a_budget_sized_slice(client):
    ramp = np.linspace(0.0, 0.8, SR * 60, dtype=np.float32)  # 60s，超出预算
    name = _session_wav("cover", ramp)
    r = client.post("/api/cover/clip", json={"src_name": name})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wav"] == f"clip_{Path(name).stem}.wav"
    assert body["total_s"] == 60.0
    assert abs(body["seconds"] - cover_api.WECHAT_AUDIO_BUDGET_S) <= 0.01
    assert body["notes"] == []  # 默认正好 54s，没有钳制要报
    clip, _ = sf.read(str(session_out.find(body["wav"])), dtype="float32")
    assert clip.size == int(cover_api.WECHAT_AUDIO_BUDGET_S * SR)
    assert abs(clip[100] - ramp[100]) < 1e-3, "截取应是原样切片"


def test_cover_clip_keeps_stereo_and_start(client):
    t = np.arange(SR * 10, dtype=np.float32) / SR
    stereo = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 220 * t)], axis=1)
    name = _session_wav("cover", stereo)
    r = client.post("/api/cover/clip", json={"src_name": name, "start_s": 8.0})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["seconds"] - 2.0) <= 0.02
    assert any("取到结尾" in n for n in body["notes"])  # 起点 8s 后只剩 2s
    clip, sr = sf.read(str(session_out.find(body["wav"])), dtype="float32")
    assert clip.ndim == 2 and clip.shape[0] == int(2.0 * sr)


def test_cover_clip_clamps_over_budget_and_reports(client):
    name = _session_wav("cover", _tone(60.0))
    r = client.post("/api/cover/clip", json={"src_name": name, "seconds": 90})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["seconds"] - cover_api.WECHAT_AUDIO_BUDGET_S) <= 0.01
    assert any("钳到" in n for n in body["notes"]), "静默钳制 = 静默改语义，必须报出来"


def test_cover_clip_errors(client):
    name = _session_wav("cover", _tone(5.0))
    assert client.post("/api/cover/clip", json={"src_name": "nope.wav"}).status_code == 404
    r = client.post("/api/cover/clip", json={"src_name": name, "start_s": 99})
    assert r.status_code == 400 and "起点" in r.json()["detail"]


# --------------------------------------------------------------- C：/bgm/start|stop|gain


def test_bgm_start_sends_an_absolute_path_to_the_worker(client, worker):
    bgm_name = _session_wav("bgm", _tone(2.0, freq=220.0))
    r = client.post("/api/soundboard/bgm/start", json={"bgm": bgm_name})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["playing"] is True and body["duration_s"] == 3.0  # 假 worker 的应答
    cmd = worker.commands[0]
    assert cmd["id"] == Path(bgm_name).stem
    assert cmd["bgm"] == str(session_out.find(bgm_name)), "必须给 worker 绝对路径"
    assert cmd["gain"] == soundboard.BGM_DEFAULT_GAIN and cmd["loop"] is True


def test_bgm_start_unknown_song_never_touches_the_worker(client, worker):
    r = client.post("/api/soundboard/bgm/start", json={"bgm": "nope.wav"})
    assert r.status_code == 404
    assert worker.commands == [], "歌都没找到，不该把空命令发给 worker"


def test_bgm_stop_and_gain_pass_through(client, worker):
    assert client.post("/api/soundboard/bgm/stop").json() == {"ok": True}
    r = client.post("/api/soundboard/bgm/gain", json={"gain": 0.42})
    assert r.status_code == 200 and r.json()["gain"] == 0.42
    assert worker.commands[0] == {"bgm_stop": True}
    assert worker.commands[1] == {"bgm_gain": 0.42}


# --------------------------------------------------------------- C：真 worker（注入假声卡）


class _FakeStream:
    """记录 OutputStream 参数与回调的桩：回调可以直接手动驱动。"""

    last: "_FakeStream | None" = None

    def __init__(self, samplerate=0, channels=0, dtype="", device=None, callback=None):
        self.samplerate, self.channels, self.device, self.callback = samplerate, channels, device, callback
        self.started = self.stopped = self.closed = False
        _FakeStream.last = self

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeSD:
    def __init__(self):
        self.played: list[tuple] = []
        self.stopped = 0

    def query_hostapis(self):
        return [{"name": "MME"}]

    def query_devices(self):
        return [{"hostapi": 0, "max_output_channels": 2, "name": "扬声器 (VB-Audio Virtual Cable)"}]

    def play(self, data, sr, device=None):
        self.played.append((data, sr, device))

    def stop(self):
        self.stopped += 1

    def OutputStream(self, **kw):
        return _FakeStream(**kw)


def _run_board_worker(monkeypatch, commands: list[dict], sd: _FakeSD):
    """假声卡 + 假 stdin/stdout 下跑真 worker（含垫底层）。返回 (退出码, 响应列表, sd)。"""
    import importlib
    import types

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(**{
        k: getattr(sd, k) for k in ("query_hostapis", "query_devices", "play", "stop", "OutputStream")
    }))
    monkeypatch.setattr(sys, "argv", ["board_worker.py", "vb-audio virtual cable", str(sfx_lib.SAMPLES_DIR)])
    bw = importlib.import_module("board_worker")
    out = io.StringIO()
    monkeypatch.setattr(bw.sys, "stdin", io.StringIO("".join(json.dumps(c) + "\n" for c in commands)))
    monkeypatch.setattr(bw.sys, "stdout", out)
    rc = bw.main()
    sys.modules.pop("board_worker", None)
    return rc, [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()], sd


def test_board_worker_bgm_layer_is_independent_from_one_shot(monkeypatch):
    """垫层与 one-shot 互不抢占：点音效不停歌，停歌不碰音效流。"""
    song = tmp = Path(sfx_lib.IMPORT_DIR)
    tmp.mkdir(parents=True, exist_ok=True)
    sf.write(str(tmp / "song.wav"), _tone(0.2, freq=220.0), 22050, subtype="PCM_16")

    sd = _FakeSD()
    rc, msgs, sd = _run_board_worker(
        monkeypatch,
        [
            {"bgm": str(song / "song.wav"), "id": "song", "gain": 0.25, "loop": True},
            {"wav": "x", "id": "boom"},  # one-shot：不应影响垫层
            {"bgm_gain": 0.4},
            {"bgm_stop": True},
        ],
        sd,
    )
    assert rc == 0
    assert [m["type"] for m in msgs] == ["ready", "bgm_playing", "playing", "bgm_gain", "bgm_stopped"]
    st = _FakeStream.last
    assert st is not None and st.started and st.stopped and st.closed
    assert st.samplerate == 22050 and st.device == 0
    # sd.stop 只该被 one-shot 的**抢占**调 1 次；垫层的启停不许碰全局播放器
    assert sd.stopped == 1
    assert len(sd.played) == 1, "one-shot 照常走全局播放器"


def test_board_worker_bgm_callback_fills_loops_and_applies_gain(monkeypatch):
    """回调：按 gain 缩放、到尾按 loop 回绕；loop=false 到尾填零且**不发任何行**。"""
    d = Path(sfx_lib.IMPORT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / "song.wav"), _tone(0.05, freq=220.0, amp=0.5), 22050, subtype="PCM_16")

    sd = _FakeSD()
    rc, msgs, _ = _run_board_worker(monkeypatch, [{"bgm": str(d / "song.wav"), "id": "song", "gain": 0.5}], sd)
    assert rc == 0
    st = _FakeStream.last
    assert st is not None and st.callback is not None
    out = np.zeros((2205, 1), dtype=np.float32)  # 0.1s @22050 = 正好两遍歌
    st.callback(out, 2205, None, None)
    assert np.abs(out[:, 0]).max() > 0.2, "0.5 幅 × 0.5 gain 应该有声"
    # 第二遍（1102..2205）得真的续上了（tone 起点 sin(0)=0，所以别拿最后一个样本说事）
    assert np.abs(out[1102:, 0]).max() > 0.2, "loop=true 应已回绕续上"

    # loop=false：数据播完只能填零，且 worker 不许因此多输出一行。
    # 不能顺带发 bgm_stop —— 那会把垫层状态清掉，回调就量不到「播完」了；
    # bgm_stop→bgm_stopped 的应答已由上面的独立用例覆盖。
    rc, msgs2, _ = _run_board_worker(
        monkeypatch,
        [{"bgm": str(d / "song.wav"), "id": "song", "gain": 0.5, "loop": False}],
        sd,
    )
    assert rc == 0
    assert [m["type"] for m in msgs2] == ["ready", "bgm_playing"], (
        "歌自然播完也不许自发输出行（协议不变量：一命令一响应）"
    )
    st = _FakeStream.last
    out2 = np.zeros((44100, 1), dtype=np.float32)  # 2s >> 0.05s 的歌
    st.callback(out2, 44100, None, None)
    head = out2[:1102, 0]
    assert np.abs(head).max() > 0.2, "第一遍该有声"
    assert np.abs(out2[22050:, 0]).max() == 0.0, "播完后填零静音"

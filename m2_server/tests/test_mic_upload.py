"""手机当麦克风（`POST /api/capture/mic/upload`）—— 假 ffmpeg / 假 RVC，不碰真模型。

为什么单独写一组而不是让桌宠那组顺带覆盖：这个端点是**手机遥控页唯一的上传入口**，
而它有三处「错了也不会报错」的地方 ——

  ① **超长录音**：微信单条语音上限 60 秒，而发送链路对超长音频是**直接拒绝**（不截断）。
     若这里不拦，用户要等完十几秒换声，才在最后一步被拒一次，手机上只会看到一句英文；
  ② **临时 raw 文件不清理**：手机每次按住都往会话目录丢一个几百 KB 的 webm，
     「退出即删」只清一次，一下午能堆到几 MB —— 这条只有断言目录内容才看得出来；
  ③ **两套命名**：`voice_id`（音色库 id，`kangaroo`）与 RVC 实验名（`kangaroo_v2`）
     传错不会报错，只会静默「没换声」。所以这里断言 `resolve_rvc_voice` 被真叫过，
     而不是只断言返回 200。

（这一条与桌宠的 `test_mic_capture.py` 是姐妹：那边管"麦克风在 PC 上"的采集会话，
这边管"麦克风在手机上"的一次上传 —— 两条链路的产物口径必须一致。）
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import capture_api  # noqa: E402
import common  # noqa: E402
import rvc_convert  # noqa: E402
import session_out  # noqa: E402

UPLOAD = "/api/capture/mic/upload"


# ---------------------------------------------------------------- 工具

def _wav_bytes(seconds: float, rate: int = 16000, channels: int = 1) -> bytes:
    """一段合法 wav 的字节（内容是无意义样本，只求头合法、时长对）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x01" * int(rate * seconds) * channels)
    return buf.getvalue()


@pytest.fixture()
def client():
    import server
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """把"解码"与"换声"两步都换成假实现，并记录调用参数。

    `_decode_to_wav` 是模块级函数（端点直接调它），所以换掉它不会碰真 ffmpeg；
    `_convert_for_send` 里的 `rvc_convert` / `resolve_rvc_voice` 是**函数内延迟 import**，
    所以补丁要打在 `rvc_convert` 模块的属性上（打在 capture_api 上无效）。
    """
    calls: dict = {"decode": 0, "convert": [], "resolve": []}
    decode_seconds = {"value": 3.0}
    decode_rate = {"value": (16000, 1)}

    def fake_decode(raw: Path, dst: Path) -> None:
        calls["decode"] += 1
        assert raw.exists(), "解码前 raw 文件必须已经落盘"
        rate, ch = decode_rate["value"]
        dst.write_bytes(_wav_bytes(decode_seconds["value"], rate, ch))

    def fake_resolve(voice_id: str) -> str:
        calls["resolve"].append(voice_id)
        return {"kangaroo": "kangaroo_v2"}.get(voice_id, "")

    def fake_convert(path: Path, voice: str, pitch: int = 0, index_rate: float = 0.5) -> Path:
        calls["convert"].append({"path": Path(path).name, "voice": voice, "pitch": pitch})
        out = session_out.new_path(f"{Path(path).stem}_{voice}")
        out.write_bytes(b"RIFF____fake")
        return out

    monkeypatch.setattr(capture_api, "_decode_to_wav", fake_decode)
    monkeypatch.setattr(rvc_convert, "resolve_rvc_voice", fake_resolve)
    monkeypatch.setattr(rvc_convert, "rvc_convert", fake_convert)
    return {"calls": calls, "decode_seconds": decode_seconds, "decode_rate": decode_rate}


def _post(client, data: bytes | None = None, **form):
    files = {} if data is None else {"file": ("phone.webm", data, "audio/webm")}
    return client.post(UPLOAD, files=files or None, data=form)


# ---------------------------------------------------------------- 主链路

def test_upload_converts_and_returns_sendable_wav(client, fake_pipeline):
    """★ 主链路：手机 webm → 换声 → 返回**可直接发给微信**的 wav 与播放地址。

    返回字段与 `/capture/mic/stop` 同名（`file`/`url`/`duration_s`）是刻意的：
    手机拿到 `file` 接着 POST `/api/wechat/send_voice {wav: file}` 就完事，
    页面不需要知道"录音/换声/发送"在后端是怎么分层的。
    """
    r = _post(client, _wav_bytes(1.0), voice_id="kangaroo", pitch="3")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["file"].endswith(".wav") and "mic_phone" in d["file"]
    assert d["url"] == f"/api/media/outputs/{session_out.rel_url(d['file'])}"
    assert d["duration_s"] == pytest.approx(3.0, abs=0.05)
    assert d["source"] == "phone"
    assert d["converted"] is True
    assert d["voice_id"] == "kangaroo"
    # ★ 命名换算真的走了 resolve（`kangaroo` → `kangaroo_v2`），否则会静默不换声
    assert fake_pipeline["calls"]["resolve"] == ["kangaroo"]
    assert d["rvc_voice"] == "kangaroo_v2"
    assert fake_pipeline["calls"]["convert"][0]["voice"] == "kangaroo_v2"
    assert fake_pipeline["calls"]["convert"][0]["pitch"] == 3
    # 两个文件两个用途：`file` = 换声后的（要发的），`raw_file` = 未换声的 16k wav。
    # 第二个就是排查"录错了还是换声错了"的那个对照物 —— 与桌宠按住说话的 `raw_file`
    # 同一含义（那边是麦克风原始录音，这边是手机录音解码后的 16k 版）。
    # 上传的原始 webm 本身**不留**（手机每次按住都传一个，留着就是垃圾）。
    assert d["raw_file"] != d["file"]
    assert d["raw_file"].endswith(".wav") and d["raw_file"].startswith("mic_phone_")
    assert "_raw_" not in d["raw_file"]


def test_upload_raw_skips_conversion(client, fake_pipeline):
    """`raw=1`：只解码不换声 —— 这是排查"录错了还是换声错了"的唯一入口。"""
    r = _post(client, _wav_bytes(1.0), voice_id="kangaroo", raw="true")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["converted"] is False
    assert fake_pipeline["calls"]["convert"] == []
    assert d["url"].startswith("/api/media/outputs/")


def test_upload_cleans_up_raw_temp_file(client, fake_pipeline):
    """★ 每次按住都上传一个 webm：raw 必须用完即删，否则会话目录会堆垃圾。"""
    _post(client, _wav_bytes(1.0), voice_id="kangaroo", raw="true")
    leftovers = [p.name for p in session_out.session_dir().glob("mic_phone_raw_*")]
    assert leftovers == [], f"临时 raw 没清掉：{leftovers}"


def test_upload_empty_body_is_readable(client):
    r = _post(client, b"", voice_id="kangaroo")
    assert r.status_code == 400
    assert "上传内容为空" in r.json()["detail"]


def test_upload_requires_file_field(client):
    """少传 `file` 时是 FastAPI 的 422 —— 不是 500，也不是静默成功。"""
    r = client.post(UPLOAD, data={"voice_id": "kangaroo"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 时长闸口

def test_upload_too_short_tells_you_to_hold_longer(client, fake_pipeline):
    fake_pipeline["decode_seconds"]["value"] = 0.1
    r = _post(client, _wav_bytes(1.0), voice_id="kangaroo")
    assert r.status_code == 400
    assert "太短" in r.json()["detail"]
    assert fake_pipeline["calls"]["convert"] == [], "太短的录音不该白跑一次换声"


def test_upload_rejects_over_wechat_limit_before_converting(client, fake_pipeline):
    """★ 超 60 秒要在这里就拒：发送链路是**直接拒绝**超长音频，不截断。

    若让它走到换声那一步，用户白等十几秒才在最后一步被拒 —— 而这里拒的时候
    还什么都没算，代价是零。
    """
    fake_pipeline["decode_seconds"]["value"] = 60.5
    r = _post(client, _wav_bytes(1.0), voice_id="kangaroo")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "60 秒" in detail and "60 秒" in detail
    assert fake_pipeline["calls"]["convert"] == []
    # 被拒的产物不该留在会话目录里（否则每次超长都多一个文件）
    assert [p.name for p in session_out.session_dir().glob("mic_phone_*") if "raw" not in p.name] == []


def test_upload_oversize_rejected(client, fake_pipeline, monkeypatch):
    """大小闸口跟着 `common.MAX_UPLOAD_BYTES` 走（不另写一个数字）。"""
    monkeypatch.setattr(common, "MAX_UPLOAD_BYTES", 10)
    r = _post(client, _wav_bytes(1.0), voice_id="kangaroo")
    assert r.status_code == 413
    assert "过大" in r.json()["detail"]


# ---------------------------------------------------------------- 真 ffmpeg

def test_upload_garbage_audio_is_a_readable_error(client, fake_pipeline, monkeypatch):
    """解不开时给人话（手机上看到的不能是 traceback 或英文 ffmpeg 输出）。"""
    monkeypatch.undo()  # 让真 _decode_to_wav 跑
    monkeypatch.setattr(rvc_convert, "resolve_rvc_voice", lambda v: "kangaroo_v2")
    monkeypatch.setattr(
        rvc_convert, "rvc_convert", lambda *a, **k: pytest.fail("解不开的音频不该走到换声")
    )
    try:
        common.find_ffmpeg()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"本机没有 ffmpeg：{e}")
    r = _post(client, b"this is not audio at all", voice_id="kangaroo")
    assert r.status_code == 400
    assert "解不开" in r.json()["detail"]


def test_decode_to_wav_normalises_to_16k_mono(tmp_path):
    """`_decode_to_wav` 的产物口径：16k 单声道（RVC 链路就吃这个）。

    48k 立体声进去、16k 单声道出来 —— 断言的是 ffmpeg 参数真的生效了，
    而不是"文件存在"（参数写错时文件照样存在，只是采样率不对，而下游不会报错）。
    """
    try:
        common.find_ffmpeg()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"本机没有 ffmpeg：{e}")
    src = tmp_path / "in.wav"
    src.write_bytes(_wav_bytes(0.5, rate=48000, channels=2))
    dst = tmp_path / "out.wav"
    capture_api._decode_to_wav(src, dst)
    with wave.open(str(dst), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getnframes() == pytest.approx(8000, rel=0.05)

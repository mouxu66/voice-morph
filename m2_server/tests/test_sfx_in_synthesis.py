"""音效标记 `[爆炸]` 混进**输字合成**链路（2026-09-25）。

此前只有微信发送那条路认标记（`wechat_voice._split_for_budget`），于是用户在
「单段合成 / 试听 / 有声书 / 桌宠说话」里写 `[爆炸]`，听到的是有人把"爆炸"
三个字念出来 —— 界面上的「插音效」工具栏其实在骗人。本文件守护修好之后的三条：

  1. **标记不进 TTS**：送进合成器的是剥掉标记的纯文本；
  2. **音效真的在产物里，且位置对**：贴在那段文字之后（不是全塞到末尾）；
  3. **写错名字要 400**，不许静默当普通文字念出来。

⚠️ 素材目录是**用户真实数据**，本文件一律用临时目录造假库
   （同类事故见 `docs/犯错档案-工程.md` §8.36 / §8.37）。

⚠️ 变异测试（本仓传统，见 `docs/犯错档案-工程.md` §8.19）：
   把 `tts_api._synth_marked` 里 `if unknown:` 的 400 删掉，或把
   `sfx_mark.parse` 的标记剔除改回用原文，本文件必须**变红**。
"""

from __future__ import annotations

import io
import struct
import sys
import wave
from pathlib import Path

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("soundfile")

import config as cfg  # noqa: E402
import sfx_lib  # noqa: E402

SR = 16000
#: 假素材库：id → (显示名, 秒数, 采样值)。采样值给三个**互不相同**的常量，
#: 这样"音效到底被插在哪一段之间"可以逐帧断言，而不是只看总时长。
_FAKE = {
    "boom": ("爆炸", 0.5, 8000),
    "applause": ("掌声", 0.25, 4000),
}


def _const_wav_bytes(frames: int, value: int, sr: int = SR) -> bytes:
    """一段**常量值**单声道 16bit wav 的字节（走标准库 `wave`，不 spawn ffmpeg）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<h", value) * frames)
    return buf.getvalue()


def _write_const_wav(path: Path, frames: int, value: int, sr: int = SR) -> None:
    path.write_bytes(_const_wav_bytes(frames, value, sr))


def _amp(value: int) -> float:
    """16bit 采样值 → soundfile 读出来的 float32 幅度。"""
    return value / 32768.0


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """输出目录指到 tmp_path：会话产物不许落到用户真实的 outputs/。"""
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def fake_sfx(monkeypatch, tmp_path_factory):
    """假素材库：`list_samples`（名字索引）+ `resolve_path`（真文件）都打掉。"""
    d = tmp_path_factory.mktemp("tts_sfx")
    files: dict[str, Path] = {}
    for sid, (_name, sec, value) in _FAKE.items():
        p = d / f"{sid}.wav"
        _write_const_wav(p, int(sec * SR), value)
        files[sid] = p

    def _list_samples():
        return [
            {"id": sid, "name": name, "tags": [], "icon": "", "duration_s": sec}
            for sid, (name, sec, _v) in _FAKE.items()
        ]

    def _resolve_path(sample_id: str):
        sid = str(sample_id or "")
        if sid not in files:
            raise sfx_lib.SfxError(f"没有这个音效：{sid}", status=404)
        return files[sid], True

    monkeypatch.setattr(sfx_lib, "list_samples", _list_samples)
    monkeypatch.setattr(sfx_lib, "resolve_path", _resolve_path)
    return files


# ---------------- sfx_lib.assemble：位置 / 静音 / 容错 ----------------


@pytest.fixture
def one_sfx(monkeypatch):
    """把 `load_pcm` 打成一个常量 PCM：`assemble` 的用例不必碰磁盘。"""

    def _load(sid: str):
        return np.full(50, _amp(8000), dtype=np.float32), 1000

    monkeypatch.setattr(sfx_lib, "load_pcm", _load)
    return _load


def test_assemble_puts_sfx_after_its_own_piece(one_sfx):
    """★ 位置语义：音效贴在**它前面那一片**之后，不是全塞到末尾。"""
    p0 = np.full(100, 0.5, dtype=np.float32)
    p1 = np.full(100, 0.2, dtype=np.float32)
    out, notes = sfx_lib.assemble([p0, p1], 1000, ["boom", None], sent_gap_s=0, sfx_gap_s=0)
    assert notes == []
    assert out.size == 250
    assert out[:100] == pytest.approx(0.5)
    assert out[100:150] == pytest.approx(_amp(8000))
    assert out[150:250] == pytest.approx(0.2)


def test_assemble_adds_both_gaps(one_sfx):
    """段间留 sent_gap、音效前留 sfx_gap —— 都是真实存在的声音，不能被省掉。"""
    p0 = np.full(100, 0.5, dtype=np.float32)
    p1 = np.full(100, 0.2, dtype=np.float32)
    out, _n = sfx_lib.assemble(
        [p0, p1], 1000, ["boom", None], sent_gap_s=0.1, sfx_gap_s=0.05
    )
    # 片0(100) + sfx_gap(50) + 音效(50) + sent_gap(100) + 片1(100)
    assert out.size == 400
    assert out[100:150] == pytest.approx(0.0), "音效前要垫静音，否则咬住上一个字"
    assert out[150:200] == pytest.approx(_amp(8000))
    assert out[200:300] == pytest.approx(0.0), "两片之间要有停顿"
    assert out[300:400] == pytest.approx(0.2)


def test_assemble_resamples_sfx_to_voice_rate(monkeypatch):
    """素材是 8k 而人声是 16k 时必须重采样，否则音效会被拉长一倍。"""

    def _load(sid: str):
        return np.full(80, 0.3, dtype=np.float32), 8000

    monkeypatch.setattr(sfx_lib, "load_pcm", _load)
    out, _n = sfx_lib.assemble(
        [np.zeros(10, dtype=np.float32)], 16000, ["boom"], sent_gap_s=0, sfx_gap_s=0
    )
    assert out.size == 170, f"8k 的 80 帧在 16k 下应是 160 帧，实际 {out.size - 10}"


def test_assemble_broken_sfx_is_a_note_not_a_crash(monkeypatch):
    """一条素材读不出来只记一笔 —— 不该让整段语音合成不出来（与 mix_into 同约定）。"""

    def _load(sid: str):
        raise sfx_lib.SfxError("素材读取失败：boom.wav（坏文件）")

    monkeypatch.setattr(sfx_lib, "load_pcm", _load)
    out, notes = sfx_lib.assemble(
        [np.full(10, 0.5, dtype=np.float32)], 1000, ["boom"], sent_gap_s=0, sfx_gap_s=0
    )
    assert out.size == 10
    assert any("坏文件" in n for n in notes), f"没报出读失败：{notes}"


def test_assemble_empty_input_returns_empty():
    out, notes = sfx_lib.assemble([], 16000, [], sent_gap_s=0, sfx_gap_s=0)
    assert out.size == 0
    assert notes == []


# ---------------- tts_api.synth_wav：输字合成认标记 ----------------


@pytest.fixture
def tts_env(monkeypatch, tmp_path, fake_sfx):
    """把 `synth_wav` 的外部依赖全打掉：音色参考音 + qwen 合成器。

    假合成器的时长**由字数决定**（0.05s/字），产物的幅度按调用次序给
    1000 / 3000 —— 于是"音效被插在第一段与第二段之间"可以逐帧验证。
    """
    import qwen3_tts
    import tts_api

    monkeypatch.setattr(tts_api, "voice_ref", lambda v: (tmp_path / "ref.wav", ""))

    calls: list[str] = []

    def _tts(text, **kw):
        calls.append(text)
        value = 1000 if len(calls) == 1 else 3000
        return _const_wav_bytes(max(1, int(len(text) * 0.05 * SR)), value)

    monkeypatch.setattr(qwen3_tts, "tts", _tts)
    return calls


def _read(path: Path) -> np.ndarray:
    import soundfile as sf

    data, _sr = sf.read(str(path), dtype="float32", always_2d=False)
    return np.asarray(data)


def test_synth_wav_marker_is_stripped_and_sfx_sits_in_the_middle(tts_env):
    """★ 端到端：标记被剔掉，音效出现在两段文字**之间**。"""
    import tts_api

    out, dur, _vid = tts_api.synth_wav("你好啊 [爆炸] 再见", "v")

    assert tts_env == ["你好啊", "再见"], f"标记必须剥掉后再送合成：{tts_env}"
    assert "[爆炸]" not in "".join(tts_env) and "爆炸" not in "".join(tts_env)

    pcm = _read(out)
    f0 = int(0.15 * SR)  # "你好啊" 3 字 × 0.05s
    sfx_gap = int(0.15 * SR)
    sfx = int(0.5 * SR)
    sent_gap = int(0.3 * SR)
    f1 = int(0.10 * SR)  # "再见" 2 字
    assert pcm.size == f0 + sfx_gap + sfx + sent_gap + f1, f"总长不对：{pcm.size}"
    assert pcm[:f0] == pytest.approx(_amp(1000)), "第一段文字不对位"
    assert pcm[f0 : f0 + sfx_gap] == pytest.approx(0.0)
    assert pcm[f0 + sfx_gap : f0 + sfx_gap + sfx] == pytest.approx(_amp(8000)), "音效没插在中间"
    assert pcm[-f1:] == pytest.approx(_amp(3000)), "第二段文字不对位"
    assert dur == pytest.approx(pcm.size / SR, abs=0.11)


def test_synth_wav_unknown_name_raises_400_before_synth(tts_env):
    """★★ 写错名字必须 400，且**一句都不许拿去合成**（用户当场能看到可用清单）。"""
    from fastapi import HTTPException

    import tts_api

    with pytest.raises(HTTPException) as ei:
        tts_api.synth_wav("你好 [爆炸声] 再见", "v")
    assert ei.value.status_code == 400
    assert "没有这个音效" in ei.value.detail and "可用" in ei.value.detail
    assert tts_env == [], "未知音效必须在合成前报错，不能白跑 TTS"


def test_synth_wav_without_mark_is_unchanged(tts_env):
    """★ 零回归：没有标记时走原来那条路 —— 一次合成、**原文一字不改**。"""
    import tts_api

    out, dur, _vid = tts_api.synth_wav("你好啊再见", "v")
    assert tts_env == ["你好啊再见"]
    pcm = _read(out)
    assert pcm.size == int(0.25 * SR), "无标记时不许加任何静音/音效"
    assert dur == pytest.approx(0.25, abs=0.05)


def test_synth_wav_mark_only_text_still_makes_sfx(tts_env):
    """整段就是一个标记（`[掌声]`）→ 没有字可念，但**音效照出**（与微信路径同口径）。"""
    import tts_api

    out, _dur, _vid = tts_api.synth_wav("[掌声]", "v")
    assert tts_env == [], "没有要念的字，不该叫合成器"
    pcm = _read(out)
    # 10ms 占位 + sfx_gap + 掌声（0.25s）
    assert pcm.size == pytest.approx(0.01 * 24000 + 0.15 * 24000 + 0.25 * 24000, abs=16)
    assert pcm[-int(0.25 * 24000) :] == pytest.approx(_amp(4000)), "音效没出来"


# ---------------- audiobook：同一套语义 ----------------


def _fake_qwen(calls: list[str]):
    def _tts(text, **kw):
        calls.append(text)
        value = 1000 if len(calls) == 1 else 3000
        return _const_wav_bytes(max(1, int(len(text) * 0.05 * SR)), value)

    return _tts


def test_audiobook_pieces_of_plain_text_is_single_piece(fake_sfx):
    """没有标记 → 原样单片返回，调用方据此走旧路径。"""
    import audiobook

    assert audiobook._pieces_of("普通的一句话") == (["普通的一句话"], [None])


def test_audiobook_pieces_of_keeps_placeholder_piece(fake_sfx):
    """整片被标记吃掉 → 片段是 `None`（占位静音），但音效不许丢。"""
    import audiobook

    texts, after = audiobook._pieces_of("[掌声]")
    assert texts == [None]
    assert after == ["applause"]


def test_audiobook_synth_pieces_inserts_sfx_between_pieces(fake_sfx):
    """★ `_synth_pieces` 的产物里音效在两段文字之间（与 tts_api 同一套语义）。"""
    import audiobook

    calls: list[str] = []
    pcm, sr = audiobook._synth_pieces(
        ["你好啊", "再见"], ["boom", None], _fake_qwen(calls), None, "v"
    )
    assert calls == ["你好啊", "再见"]
    assert sr == SR
    f0 = int(0.15 * SR)
    assert pcm[:f0] == pytest.approx(_amp(1000))
    assert pcm[f0 + int(0.15 * SR) : f0 + int(0.15 * SR) + int(0.5 * SR)] == pytest.approx(
        _amp(8000)
    )


def test_audiobook_synth_pieces_without_mark_returns_raw(one_sfx):
    """无标记时不组装、不加静音 —— 现有产物的时长一个字节都不变。"""
    import audiobook

    calls: list[str] = []
    pcm, sr = audiobook._synth_pieces(["你好"], [None], _fake_qwen(calls), None, "v")
    assert pcm.size == int(0.1 * SR)
    assert sr == SR


def test_audiobook_rejects_unknown_mark(fake_sfx):
    """入口同步校验：错名字要在起线程之前报 400（线程里报错用户看不见）。"""
    from fastapi import HTTPException

    import audiobook

    audiobook._reject_unknown_marks(["正常一句。"])  # 不抛
    with pytest.raises(HTTPException) as ei:
        audiobook._reject_unknown_marks(["正常一句。", "来一声 [爆炸声] 吧"])
    assert ei.value.status_code == 400
    assert "没有这个音效" in ei.value.detail

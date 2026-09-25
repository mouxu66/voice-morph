"""会话产物（`session_out.py`）的回归用例。

「合成 → 应用内试听 → 退出清空，想留才保存」这条链路有三处**只有靠测试才守得住**：

1. **默认不落盘**：产物必须落在 `outputs/.session/`，而不是 `outputs/` 根 ——
   否则"退出即删"就删不到它，用户磁盘里还是堆文件。
2. **保存幂等**：重复点「保存」不许产生第二份文件、第二条历史。用户手会抖，
   而重复登记会在「作品库」里留下两条指向同一个 wav 的记录 —— 删掉其中一条，
   另一条就悬空了（`history` 的删除是按 wav 名删文件的）。
3. **裸名契约**：对外只给裸名（`tts_x.wav`），不带目录成分。消费侧
   （`soundboard._outputs_wav`、微信链路）显式拒绝含 `/` 的名字，
   一旦这里漏出相对路径，那些链路会静默地"找不到文件"。

★ 铁律：**不许模块级早绑定 `cfg.OUTPUTS_DIR`**。`session_out` 全部走
`session_dir()` 现读，所以下面每个用例都能用 monkeypatch 把它指到 tmp_path；
哪天有人在模块里加了 `SESSION_DIR = cfg.OUTPUTS_DIR / ".session"`，这些用例会
**串到真实 outputs 目录**并且**假绿**（写到用户真实数据上还报通过）。
这一条由 `test_output_isolation.py` 的 AST 扫描守着，本文件只需覆盖行为。
"""

import sys
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import config as cfg  # noqa: E402
import session_out  # noqa: E402


@pytest.fixture()
def outputs(tmp_path, monkeypatch):
    """把 `cfg.OUTPUTS_DIR` 指到临时目录（`session_out` 每次现读，所以立刻生效）。"""
    d = tmp_path / "outputs"
    d.mkdir()
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", d)
    # `_SAVED` 是进程级记忆，用例之间必须隔离，否则第二条用例会命中上一条的幂等缓存
    session_out._SAVED.clear()
    return d


@pytest.fixture()
def wav_bytes():
    """一段最小的合法 WAV（44 字节头 + 8 字节静音），够 `soundfile` 读时长。"""
    import struct

    frames = 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + frames)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", frames)
        + b"\x80" * frames
    )


def _make_session_wav(name: str, data: bytes) -> Path:
    p = session_out.ensure() / name
    p.write_bytes(data)
    return p


# ---------------- 目录与路径 ----------------


def test_session_dir_is_under_outputs_dot_session(outputs):
    """会话目录必须挂在 OUTPUTS_DIR 之下（media_api 的白名单靠 is_relative_to 放行子目录）。"""
    assert session_out.session_dir() == outputs / ".session"


def test_new_path_lands_in_session_dir(outputs):
    p = session_out.new_path("tts")
    assert p.parent == outputs / ".session"
    assert p.name.startswith("tts_")
    assert p.suffix == ".wav"


def test_is_session_distinguishes_root_from_session(outputs, wav_bytes):
    """根目录文件与会话文件必须分得清 —— `rel_url` / `derived_dir` 都靠它。"""
    in_session = _make_session_wav("tts_a.wav", wav_bytes)
    in_root = outputs / "tts_b.wav"
    in_root.write_bytes(wav_bytes)

    assert session_out.is_session(in_session) is True
    assert session_out.is_session(in_root) is False


def test_derived_dir_follows_source(outputs, wav_bytes):
    """派生件（RVC 换声、裁静音）必须落在与源**同一个**目录。

    输入在会话目录而派生件写 outputs 根 ⇒ "即用即删"每次都漏掉一个文件。
    """
    in_session = _make_session_wav("tts_c.wav", wav_bytes)
    in_root = outputs / "tts_d.wav"
    in_root.write_bytes(wav_bytes)

    assert session_out.derived_dir(in_session) == outputs / ".session"
    assert session_out.derived_dir(in_root) == outputs


# ---------------- 裸名契约 ----------------


@pytest.mark.parametrize(
    "bad",
    ["", "   ", ".", "..", "../x.wav", "a/b.wav", "a\\b.wav", "/abs/x.wav"],
)
def test_bare_name_rejects_traversal_and_dirs(bad):
    """带目录成分/回溯的名字一律拒收（`find` 返回 None，`save` 报错）。

    这是**路径穿越**的第一道门：请求体里的 `wav` 直接来自前端。
    """
    assert session_out.find(bad) is None
    assert session_out.save(bad)["ok"] is False
    assert session_out.rel_url(bad) == ""


# ---------------- 定位 ----------------


def test_find_prefers_session_over_root(outputs, wav_bytes):
    """同名时优先会话目录：正在被试听的那份一定是用户此刻看到的。"""
    _make_session_wav("tts_dup.wav", b"SESSION")
    (outputs / "tts_dup.wav").write_bytes(b"ROOT")

    hit = session_out.find("tts_dup.wav")
    assert hit is not None
    assert hit.read_bytes() == b"SESSION"


def test_find_falls_back_to_root(outputs, wav_bytes):
    """已保存过的文件（只在根目录）也必须能找到 —— 微信链路会回查它。"""
    (outputs / "tts_saved.wav").write_bytes(wav_bytes)
    assert session_out.find("tts_saved.wav") == outputs / "tts_saved.wav"


def test_newest_tts_sees_both_dirs(outputs, wav_bytes, monkeypatch):
    """`newest_tts` 必须**同时**看会话目录与根目录。

    这是"收敛了四处 glob"的那个方法：任何一处漏掉会话目录，那条链路就会
    假装"还没有合成过"（微信按住说话会报"请先合成一条语音"）。
    """
    # 先放一个根目录的（旧）
    old = outputs / "tts_old.wav"
    old.write_bytes(wav_bytes)
    assert session_out.newest_tts() == old

    # 再放一个会话目录的（新）—— 必须胜出
    new = _make_session_wav("tts_new.wav", wav_bytes)
    import os
    import time

    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    assert session_out.newest_tts() == new


def test_newest_tts_returns_none_when_empty(outputs):
    assert session_out.newest_tts() is None


# ---------------- URL 映射 ----------------


def test_rel_url_marks_session_files(outputs, wav_bytes):
    """未保存 → `.session/x.wav`；已保存 → `x.wav`。前端拿到就能直接播。"""
    _make_session_wav("tts_s.wav", wav_bytes)
    assert session_out.rel_url("tts_s.wav") == ".session/tts_s.wav"

    # 复制进根目录（模拟"保存过"）：现在会话里那份没了 → 退回裸名
    (session_out.session_dir() / "tts_s2.wav").write_bytes(wav_bytes)
    assert session_out.rel_url("tts_s2.wav") == ".session/tts_s2.wav"

    (outputs / "tts_r.wav").write_bytes(wav_bytes)
    assert session_out.rel_url("tts_r.wav") == "tts_r.wav"


# ---------------- 保存 ----------------


def test_save_copies_to_root_and_registers(outputs, wav_bytes, monkeypatch):
    """保存 = 复制到 outputs 根 + 登记历史。两件事都要真发生。"""
    _make_session_wav("tts_k.wav", wav_bytes)
    calls = []

    import history

    monkeypatch.setattr(
        history,
        "register",
        lambda *a, **kw: calls.append((a, kw)) or "id-1",
    )

    res = session_out.save("tts_k.wav", kind="tts", voice_id="v1", input_text="你好")
    assert res["ok"] is True
    assert res["already"] is False
    assert res["item_id"] == "id-1"
    assert res["url"] == "/api/media/outputs/tts_k.wav"

    # 文件真的出现在了根目录
    assert (outputs / "tts_k.wav").is_file()
    # 会话目录那份**不删**：用户可能还要再试听一遍
    assert (session_out.session_dir() / "tts_k.wav").is_file()
    # 登记时 url 是 outputs 根形态（不是 .session/）
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[2] == "tts_k.wav"  # wav 名
    assert args[3] == "/api/media/outputs/tts_k.wav"  # url


def test_save_is_idempotent(outputs, wav_bytes, monkeypatch):
    """★ 重复保存不该产生第二份文件、第二条历史。

    后果不像看起来那么轻：`history` 的删除按 wav 名删文件 —— 两条记录指向同一个
    wav 时，删掉一条会把另一条也变成悬空记录。
    """
    _make_session_wav("tts_idem.wav", wav_bytes)
    calls = []

    import history

    monkeypatch.setattr(history, "register", lambda *a, **kw: calls.append(1) or "id-1")

    first = session_out.save("tts_idem.wav")
    second = session_out.save("tts_idem.wav")

    assert first["already"] is False
    assert second["already"] is True
    assert second["item_id"] == first["item_id"]
    assert len(calls) == 1, "第二次保存又登记了一条历史"


def test_save_reports_missing_session_file(outputs):
    """会话里没有的文件要**如实报错**，而不是去根目录"顺手补一份"。"""
    res = session_out.save("tts_nope.wav")
    assert res["ok"] is False
    assert "找不到" in res["error"]


def test_save_rejects_bad_name(outputs):
    res = session_out.save("../evil.wav")
    assert res["ok"] is False
    assert "非法" in res["error"]


# ---------------- 清空 ----------------


def test_purge_empties_dir_but_keeps_it(outputs, wav_bytes):
    """`purge` 清内容、留目录本身 —— 且**只动会话目录**，不碰 outputs 根。"""
    _make_session_wav("tts_p1.wav", wav_bytes)
    _make_session_wav("tts_p2.wav", wav_bytes)
    (outputs / "tts_keep.wav").write_bytes(wav_bytes)

    removed = session_out.purge()
    assert removed == 2
    assert session_out.session_dir().is_dir(), "目录本身被删了（下次 mkdir 又会建回来，但没必要）"
    assert list(session_out.session_dir().iterdir()) == []
    assert (outputs / "tts_keep.wav").is_file(), "purge 把手伸到了 outputs 根"


def test_purge_on_missing_dir_is_noop(outputs):
    assert session_out.purge() == 0


def test_purge_forgets_saved_memory(outputs, wav_bytes, monkeypatch):
    """清空后"已保存"的记忆要跟着失效。

    否则：清空 → 合成一个新文件**恰好同名**（毫秒时间戳碰撞，罕见但可能）→
    用户点保存，却拿到"已保存"的空结果。
    """
    _make_session_wav("tts_mem.wav", wav_bytes)

    import history

    monkeypatch.setattr(history, "register", lambda *a, **kw: "id-x")
    assert session_out.save("tts_mem.wav")["ok"] is True
    assert session_out._SAVED, "保存后没有记住（幂等判据失效）"

    session_out.purge()
    assert session_out._SAVED == {}

"""history.py 变声任务持久化单测（用 monkeypatch 隔离 outputs 目录）。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as cfg  # noqa: E402
import history  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把 history 的输出目录切到临时目录，避免污染真实 outputs/。"""
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    history.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 写一个 dummy wav 供 delete 测试
    return tmp_path


def test_register_and_query(isolated):
    hid = history.register(
        "tts", "abc", "tts_1.wav", "/api/media/outputs/tts_1.wav", 2.5, input_text="你好"
    )
    assert hid
    res = history.query()
    assert res["total"] == 1
    item = res["items"][0]
    assert item["kind"] == "tts"
    assert item["voice_id"] == "abc"
    assert item["input_text"] == "你好"


def test_seedvc_kind_preserved(isolated):
    """Seed-VC（表达力变声）产出必须保留 seedvc 类型，不得静默降级为 tts。

    回归（2026-09-19）：seed_vc.py 以 kind="seedvc" 登记，但 _KINDS 缺该值，
    被 register 降级成 "tts"——作品库全显示成「语音合成」，无法按类型筛选。
    """
    hid = history.register("seedvc", "kangaroo", "s1.wav", "/api/media/outputs/s1.wav", 3.0)
    assert hid
    item = history.query(kind="seedvc")["items"][0]
    assert item["kind"] == "seedvc"
    assert history.query(kind="tts")["total"] == 0


def test_query_filter_by_kind_and_voice(isolated):
    history.register("tts", "a", "1.wav", "/x", 1.0)
    history.register("fx", "b", "2.wav", "/x", 1.0)
    history.register("tts", "a", "3.wav", "/x", 1.0)
    assert history.query(kind="tts")["total"] == 2
    assert history.query(kind="fx")["total"] == 1
    assert history.query(voice_id="a")["total"] == 2
    assert history.query(kind="tts", voice_id="a")["total"] == 2


def test_query_desc_order_and_pagination(isolated):
    for i in range(5):
        history.register("tts", "a", f"{i}.wav", "/x", 1.0)
    res = history.query(limit=2, offset=0)
    assert len(res["items"]) == 2
    assert res["total"] == 5
    # ts 倒序：最新（第4条）在前
    assert res["items"][0]["wav"] == "4.wav"
    res2 = history.query(limit=2, offset=4)
    assert res2["items"][0]["wav"] == "0.wav"


def test_corrupt_line_skipped(isolated):
    hid = history.register("tts", "a", "1.wav", "/x", 1.0)
    with open(history.HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    res = history.query()
    assert res["total"] == 1  # 坏行被跳过
    assert res["items"][0]["id"] == hid


def test_delete_record_and_file(isolated):
    (isolated / "tts_x.wav").write_bytes(b"RIFF")
    history.register("tts", "a", "tts_x.wav", "/x", 1.0)
    item = history.query()["items"][0]
    res = history.delete(item["id"], keep_file=False)
    assert res["ok"] is True
    assert res["file_gone"] is False  # 文件存在并被删除
    assert history.query()["total"] == 0
    assert not (isolated / "tts_x.wav").exists()


def test_delete_keep_file(isolated):
    (isolated / "tts_x.wav").write_bytes(b"RIFF")
    history.register("tts", "a", "tts_x.wav", "/x", 1.0)
    item = history.query()["items"][0]
    history.delete(item["id"], keep_file=True)
    assert (isolated / "tts_x.wav").exists()  # 文件保留


def test_delete_missing_id(isolated):
    res = history.delete("nonexistent")
    assert res["ok"] is False


def test_register_failure_does_not_raise(isolated, monkeypatch):
    """写失败只返回空串、不抛异常（磁盘满/不可写场景）。"""
    # 父路径是「文件」→ mkdir(parents=True) 失败，触发 register 的 except 分支
    (isolated / "blocker").write_bytes(b"x")
    monkeypatch.setattr(history, "HISTORY_FILE", isolated / "blocker" / "h.jsonl")
    hid = history.register("tts", "a", "1.wav", "/x", 1.0)
    assert hid == ""


def test_trim_keeps_max_items(isolated, monkeypatch):
    monkeypatch.setattr(history, "_MAX_ITEMS", 3)
    for i in range(5):
        history.register("tts", "a", f"{i}.wav", "/x", 1.0)
    res = history.query()
    assert res["total"] == 3
    # 保留最新 3 条（4,3,2）
    wavs = [r["wav"] for r in res["items"]]
    assert wavs == ["4.wav", "3.wav", "2.wav"]

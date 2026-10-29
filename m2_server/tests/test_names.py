"""chat_radar.names 单测：昵称条分组、特征匹配、视觉读名的解析与降级。

全部用合成图（PIL 画字），不碰真微信、不碰 GDI、不启模型。
真实数据上的标定见 `names.py` 头部注释（同人 0.772/1.000，不同人 ≤0.425）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_radar import names as nz  # noqa: E402

FONT_PATHS = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc")


def _font(size: int = 11):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def band(text: str, dy: int = 0, dx: int = 0, w: int = 200, h: int = 35) -> np.ndarray:
    """造一条昵称带：白底灰字，文字起点约 (82,14) —— 与真实裁切一致。"""
    img = Image.new("RGB", (w, h), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.text((82 + dx, 14 + dy), text, fill=(150, 150, 150), font=_font())
    return np.asarray(img)


@pytest.fixture(autouse=True)
def _clean():
    nz.reset()
    yield
    nz.reset()


# ---------------------------------------------------------------- 特征


def test_signature_is_deterministic():
    b = band("巴黎罗")
    a1, a2 = nz.signature(b), nz.signature(b)
    assert a1 is not None and a2 is not None
    assert a1.shape == (nz.NORM_H, nz.NORM_W)
    assert np.array_equal(a1, a2)


def test_signature_survives_one_pixel_shift():
    """UIA 矩形取整会带来 1px 上下偏移 —— 同一个人不能被拆开。"""
    a = nz.signature(band("巴黎罗"))
    b = nz.signature(band("巴黎罗", dy=1))
    assert a is not None and b is not None
    # 允许平移后比较
    best = max(
        nz._iou(np.roll(np.roll(a, dy, 0), dx, 1), b)
        for dy in range(-2, 3)
        for dx in range(-2, 3)
    )
    assert best >= nz.MATCH_IOU


def test_signature_separates_different_names():
    a = nz.signature(band("巴黎罗"))
    b = nz.signature(band("红姜"))
    assert a is not None and b is not None
    assert nz._iou(a, b) < nz.MATCH_IOU


def test_signature_rejects_blank_band():
    blank = np.full((35, 200, 3), 250, dtype=np.uint8)
    assert nz.signature(blank) is None
    assert nz.has_ink(blank) is False


# ---------------------------------------------------------------- 分组


def test_identify_merges_same_name_and_splits_others():
    bands = [band("巴黎罗"), band("巴黎罗", dy=1), band("红姜"), band("酒酿丸子")]
    got = nz.identify(bands)
    ids = [sid for sid, _n in got]
    assert ids[0] == ids[1]          # 同一个人的两条（含 1px 偏移）
    assert len({ids[0], ids[2], ids[3]}) == 3
    assert all(sid > 0 for sid in ids)


def test_identify_blank_rows_get_zero():
    blank = np.full((35, 200, 3), 250, dtype=np.uint8)
    got = nz.identify([blank, band("红姜")])
    assert got[0][0] == 0
    assert got[1][0] == 1


def test_identify_is_stable_across_calls():
    b = band("巴黎罗")
    first = nz.identify([b])[0][0]
    second = nz.identify([b])[0][0]
    assert first == second == 1


def test_names_returns_cached_only():
    b = band("巴黎罗")
    assert nz.names([b]) == [""]     # 还没读过
    nz.set_name(1, "巴黎罗")
    assert nz.names([b]) == ["巴黎罗"]


def test_set_name_rejects_placeholder_like_text():
    nz.set_name(1, "[图片]")
    assert nz.name_of(1) == ""
    nz.set_name(1, "巴黎罗")
    assert nz.name_of(1) == "巴黎罗"


# ---------------------------------------------------------------- 文本清洗


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("巴黎罗", "巴黎罗"),
        ("  巴黎罗  ", "巴黎罗"),
        ("、红姜、", "红姜"),
        ("[图片]", ""),
        ("Lnlion: [图片](共3条)", ""),
        ("", ""),
        ("这个名字实在是太长了根本不像昵称", ""),
    ],
)
def test_clean(raw, expect):
    assert nz._clean(raw) == expect


# ---------------------------------------------------------------- 读名字


class _FakeRA:
    """替身：不启 llama-server。"""

    def __init__(self, content: str, ready=(True, ""), busy=""):
        self.content = content
        self._ready = ready
        self._busy = busy
        self.calls = 0
        self.last_b64 = ""

    def _realtime_busy(self):
        return self._busy

    def vision_ready(self):
        return self._ready

    def _ensure_server(self):
        return 1

    def _chat(self, port, prompt, timeout, **kw):
        self.calls += 1
        self.last_b64 = kw.get("image_b64") or ""
        return self.content

    @staticmethod
    def close_unbalanced_json(text):
        return text if text.strip().startswith("{") else None


def _patch(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "reply_advisor", fake)


def test_refresh_fills_names_by_index(monkeypatch):
    fake = _FakeRA('{"items":[{"n":1,"name":"巴黎罗"},{"n":2,"name":"红姜"}]}')
    _patch(monkeypatch, fake)
    bands = [band("巴黎罗"), band("红姜")]
    got, why = nz.refresh(bands)
    assert (got, why) == (2, "")
    assert nz.names(bands) == ["巴黎罗", "红姜"]
    assert fake.calls == 1                 # 一批只调一次
    assert fake.last_b64                   # 确实带了图


def test_refresh_out_of_range_index_is_ignored(monkeypatch):
    """序号越界不能把名字安到别人头上。"""
    _patch(monkeypatch, _FakeRA('{"items":[{"n":9,"name":"张三"}]}'))
    bands = [band("巴黎罗")]
    got, why = nz.refresh(bands)
    assert got == 0 and why == ""
    assert nz.names(bands) == [""]


def test_refresh_dedupes_same_speaker(monkeypatch):
    """同一个人连发多条，拼图里只出现一次。"""
    fake = _FakeRA('{"items":[{"n":1,"name":"巴黎罗"}]}')
    _patch(monkeypatch, fake)
    got, _ = nz.refresh([band("巴黎罗"), band("巴黎罗", dy=1)])
    assert got == 1
    assert nz.names([band("巴黎罗"), band("巴黎罗", dy=1)]) == ["巴黎罗", "巴黎罗"]


def test_refresh_skips_when_nothing_new(monkeypatch):
    fake = _FakeRA('{"items":[]}')
    _patch(monkeypatch, fake)
    nz.set_name(1, "巴黎罗")
    got, why = nz.refresh([band("巴黎罗")])
    assert (got, why) == (0, "")
    assert fake.calls == 0


def test_refresh_yields_to_realtime_voice_change(monkeypatch):
    """实时变声在跑时不拉模型：9B + 视觉塔 ~6.3GB 会把 RVC 挤到 OOM。

    这条守卫 2026-10-29 补的 —— `suggest()` / `suggest_one()` 都让路了，
    认人这条路径当时漏了。所以这里要钉死「一次 `_chat` 都没发生」。
    """
    fake = _FakeRA('{"items":[{"n":1,"name":"巴黎罗"}]}', busy="实时变声正在运行（显存让它先占着）")
    _patch(monkeypatch, fake)
    got, why = nz.refresh([band("巴黎罗")])
    assert got == 0
    assert "实时变声" in why
    assert fake.calls == 0
    assert nz.names([band("巴黎罗")]) == [""]


def test_refresh_reports_missing_vision_without_raising(monkeypatch):
    _patch(monkeypatch, _FakeRA("", ready=(False, "没找到视觉权重 mmproj")))
    got, why = nz.refresh([band("巴黎罗")])
    assert got == 0
    assert "mmproj" in why


def test_refresh_bad_json_does_not_raise(monkeypatch):
    _patch(monkeypatch, _FakeRA("我看不清这张图"))
    got, why = nz.refresh([band("巴黎罗")])
    assert got == 0
    assert why


# ---------------------------------------------------------------- 落盘


def test_names_come_back_after_restart(monkeypatch, tmp_path):
    """认过的名字要能跨进程活下来 —— 否则每次开应用都重付 2.7s + 6G 显存。"""
    db = tmp_path / "chat_names.json"
    monkeypatch.setenv("VM_CHAT_NAMES_DB", str(db))
    _patch(monkeypatch, _FakeRA('{"items":[{"n":1,"name":"巴黎罗"}]}'))
    assert nz.refresh([band("巴黎罗")])[0] == 1
    assert db.exists()
    nz.reset()  # 模拟后端重启：内存清空，下次识别从盘上读回
    assert nz.names([band("巴黎罗")]) == ["巴黎罗"]


def test_store_corrupt_file_is_ignored(monkeypatch, tmp_path):
    """坏文件当没有历史：绝不抛，也绝不拿半份数据去认人。"""
    db = tmp_path / "chat_names.json"
    monkeypatch.setenv("VM_CHAT_NAMES_DB", str(db))
    db.write_text("{不是 JSON", encoding="utf-8")
    nz.reset()
    assert nz.names([band("巴黎罗")]) == [""]


def test_store_is_dropped_when_band_height_changes(monkeypatch, tmp_path):
    """DPI / 微信字号换了，旧指纹就不再可信 —— 宁可重认，也不能张冠李戴。"""
    db = tmp_path / "chat_names.json"
    monkeypatch.setenv("VM_CHAT_NAMES_DB", str(db))
    _patch(monkeypatch, _FakeRA('{"items":[{"n":1,"name":"巴黎罗"}]}'))
    assert nz.refresh([band("巴黎罗")])[0] == 1
    doc = json.loads(db.read_text(encoding="utf-8"))
    doc["h"] = 62  # 条带高度从 35 变到 62（约 125% → 200%）
    db.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    nz.reset()
    assert nz.names([band("巴黎罗")]) == [""]

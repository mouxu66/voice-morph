"""PPTX → 逐页 PNG 单测（2026-10-08）。

被测：`tools/pptx_to_png.py`（讲课视频的"画面"那一半）。

★ 这个文件守的核心是**页号对齐**：`course_api.parse_pptx` 用 `slideN.xml` 的 **N**
  当页号，而 PowerPoint 导出 PNG 是按**展示顺序** —— 用户拖动调整过页序的 pptx
  上两者不同，照导出顺序命名就会出现「第 3 页的画面配第 2 页的旁白」，
  成片看着完全正常，只有懂行的人盯着才发现。所以：

  1. `slide_part_order` 必须按 `presentation.xml` 的 `sldIdLst` 解，**不许**用
     `sorted(slideN)`（那正是错的写法）—— 有一条用"错位课件"钉死的用例；
  2. 页数对不上（PowerPoint 说有 N 张、解析只有 M 个）→ **拒绝导出**，宁可不做；
  3. 导出后逐张复核：打不开/尺寸不对的半截 PNG 不许当成"这页好了"（否则成片里
     那一页是花屏，而日志一路绿灯）；
  4. 失败路径也要收尾（收掉**我们拉起来的** PowerPoint 实例），且**绝不动**
     用户本来就开着的那个。

不需要 Office：解析/编排全是纯逻辑 + 假引擎（真 COM 只在手工/CLI 跑，
本机实测 19 页 9.2s，见逐日 2026-10-08 §第五轮）。

变异测试：把 `slide_part_order` 换成 `sorted(...)`，或去掉导出后的 `_png_ok` 复核，
对应用例必须变红。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402

import pptx_to_png as p2p  # noqa: E402

pytest.importorskip("PIL")


# ---------------- 造一个最小 pptx（只带解析器要读的那几个部件） ----------------

def _mk_pptx(path: Path, *, order: list[int], slide_files: list[int] | None = None,
             target_of: dict[str, str] | None = None) -> Path:
    """order = 展示顺序（sldIdLst 里 rId 的顺序，值 = slideN 的 N）。"""
    slide_files = slide_files if slide_files is not None else sorted(order)
    sld_ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>'
                      for i in range(len(order)))
    pres = f'<p:presentation><p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>'
    if target_of is None:
        target_of = {f"rId{i + 1}": f"slides/slide{n}.xml" for i, n in enumerate(order)}
    rels = "".join(f'<Relationship Id="{rid}" Target="{tgt}"/>'
                   for rid, tgt in target_of.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/presentation.xml", pres)
        z.writestr("ppt/_rels/presentation.xml.rels", f"<Relationships>{rels}</Relationships>")
        for n in slide_files:
            z.writestr(f"ppt/slides/slide{n}.xml", "<p:sld/>")
    return path


class FakeEngine:
    """假引擎：记下每次调用，可注入"页数不一致""导出坏图"等故障。

    `count=None` 时用**真解析器**数页数（最忠实的替身：页数来源与生产同一条路）；
    给具体数字则用来造"PowerPoint 说的与解析出的对不上"。
    """

    def __init__(self, count: int | None = None, *, write_png=True,
                 png_size=(64, 64), version="PowerPoint 假引擎 1.0"):
        self.count = count
        self.write_png = write_png
        self.png_size = png_size
        self._version = version
        self.opened: list[Path] = []
        self.exported: list[tuple[int, str]] = []
        self.closed = 0

    def open(self, pptx: Path) -> int:
        self.opened.append(pptx)
        return len(p2p.slide_part_order(pptx)) if self.count is None else self.count

    def version(self) -> str:
        return self._version

    def export_slide(self, index: int, out: Path, width: int, height: int) -> None:
        self.exported.append((index, out.name))
        if not self.write_png:
            return  # 模拟"导出没成功但也没抛异常"
        from PIL import Image

        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", self.png_size, (10, 10, 10)).save(out)

    def close(self) -> None:
        self.closed += 1


# ---------------- 页序解析（对齐的命门） ----------------

class TestSlideOrder:
    def test_normal_deck_is_file_order(self, tmp_path):
        p = _mk_pptx(tmp_path / "a.pptx", order=[1, 2, 3])
        assert p2p.slide_part_order(p) == [1, 2, 3]

    def test_reordered_deck_follows_presentation_order_not_file_numbers(self, tmp_path):
        """★ 命门：展示顺序 ≠ 文件序号时，必须按 presentation.xml 的 sldIdLst 解。

        `sorted(slideN)` 会给出 [1,2,3]，而正确答案是 [3,1,2] —— 差一点就"画面配错旁白"。
        """
        p = _mk_pptx(tmp_path / "b.pptx", order=[3, 1, 2])
        assert p2p.slide_part_order(p) == [3, 1, 2]
        assert p2p.slide_part_order(p) != sorted([3, 1, 2]), "别退化成 sorted(slideN)"

    def test_bad_zip_is_a_clear_error(self, tmp_path):
        bad = tmp_path / "bad.pptx"
        bad.write_bytes(b"not a zip")
        with pytest.raises(p2p.PptxError, match="pptx"):
            p2p.slide_part_order(bad)

    def test_missing_sld_id_list_is_an_error(self, tmp_path):
        p = tmp_path / "c.pptx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("ppt/presentation.xml", "<p:presentation/>")
            z.writestr("ppt/_rels/presentation.xml.rels", "<Relationships/>")
            z.writestr("ppt/slides/slide1.xml", "<p:sld/>")
        with pytest.raises(p2p.PptxError, match="sldIdLst"):
            p2p.slide_part_order(p)

    def test_rid_pointing_at_a_non_slide_part_is_an_error(self, tmp_path):
        p = _mk_pptx(tmp_path / "d.pptx", order=[1],
                     target_of={"rId1": "notesSlides/notesSlide1.xml"})
        with pytest.raises(p2p.PptxError, match="不是幻灯片部件"):
            p2p.slide_part_order(p)

    def test_rid_pointing_at_a_missing_part_is_an_error(self, tmp_path):
        p = _mk_pptx(tmp_path / "e.pptx", order=[1], slide_files=[], )
        with pytest.raises(p2p.PptxError, match="不在包里"):
            p2p.slide_part_order(p)


# ---------------- 命名与复用 ----------------

class TestPlanExports:
    def test_names_follow_file_numbers_indexed_by_display_position(self, tmp_path):
        todo, reuse = p2p.plan_exports([3, 1, 2], tmp_path, 64, 64, force=False)
        assert [p.name for _i, p in todo] == ["page_003.png", "page_001.png", "page_002.png"]
        assert [i for i, _p in todo] == [1, 2, 3]  # 第 i 张（喂给 Slides(i)）
        assert reuse == []

    def test_valid_existing_png_is_reused(self, tmp_path):
        from PIL import Image

        Image.new("RGB", (64, 64), (0, 0, 0)).save(tmp_path / "page_001.png")
        todo, reuse = p2p.plan_exports([1, 2], tmp_path, 64, 64, force=False)
        assert [p.name for _i, p in reuse] == ["page_001.png"]
        assert [p.name for _i, p in todo] == ["page_002.png"]

    def test_half_written_or_wrong_size_png_is_reexported(self, tmp_path):
        """★ 只看"文件在不在"会把半截 PNG 当成"这页好了" → 成片里那页是花屏。"""
        (tmp_path / "page_001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)  # 打不开
        from PIL import Image

        Image.new("RGB", (32, 32), (0, 0, 0)).save(tmp_path / "page_002.png")  # 尺寸不对
        todo, reuse = p2p.plan_exports([1, 2], tmp_path, 64, 64, force=False)
        assert reuse == []
        assert [p.name for _i, p in todo] == ["page_001.png", "page_002.png"]

    def test_force_reexports_everything(self, tmp_path):
        from PIL import Image

        Image.new("RGB", (64, 64), (0, 0, 0)).save(tmp_path / "page_001.png")
        todo, reuse = p2p.plan_exports([1], tmp_path, 64, 64, force=True)
        assert len(todo) == 1 and reuse == []


# ---------------- 编排（假引擎） ----------------

class TestExportOrchestration:
    def test_exports_and_writes_provenance(self, tmp_path, monkeypatch):
        deck = _mk_pptx(tmp_path / "deck.pptx", order=[3, 1, 2])
        eng = FakeEngine()
        info = p2p.export_pptx_to_png(deck, tmp_path / "slides", width=64, height=64, engine=eng)
        assert eng.closed == 1
        assert [n for _i, n in eng.exported] == ["page_003.png", "page_001.png", "page_002.png"]
        assert info["slide_order"] == [3, 1, 2]
        assert info["pptx_sha256"] and len(info["pages"]) == 3
        assert info["pages"][0] == {"file": "page_003.png",
                                   "slide_part": "ppt/slides/slide3.xml"}
        assert (tmp_path / "slides" / "page_001.png").is_file()

    def test_page_count_mismatch_refuses_and_still_closes(self, tmp_path):
        """页数对不上 = 映射不可信 → 拒绝导出，且**引擎要收尾**（失败路径也收）。"""
        deck = _mk_pptx(tmp_path / "deck.pptx", order=[1, 2])
        eng = FakeEngine(count=3)  # PowerPoint 说 3 张
        with pytest.raises(p2p.PptxError, match="页序映射不可信"):
            p2p.export_pptx_to_png(deck, tmp_path / "slides", width=64, height=64, engine=eng)
        assert eng.closed == 1, "失败路径也必须 close（否则留下隐形 POWERPNT.EXE）"
        assert eng.exported == [], "校验没过就不该导任何一页"

    def test_silent_export_failure_is_caught_afterwards(self, tmp_path):
        """★ 引擎"没抛异常但也没写出图"→ 必须被导出后复核抓住，不许当成功。"""
        deck = _mk_pptx(tmp_path / "deck.pptx", order=[1])
        eng = FakeEngine(write_png=False)
        with pytest.raises(p2p.PptxError, match="没导出成功"):
            p2p.export_pptx_to_png(deck, tmp_path / "slides", width=64, height=64, engine=eng)
        assert eng.closed == 1

    def test_odd_size_is_rejected_before_touching_the_engine(self, tmp_path):
        deck = _mk_pptx(tmp_path / "deck.pptx", order=[1])
        eng = FakeEngine()
        with pytest.raises(p2p.PptxError, match="偶数"):
            p2p.export_pptx_to_png(deck, tmp_path / "slides", width=63, height=64, engine=eng)
        assert eng.opened == [] and eng.closed == 0

    def test_dry_run_touches_nothing(self, tmp_path, capsys):
        deck = _mk_pptx(tmp_path / "deck.pptx", order=[2, 1])
        eng = FakeEngine()
        info = p2p.export_pptx_to_png(deck, tmp_path / "slides", width=64, height=64,
                                      dry_run=True, engine=eng)
        assert info["dry_run"] is True
        assert eng.opened == [] and eng.exported == []
        assert not (tmp_path / "slides" / "page_001.png").exists()
        assert "[dry-run]" in capsys.readouterr().out

    def test_missing_pptx_is_an_error(self, tmp_path):
        with pytest.raises(p2p.PptxError, match="找不到文件"):
            p2p.export_pptx_to_png(tmp_path / "nope.pptx", tmp_path / "s", engine=FakeEngine())


# ---------------- 没有 Office 时的说法 ----------------

def test_missing_powerpoint_says_what_to_do_instead(monkeypatch, tmp_path):
    """没有 Office 的机器：给明确报错 + 替代方向，**不静默降级**成别的画面。"""
    monkeypatch.setattr(p2p, "powerpoint_exe", lambda: None)
    deck = _mk_pptx(tmp_path / "deck.pptx", order=[1])
    with pytest.raises(p2p.PptxError, match="找不到 Microsoft PowerPoint"):
        p2p.ComEngine().open(deck)


def test_provenance_is_json_serializable(tmp_path):
    """凭据要能直接落盘（含中文路径）。"""
    deck = _mk_pptx(tmp_path / "课件.pptx", order=[1])
    info = p2p.export_pptx_to_png(deck, tmp_path / "slides", width=64, height=64,
                                  engine=FakeEngine())
    text = json.dumps(info, ensure_ascii=False, indent=2)
    assert "slide_order" in text and "课件.pptx" in text

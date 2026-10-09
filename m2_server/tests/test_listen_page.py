"""听测页生成器单测（2026-10-10，大创研究层）。

被测：`tools/listen_page.py`（发放包 → 离线可打分的单文件网页）。

★ 这个文件守的是**网页版盲听特有的三种静默错误**：

  1. **重编码**：为了让单文件小一点，很容易顺手把 wav 转成 mp3 再内嵌 ——
     但那改变了被测刺激本身，主观分与客观指标就对不上了。
     所以有一条测试**逐字节核对**内嵌音频与源文件（sha256），钉死"原样内嵌"。
  2. **顺序被重排**：页面若自己按盲号排序（而不是照 playlist），
     听者的疲劳效应就不再随机分布 —— 位置效应直接掺进条件效应。
  3. **网页版泄题**：页面里带上了说话人/条件/时长档字样。
     网页比 CSV 更危险 —— 听者按 F12 就能看全，而且截图会流出去。

  另有一条边界：**不自动连播**（浏览器自动播放策略要求用户手势），
  这条从"页面里没有 autoplay 属性、每条一个播放按钮"上钉。
"""

import csv
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import listen_page as lp  # noqa: E402

CONDS = ("same", "cross")
SPKS = ("SSB0033", "SSB0603")


def _wav(path: Path, *, seed: int = 0, seconds: float = 0.1) -> Path:
    """写一段可分辨的小 wav（每条内容不同，才能验证"没被换过"）。"""
    import numpy as np
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(24000 * seconds)) / 24000
    sf.write(str(path), 0.3 * np.sin(2 * np.pi * (200 + seed) * t), 24000)
    return path


def _make_pack(tmp_path: Path, *, n: int = 4, exp: str = "t01",
               n_kind: bool = False) -> tuple[Path, Path]:
    """造一个最小发放包（clips/ + playlist.txt）+ 实验目录（用于泄漏判据）。"""
    pack = tmp_path / "pack"
    base = tmp_path / "vb" / exp
    rows = []
    lines = [f"# 播放清单 · {exp}", "# 共 %d 条" % n, ""]
    for i in range(1, n + 1):
        cond = CONDS[i % len(CONDS)]
        spk = SPKS[i % len(SPKS)]
        _wav(pack / "clips" / f"S-{i:04d}.wav", seed=i)
        lines.append(f"S-{i:04d}\tclips/S-{i:04d}.wav")
        rows.append({"blind_id": f"S-{i:04d}", "pos": str(i),
                     "sample_id": f"{spk}/{cond}", "speaker": spk,
                     "duration_tag": "10s", "condition": cond,
                     "sample": f"samples/{spk}/{cond}.wav", "truth": ""})
    (pack / "playlist.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    listen = base / "listen"
    listen.mkdir(parents=True, exist_ok=True)
    with open(listen / "key.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return pack, base


def _ids_in_page(text: str) -> list[str]:
    return re.findall(r'"id":\s*"(S-\d{4})"', text)


# ---------------- 1. 顺序 / 音频原样 ----------------

class TestOrderAndAudio:
    def test_page_keeps_playlist_order(self, tmp_path):
        """顺序必须照 playlist，不许页面自己重排（位置效应会掺进条件效应）。"""
        pack, base = _make_pack(tmp_path, n=4)
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert res["error"] is None, res
        ids = _ids_in_page((tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8"))
        assert ids == ["S-0001", "S-0002", "S-0003", "S-0004"]

    def test_embed_is_byte_identical_no_reencode(self, tmp_path):
        """★ 内嵌音频必须与源文件逐字节一致 —— 不许为了瘦身转码。"""
        import base64
        import hashlib

        pack, base = _make_pack(tmp_path, n=2)
        res = lp.build("t01", pack=pack, out=tmp_path / "out",
                       root=tmp_path / "vb", embed=True)
        assert res["error"] is None, res
        page = (tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8")
        uris = re.findall(r'"src":\s*"(data:audio/wav;base64,[A-Za-z0-9+/=]+)"', page)
        assert len(uris) == 2, "内嵌模式下每条都该是 data URI"
        for i, uri in enumerate(uris, 1):
            got = base64.b64decode(uri.split(",", 1)[1])
            want = (pack / "clips" / f"S-{i:04d}.wav").read_bytes()
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(want).hexdigest()

    def test_folder_mode_uses_relative_paths(self, tmp_path):
        pack, base = _make_pack(tmp_path, n=2)
        assert lp.build("t01", pack=pack, out=tmp_path / "out",
                        root=tmp_path / "vb")["error"] is None
        page = (tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8")
        assert "clips/S-0001.wav" in page
        assert "data:audio" not in page, "非内嵌模式不该塞 base64"
        assert str(pack).replace("\\", "/") not in page, "不许出现本机绝对路径"

    def test_no_autoplay_and_one_button_per_item(self, tmp_path):
        """★ 自动播放策略要求用户手势 → 页面不许自动连播。"""
        pack, base = _make_pack(tmp_path, n=3)
        assert lp.build("t01", pack=pack, out=tmp_path / "out",
                        root=tmp_path / "vb")["error"] is None
        page = (tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8")
        assert "autoplay" not in page.lower()
        assert "▶ 播放" in page, "每条要有自己的播放按钮（无手势不放音）"


# ---------------- 2. 网页版泄题 ----------------

class TestLeakGuard:
    def test_identity_token_in_page_is_refused(self, tmp_path):
        """把条件名做成页面里必然出现的词 → 必须拒绝产出。"""
        pack, base = _make_pack(tmp_path, n=2)
        # 条件名取 "mos" —— 它一定出现在页面里（导出表头 blind_id,mos,note）。
        # 这不是随便挑的：守卫的价值就在于"页面里真有的词"也能被认出来。
        kp = base / "listen" / "key.csv"
        rows = list(csv.DictReader(open(kp, encoding="utf-8-sig")))
        for r in rows:
            r["condition"] = "mos"
        with open(kp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        out = tmp_path / "out"
        res = lp.build("t01", pack=pack, out=out, root=tmp_path / "vb")
        assert res["error_stage"] == "泄漏守卫"
        assert res["leaks"]
        assert not (out / lp.PAGE_FILE).exists(), "拒绝时不该留下页面"

    def test_clean_pack_passes_and_has_no_identity(self, tmp_path):
        pack, base = _make_pack(tmp_path, n=3)
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert res["error"] is None, res
        page = (tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8")
        for tok in ("SSB0033", "SSB0603", "same", "cross", "10s", "samples/"):
            assert tok not in page, f"页面里出现了身份字样 {tok}"

    def test_missing_key_warns_but_does_not_block(self, tmp_path):
        """没有揭盲表时只告警：生成页面本身不依赖它。"""
        pack, _ = _make_pack(tmp_path, n=2)
        res = lp.build("t01", pack=pack, out=tmp_path / "out")
        assert res["error"] is None
        assert any("未做身份泄漏扫描" in w for w in res["warnings"])


# ---------------- 3. 拒绝策略 / 进度键 ----------------

class TestRefusalsAndSid:
    def test_missing_clip_refuses(self, tmp_path):
        pack, base = _make_pack(tmp_path, n=3)
        (pack / "clips" / "S-0002.wav").unlink()
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert res["error_stage"] == "音频"
        assert not (tmp_path / "out" / lp.PAGE_FILE).exists()

    def test_missing_playlist_reports_instead_of_raising(self, tmp_path):
        (tmp_path / "pack").mkdir()
        res = lp.build("t01", pack=tmp_path / "pack", out=tmp_path / "out")
        assert res["error_stage"] == "播放清单"

    def test_duplicate_blind_id_refused(self, tmp_path):
        pack, base = _make_pack(tmp_path, n=3)
        pl = pack / lp.vp.PLAYLIST_FILE
        pl.write_text(pl.read_text(encoding="utf-8") + "S-0001\tclips/S-0001.wav\n",
                      encoding="utf-8")
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert res["error_stage"] == "播放清单"

    def test_refuses_overwrite_without_force(self, tmp_path):
        pack, base = _make_pack(tmp_path, n=2)
        assert lp.build("t01", pack=pack, out=tmp_path / "out",
                        root=tmp_path / "vb")["error"] is None
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert res["error_stage"] == "目标文件"
        assert lp.build("t01", pack=pack, out=tmp_path / "out",
                        root=tmp_path / "vb", force=True)["error"] is None

    def test_sid_tracks_clip_content(self, tmp_path):
        """进度键绑在音频内容上：换一批音频 → 换键 → 旧进度不串味到新批次。"""
        pack, base = _make_pack(tmp_path, n=2)
        a = lp.build("t01", pack=pack, out=tmp_path / "o1", root=tmp_path / "vb")
        b = lp.build("t01", pack=pack, out=tmp_path / "o2", root=tmp_path / "vb")
        assert a["sid"] == b["sid"]
        _wav(pack / "clips" / "S-0001.wav", seed=99)
        c = lp.build("t01", pack=pack, out=tmp_path / "o3", root=tmp_path / "vb")
        assert c["sid"] != a["sid"]

    def test_warns_no_loudness_normalization(self, tmp_path):
        """不做响度归一化必须在结果里说出来（归一化会改变被测刺激）。"""
        pack, base = _make_pack(tmp_path, n=2)
        res = lp.build("t01", pack=pack, out=tmp_path / "out", root=tmp_path / "vb")
        assert any("响度归一化" in w for w in res["warnings"])


# ---------------- 4. 导出契约 ----------------

class TestExportContract:
    def test_export_header_matches_merge_listen_columns(self, tmp_path):
        """页面导出的表头必须能被 `merge-listen` 直接吃（逐字一致）。"""
        pack, base = _make_pack(tmp_path, n=3)
        assert lp.build("t01", pack=pack, out=tmp_path / "out",
                        root=tmp_path / "vb")["error"] is None
        page = (tmp_path / "out" / lp.PAGE_FILE).read_text(encoding="utf-8")
        assert '"blind_id,mos,note\\n"' in page, "导出的表头必须与评分表列一致"
        assert page.count("${{it.id}},${{rec.mos || \"\"}}") >= 0  # 行模板存在
        assert "__vmExport" in page, "评测要读导出结果，钩子不能少"

    def test_playlist_reader_skips_comments_and_blanks(self, tmp_path):
        pack, _ = _make_pack(tmp_path, n=2)
        pl = pack / "playlist.txt"
        pl.write_text("# 注释\n\nS-0007\tclips/S-0007.wav\n", encoding="utf-8")
        items = lp._read_playlist(pack)
        assert items == [{"blind_id": "S-0007", "rel": "clips/S-0007.wav"}]

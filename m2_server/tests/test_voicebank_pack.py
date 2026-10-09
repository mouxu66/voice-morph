"""盲听发放包单测（2026-10-09，大创研究层）。

被测：`tools/voicebank_pack.py`（`listen/` 材料 → 可发给听者的脱敏目录）。

★ 这个文件守的是**盲法本身**，不是文件拷贝。拷贝代码写错了会立刻发现
  （文件不在、数量不对），而盲法写错了**一声不响**：听者照样打分、报告照样出数、
  相关系数照样算得出来 —— 只是那些分数来自知道答案的人。所以下面每一条都对着
  一个具体的"看起来是盲的、其实不是"的错法：

  1. **文件名即泄题**：`playlist.txt` 原本写的是真实样本路径
     （`samples/SSB0033/30s/long.wav`），说话人/时长档/条件全在名字里。
     发出去的每一个文件名必须只有盲号。
  2. **守卫必须真的会响**：一个从不报警的 guard 等于没有，而且更糟 ——
     它给了"检查过了"的错觉。所以有一条测试**故意造泄漏**，要求必须拒绝产出。
  3. **判据不能过宽**：实验名（`pilotC`）会出现在发放包说明里，
     把整条真实路径当身份 token 会让每个正常包都报警 → 有人会加 `--skip` 绕过。
     所以另有一条测试钉住"实验名不算泄漏"这个边界。
  4. **发一张填不了的表格**：本轮 `truth/` 为空 → ABX 评不了。
     发带 `abx` 空列的表格会让听者去猜那一列填什么。
  5. **缺音频不能少拷几段凑合**：到了现场才发现少一条，整轮听测作废。
  6. **不许覆盖已有发放包**：听者交回来的表格就放在 `sheets/` 里，
     重跑一次打包会把已回收的数据冲掉。
"""

import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
import voicebank_pack as vp  # noqa: E402

SPEAKERS = ("SSB0033", "SSB0603")
TAGS = ("3s", "30s")
CONDITIONS = ("same", "cross")


def _wav(path: Path, seconds: float = 0.2) -> Path:
    """写一个极短的合法 wav（内容不重要 —— 本模块只拷字节）。"""
    import numpy as np
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(22050 * seconds)) / 22050
    sf.write(str(path), 0.3 * np.sin(2 * np.pi * 150 * t), 22050)
    return path


def _make_exp(tmp_path: Path, *, exp: str = "t01") -> Path:
    """搭一个最小实验目录：listen/key.csv + 对应的 samples/。"""
    base = tmp_path / "vb" / exp
    rows = []
    pos = 0
    for sp in SPEAKERS:
        for tag in TAGS:
            for cond in CONDITIONS:
                pos += 1
                sid = f"{sp}/{tag}/{cond}"
                sample = f"samples/{sp}/{tag}/{cond}.wav"
                _wav(base / sample)
                # 每条样本是独立文件，重复 pos 只影响数量，不影响本模块判据
                rows.append({
                    "blind_id": f"S-{pos:04d}", "pos": str(pos), "sample_id": sid,
                    "speaker": sp, "duration_tag": tag, "condition": cond,
                    "sample": sample, "truth": f"truth/{sp}/{cond}.wav",
                })
    listen = base / "listen"
    listen.mkdir(parents=True, exist_ok=True)
    with open(listen / "key.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return base


def _shipped_texts(out: Path) -> list[Path]:
    """发出去的东西：播放清单 + 听者说明 + 评分表（不含 MANIFEST）。"""
    return [out / vp.PLAYLIST_FILE, out / vp.READER_NOTE,
            *sorted((out / vp.SHEETS_DIR).glob("*.csv"))]


# ---------------- 1. 盲号命名 / 不泄题 ----------------

class TestBlinding:
    def test_clips_are_named_by_blind_id_only(self, tmp_path):
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)
        assert res["error"] is None, res
        names = sorted(p.name for p in (out / vp.CLIPS_DIR).iterdir())
        assert names == [f"S-{i:04d}.wav" for i in range(1, len(names) + 1)]

    @pytest.mark.parametrize("col", ["speaker", "duration_tag", "condition"])
    def test_no_identity_token_in_shipped_files(self, tmp_path, col):
        """发出去的每个文本文件里都不许出现身份列的值（三种列各测一遍）。"""
        base = _make_exp(tmp_path)
        out = tmp_path / "pack"
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=2)
        assert res["error"] is None, res
        tokens = {r[col] for r in csv.DictReader(
            open(base / "listen" / "key.csv", encoding="utf-8-sig"))}
        for f in _shipped_texts(out):
            text = f.read_text(encoding="utf-8").lower()
            for t in tokens:
                assert t.lower() not in text, f"{f.name} 里出现了 {col}={t}"

    def test_key_csv_is_not_in_package(self, tmp_path):
        """揭盲表不外发 —— 与评分表分开存是硬要求（见 voicebank_eval 同名约束）。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        assert not (out / "key.csv").exists()
        assert not list(out.rglob("key.csv"))

    def test_reports_that_duration_tier_cannot_be_blinded(self, tmp_path):
        """★ 时长档**无法盲**（一听就知道是 3s 还是 60s）—— 必须主动说出来。

        这条不是形式主义：把 RQ1 的听测当成严格盲测写进材料，答辩时一问就穿。
        """
        _make_exp(tmp_path)
        res = vp.pack("t01", root=tmp_path / "vb", out=tmp_path / "pack", n_sheets=1)
        assert any("无法盲" in w for w in res["warnings"]), res["warnings"]


# ---------------- 2. 泄漏守卫：必须真的会响 ----------------

class TestLeakGuard:
    def test_guard_fires_and_rolls_back_clips(self, tmp_path, monkeypatch):
        """故意把说话人 id 写进听者说明 → 必须拒绝产出，且不留半个包。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        monkeypatch.setattr(vp, "_reader_note",
                            lambda exp, n: f"# 说明\n本批素材来自 {SPEAKERS[0]}\n")
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)
        assert res["error"] is not None
        assert res["error_stage"] == "泄漏守卫"
        assert res["leaks"], "命中列表不能为空 —— 否则错误信息说不清哪泄漏了"
        assert not (out / vp.CLIPS_DIR).exists(), "泄漏时音频必须回收，不留半成品"

    def test_guard_does_not_fire_on_experiment_name(self, tmp_path):
        """边界：实验名出现在说明里**不算**泄漏。

        真实样本路径含实验名与固定目录名；把它们当身份 token，会让每个正常包
        都报警 —— 一个总在报警的 guard 只会被人 `--skip` 绕过。
        """
        _make_exp(tmp_path)
        res = vp.pack("t01", root=tmp_path / "vb", out=tmp_path / "pack", n_sheets=1)
        assert res["error"] is None, res
        # 说明里确实带了实验名，而守卫放行了
        note = (tmp_path / "pack" / vp.READER_NOTE).read_text(encoding="utf-8")
        assert "t01" in note

    def test_identity_tokens_exclude_paths_and_short_strings(self):
        rows = [{"speaker": "SSB0033", "duration_tag": "30s", "condition": "long",
                 "sample": "D:/变声/outputs/voicebank/t01/samples/SSB0033/30s/long.wav"},
                {"speaker": "s", "duration_tag": "3s", "condition": "a", "sample": "x"}]
        toks = vp._identity_tokens(rows)
        assert toks == {"SSB0033", "30s", "3s", "long"}
        assert not any("/" in t or "\\" in t for t in toks)
        assert "t01" not in toks and "samples" not in toks and "s" not in toks


# ---------------- 3. 评分表 ----------------

class TestSheets:
    def test_sheet_is_mos_only_and_has_no_identity_column(self, tmp_path):
        """评分表只留 `mos`：发一张填不了的 abx 空列，等于让听者去猜。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=3)["error"] is None
        sheets = sorted((out / vp.SHEETS_DIR).glob("*.csv"))
        assert len(sheets) == 3, "一人一份，预命名"
        rows = list(csv.DictReader(open(sheets[0], encoding="utf-8-sig")))
        assert list(rows[0].keys()) == ["blind_id", "mos", "note"]
        assert "sample_id" not in rows[0]
        assert all(r["mos"] == "" for r in rows), "空白表格 — 不许带任何预填值"

    def test_sheet_rows_cover_every_blind_id_in_play_order(self, tmp_path):
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        order = [ln.split("\t")[0] for ln in
                 (out / vp.PLAYLIST_FILE).read_text(encoding="utf-8").splitlines()
                 if ln and not ln.startswith("#")]
        rows = [r["blind_id"] for r in csv.DictReader(
            open(out / vp.SHEETS_DIR / "listener_01.csv", encoding="utf-8-sig"))]
        assert rows == order

    def test_playlist_order_matches_key_pos(self, tmp_path):
        """播放顺序必须与 listen 落盘的 pos 一致（顺序随机化的结果，不许重排）。"""
        base = _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        key = list(csv.DictReader(open(base / "listen" / "key.csv", encoding="utf-8-sig")))
        expect = [r["blind_id"] for r in sorted(key, key=lambda r: int(r["pos"]))]
        got = [ln.split("\t")[0] for ln in
               (out / vp.PLAYLIST_FILE).read_text(encoding="utf-8").splitlines()
               if ln and not ln.startswith("#")]
        assert got == expect

    def test_playlist_uses_relative_clip_paths(self, tmp_path):
        """清单里写相对路径：绝对路径会把机器名和实验目录一起带出去。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        lines = [ln for ln in (out / vp.PLAYLIST_FILE).read_text(encoding="utf-8")
                 .splitlines() if ln and not ln.startswith("#")]
        assert all(ln.split("\t")[1].startswith(f"{vp.CLIPS_DIR}/") for ln in lines)


# ---------------- 4. 完整性 / 拒绝策略 ----------------

class TestIntegrityAndRefusals:
    def test_manifest_has_no_identity_and_hashes_match(self, tmp_path):
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        blob = (out / vp.MANIFEST_FILE).read_text(encoding="utf-8")
        assert "SSB0033" not in blob and "samples/" not in blob
        man = json.loads(blob)
        assert man["n_clips"] == len(man["clips"])
        for m in man["clips"]:
            assert vp._sha256(out / m["file"]) == m["sha256"]

    def test_missing_audio_refuses_and_leaves_nothing(self, tmp_path):
        """缺一条音频就整体拒绝：到了现场才发现文件不在，整轮听测作废。"""
        base = _make_exp(tmp_path)
        (base / "samples" / SPEAKERS[0] / "3s" / "same.wav").unlink()
        out = tmp_path / "pack"
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)
        assert res["error_stage"] == "音频"
        assert len(res["missing"]) == 1
        assert not (out / vp.CLIPS_DIR).exists()

    def test_missing_key_is_reported_not_raised(self, tmp_path):
        (tmp_path / "vb" / "t01").mkdir(parents=True)
        res = vp.pack("t01", root=tmp_path / "vb", out=tmp_path / "pack")
        assert res["error_stage"] == "揭盲表"
        assert res["error"]

    def test_duplicate_blind_id_refused(self, tmp_path):
        """重复盲号 = 汇总时张冠李戴，而且不会报错。"""
        base = _make_exp(tmp_path)
        kp = base / "listen" / "key.csv"
        rows = list(csv.DictReader(open(kp, encoding="utf-8-sig")))
        rows[1]["blind_id"] = rows[0]["blind_id"]
        with open(kp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        res = vp.pack("t01", root=tmp_path / "vb", out=tmp_path / "pack")
        assert res["error_stage"] == "揭盲表"

    def test_refuses_to_overwrite_nonempty_dir(self, tmp_path):
        """听者交回的表格就住在 sheets/ 里 —— 重跑打包会把它冲掉。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)
        assert res["error_stage"] == "目标目录"

    def test_force_allows_overwrite_but_wipes_previous(self, tmp_path):
        """`--force` 是明示的：它**会**冲掉已回收的评分表，所以必须是显式动作。"""
        _make_exp(tmp_path)
        out = tmp_path / "pack"
        assert vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1)["error"] is None
        got = out / vp.SHEETS_DIR / "listener_01.csv"
        text = got.read_text(encoding="utf-8-sig").replace("S-0001,,\n", "S-0001,5,\n")
        got.write_text(text, encoding="utf-8-sig")
        res = vp.pack("t01", root=tmp_path / "vb", out=out, n_sheets=1, force=True)
        assert res["error"] is None, res
        assert "S-0001,,\n" in got.read_text(encoding="utf-8-sig"), "旧表已被新空白表覆盖"

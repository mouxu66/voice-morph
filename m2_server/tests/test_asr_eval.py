"""`asr_eval` 的守卫：归一化 / CER / 说话人切分 / manifest 读取。

这些用例**故意不依赖 zhconv 或 opencc** —— CI 的 requirements 里两个都没列，
硬依赖会让干净环境整片红；简繁转换的分支用注入的假 converter 覆盖。
"""

from __future__ import annotations

import json

import asr_eval as A
import pytest


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
class TestNormalize:
    def test_strips_punct_and_space_but_keeps_hanzi(self):
        got = A.normalize_zh("但 現有方法，門檻太高！", converter=None, t2s=False)
        assert got == "但現有方法門檻太高"

    def test_keeps_dialect_chars(self):
        # 阮/厝/食/汝 都是潮汕正字里的常用字，归一化不能把它们当噪声删掉
        got = A.normalize_zh("阮厝食飯，汝來。", converter=None, t2s=False)
        assert got == "阮厝食飯汝來"

    def test_converter_is_injectable(self):
        got = A.normalize_zh(
            "臺灣", converter=lambda s: s.replace("臺", "台").replace("灣", "湾")
        )
        assert got == "台湾"

    def test_t2s_off_keeps_traditional(self):
        assert A.normalize_zh("門檻", converter=lambda s: "门槛", t2s=False) == "門檻"

    def test_hanzi_only_drops_latin_and_digits(self):
        # t2s=False：这里只验「只留汉字」这一件事，不让简繁转换参与
        got = A.normalize_zh("ngo2 食飯 2", converter=None, t2s=False, hanzi_only=True)
        assert got == "食飯"

    def test_missing_converter_backend_is_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(A, "_default_converter", lambda: None)
        assert A.normalize_zh("門檻") == "門檻"

    def test_none_and_empty(self):
        assert A.normalize_zh(None) == ""
        assert A.normalize_zh("，。！") == ""


# --------------------------------------------------------------------------
# CER
# --------------------------------------------------------------------------
class TestCer:
    def test_identical_is_zero(self):
        assert A.cer("但现有方法门槛太高", "但现有方法门槛太高") == 0.0

    def test_one_deletion(self):
        assert A.cer("abcd", "abc") == pytest.approx(0.25)

    def test_one_insertion(self):
        assert A.cer("abc", "abcd") == pytest.approx(1 / 3)

    def test_one_substitution(self):
        assert A.cer("abcd", "abce") == pytest.approx(0.25)

    def test_punctuation_and_traditional_are_normalized(self):
        # 繁简 + 标点差异都不该算错：这正是 whisper 默认出繁体那个坑的守卫
        assert A.cer("但現有方法，門檻太高。", "但现有方法门槛太高",
                     converter=lambda s: s.replace("現", "现")
                     .replace("門", "门").replace("檻", "槛")) == 0.0

    def test_empty_reference_raises(self):
        with pytest.raises(ValueError):
            A.cer("，。", "随便")

    def test_hanzi_only_ignores_pinyin_digits(self):
        assert A.cer("食飯", "食飯 ngo2", hanzi_only=True) == 0.0


class TestCorpusCer:
    def test_aggregates_by_ref_chars(self):
        got = A.corpus_cer([("abcd", "abc"), ("ab", "ab")])
        assert got["edits"] == 1
        assert got["ref_chars"] == 6
        assert got["cer"] == pytest.approx(1 / 6)
        assert got["utterances"] == 2
        assert got["skipped"] == 0

    def test_skips_empty_ref_but_counts_it(self):
        got = A.corpus_cer([("，。", "什么"), ("ab", "ab")])
        assert got["skipped"] == 1
        assert got["utterances"] == 1
        assert got["cer"] == 0.0

    def test_all_empty_gives_none_not_zero(self):
        got = A.corpus_cer([("，。", "x")])
        assert got["cer"] is None


class TestEditDistance:
    def test_empty_sides(self):
        assert A.edit_distance("", "") == 0
        assert A.edit_distance("", "abc") == 3
        assert A.edit_distance("abc", "") == 3


# --------------------------------------------------------------------------
# 说话人切分
# --------------------------------------------------------------------------
def _rows(n_spk=6, per=3):
    return [
        {"wav": f"s{s}_{i}.wav", "text": "食飯", "speaker": f"s{s}"}
        for s in range(n_spk) for i in range(per)
    ]


class TestSplit:
    def test_parts_are_speaker_disjoint(self):
        got = A.split_by_speaker(_rows(), n_eval=2, n_dev=2)
        tr, dv, ev = (set(got["speakers"][k]) for k in ("train", "dev", "eval"))
        assert not (tr & dv) and not (tr & ev) and not (dv & ev)

    def test_every_row_lands_exactly_once(self):
        rows = _rows()
        got = A.split_by_speaker(rows, n_eval=2, n_dev=2)
        assert sum(got["counts"].values()) == len(rows)
        assert {r["wav"] for r in got["train"] + got["dev"] + got["eval"]} == {
            r["wav"] for r in rows
        }

    def test_deterministic_for_same_seed(self):
        a = A.split_by_speaker(_rows(), seed=7)
        b = A.split_by_speaker(_rows(), seed=7)
        assert a["speakers"] == b["speakers"]
        assert [r["wav"] for r in a["eval"]] == [r["wav"] for r in b["eval"]]

    def test_too_few_speakers_raises(self):
        with pytest.raises(ValueError):
            A.split_by_speaker(_rows(n_spk=4), n_eval=2, n_dev=2)

    def test_missing_speaker_raises(self):
        rows = [{"wav": "a.wav", "text": "食飯"}]
        with pytest.raises(ValueError):
            A.split_by_speaker(rows)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
class TestManifest:
    def test_jsonl(self, tmp_path):
        p = tmp_path / "m.jsonl"
        p.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in _rows(3))
            + "\n# 注释行应被忽略\n",
            encoding="utf-8",
        )
        rows = A.load_manifest(p)
        assert len(rows) == 9
        assert rows[0]["speaker"] == "s0"

    def test_csv_with_aliases(self, tmp_path):
        p = tmp_path / "m.csv"
        p.write_text(
            "audio,transcript,spk\ns0_0.wav,食飯,s0\n", encoding="utf-8"
        )
        rows = A.load_manifest(p)
        assert rows == [{"wav": "s0_0.wav", "text": "食飯", "speaker": "s0"}]

    def test_json_array(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(_rows(2)), encoding="utf-8")
        assert len(A.load_manifest(p)) == 6

    def test_unknown_suffix_raises(self, tmp_path):
        p = tmp_path / "m.txt"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            A.load_manifest(p)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            A.load_manifest(tmp_path / "nope.jsonl")


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
class TestCli:
    def test_writes_split_and_reports_missing_wavs(self, tmp_path, capsys):
        rows = _rows(5)
        p = tmp_path / "m.jsonl"
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                     encoding="utf-8")
        out = tmp_path / "split.json"
        code = A.main(["--manifest", str(p), "--split-out", str(out),
                       "--n-eval", "1", "--n-dev", "1", "--check-files"])
        assert code == 1                     # 所有 wav 都不存在
        assert out.is_file()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["counts"]["train"] + data["counts"]["dev"] + data["counts"]["eval"] == len(rows)
        assert "缺失 wav" in capsys.readouterr().out

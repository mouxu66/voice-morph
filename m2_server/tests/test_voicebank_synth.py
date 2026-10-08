"""评测样本批量合成单测（2026-10-08，大创研究层）。

被测：`tools/voicebank_synth.py`（matrix.json → samples/<sp>/<档>/<条件>.wav）。

★ 这个文件守的是「跑得通、但结果悄悄错了」的地方 —— 合成这一环的所有错法
  都是**不报错**的：

  1. **挑错参考音**：把 10s 档的样本写进 30s 档目录，自变量标签就错了，
     `analyze` 照常出报告。所以有一条断言直接读 `provenance.json` 里每格的
     `ref`，确认「档位 → 参考音」的对应来自矩阵而不是人手。
  2. **半套矩阵**：一格失败却静默跳过（或缺格），后面 `analyze` 只会说
     「数据不足以判定」—— 错因指向统计，实际在采集。所以失败必须计数、
     必须进 failures、必须让 `ok=False`，且**不产生空文件占位**。
  3. **临时音色漏删**：跑完在音色库留下 48 个实验音色，界面上一眼看不出来，
     下次挑音色就会撞见。所以每次都要断言跑完那些目录真的没了。
  4. **伪造 ref_text**：猜一份与参考音不符的稿子会污染"复刻质量"这个因变量。
     默认必须**不写** `ref_text.txt`；只有显式给了稿子才写。
  5. **agent 的第一次踩坑**：后端认的 media 目录与 `--media-dir` 不一致时，
     TTS 只回一句「音色不存在」—— 报错里必须带上排查指向，否则下一个人
     会在"为什么本地明明有这个音色"上耗一小时。
"""

import io
import json
import sys
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import voicebank_synth as vs  # noqa: E402


# ---------------- 夹具 ----------------

def _wav_bytes(seconds: float = 1.0, sr: int = 22050, f0: float = 150.0) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    y = 0.4 * np.sin(2 * np.pi * f0 * t)
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, y, sr, format="WAV")
    return buf.getvalue()


def _write_wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes(seconds))
    return path


def _make_exp(tmp_path: Path, *, exp: str = "t01",
              speakers=("spk01",), tags=("3s", "30s"),
              conditions=("same", "cross")) -> Path:
    """搭一个最小实验目录：matrix.json + refs/<sp>/<档>.wav（约定与 plan 一致）。"""
    base = tmp_path / "vb" / exp
    base.mkdir(parents=True, exist_ok=True)
    matrix = []
    for sp in speakers:
        for tag in tags:
            _write_wav(base / "refs" / sp / f"{tag}.wav", 2.0)
            for cond in conditions:
                matrix.append({
                    "sample_id": f"{sp}/{tag}/{cond}", "speaker": sp,
                    "duration_s": float(tag.rstrip("s")), "duration_tag": tag,
                    "condition": cond, "text": f"{sp}-{tag}-{cond} 的文本",
                    "style": "平实", "ref": f"refs/{sp}/{tag}.wav",
                    "truth": f"truth/{sp}/{cond}.wav",
                    "sample": f"samples/{sp}/{tag}/{cond}.wav",
                })
    (base / "matrix.json").write_text(json.dumps(
        {"exp": exp, "matrix": matrix}, ensure_ascii=False), encoding="utf-8")
    return base


def _fake_tts(monkeypatch, *, fail_on_text=None, http_404_text=None, bad_bytes=False,
              wav_seconds=1.0, on_post=None):
    """假后端：POST /api/tts 返回 url，GET 返回 wav 字节。返回 calls 供断言。"""
    calls: list[dict] = []

    def fake_post(url, payload, timeout=0):
        calls.append({"url": url, "payload": dict(payload)})
        if on_post:
            on_post(payload)
        if http_404_text and payload.get("text") == http_404_text:
            body = io.BytesIO(json.dumps(
                {"detail": f"音色 [{payload.get('voice_id')}] 不存在"}).encode())
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, body)
        if fail_on_text and payload.get("text") == fail_on_text:
            raise RuntimeError("worker 掉线")
        return {"ok": True, "voice_id": payload.get("voice_id"), "wav": "tts_x.wav",
                "url": "/api/media/outputs/.session/tts_x.wav",
                "duration_s": round(wav_seconds, 1)}

    def fake_get(url, timeout=0):
        return b"not-a-wav" if bad_bytes else _wav_bytes(wav_seconds)

    monkeypatch.setattr(vs, "_post_json", fake_post)
    monkeypatch.setattr(vs, "_get_bytes", fake_get)
    return calls


def _run(tmp_path, **kw):
    return vs.run(kw.pop("exp", "t01"), root=tmp_path / "vb",
                  media_dir=tmp_path / "media", api="http://fake", log=lambda *a, **k: None,
                  **kw)


# ---------------- 正常路径 ----------------

class TestHappyPath:
    def test_samples_land_at_matrix_paths(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        calls = _fake_tts(monkeypatch)
        res = _run(tmp_path)

        assert res["ok"] is True
        assert res["synthesized"] == 4          # 1 说话人 × 2 档 × 2 条件
        assert res["failed"] == 0 and res["skipped"] == 0
        assert len(calls) == 4
        for sp, tag, cond in (("spk01", "3s", "same"), ("spk01", "30s", "cross")):
            f = base / "samples" / sp / tag / f"{cond}.wav"
            assert f.exists() and f.stat().st_size > 0

    def test_each_cell_uses_its_own_tier_reference(self, tmp_path, monkeypatch):
        """★ 自变量标签的命门：3s 档的样本必须由 3s 的参考音生成。"""
        base = _make_exp(tmp_path)
        seen: list[tuple[str, str]] = []

        def on_post(payload):
            vid = payload["voice_id"]
            # 从临时音色目录里读回它的参考音，确认用的是这一档的那一段
            ref = tmp_path / "media" / "voicebank" / vid / "reference.wav"
            assert ref.exists(), "合成时临时音色必须已就位"
            seen.append((vid, payload["text"]))

        _fake_tts(monkeypatch, on_post=on_post)
        res = _run(tmp_path)

        prov = json.loads((base / "provenance.json").read_text("utf-8"))
        assert res["ok"] is True
        for cell in prov["cells"].values():
            assert cell["ref"] == f"refs/{cell['speaker']}/{cell['duration_tag']}.wav"
            assert cell["sample_id"].split("/")[1] == cell["duration_tag"]
        # 参考音目录里的档位名就是临时音色 id 的后缀（3s/30s 各一）
        assert sorted(v.split("_")[-1] for v, _ in seen) == ["30s", "30s", "3s", "3s"]

    def test_provenance_records_engine_and_empty_ref_text(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        _fake_tts(monkeypatch)
        _run(tmp_path)
        prov = json.loads((base / "provenance.json").read_text("utf-8"))
        assert "qwen3-tts" in prov["engine"]
        assert prov["ref_text_mode"].startswith("无 ref_text")
        assert len(prov["runs"]) == 1 and prov["runs"][0]["synth"] == 4
        # 每格都要有可追溯字段，缺一个就没法在论文里自证
        for cell in prov["cells"].values():
            for k in ("sample_id", "ref", "ref_actual_s", "text", "style",
                      "audio_s", "at"):
                assert k in cell


# ---------------- 断点续跑 ----------------

class TestResume:
    def test_existing_samples_are_skipped(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        done = base / "samples" / "spk01" / "3s" / "same.wav"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_bytes(b"SENTINEL")
        calls = _fake_tts(monkeypatch)
        res = _run(tmp_path)
        assert res["skipped"] == 1 and res["synthesized"] == 3
        assert len(calls) == 3
        assert done.read_bytes() == b"SENTINEL"       # 已存在的样本不许被覆盖

    def test_force_resynthesizes(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        done = base / "samples" / "spk01" / "3s" / "same.wav"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_bytes(b"SENTINEL")
        calls = _fake_tts(monkeypatch)
        res = _run(tmp_path, force=True)
        assert res["skipped"] == 0 and len(calls) == 4
        assert done.read_bytes() != b"SENTINEL"


# ---------------- 失败语义 ----------------

class TestFailures:
    def test_one_failure_is_counted_and_others_still_run(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        _fake_tts(monkeypatch, fail_on_text="spk01-3s-same 的文本")
        res = _run(tmp_path)
        assert res["ok"] is False and res["failed"] == 1 and res["synthesized"] == 3
        assert res["failures"][0]["sample_id"] == "spk01/3s/same"
        assert res["error_stage"] == "合成"
        # 失败的格**不落盘**：空文件会让矩阵"看起来是满的"
        assert not (base / "samples" / "spk01" / "3s" / "same.wav").exists()
        # 其余格照常
        assert (base / "samples" / "spk01" / "3s" / "cross.wav").exists()

    def test_unreadable_bytes_do_not_land(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        _fake_tts(monkeypatch, bad_bytes=True)
        res = _run(tmp_path)
        assert res["ok"] is False and res["failed"] == 4
        assert not (base / "samples").exists() or not list((base / "samples").rglob("*.wav"))

    def test_voice_not_found_error_names_the_media_dir(self, tmp_path, monkeypatch):
        """后端不认我们的 media 目录时，报错必须给出排查方向。"""
        _make_exp(tmp_path)
        _fake_tts(monkeypatch, http_404_text="spk01-3s-same 的文本")
        res = _run(tmp_path)
        msg = res["failures"][0]["error"]
        assert "音色" in msg and "--media-dir" in msg

    def test_missing_matrix_reports_stage_without_traceback(self, tmp_path):
        res = vs.run("nope", root=tmp_path / "vb", media_dir=tmp_path / "media",
                     api="http://fake", log=lambda *a, **k: None)
        assert res["ok"] is False and res["error_stage"] == "读矩阵"
        assert "plan" in res["error"]


# ---------------- 临时音色 ----------------

class TestTmpVoice:
    def test_tmp_voice_removed_even_on_failure(self, tmp_path, monkeypatch):
        _make_exp(tmp_path)
        _fake_tts(monkeypatch, fail_on_text="spk01-3s-same 的文本")
        res = _run(tmp_path)
        vbank = tmp_path / "media" / "voicebank"
        assert res["failed"] == 1
        assert not vbank.exists() or not list(vbank.iterdir())

    def test_keep_voices_flag_leaves_them_for_inspection(self, tmp_path, monkeypatch):
        _make_exp(tmp_path)
        _fake_tts(monkeypatch)
        _run(tmp_path, keep_voices=True)
        left = sorted(p.name for p in (tmp_path / "media" / "voicebank").iterdir())
        assert left == ["vbsyn_t01_spk01_30s", "vbsyn_t01_spk01_3s"]

    def test_voice_id_is_legal_for_the_backend_regex(self, tmp_path):
        """后端 `common._VOICE_ID_RE` = `^[A-Za-z0-9_-]+$`；实验名带点/中文也不能越界。"""
        import re

        for exp, sp, tag in (("t01", "spk01", "3s"), ("vb.01 x", "说者 A", "60s")):
            vid = vs.tmp_voice_id(exp, sp, tag)
            assert re.match(r"^[A-Za-z0-9_-]+$", vid), vid
            assert vid.startswith(vs.TMP_VOICE_PREFIX)

    def test_ref_text_written_only_when_provided(self, tmp_path, monkeypatch):
        """默认不伪造稿子；给了 txt 才写（内容是 txt 里的原文）。"""
        _make_exp(tmp_path)
        texts = tmp_path / "ref_texts" / "spk01"
        texts.mkdir(parents=True)
        (texts / "3s.txt").write_text("这是 3 秒档真实念的句子。", encoding="utf-8")
        seen: list[tuple[str, str]] = []

        def on_post(payload):
            d = tmp_path / "media" / "voicebank" / payload["voice_id"]
            rt = d / "ref_text.txt"
            seen.append((payload["voice_id"], rt.read_text("utf-8") if rt.exists() else ""))

        _fake_tts(monkeypatch, on_post=on_post)
        _run(tmp_path, ref_texts_dir=tmp_path / "ref_texts")
        by_id = dict(seen)
        assert by_id["vbsyn_t01_spk01_3s"] == "这是 3 秒档真实念的句子。"
        assert by_id["vbsyn_t01_spk01_30s"] == ""      # 缺稿的那档回退纯声纹

    def test_stale_ref_text_is_cleared(self, tmp_path):
        """上一个实验在同一 id 上留下的稿子不能串到这一档。"""
        d = tmp_path / "media" / "voicebank" / "vbsyn_t01_spk01_3s"
        d.mkdir(parents=True)
        (d / "ref_text.txt").write_text("旧稿", encoding="utf-8")
        ref = _write_wav(tmp_path / "refs" / "3s.wav")
        assert vs.install_tmp_voice(tmp_path / "media", d.name, ref).get("ok")
        assert not (d / "ref_text.txt").exists()


# ---------------- 过滤与 dry-run ----------------

class TestSelection:
    def test_duration_filter_only_synthesizes_that_tier(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        calls = _fake_tts(monkeypatch)
        res = _run(tmp_path, durations=["3s"])
        assert res["cells"] == 2 and len(calls) == 2
        assert not (base / "samples" / "spk01" / "30s").exists()

    def test_speaker_filter_unknown_is_empty_and_reported(self, tmp_path):
        _make_exp(tmp_path)
        res = vs.run("t01", root=tmp_path / "vb", media_dir=tmp_path / "media",
                     api="http://fake", speakers=["nobody"],
                     log=lambda *a, **k: None)
        assert res["ok"] is False and res["error_stage"] == "选格"

    def test_dry_run_touches_nothing(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        calls = _fake_tts(monkeypatch)
        res = _run(tmp_path, dry_run=True)
        assert calls == [] and res["dry_run"] is True and len(res["plan"]) == 4
        assert not (base / "samples").exists()
        assert not (tmp_path / "media").exists()
        assert not (base / "provenance.json").exists()

    def test_missing_reference_surfaces_as_failure(self, tmp_path, monkeypatch):
        base = _make_exp(tmp_path)
        (base / "refs" / "spk01" / "3s.wav").unlink()
        _fake_tts(monkeypatch)
        res = _run(tmp_path)
        assert res["ok"] is False and res["failed"] == 2      # 该档的两格都失败
        assert all("参考音不存在" in f["error"] for f in res["failures"])
        assert res["synthesized"] == 2                        # 30s 档照常


class TestCli:
    def test_help_does_not_crash_on_percent_signs(self, monkeypatch, capsys):
        """argparse 的 help 字符串会被做 `%` 格式化 —— help 里手写 `%APPDATA%`

        会让 `--help` 自己崩在 `ValueError: unsupported format character 'A'`。
        这条只有人工跑一次 --help 才会发现，所以钉住它。
        """
        monkeypatch.setattr(sys, "argv", ["voicebank_synth.py", "--help"])
        with pytest.raises(SystemExit) as exc:
            vs.main()
        assert exc.value.code == 0
        assert "media-dir" in capsys.readouterr().out

    def test_dry_run_cli_exits_zero_and_lists_plan(self, tmp_path, monkeypatch, capsys):
        base = _make_exp(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "voicebank_synth.py", "--exp", "t01", "--root", str(tmp_path / "vb"),
            "--media-dir", str(tmp_path / "media"), "--api", "http://fake", "--dry-run"])
        assert vs.main() == 0
        out = capsys.readouterr().out
        assert "待合成" in out and "spk01/3s/same" in out
        assert not (base / "samples").exists()

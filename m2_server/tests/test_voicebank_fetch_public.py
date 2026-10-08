"""公开许可语料抓取单测（2026-10-08）。

★ 这个文件守的是**合规**，不是功能 —— 而合规的失效方式恰恰是"跑得通"：
  ND/NC 语料照样能下、能拼、能出一条漂亮的报告，等到别人问"许可呢"才发现不能用。
  所以下面每条都对着一个具体的翻车法：

  1. **许可白名单**：`CC-BY-NC-ND`（ST-CMDS 就是）必须被拒 —— 切档与拼接是演绎，
     NC 与 ND 两条全踩。这条不能靠人记，得靠门禁。
  2. **不得进训练集**（袋鼠音素材铁律）：输出目录命中 `clips/rvc_dataset/dataset_raw`
     就拒。公开语料是评测素材，混进训练集是**事后不可撤销**的污染。
  3. **坏条不许静默拼进去**：削波 / 静音 / 读不出 的条要剔除并记录 ——
     一条爆音会把 reference 的 HNR/jitter 整个带偏，而报告上看不出原因。
  4. **采样率不一致要剔除**：跨设备的条拼进同一条"连续录音"，等于把
     "同一麦"这个控制变量悄悄破坏掉。
  5. **来源可追溯**：`SOURCES.json` 必须记许可、许可出处、署名、检索时间与逐条哈希。
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import voicebank_fetch_public as fp  # noqa: E402


def _wav_bytes(seconds=1.0, sr=44100, amp=0.4, f0=160.0) -> bytes:
    import io

    t = np.arange(int(sr * seconds)) / sr
    y = amp * np.sin(2 * np.pi * f0 * t)
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _manifest(**over):
    m = {
        "corpus": "AISHELL-3", "license": "Apache-2.0",
        "license_source": "https://www.openslr.org/93/", "attribution": "AISHELL-3",
        "base_url": "https://example.invalid/x", "split": "train",
        "clips_per_speaker": 3, "gap_ms": 300,
        "speakers": [{"id": "SSB0001", "note": "n"}],
    }
    m.update(over)
    return m


def _write_manifest(tmp_path, **over) -> Path:
    p = tmp_path / "m.json"
    p.write_text(json.dumps(_manifest(**over), ensure_ascii=False), encoding="utf-8")
    return p


def _scrub_dir_gate(monkeypatch) -> None:
    """把「不得进训练集」的目录门禁暂时关掉。

    ⚠️ 为什么需要：pytest 的 `tmp_path` **目录名取自用例名**（且会被截断），
    而用例名叫 `test_all_clips_bad_...` 时路径里就带着 `clips` —— 于是「合法路径」
    的用例会被目录门禁误红。门禁本身另有两条专用用例守着
    （`TestTrainingSetGate`），这里只隔离这个正交因素。
    """
    monkeypatch.setattr(fp, "FORBIDDEN_OUT_TOKENS", ())


def _fake_get(monkeypatch, *, payload=None, fail_names=()):
    """假下载：按文件名返回不同的 wav；fail_names 里的条返回坏字节。"""
    calls = []

    def fake(url, timeout=0):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        if name in fail_names:
            return b"not-a-wav"
        return (payload or {}).get(name) or _wav_bytes(1.0)

    monkeypatch.setattr(fp, "_get", fake)
    return calls


# ---------------- 1. 许可门禁 ----------------

class TestLicenseGate:
    def test_apache_and_cc0_pass(self):
        for lic in ("Apache-2.0", "apache 2.0", "CC0-1.0", "cc-by-4.0", "public-domain"):
            assert fp.check_license({"license": lic})["ok"], lic

    def test_nc_nd_is_rejected_with_reason(self):
        """ST-CMDS 就是 CC BY-NC-ND —— 本项目要切档+拼接（=演绎），两条都踩。"""
        got = fp.check_license({"license": "CC BY-NC-ND 4.0"})
        assert got["ok"] is False
        assert "NC" in got["reason"] and "ND" in got["reason"]

    def test_unknown_license_rejected(self):
        assert fp.check_license({"license": "whatever-1.0"})["ok"] is False
        assert fp.check_license({})["ok"] is False

    def test_run_refuses_before_downloading(self, tmp_path, monkeypatch):
        calls = _fake_get(monkeypatch)
        res = fp.run(_write_manifest(tmp_path, license="CC-BY-NC-4.0"),
                     tmp_path / "out", log=lambda *a, **k: None)
        assert res["ok"] is False and res["error_stage"] == "许可门禁"
        assert calls == []                      # 不合规就一条都不下
        assert not (tmp_path / "out").exists()

    def test_run_records_license_and_usage_limits(self, tmp_path, monkeypatch):
        _fake_get(monkeypatch)
        _scrub_dir_gate(monkeypatch)
        res = fp.run(_write_manifest(tmp_path), tmp_path / "out", log=lambda *a, **k: None)
        assert res["ok"] is True
        src = json.loads((tmp_path / "out" / "SOURCES.json").read_text("utf-8"))
        assert src["license"] == "Apache-2.0"       # 原样（人工对来源页用）
        assert src["license_id"] == "apache-2.0"     # 规范化（机器比白名单用）
        assert "允许商用与演绎" in src["license_note"]
        assert src["license_source"] == "https://www.openslr.org/93/"
        assert src["speakers"][0]["url_dir"].startswith("https://example.invalid/x/train/wav/SSB0001")
        assert any("被试" in x for x in src["usage_limits"])
        assert src["speakers"][0]["clips_ok"] == 3


# ---------------- 2. 训练集目录门禁 ----------------

class TestTrainingSetGate:
    def test_forbidden_dirs_rejected(self):
        for bad in ("media/clips/pub", "D:/RVC/dataset_raw/x", "data/rvc_dataset/k"):
            got = fp.check_out_dir(Path(bad))
            assert got["ok"] is False, bad
            assert "训练集" in got["reason"]

    def test_normal_dir_passes(self):
        assert fp.check_out_dir(Path("media/vocals/public_pilot"))["ok"] is True

    def test_run_refuses_training_dir(self, tmp_path, monkeypatch):
        calls = _fake_get(monkeypatch)
        res = fp.run(_write_manifest(tmp_path), tmp_path / "rvc_dataset" / "o",
                     log=lambda *a, **k: None)
        assert res["ok"] is False and res["error_stage"] == "目录门禁"
        assert calls == []


# ---------------- 3/4. 逐条体检与拼接 ----------------

class TestClips:
    def test_stitches_with_gaps_and_writes_source(self, tmp_path, monkeypatch):
        _fake_get(monkeypatch)
        _scrub_dir_gate(monkeypatch)
        out = tmp_path / "out"
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=3), out,
                     log=lambda *a, **k: None)
        assert res["ok"] is True
        f = out / "SSB0001" / "source.wav"
        assert f.exists()
        info = sf.info(str(f))
        assert info.samplerate == 44100
        # 3 条 × 1s + 3 个 300ms 间隔
        assert abs(info.frames / info.samplerate - 3.9) < 0.05

    def test_clipped_and_unreadable_clips_are_dropped_and_recorded(self, tmp_path, monkeypatch):
        payload = {
            "SSB00010001.wav": _wav_bytes(1.0, amp=1.0),        # 削波（peak=1.0）
            "SSB00010003.wav": _wav_bytes(1.0),
        }
        _fake_get(monkeypatch, payload=payload, fail_names=("SSB00010002.wav",))
        _scrub_dir_gate(monkeypatch)
        out = tmp_path / "out"
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=3), out, log=lambda *a, **k: None)
        assert res["ok"] is True
        spk = res["speakers"][0]
        assert spk["clips_ok"] == 1
        reasons = " ".join(s["reason"] for s in spk["clips_skipped"])
        assert "削波" in reasons and "读不出" in reasons
        # 只拼了那 1 条可用音频
        assert abs(sf.info(str(out / "SSB0001" / "source.wav")).frames / 44100 - 1.3) < 0.05

    def test_silent_clip_dropped(self, tmp_path, monkeypatch):
        payload = {"SSB00010001.wav": np.zeros(44100, dtype="float32")}
        import io

        buf = io.BytesIO()
        sf.write(buf, payload["SSB00010001.wav"], 44100, format="WAV", subtype="PCM_16")
        _fake_get(monkeypatch, payload={"SSB00010001.wav": buf.getvalue(),
                                        "SSB00010002.wav": _wav_bytes(1.0),
                                        "SSB00010003.wav": _wav_bytes(1.0)})
        _scrub_dir_gate(monkeypatch)
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=3), tmp_path / "out",
                     log=lambda *a, **k: None)
        reasons = " ".join(s["reason"] for s in res["speakers"][0]["clips_skipped"])
        assert "静音" in reasons

    def test_sample_rate_mismatch_dropped(self, tmp_path, monkeypatch):
        _fake_get(monkeypatch, payload={"SSB00010002.wav": _wav_bytes(1.0, sr=16000)})
        _scrub_dir_gate(monkeypatch)
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=3), tmp_path / "out",
                     log=lambda *a, **k: None)
        reasons = " ".join(s["reason"] for s in res["speakers"][0]["clips_skipped"])
        assert "采样率不一致" in reasons

    def test_all_utterances_bad_is_error_with_hint(self, tmp_path, monkeypatch):
        _fake_get(monkeypatch, fail_names=("SSB00010001.wav", "SSB00010002.wav", "SSB00010003.wav"))
        _scrub_dir_gate(monkeypatch)
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=3), tmp_path / "out",
                     log=lambda *a, **k: None)
        assert res["ok"] is False and res["error_stage"].startswith("抓取")
        assert not (tmp_path / "out" / "SSB0001" / "source.wav").exists()

    def test_dry_run_lists_urls_without_downloading(self, tmp_path, monkeypatch):
        calls = _fake_get(monkeypatch)
        res = fp.run(_write_manifest(tmp_path, clips_per_speaker=4), tmp_path / "out",
                     dry_run=True, log=lambda *a, **k: None)
        assert calls == [] and res["ok"] is True
        assert res["speakers"][0]["files"][-1].endswith("SSB00010004.wav")
        assert not (tmp_path / "out").exists()

    def test_sources_json_has_per_clip_hashes(self, tmp_path, monkeypatch):
        _fake_get(monkeypatch)
        _scrub_dir_gate(monkeypatch)
        fp.run(_write_manifest(tmp_path, clips_per_speaker=2), tmp_path / "out",
               log=lambda *a, **k: None)
        src = json.loads((tmp_path / "out" / "SOURCES.json").read_text("utf-8"))
        for f in src["speakers"][0]["files"]:
            assert len(f["sha256"]) == 64 and f["duration_s"] > 0

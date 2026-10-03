"""声音保存档案（voice preservation）单测（2026-10-02，大创产品层）。

被测：`m2_server/voicepreserve.py`（档案存储层）+ `m2_server/voicepreserve_api.py`（路由）。

★ 这个文件守的是**这个功能最不能出错的两件事**：

  1. **同意先于克隆**。`add_reference` **不检查** consent（录制本身不是克隆，
     卡在这里用户就没法先试录一段听听效果），但 `gate()` 必须把「没有同意」
     判成不可复现。这条语义**写反了不会报错** —— 它会安静地变成一个
     能克隆任何人的工具。所以两个方向都有断言。
  2. **路径穿越**。档案 id 会拼进路径。本模块的防线是**结构性的**：
     id 由 `new_profile_id()` 生成、形状受正则约束，而不是靠调用方传对。
     `delete_profile` 还有第二层「结果必须落在档案库之内」的断言 ——
     删用户录音这种事，一次穿越就是不可逆的数据丢失。

  另外守两条容易"看着对其实错"的：
  3. **webm 不能当 wav 存**。前端录的是 webm/opus，直接命名成 `.wav` 写下来
     **不报任何错**，只会在几秒后以"读不出浊音段"的形式暴露。
     所以 `add_reference` 必须走 ffmpeg 归一，且有一条用例真的喂 webm 进去。
  4. **标称时长 ≠ 实际时长**。报结论要用实际值；拿标称值冒充就是把自变量写错。
"""

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import voicepreserve as vp  # noqa: E402
import voicepreserve_api as vp_api  # noqa: E402


# ---------------- 夹具 ----------------

@pytest.fixture(autouse=True)
def iso_media(tmp_path, monkeypatch):
    """把档案库指到 tmp —— 否则会往用户真实的 `media/preserve/` 里写录音。

    ★ 能这么打是因为 `store_dir()` 是**每次调用时**读 `cfg.MEDIA_DIR`，
    不是导入时早绑定（`docs/犯错档案-工程.md` §8.36 那类坑在这里不适用）。
    """
    monkeypatch.setattr(vp.cfg, "MEDIA_DIR", tmp_path / "media")
    return tmp_path / "media"


def _wav(path: Path, seconds: float = 1.0, sr: int = 22050, f0: float = 150.0) -> Path:
    """写一段元音状合成 wav（有浊音段，能被指标模块接受）。"""
    import soundfile as sf

    t = np.arange(int(sr * seconds)) / sr
    y = np.zeros_like(t)
    for k in range(1, 20):
        if k * f0 > sr / 2:
            break
        y += 10 ** (-6.0 * (k - 1) / 20.0) * np.sin(2 * np.pi * k * f0 * t)
    peak = float(np.abs(y).max())
    y = (y / peak * 0.5) if peak > 0 else y
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y, sr)
    return path


def _stub_convert(monkeypatch, ok: bool = True):
    """把 ffmpeg 归一替换成"直接把源拷过去"，用于**不需要真转换**的逻辑用例。

    `_to_wav` 单独有自己的用例（喂真 webm，见 `TestFfmpegNormalisation`）——
    这里打桩是为了让 CRUD / 门禁这些用例不依赖 ffmpeg 是否装好。
    """
    def _fake(src: Path, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(src).read_bytes())
        return (ok, "" if ok else "音频转换失败: 打桩失败")

    monkeypatch.setattr(vp, "_to_wav", _fake)


@pytest.fixture
def client():
    """只挂本插件的 router —— 不拉整个 `server.app`。

    为什么不测 `server.app`：`sound.preserve` **不在 `standard` 预设里**，
    默认是关的 → 在整套 app 上打它会 404，而那测的是"插件开关"，
    不是这个模块的行为。开关那一层由 `test_plugin_switch.py` 管。
    """
    app = FastAPI()
    app.include_router(vp_api.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------- 1. 档案 id 与路径守卫 ----------------

class TestProfileIdSafety:
    def test_generated_id_matches_the_pattern(self):
        for _ in range(50):
            assert vp.is_valid_profile_id(vp.new_profile_id())

    def test_ids_are_unique_within_the_same_second(self):
        """★ 纯时间戳会撞车，而撞车 = 两个档案互相覆盖（最坏是同意书被换掉）。"""
        ids = {vp.new_profile_id() for _ in range(200)}
        assert len(ids) == 200

    @pytest.mark.parametrize("bad", [
        "", "..", "../etc", "p/../../x", "abc", "p123", "p" + "9" * 10,  # 后缀缺
        "P1234567890abcdef",  # 大写开头
        "p1234567890abcde/",  # 带分隔符
    ])
    def test_bad_ids_are_rejected(self, bad):
        assert not vp.is_valid_profile_id(bad)
        with pytest.raises(ValueError):
            vp.profile_dir(bad)

    def test_traversal_cannot_escape_the_store(self, iso_media):
        """拼一个能穿越的 id 必须被**形状校验**挡住，而不是靠后面碰运气。"""
        with pytest.raises(ValueError):
            vp.profile_dir("../../windows")

    def test_delete_refuses_paths_outside_the_store(self, iso_media):
        """第二层守卫：即便形状校验被绕过，删除也要再确认落在库内。"""
        outside = iso_media.parent / "not_the_store"
        outside.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError):
            vp.delete_profile(str(outside))


# ---------------- 2. CRUD ----------------

class TestCrud:
    def test_create_and_get(self, iso_media):
        p = vp.create_profile("张老师", note="高一语文")
        assert p["id"] and p["label"] == "张老师"
        assert vp.get_profile(p["id"])["note"] == "高一语文"

    def test_create_makes_the_directory_skeleton(self, iso_media):
        p = vp.create_profile("甲")
        d = vp.profile_dir(p["id"])
        assert (d / "profile.json").exists()
        assert (d / "refs").is_dir() and (d / "truth").is_dir()

    def test_create_does_not_assume_consent(self, iso_media):
        """★ 建档**不能**替用户默认同意 —— 那会让「同意先于克隆」形同虚设。"""
        p = vp.create_profile("甲")
        assert p["consent"] is None
        assert vp.has_consent(p["id"]) is False

    def test_empty_label_is_rejected(self, iso_media):
        with pytest.raises(ValueError):
            vp.create_profile("   ")

    def test_label_is_clipped_not_rejected(self, iso_media):
        """超长标签截断即可 —— 它只是给人看的名字，不该因此建不了档。"""
        p = vp.create_profile("字" * 200)
        assert len(p["label"]) == vp._MAX_LABEL

    def test_list_is_newest_first_even_within_the_same_second(self, iso_media):
        """★ 秒级时间戳排不出「同一秒内建的两个档案」的先后 ——
        连建两个时列表顺序会变成不确定的（2026-10-02 实测），
        用户看到的是"我刚建的那个不在最上面"，而这类 bug 极难复现。
        所以排序用 `created_ms`。
        """
        a = vp.create_profile("先")
        b = vp.create_profile("后")
        got = [x["id"] for x in vp.list_profiles()]
        assert got.index(b["id"]) < got.index(a["id"])

    def test_list_survives_a_profile_without_created_ms(self, iso_media):
        """老档案没有 `created_ms` 时回落到 0，排在最后 —— 但不丢、不报错。"""
        p = vp.create_profile("甲")
        d = vp.profile_dir(p["id"])
        data = json.loads((d / "profile.json").read_text(encoding="utf-8"))
        data.pop("created_ms")
        (d / "profile.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert [x["id"] for x in vp.list_profiles()] == [p["id"]]

    def test_list_skips_broken_dirs(self, iso_media):
        """一条脏目录不该毁掉整页列表。"""
        good = vp.create_profile("好的")
        (vp.store_dir() / "pjunk000000000000").mkdir(parents=True)
        (vp.store_dir() / "pjunk000000000000" / "profile.json").write_text("{坏", encoding="utf-8")
        got = [x["id"] for x in vp.list_profiles()]
        assert got == [good["id"]]

    def test_delete_removes_everything(self, iso_media):
        p = vp.create_profile("甲")
        d = vp.profile_dir(p["id"])
        assert vp.delete_profile(p["id"]) is True
        assert not d.exists()
        assert vp.get_profile(p["id"]) is None

    def test_delete_missing_returns_false(self, iso_media):
        assert vp.delete_profile(vp.new_profile_id()) is False


# ---------------- 3. 同意先于克隆（★ 安全语义） ----------------

class TestConsent:
    def test_record_and_read_back(self, iso_media):
        p = vp.create_profile("甲")
        c = vp.record_consent(p["id"], scope="self", statement="我确认…", operator="本人")
        assert c["version"] == vp.CONSENT_VERSION
        assert c["scope"] == "self" and c["operator"] == "本人"
        assert vp.has_consent(p["id"]) is True

    def test_bad_scope_is_rejected(self, iso_media):
        p = vp.create_profile("甲")
        with pytest.raises(ValueError):
            vp.record_consent(p["id"], scope="everyone")

    def test_consent_on_missing_profile_is_structured_error(self, iso_media):
        res = vp.record_consent(vp.new_profile_id())
        assert res.get("error") and res["error_stage"] == "读取档案"

    def test_recording_does_not_require_consent(self, iso_media, monkeypatch):
        """★ 方向一：**录制**不卡同意 —— 否则用户没法先试录一段听听效果。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        src = _wav(iso_media / "in.wav")
        rec = vp.add_reference(p["id"], src, 3.0)
        assert rec.get("error") is None
        assert vp.has_consent(p["id"]) is False

    def test_gate_blocks_reproduction_without_consent(self, iso_media, monkeypatch):
        """★ 方向二：**复现**必须卡同意。写反了就成了能克隆任何人的工具。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "in.wav"), 10.0)
        g = vp.gate(p["id"])
        assert g["verdict"] == "not_ready"
        assert any("知情同意" in r for r in g["reasons"])

    def test_consent_is_logged(self, iso_media):
        p = vp.create_profile("甲")
        vp.record_consent(p["id"], operator="本人")
        kinds = [e["kind"] for e in vp.read_events(p["id"])]
        assert "consent" in kinds


# ---------------- 4. 参考音 ----------------

class TestReferences:
    def test_add_and_read_back(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        rec = vp.add_reference(p["id"], _wav(iso_media / "a.wav"), 10.0)
        assert rec["tag"] == "10s" and rec["file"] == "10s.wav"
        assert (vp.profile_dir(p["id"]) / "refs" / "10s.wav").exists()
        assert len(vp.get_profile(p["id"])["refs"]) == 1

    def test_same_tag_replaces_not_appends(self, iso_media, monkeypatch):
        """★ 同一档重录必须**替换** —— 追加会让门禁拿一堆重复档去算，中位数失去意义。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav", 1.0), 10.0)
        vp.add_reference(p["id"], _wav(iso_media / "b.wav", 2.0), 10.0)
        refs = vp.get_profile(p["id"])["refs"]
        assert len(refs) == 1
        assert refs[0]["actual_s"] == pytest.approx(2.0, abs=0.1)

    def test_refs_stay_sorted_by_nominal_duration(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        for d in (30.0, 3.0, 10.0):
            vp.add_reference(p["id"], _wav(iso_media / f"{int(d)}.wav"), d)
        got = [r["duration_s"] for r in vp.get_profile(p["id"])["refs"]]
        assert got == [3.0, 10.0, 30.0]

    def test_nominal_and_actual_duration_are_recorded_separately(self, iso_media, monkeypatch):
        """★ 标称档 3s 实际可能只有 1.2s。两个都要记 ——
        报结论要用实际值，拿标称值冒充就是把自变量写错。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        rec = vp.add_reference(p["id"], _wav(iso_media / "a.wav", 1.2), 3.0)
        assert rec["duration_s"] == 3.0
        assert rec["actual_s"] == pytest.approx(1.2, abs=0.05)

    def test_bad_duration_is_a_structured_error(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        src = _wav(iso_media / "a.wav")
        for bad in (0, -1, "abc"):
            res = vp.add_reference(p["id"], src, bad)
            assert res.get("error") and res["error_stage"] == "参数"

    def test_empty_source_is_rejected(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        empty = iso_media / "empty.wav"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        res = vp.add_reference(p["id"], empty, 3.0)
        assert res.get("error")

    def test_conversion_failure_is_reported_not_silent(self, iso_media, monkeypatch):
        """转换失败必须报出来 —— 静默写一个坏文件比报错差得多。"""
        _stub_convert(monkeypatch, ok=False)
        p = vp.create_profile("甲")
        res = vp.add_reference(p["id"], _wav(iso_media / "a.wav"), 3.0)
        assert res.get("error") and res["error_stage"] == "音频转换"
        assert vp.get_profile(p["id"])["refs"] == []

    def test_reference_path_prefers_longest(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        for d in (3.0, 30.0, 10.0):
            vp.add_reference(p["id"], _wav(iso_media / f"{int(d)}.wav"), d)
        assert vp.reference_path(p["id"]).name == "30s.wav"
        assert vp.reference_path(p["id"], 10.0).name == "10s.wav"
        assert vp.reference_path(p["id"], 60.0) is None

    def test_truth_requires_a_known_condition(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        src = _wav(iso_media / "t.wav")
        assert vp.add_truth(p["id"], "nope", src).get("error")
        assert vp.add_truth(p["id"], "cross", src).get("error") is None


# ---------------- 5. ffmpeg 归一（★ 真跑 ffmpeg） ----------------

class TestFfmpegNormalisation:
    def test_webm_is_not_stored_as_wav_bytes(self, iso_media, ffmpeg_bin, tmp_path):
        """★★ 前端录的是 webm/opus。直接把它命名成 `.wav` 写下来**不报错** ——
        只会在几秒后以"读不出浊音段"的形式暴露。这条用例真的喂 webm 进去，
        断言存下来的是一个**能被 soundfile 读出的真 wav**。
        """
        import soundfile as sf

        src_wav = _wav(tmp_path / "src.wav", 1.0)
        webm = tmp_path / "rec.webm"
        import subprocess

        r = subprocess.run(
            [ffmpeg_bin, "-y", "-loglevel", "error", "-i", str(src_wav),
             "-c:a", "libopus", str(webm)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not webm.exists():
            pytest.skip(f"这台机器造不出 opus 测试素材: {(r.stderr or '')[:200]}")

        assert webm.read_bytes()[:4] != b"RIFF", "前提不成立：源本身不是 webm"

        p = vp.create_profile("甲")
        rec = vp.add_reference(p["id"], webm, 3.0)
        assert rec.get("error") is None, rec
        stored = vp.profile_dir(p["id"]) / "refs" / "3s.wav"
        assert stored.read_bytes()[:4] == b"RIFF", "存下来的不是 wav —— webm 被原样写进去了"
        info = sf.info(str(stored))
        assert info.channels == 1, "必须是单声道"
        assert info.frames > 0

    def test_conversion_keeps_source_sample_rate(self, iso_media, ffmpeg_bin, tmp_path):
        """不降采样：降采样不可逆地削掉高频，而克隆要的正是高频里的音色信息。"""
        import soundfile as sf

        src = _wav(tmp_path / "src48.wav", 1.0, sr=48000)
        p = vp.create_profile("甲")
        rec = vp.add_reference(p["id"], src, 3.0)
        assert rec.get("error") is None, rec
        assert sf.info(str(vp.profile_dir(p["id"]) / "refs" / "3s.wav")).samplerate == 48000

    def test_garbage_input_reports_conversion_failure(self, iso_media, ffmpeg_bin, tmp_path):
        junk = tmp_path / "junk.wav"
        junk.write_bytes(b"this is definitely not audio" * 10)
        p = vp.create_profile("甲")
        res = vp.add_reference(p["id"], junk, 3.0)
        assert res.get("error") and res["error_stage"] == "音频转换"


# ---------------- 6. 门禁三态 ----------------

class TestGate:
    def test_not_ready_without_refs(self, iso_media):
        p = vp.create_profile("甲")
        g = vp.gate(p["id"])
        assert g["verdict"] == "not_ready"
        assert any("参考音" in r for r in g["reasons"])

    def test_not_ready_without_consent_even_with_refs(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav", 1.0), 3.0)
        assert vp.gate(p["id"])["verdict"] == "not_ready"

    def test_ready_when_consent_and_refs_and_metrics(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav", 2.0), 10.0)
        vp.record_consent(p["id"], operator="本人")
        g = vp.gate(p["id"])
        # parselmouth 缺失时指标算不出 → partial，这在这台机器上是合法结果
        assert g["verdict"] in ("ready", "partial")
        assert g["consent_ok"] is True
        if g["verdict"] == "partial":
            assert any("指标" in r or "体检" in r for r in g["reasons"])

    def test_lamps_do_not_decide_the_verdict(self, iso_media, monkeypatch):
        """★ 三灯**不参与** verdict。

        它的阈值锚在另一批素材（袋鼠 73 条自录切片）上，
        拿它否决一位教师的档案 = 用别人的嗓音标准判他的声音。
        所以这里断言：即便三灯全是红的，verdict 也照样由结构性事实决定。
        """
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav", 2.0), 10.0)
        vp.record_consent(p["id"])
        monkeypatch.setattr(vp, "gate", vp.gate)  # 保持真实现
        g = vp.gate(p["id"])
        assert g["verdict"] in ("ready", "partial")
        assert "lamps" in g

    def test_missing_profile_is_not_ready_not_a_crash(self, iso_media):
        g = vp.gate(vp.new_profile_id())
        assert g["verdict"] == "not_ready" and g["reasons"]

    def test_bad_id_is_not_ready_not_a_crash(self, iso_media):
        g = vp.gate("../etc")
        assert g["verdict"] == "not_ready"


# ---------------- 7. 操作日志 ----------------

class TestEvents:
    def test_append_only_and_readable(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav"), 3.0)
        vp.record_consent(p["id"])
        kinds = [e["kind"] for e in vp.read_events(p["id"])]
        assert kinds[0] == "create"
        assert "add_reference" in kinds and "consent" in kinds

    def test_limit_keeps_the_newest(self, iso_media):
        p = vp.create_profile("甲")
        for i in range(5):
            vp.log_event(p["id"], "tick", i=i)
        got = vp.read_events(p["id"], limit=3)
        assert [e["i"] for e in got] == [2, 3, 4]

    def test_broken_lines_are_skipped(self, iso_media):
        p = vp.create_profile("甲")
        vp.log_event(p["id"], "good")
        with open(vp.events_path(p["id"]), "a", encoding="utf-8") as f:
            f.write("{不是 json\n")
        vp.log_event(p["id"], "also_good")
        kinds = [e["kind"] for e in vp.read_events(p["id"])]
        assert kinds == ["create", "good", "also_good"]

    def test_logging_never_raises_on_a_missing_profile(self, iso_media):
        """日志写失败不该成为主流程的单点故障。"""
        vp.log_event("bogus-id", "x")

    def test_events_survive_reload(self, iso_media):
        p = vp.create_profile("甲")
        assert len(vp.read_events(p["id"])) == 1


# ---------------- 8. 引导语料 ----------------

class TestGuide:
    def test_shape(self, iso_media):
        g = vp.guide()
        assert len(g["script"]) == len(vp.RECORD_SCRIPT)
        assert g["conditions"] == list(vp.REPRO_CONDITIONS)
        assert g["consent"]["version"] == vp.CONSENT_VERSION
        assert g["consent"]["statement"]

    def test_every_duration_has_a_sentence_count(self, iso_media):
        for d in vp.guide()["durations"]:
            assert d["sentences"] >= 1
            assert d["tag"] == vp._dur_tag(d["duration_s"])

    def test_over_capacity_durations_are_flagged(self, iso_media):
        """★ 脚本容量约 91s。要 300s 档却不扩脚本 → 录不满，必须提前标出来。"""
        g = vp.guide()
        covered = {d["duration_s"]: d["covered"] for d in g["durations"]}
        assert covered[3.0] and covered[10.0]
        assert all(isinstance(v, bool) for v in covered.values())

    def test_repro_texts_cover_every_condition(self, iso_media):
        for cond in vp.REPRO_CONDITIONS:
            assert vp.REPRO_TEXTS.get(cond)

    def test_same_text_is_in_the_script_and_cross_is_not(self, iso_media):
        """★ 四类条件的语义：same 必须念过，cross 必须没念过。
        混了 RQ2 就废了。"""
        for t in vp.REPRO_TEXTS["same"]:
            assert t["text"] in vp.RECORD_SCRIPT
        for t in vp.REPRO_TEXTS["cross"]:
            assert t["text"] not in vp.RECORD_SCRIPT

    def test_guide_is_computed_from_the_backend_not_hardcoded(self, iso_media):
        """引导由后端现算 —— 改语料，引导跟着变（前端不手抄一份）。"""
        assert vp.guide()["script"] == list(vp.RECORD_SCRIPT)


# ---------------- 9. 复现桥（注册为音色库音色） ----------------

class TestReproduce:
    """「去复现」桥：档案 → voicebank 音色（`register_as_voice`）。

    ★ 守的还是那条主线：**同意先于克隆**。注册成音色 = 交给克隆链路，
    没有同意这一步必须失败 —— 写反了就是一个能克隆任何人的工具。
    """

    def _ready_profile(self, iso_media, monkeypatch, label="王老师"):
        _stub_convert(monkeypatch)
        p = vp.create_profile(label)
        vp.record_consent(p["id"], scope="self", statement="x", operator="t")
        vp.add_reference(p["id"], _wav(iso_media / "ref.wav", 1.0), 10.0)
        return p

    def test_without_consent_it_must_fail(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("没同意的人")
        vp.add_reference(p["id"], _wav(iso_media / "a.wav"), 3.0)
        rep = vp.register_as_voice(p["id"])
        assert rep.get("error") and rep["error_stage"] == "同意门禁"
        # 一个字节的克隆产物都不能有
        assert not (iso_media / "voicebank").exists()

    def test_without_reference_it_must_fail(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        p = vp.create_profile("只有同意")
        vp.record_consent(p["id"], scope="self", statement="x")
        rep = vp.register_as_voice(p["id"])
        assert rep.get("error") and rep["error_stage"] == "参考音"

    def test_registers_voicebank_voice_and_selects_it(self, iso_media, monkeypatch):
        p = self._ready_profile(iso_media, monkeypatch)
        rep = vp.register_as_voice(p["id"])
        assert rep.get("error") is None, rep
        vb = iso_media / "voicebank" / rep["voice_id"]
        assert (vb / "reference.wav").exists()
        # 音色 id 满足 voicebank 白名单（它会被 common.voice_ref 按形状校验）
        assert re.match(r"^[A-Za-z0-9_-]+$", rep["voice_id"])
        # 文字稿非空 —— ref_text 有内容才走 ICL 语气克隆，空了静默回退纯声纹
        assert (vb / "ref_text.txt").read_text(encoding="utf-8")
        # meta 带档案回链：音色库里能查到它出自哪份档案
        meta = json.loads((vb / "meta.json").read_text(encoding="utf-8"))
        assert meta["preserve_profile"] == p["id"]
        assert meta["source"] == "preserve"
        assert meta["display_name"] == "王老师"
        # 注册即选中 —— 前端跳输字页直接可用
        sel = json.loads(
            (iso_media / "voicebank" / "selected_voice.json").read_text(encoding="utf-8"))
        assert sel["voice_id"] == rep["voice_id"]
        # 操作日志记了这一笔（可审计）
        assert any(e["kind"] == "reproduce" for e in vp.read_events(p["id"]))

    def test_uses_longest_tier(self, iso_media, monkeypatch):
        p = self._ready_profile(iso_media, monkeypatch)
        vp.add_reference(p["id"], _wav(iso_media / "long.wav", 2.0), 30.0)
        rep = vp.register_as_voice(p["id"])
        assert rep.get("error") is None, rep
        assert rep["ref"] == "30s.wav"

    def test_rerun_refreshes_after_rerecording(self, iso_media, monkeypatch):
        """幂等：重录后再次注册 = 用新的最长档刷新 reference（档案是唯一真相源）。"""
        p = self._ready_profile(iso_media, monkeypatch)
        assert vp.register_as_voice(p["id"]).get("error") is None
        vb_ref = (iso_media / "voicebank" / (vp.VOICE_ID_PREFIX + p["id"])
                  / "reference.wav")
        first = vb_ref.read_bytes()
        vp.add_reference(p["id"], _wav(iso_media / "new.wav", 2.0), 30.0)
        rep2 = vp.register_as_voice(p["id"])
        assert rep2.get("error") is None and rep2["ref"] == "30s.wav"
        assert vb_ref.read_bytes() != first

    def test_ref_text_is_the_script_prefix_for_the_tier(self, iso_media):
        """ref_text 不是猜的：10s 档 = 脚本按估算语速截到 10s 的前缀。"""
        text = vp._ref_text_for(10.0)
        full = "".join(vp.RECORD_SCRIPT)
        assert text and full.startswith(text) and len(text) < len(full)
        # 循环是「先加句再判够」，所以字数必然达到预算（整句粒度，只会多不会少）
        assert vp.char_count(text) >= 10.0 * vp.SCRIPT_CHARS_PER_SEC

    def test_api_reproduce_gate_and_roundtrip(self, client, iso_media, monkeypatch):
        """HTTP 层：没同意 400 且点名同意门禁；齐备 200；不存在 404。"""
        _stub_convert(monkeypatch)
        pid = client.post(
            "/api/preserve/profiles", json={"label": "刘老师"}).json()["profile"]["id"]
        r0 = client.post(f"/api/preserve/profiles/{pid}/reproduce")
        assert r0.status_code == 400
        assert "知情同意" in r0.json()["detail"]
        vp.record_consent(pid, scope="self", statement="x")
        vp.add_reference(pid, _wav(iso_media / "r.wav"), 10.0)
        r1 = client.post(f"/api/preserve/profiles/{pid}/reproduce")
        assert r1.status_code == 200
        body = r1.json()
        assert body["ok"] and body["voice_id"] == vp.VOICE_ID_PREFIX + pid
        # 档案不存在（id 形状合法）→ 404，不是 500
        fake = "p" + "1" * 10 + "aaaaaa"
        assert client.post(f"/api/preserve/profiles/{fake}/reproduce").status_code == 404


# ---------------- 10. HTTP 层 ----------------

class TestApi:
    def test_guide(self, client):
        r = client.get("/api/preserve/guide")
        assert r.status_code == 200
        assert r.json()["script"] == list(vp.RECORD_SCRIPT)

    def test_create_list_get_delete_roundtrip(self, client):
        r = client.post("/api/preserve/profiles", json={"label": "李老师"})
        assert r.status_code == 200
        pid = r.json()["profile"]["id"]

        assert client.get("/api/preserve/profiles").json()["profiles"][0]["id"] == pid
        assert client.get(f"/api/preserve/profiles/{pid}").json()["profile"]["label"] == "李老师"
        assert client.delete(f"/api/preserve/profiles/{pid}").json()["deleted"] == pid
        assert client.get(f"/api/preserve/profiles/{pid}").status_code == 404

    def test_empty_label_is_400(self, client):
        assert client.post("/api/preserve/profiles", json={"label": "  "}).status_code == 400

    def test_bad_id_is_400_not_404(self, client):
        """非法 id 是**参数错**（400），不是"不存在"（404）——
        混在一起会让排查的人以为档案丢了。"""
        assert client.get("/api/preserve/profiles/abc").status_code == 400
        assert client.get("/api/preserve/profiles/12345").status_code == 400

    def test_traversal_is_rejected_before_reaching_the_handler(self, client):
        """`..%2Fetc` 解码后带 `/`，**路由层**就把它切成两段了 → 404。

        这比 400 更好：请求根本没进到业务代码。这里显式钉住它，
        免得哪天路由改成 `{pid:path}` 把这条防线悄悄拆掉
        （`docs/犯错指南.md` 速查表第 77 条：`{sample_id:path}` 会先吃掉后面的路由）。
        """
        assert client.get("/api/preserve/profiles/..%2Fetc").status_code == 404

    def test_missing_profile_is_404(self, client):
        pid = vp.new_profile_id()
        assert client.get(f"/api/preserve/profiles/{pid}").status_code == 404

    def test_consent_endpoint(self, client):
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        r = client.post(f"/api/preserve/profiles/{pid}/consent",
                        json={"scope": "self", "statement": "确认", "operator": "本人"})
        assert r.status_code == 200 and r.json()["consent"]["operator"] == "本人"
        assert client.post(f"/api/preserve/profiles/{pid}/consent",
                           json={"scope": "everyone"}).status_code == 400

    def test_gate_endpoint_is_post_and_reports_verdict(self, client):
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        r = client.post(f"/api/preserve/profiles/{pid}/gate")
        assert r.status_code == 200
        assert r.json()["verdict"] == "not_ready"

    def test_reference_endpoint_404_when_none(self, client):
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        assert client.get(f"/api/preserve/profiles/{pid}/reference").status_code == 404

    def test_reference_endpoint_returns_playable_url(self, client, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        vp.add_reference(pid, _wav(iso_media / "a.wav"), 10.0)
        body = client.get(f"/api/preserve/profiles/{pid}/reference").json()
        assert body["file"] == "10s.wav"
        assert body["url"].startswith("/api/media/preserve/")

    def test_events_endpoint(self, client):
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        kinds = [e["kind"] for e in
                 client.get(f"/api/preserve/profiles/{pid}/events").json()["events"]]
        assert "create" in kinds

    def test_upload_reference_multipart(self, client, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        wav = _wav(iso_media / "a.wav", 1.0)
        with open(wav, "rb") as f:
            r = client.post(f"/api/preserve/profiles/{pid}/references",
                            files={"file": ("a.wav", f, "audio/wav")},
                            data={"duration_s": "10"})
        assert r.status_code == 200, r.text
        assert r.json()["reference"]["tag"] == "10s"

    def test_upload_rejects_empty_file(self, client, iso_media):
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        r = client.post(f"/api/preserve/profiles/{pid}/references",
                        files={"file": ("a.wav", b"", "audio/wav")},
                        data={"duration_s": "10"})
        assert r.status_code == 400

    def test_upload_truth_rejects_unknown_condition(self, client, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        wav = _wav(iso_media / "t.wav")
        with open(wav, "rb") as f:
            r = client.post(f"/api/preserve/profiles/{pid}/truth",
                            files={"file": ("t.wav", f, "audio/wav")},
                            data={"condition": "nope"})
        assert r.status_code == 400

    def test_summary_endpoint(self, client):
        client.post("/api/preserve/profiles", json={"label": "甲"})
        body = client.get("/api/preserve/summary").json()
        assert body["count"] == 1 and body["with_consent"] == 0

    def test_summary_reports_a_nonempty_dir(self, client):
        """★ `dir` 必须指到真档案库 —— 它会在界面上显示"你的录音存在哪"，
        指向一个错路径比不显示更糟。"""
        body = client.get("/api/preserve/summary").json()
        assert body["dir"].endswith(vp.STORE_NAME)

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
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import voicepreserve as vp  # noqa: E402
import voicepreserve_api as vp_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------- 夹具 ----------------

@pytest.fixture(autouse=True)
def iso_media(tmp_path, monkeypatch):
    """把档案库指到 tmp —— 否则会往用户真实的 `media/preserve/` 里写录音。

    ★ 能这么打是因为 `store_dir()` 是**每次调用时**读 `cfg.MEDIA_DIR`，
    不是导入时早绑定（`docs/犯错档案-工程.md` §8.36 那类坑在这里不适用）。
    """
    monkeypatch.setattr(vp.cfg, "MEDIA_DIR", tmp_path / "media")
    return tmp_path / "media"


@pytest.fixture(autouse=True)
def iso_outputs(tmp_path, monkeypatch):
    """把 `OUTPUTS_DIR` 也指到 tmp（导出桥会往 `outputs/voicebank/` 写）。

    与 `iso_media` 同一理由：导出是**写文件**的操作，指错根就会污染用户目录。
    autouse 是为了让"新建的导出用例忘了隔离"这件事不可能发生。
    """
    monkeypatch.setattr(vp.cfg, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    return tmp_path / "outputs"


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


def _profile_with_all_refs(iso_media, monkeypatch, label: str = "王老师") -> str:
    """建一个四档参考音齐备的档案，返回 pid。

    ★ 刻意做成**模块级函数而不是类里的方法**：pytest 会把类体内任何方法
      都当候选夹具收集，一个下划线开头的方法会被报
      `Fixture "..." called directly`（实测踩到）。
    """
    _stub_convert(monkeypatch)
    pid = vp.create_profile(label)["id"]
    for d in vp.REF_DURATIONS:
        vp.add_reference(pid, _wav(iso_media / f"r{d}.wav", seconds=1.0), d)
    return pid


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


# ---------------- 4b. 一次录音 → 前缀切四档（★ 评测协议的关键前提） ----------------

class TestBatchReferenceSlicing:
    """★ 守的是**评测协议能不能落地**这件事。

    `docs/嗓音保存评测协议.md` §3.1 把四档定义为「**同一段录音的前缀截取**」，
    控制变量含"同一条 take"。若让用户录四次，四次是不同的 take ——
    自变量（时长）就被"这次录得好不好"混淆，RQ1 归因不成立。
    `add_reference_batch` 就是补这一步。
    """

    def test_one_recording_yields_all_four_tiers(self, iso_media, monkeypatch):
        """★ 核心用例：录一条 → refs 下恰好四个档，且每档实际时长贴合标称值。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        # 录 65s（最高档 60s），一次切完
        res = vp.add_reference_batch(p["id"], _wav(iso_media / "rec.wav", 65.0))
        assert res.get("error") is None, res
        assert res["tags"] == ["3s", "10s", "30s", "60s"]
        refs = vp.get_profile(p["id"])["refs"]
        assert len(refs) == 4
        for r in refs:
            # 切出来的片段实际时长应贴合标称档（前缀截取，误差来自 wav 帧对齐）
            assert r["actual_s"] == pytest.approx(r["duration_s"], abs=0.05)

    def test_insufficient_length_is_a_structured_error(self, iso_media, monkeypatch):
        """★ 录音不足最高档必须**报错**，不能只切出前三档。

        静默产生"缺一档的矩阵"最危险：`analyze` 会报"数据不足以判定"，
        而用户以为自己录完了 —— 错因指向统计，实际在采集。
        """
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        res = vp.add_reference_batch(p["id"], _wav(iso_media / "short.wav", 20.0))
        assert res.get("error") and res["error_stage"] == "时长不足"
        assert res["total_s"] == pytest.approx(20.0, abs=0.1)
        assert res["needed_s"] == 60.0
        # 且**不能**留下半套数据
        assert vp.get_profile(p["id"])["refs"] == []

    def test_batch_replaces_same_tag_not_appends(self, iso_media, monkeypatch):
        """重录一次 = 四档全部替换，不产生 8 条 refs。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference_batch(p["id"], _wav(iso_media / "r1.wav", 65.0))
        vp.add_reference_batch(p["id"], _wav(iso_media / "r2.wav", 70.0))
        refs = vp.get_profile(p["id"])["refs"]
        assert len(refs) == 4
        assert all(r.get("from_batch") is True for r in refs)

    def test_missing_profile_and_empty_source_are_reported(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        # 非法 id 由 `profile_dir` 抛 ValueError（既有约定，见 TestProfileIdSafety），
        # 这里测的是「**合法形状但不存在**」的 id → 走结构化错误分支。
        assert vp.add_reference_batch("p0000000000abcdef",
                                     _wav(iso_media / "a.wav", 65.0)).get("error")
        p = vp.create_profile("甲")
        empty = iso_media / "empty.wav"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        res = vp.add_reference_batch(p["id"], empty)
        assert res.get("error") and res["error_stage"] == "参数"

    def test_batch_records_an_audit_event(self, iso_media, monkeypatch):
        """一次切四档要留一条审计日志 —— 声音档案是"谁在什么时候动过"的追溯基础。"""
        _stub_convert(monkeypatch)
        p = vp.create_profile("甲")
        vp.add_reference_batch(p["id"], _wav(iso_media / "rec.wav", 65.0))
        kinds = [e["kind"] for e in vp.read_events(p["id"])]
        assert "add_reference_batch" in kinds


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


# ---------------- 9.5 评测桥（档案 → 实验目录） ----------------

class TestExportExperiment:
    """导出桥守的是「产品与研究两套目录约定之间的转换不失真」。

    ★ 为什么这个转换值得单测：它是**纯拷贝**，而纯拷贝的失败是安静的 ——
      拷漏一档 → 实验矩阵缺格 → `analyze` 报"数据不足以判定"（错因指向统计）；
      拷错档（把 10s 的文件命名成 30s）→ 自变量标签是错的，而**没有任何一处会报错**。
      所以每条断言都对着"档位与文件名是否真的一致"。
    """

    def test_exports_every_tier_with_matching_filenames(self, iso_media, iso_outputs,
                                                        monkeypatch):
        """四档都导出，且文件名 == 档案里的档位（拷错档是安静的致命错）。"""
        pid = _profile_with_all_refs(iso_media, monkeypatch)
        res = vp.export_experiment(pid)
        assert res.get("error") is None, res
        assert res["exp"] == vp.EXPORT_EXP_PREFIX + pid
        base = Path(res["root"])
        got = sorted(p.name for p in (base / "refs" / res["speaker"]).glob("*.wav"))
        assert got == ["10s.wav", "30s.wav", "3s.wav", "60s.wav"]
        # 内容必须真拷过去，不是建了空文件
        for r in res["refs"]:
            p = base / "refs" / res["speaker"] / f"{r['tag']}.wav"
            assert p.exists() and p.stat().st_size > 0
            assert r["bytes"] == p.stat().st_size

    def test_missing_tiers_are_reported_not_faked(self, iso_media, iso_outputs, monkeypatch):
        """只录了一档 → 导出如实说缺三档，**不补空文件充数**。

        ★ 补空文件的危害：矩阵"看起来是满的"，`measure` 对空文件报指标算不出，
        最后结论变成"这个档位质量差" —— 而真相是"这一档根本没录"。
        """
        _stub_convert(monkeypatch)
        pid = vp.create_profile("只录一档")["id"]
        vp.add_reference(pid, _wav(iso_media / "r.wav"), 10.0)
        res = vp.export_experiment(pid)
        assert res.get("error") is None, res
        assert res["n_refs"] == 1
        # 缺 3 档参考音 + 4 条 truth（4 + 3 = 7）—— **每一件都点名**，
        # 不是只说"缺了点东西"
        assert len(res["missing"]) == 3 + len(vp.REPRO_CONDITIONS)
        assert any("3s" in m for m in res["missing"])
        assert any("emotion" in m for m in res["missing"])
        # 缺的档不许有文件
        base = Path(res["root"]) / "refs" / res["speaker"]
        assert not (base / "3s.wav").exists()

    def test_truth_files_are_exported_under_their_condition(self, iso_media,
                                                            iso_outputs, monkeypatch):
        _stub_convert(monkeypatch)
        pid = vp.create_profile("有真值")["id"]
        vp.add_reference(pid, _wav(iso_media / "r.wav"), 10.0)
        for cond in ("same", "long"):
            vp.add_truth(pid, cond, _wav(iso_media / f"t_{cond}.wav"))
        res = vp.export_experiment(pid)
        base = Path(res["root"]) / "truth" / res["speaker"]
        assert sorted(p.name for p in base.glob("*.wav")) == ["long.wav", "same.wav"]
        assert res["n_truth"] == 2

    def test_export_is_idempotent(self, iso_media, iso_outputs, monkeypatch):
        """同一档案导出两次 → 同一目录、文件数不翻倍（幂等）。

        ★ 不幂等的后果：第二次导出会留下上一次的残留（比如删过一档之后，
        旧文件还在），于是"目录里有什么"与"档案里有什么"对不上。
        """
        pid = _profile_with_all_refs(iso_media, monkeypatch)
        r1 = vp.export_experiment(pid)
        r2 = vp.export_experiment(pid)
        assert r1["root"] == r2["root"]
        base = Path(r1["root"]) / "refs" / r1["speaker"]
        assert len(list(base.glob("*.wav"))) == 4

    def test_custom_exp_name_and_illegal_name_rejected(self, iso_media, iso_outputs,
                                                       monkeypatch):
        pid = _profile_with_all_refs(iso_media, monkeypatch)
        ok = vp.export_experiment(pid, "my_study_01")
        assert ok.get("error") is None and ok["exp"] == "my_study_01"
        # 实验名会拼进路径 → 形状先堵死（与档案 id 同一个理由）
        bad = vp.export_experiment(pid, "../escape")
        assert bad["error"] and bad["error_stage"] == "参数"

    def test_readme_records_what_is_missing(self, iso_media, iso_outputs, monkeypatch):
        """交接说明必须把缺件写进去 —— `missing` 只活在这次响应里，
        下一个打开目录的人（或 agent）看不到它。"""
        _stub_convert(monkeypatch)
        pid = vp.create_profile("缺件")["id"]
        vp.add_reference(pid, _wav(iso_media / "r.wav"), 3.0)
        res = vp.export_experiment(pid)
        text = (Path(res["root"]) / "README-导出.md").read_text(encoding="utf-8")
        assert "缺件" in text and "10s" in text
        assert "measure --exp" in text          # 下一步命令要能直接照抄

    def test_audit_event_is_logged(self, iso_media, iso_outputs, monkeypatch):
        pid = _profile_with_all_refs(iso_media, monkeypatch)
        vp.export_experiment(pid)
        kinds = [e["kind"] for e in vp.read_events(pid, limit=0)]
        assert "export_experiment" in kinds


class TestExportApi:
    def test_export_requires_consent(self, client, iso_media, iso_outputs, monkeypatch):
        """★ 同意门禁：数据离开档案库之前就必须有授权。

        与 `reproduce` 同口径，但比它更靠前 —— 复现只是在档案里读一段参考音，
        导出是把它**复制到档案库之外**，约束的落点必须在这之前。
        """
        _stub_convert(monkeypatch)
        pid = client.post(
            "/api/preserve/profiles", json={"label": "未同意"}).json()["profile"]["id"]
        vp.add_reference(pid, _wav(iso_media / "r.wav"), 10.0)
        r = client.post(f"/api/preserve/profiles/{pid}/export")
        assert r.status_code == 400
        assert "知情同意" in r.json()["detail"]

    def test_export_roundtrip(self, client, iso_media, iso_outputs, monkeypatch):
        _stub_convert(monkeypatch)
        pid = client.post(
            "/api/preserve/profiles", json={"label": "已同意"}).json()["profile"]["id"]
        vp.record_consent(pid, scope="self", statement="x")
        for d in vp.REF_DURATIONS:
            vp.add_reference(pid, _wav(iso_media / f"r{d}.wav"), d)
        r = client.post(f"/api/preserve/profiles/{pid}/export", json={"exp": ""})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["n_refs"] == 4
        assert body["exp"] == vp.EXPORT_EXP_PREFIX + pid
        assert "measure --exp" in body["measure_hint"]

    def test_export_missing_profile_is_404(self, client, iso_outputs):
        fake = "p" + "1" * 10 + "aaaaaa"
        assert client.post(f"/api/preserve/profiles/{fake}/export").status_code == 404


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

    def test_upload_batch_slices_all_tiers_in_one_request(self, client, iso_media, monkeypatch):
        """★ 录一条 → 一次请求切四档（评测协议要求的采集形态）。"""
        _stub_convert(monkeypatch)
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        wav = _wav(iso_media / "rec.wav", 65.0)
        with open(wav, "rb") as f:
            r = client.post(f"/api/preserve/profiles/{pid}/references/batch",
                            files={"file": ("rec.wav", f, "audio/wav")})
        assert r.status_code == 200, r.text
        assert r.json()["tags"] == ["3s", "10s", "30s", "60s"]

    def test_upload_batch_short_recording_is_400(self, client, iso_media, monkeypatch):
        """录音不足最高档 → 400，且明确说差多少（不是 500、不是静默切一半）。"""
        _stub_convert(monkeypatch)
        pid = client.post("/api/preserve/profiles", json={"label": "甲"}).json()["profile"]["id"]
        wav = _wav(iso_media / "short.wav", 20.0)
        with open(wav, "rb") as f:
            r = client.post(f"/api/preserve/profiles/{pid}/references/batch",
                            files={"file": ("short.wav", f, "audio/wav")})
        assert r.status_code == 400
        assert "60s" in r.json()["detail"]

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


# ---------------- 11. 纵向随访 ----------------

_LAMPS_FIXTURE = {
    "lamps": [
        {"label": "相似度", "lamp": None, "detail": "未测"},
        {"label": "自然度", "lamp": "green", "detail": "HNR 够高"},
        {"label": "发声负荷", "lamp": "yellow", "detail": "H1-H2 偏高"},
    ],
    "verdict": "部分可用",
    "tested_count": 2,
}


def _stub_metrics(monkeypatch, ok: bool = True):
    """把指标计算打桩（真跑 parselmouth 是 1s/次的慢调用，逻辑用例不需要它）。"""
    def _fake(wav):
        if ok:
            return {
                "metrics": {"hnr": 18.0, "h1_h2": 4.0, "spectral_tilt": -3.0,
                            "jitter_local": 0.01, "shimmer_local": 0.05,
                            "f0_median": 160.0, "voiced_ratio": 0.8,
                            "duration_s": 12.0},
                "lamps": json.loads(json.dumps(_LAMPS_FIXTURE)),
                "error": "",
            }
        return {"metrics": None, "lamps": None, "error": "读不出浊音段"}

    monkeypatch.setattr(vp, "_metrics_for", _fake)


class TestFollowups:
    """纵向随访（声音档案从「一次保存」到「长期跟随」）。

    ★ 守四条容易写错的语义：
      1. **追加不替换** —— 随访的意义就在「不同时间的多次」；「同档替换」
         只属于参考音。写反了趋势图只剩最后一个点，功能等于白做。
      2. **指标算不出来不是失败** —— 录音照样入库、metrics 记 None（与 gate()
         的 partial 同哲学）；编一个数字顶上去比留白危险得多。
      3. **相似度是加分项** —— worker 没跑 / 没基线 → None，绝不阻塞主流程。
      4. **措辞红线** —— 趋势是「发声状态趋势参考」，不是医疗诊断。
    """

    def test_records_point_appends_not_replaces(self, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("王老师")["id"]
        r1 = vp.add_followup(pid, _wav(iso_media / "f1.wav"))
        r2 = vp.add_followup(pid, _wav(iso_media / "f2.wav"))
        assert r1.get("error") is None and r2.get("error") is None, (r1, r2)
        pts = vp.read_followups(pid)
        assert len(pts) == 2, "随访必须追加 —— 参考音的「同档替换」语义不能搬过来"
        assert pts[0]["file"] != pts[1]["file"]
        assert pts[0]["metrics"]["hnr"] == 18.0
        assert pts[0]["lamps"]["lamps"][1]["lamp"] == "green"
        assert pts[0]["metrics_error"] == ""
        # wav 真的落盘（不是只记了一行索引）
        assert (vp.followups_dir(pid) / pts[0]["file"]).exists()
        # 审计日志记了这一笔
        assert any(e["kind"] == "followup" for e in vp.read_events(pid))

    def test_points_are_chronological_even_if_written_out_of_order(self, iso_media):
        """时序写入：读出来必须按 at_ms 升序 —— 哪怕写入顺序乱了（崩溃恢复 /
        手工补录都会造成乱序）。趋势图的横轴靠它。"""
        pid = vp.create_profile("甲")["id"]
        idx = vp._followups_index(pid)
        idx.parent.mkdir(parents=True, exist_ok=True)

        def rec(ms, tag):
            return {"at": "t", "at_ms": ms, "file": f"{tag}.wav"}

        with open(idx, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec(3000, "late")) + "\n")
            f.write(json.dumps(rec(1000, "early")) + "\n")
            f.write(json.dumps(rec(2000, "mid")) + "\n")
        assert [p["file"] for p in vp.read_followups(pid)] == \
            ["early.wav", "mid.wav", "late.wav"]

    def test_broken_lines_are_skipped(self, iso_media):
        """一行脏数据毁掉整条曲线 = 趋势图整个消失 —— 不行（同 read_events 先例）。"""
        pid = vp.create_profile("甲")["id"]
        idx = vp._followups_index(pid)
        idx.parent.mkdir(parents=True, exist_ok=True)
        with open(idx, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": "t", "at_ms": 1, "file": "a.wav"}) + "\n")
            f.write("不是 json\n")
            f.write(json.dumps({"at": "t", "at_ms": 2, "file": "b.wav"}) + "\n")
        assert [p["file"] for p in vp.read_followups(pid)] == ["a.wav", "b.wav"]

    def test_single_point_trend_is_valid(self, iso_media, monkeypatch):
        """单点基线：只有一次随访也返回合法结构（前端画点不画线）。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("甲")["id"]
        vp.add_followup(pid, _wav(iso_media / "f.wav"))
        t = vp.trend(pid)
        assert t["n"] == 1 and len(t["points"]) == 1

    def test_empty_trend_is_a_valid_structure_not_a_crash(self, iso_media):
        t = vp.trend(vp.create_profile("甲")["id"])
        assert t["n"] == 0 and t["points"] == [] and t["hint"]

    def test_metrics_failure_degrades_not_dies(self, iso_media, monkeypatch):
        """★ 指标算不出来 ≠ 随访失败：录音入库、metrics 记 None 并留原因 ——
        与 gate() 的 partial 同一哲学（如实降级，不编数字）。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch, ok=False)
        pid = vp.create_profile("甲")["id"]
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"), note="感冒了")
        assert rec.get("error") is None, rec
        assert rec["metrics"] is None and rec["lamps"] is None
        assert "浊音" in rec["metrics_error"]
        assert rec["note"] == "感冒了"
        assert vp.trend(pid)["n"] == 1, "数据点必须还在 —— 趋势曲线不该断"

    def test_missing_profile_and_empty_source_are_structured_errors(self, iso_media,
                                                                    monkeypatch):
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        assert vp.add_followup("p" + "1" * 10 + "aaaaaa",
                               _wav(iso_media / "f.wav")).get("error")
        pid = vp.create_profile("甲")["id"]
        assert vp.add_followup(pid, iso_media / "nope.wav").get("error") == "录音为空"

    def test_no_baseline_emb_sim_is_none(self, iso_media, monkeypatch):
        """档案还没有参考音 = 没有基线可比 → 相似度 None，不是报错。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("甲")["id"]
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"))
        assert rec.get("error") is None
        assert rec["emb_sim"] is None

    def test_worker_down_degrades_to_none_without_calling_it(self, iso_media,
                                                             monkeypatch):
        """★ worker 没跑 → 相似度 None，且**绝不把 worker 拉起来**
        （post 一旦被调用就炸 —— 加分项不许阻塞主流程）。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("甲")["id"]
        vp.add_reference(pid, _wav(iso_media / "ref.wav"), 10.0)

        def _must_not_call(*a, **k):
            raise AssertionError("worker 不该被调用")

        fake = types.SimpleNamespace(worker_alive=lambda: False, post=_must_not_call)
        monkeypatch.setitem(sys.modules, "qwen3_tts", fake)
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"))
        assert rec.get("error") is None
        assert rec["emb_sim"] is None

    def test_emb_sim_computed_when_worker_up_and_lamps_gain_similarity(self,
                                                                       iso_media,
                                                                       monkeypatch):
        """worker 在跑 + 有基线 → 算出余弦，且三灯重算后「相似度」灯有值
        （阈值函数仍是 voice_report 那一份，不存在第二套标准）。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("甲")["id"]
        vp.add_reference(pid, _wav(iso_media / "ref.wav"), 10.0)

        calls = {"n": 0}

        def fake_post(path, payload, timeout=0):
            calls["n"] += 1
            emb = [1.0, 0.0, 0.0] if calls["n"] == 1 else [0.8, 0.6, 0.0]
            return json.dumps({"emb": emb}).encode()

        fake = types.SimpleNamespace(worker_alive=lambda: True, post=fake_post)
        monkeypatch.setitem(sys.modules, "qwen3_tts", fake)
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"))
        assert rec["emb_sim"] == pytest.approx(0.8, abs=1e-3)
        sim_lamp = next(l for l in rec["lamps"]["lamps"] if "相似" in l["label"])
        assert sim_lamp["lamp"] is not None

    def test_no_medical_wording_in_trend_payload(self, iso_media, monkeypatch):
        """★ 措辞红线（后端侧）：trend 吐给界面的文案不得出现
        「诊断 / 健康监测 / 疗效」。守卫咬的是真实下发文案（hint 必须存在，
        不许空转）；末尾的变异验证确认这套检查真的会咬人。"""
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = vp.create_profile("甲")["id"]
        vp.add_followup(pid, _wav(iso_media / "f.wav"))
        t = vp.trend(pid)
        assert t["hint"], "hint 必须存在 —— 守卫的对象是真实文案，不是空集"
        blob = json.dumps(t, ensure_ascii=False)
        for bad in ("诊断", "健康监测", "疗效"):
            assert bad not in blob, f"趋势文案出现了违禁词：{bad}"
        # 变异验证：注入一个真违规，同一套检查必须能抓到
        bad_blob = json.dumps({**t, "hint": "本功能可用于嗓音疾病诊断"},
                              ensure_ascii=False)
        assert any(bad in bad_blob for bad in ("诊断", "健康监测", "疗效"))

    # ---- HTTP 层 ----

    def test_api_followup_and_trend_roundtrip(self, client, iso_media, monkeypatch):
        _stub_convert(monkeypatch)
        _stub_metrics(monkeypatch)
        pid = client.post("/api/preserve/profiles",
                          json={"label": "甲"}).json()["profile"]["id"]
        wav = _wav(iso_media / "f.wav")
        with open(wav, "rb") as f:
            r = client.post(f"/api/preserve/profiles/{pid}/followup",
                            files={"file": ("f.wav", f, "audio/wav")},
                            data={"note": "开学第一周"})
        assert r.status_code == 200, r.text
        body = r.json()["followup"]
        assert body["note"] == "开学第一周"
        t = client.get(f"/api/preserve/profiles/{pid}/trend").json()
        assert t["n"] == 1
        assert t["points"][0]["url"].startswith("/api/media/preserve/")
        # 档案不存在（id 形状合法）→ 404
        fake = "p" + "1" * 10 + "aaaaaa"
        assert client.get(f"/api/preserve/profiles/{fake}/trend").status_code == 404

    def test_api_followup_bad_id_is_400(self, client):
        assert client.get("/api/preserve/profiles/abc/trend").status_code == 400

    def test_api_followup_empty_file_is_400(self, client):
        pid = client.post("/api/preserve/profiles",
                          json={"label": "甲"}).json()["profile"]["id"]
        r = client.post(f"/api/preserve/profiles/{pid}/followup",
                        files={"file": ("a.wav", b"", "audio/wav")})
        assert r.status_code == 400

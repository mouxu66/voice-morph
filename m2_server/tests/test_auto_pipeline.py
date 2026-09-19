"""tools/auto_pipeline.py 单测 —— 只测纯编排逻辑，不碰真服务/GPU。

无人值守工具的价值在「判得准 + 失败就停」：每个阶段的断言只要有一条放松，
它就会把「跑完了但结果是空的」报成成功。这里用注入的假 client 把每个
断言路径锁死；真链路交给 tools/auto_pipeline.py 对着 8000 服务自己跑。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "auto_pipeline.py"

_M2 = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("auto_pipeline", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auto_pipeline"] = mod
    spec.loader.exec_module(mod)
    return mod


ap = _load()


# ---------------- 假 client ----------------


class FakeClient:
    """按 path 精确匹配返回；未登记的 path 抛 AssertionError 便于发现漏配。"""

    def __init__(self):
        self.gets: dict[str, object] = {}
        self.posts: dict[str, object] = {}
        self.forms: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, object]] = []

    def get(self, path):
        if path not in self.gets:
            raise AssertionError(f"未预期的 GET {path}")
        return self.gets[path]

    def post(self, path, payload=None):
        self.post_calls.append((path, payload))
        if path not in self.posts:
            raise AssertionError(f"未预期的 POST {path}")
        return self.posts[path]

    def post_form(self, path, fields):
        self.forms.append((path, fields))
        return {"ok": True, "picked": 5, "duration_s": 31.0}


# ---------------- list_materials ----------------


def test_list_materials_all():
    c = FakeClient()
    c.gets["/raw_videos"] = {"videos": [{"name": "a.mp4"}, {"name": "b.wav"}]}
    assert ap.list_materials(c.get, None) == ["a.mp4", "b.wav"]


def test_list_materials_missing_file_fails():
    c = FakeClient()
    c.gets["/raw_videos"] = {"videos": [{"name": "a.mp4"}]}
    with pytest.raises(ap.PipelineError, match="素材不存在"):
        ap.list_materials(c.get, ["nope.mp4"])


def test_list_materials_empty_dir_fails_later():
    """空目录返回空清单——由 run 阶段的 _require 统一拦截。"""
    c = FakeClient()
    c.gets["/raw_videos"] = {"videos": []}
    assert ap.list_materials(c.get, None) == []


# ---------------- wait_pipeline ----------------


def _status_seq(statuses):
    it = iter(statuses)

    def poll():
        return next(it)

    return poll


def test_wait_pipeline_done_returns_state():
    poll = _status_seq(
        [
            {"status": "running", "percent": 10, "message": "提取音轨"},
            {"status": "running", "percent": 50, "message": "去除背景音乐"},
            {"status": "done", "clips": 73, "message": "流水线完成"},
        ]
    )
    st = ap.wait_pipeline(poll, deadline_s=60)
    assert st["clips"] == 73


def test_wait_pipeline_error_raises():
    poll = _status_seq([{"status": "error", "error": "demucs 挂了"}])
    with pytest.raises(ap.PipelineError, match="demucs"):
        ap.wait_pipeline(poll, deadline_s=60)


def test_wait_pipeline_cancelled_raises():
    poll = _status_seq([{"status": "cancelled"}])
    with pytest.raises(ap.PipelineError, match="流水线失败"):
        ap.wait_pipeline(poll, deadline_s=60)


def test_wait_pipeline_timeout(monkeypatch):
    # 让 sleep 立刻返回、monotonic 快进，模拟超时
    t = {"now": 0.0}
    monkeypatch.setattr(ap.time, "sleep", lambda s: None)
    monkeypatch.setattr(ap.time, "monotonic", lambda: t["now"])

    def fake_sleep(_):
        t["now"] += 999

    monkeypatch.setattr(ap.time, "sleep", fake_sleep)
    poll = _status_seq(
        [
            {"status": "running", "percent": 1, "message": "x"},
            {"status": "running", "percent": 2, "message": "x"},
            {"status": "running", "percent": 3, "message": "x"},
        ]
    )
    with pytest.raises(ap.PipelineError, match="超时"):
        ap.wait_pipeline(poll, deadline_s=60)


# ---------------- stage_pipeline ----------------


def test_stage_pipeline_zero_clips_fails():
    c = FakeClient()
    c.posts["/pipeline/run"] = {"ok": True, "started": True, "scope": "1 个素材"}
    c.gets["/pipeline/status"] = {"status": "done", "clips": 0, "message": "流水线完成"}
    with pytest.raises(ap.PipelineError, match="0 条切片"):
        ap.stage_pipeline(c.get, c.post, ["a.mp4"], timeout_s=60)


def test_stage_pipeline_happy():
    c = FakeClient()
    c.posts["/pipeline/run"] = {"ok": True, "started": True}
    c.gets["/pipeline/status"] = {"status": "done", "clips": 73, "message": "ok"}
    st = ap.stage_pipeline(c.get, c.post, ["a.mp4"], timeout_s=60)
    assert st["clips"] == 73
    # file 参数按 JSON body 传（pipeline/run 的接口约定）
    assert c.post_calls[0] == ("/pipeline/run", {"file": ["a.mp4"]})


# ---------------- stage_qc ----------------


def test_stage_qc_accumulates_grades():
    c = FakeClient()
    c.posts["/clips/qc?file=a.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 10, "B": 5, "C": 2, "D": 1},
    }
    c.posts["/clips/qc?file=b.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 0, "B": 0, "C": 0, "D": 9},
    }
    grades = ap.stage_qc(c.post, ["a.mp4", "b.mp4"], with_spk=False)
    assert grades == {"A": 10, "B": 5, "C": 2, "D": 10}


def test_stage_qc_diarize_failure_degrades_not_abort():
    """说话人分离失败只降级（质检缺声纹维度），绝不中断无人值守流程。"""
    c = FakeClient()
    c.posts["/clips/qc?file=a.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 3, "B": 0, "C": 0, "D": 0},
    }

    def failing_diarize(path, payload=None):
        if "diarize" in path:
            raise RuntimeError("CAM++ 加载失败")
        return c.posts[path]

    grades = ap.stage_qc(failing_diarize, ["a.mp4"], with_spk=True)
    assert grades["A"] == 3


def test_stage_qc_zero_usable_is_reported_not_swallowed():
    """A/B=0 的等级分布要原样返回，由上层断言拦截——不许悄悄放行。"""
    c = FakeClient()
    c.posts["/clips/qc?file=a.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 0, "B": 0, "C": 0, "D": 12},
    }
    grades = ap.stage_qc(c.post, ["a.mp4"], with_spk=False)
    assert grades == {"A": 0, "B": 0, "C": 0, "D": 12}


# ---------------- stage_voicebank ----------------


def test_voicebank_form_fields():
    c = FakeClient()
    r = ap.stage_voicebank(c.post_form, "kangaroo", 60.0, enhance=True)
    assert r["picked"] == 5
    path, fields = c.forms[0]
    assert "voice_id=kangaroo" in path
    assert fields["auto"] == "1"
    assert fields["target_s"] == "60"
    assert fields["enhance"] == "1"


def test_voicebank_zero_picked_fails():
    class ZeroClient(FakeClient):
        def post_form(self, path, fields):
            return {"ok": True, "picked": 0, "duration_s": 0.0}

    with pytest.raises(ap.PipelineError, match="未选中任何切片"):
        ap.stage_voicebank(ZeroClient().post_form, "kangaroo", 30.0, False)


# ---------------- stage_tts_corpus ----------------


def test_tts_corpus_incomplete_fails():
    c = FakeClient()
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 13, "total": 20}
    with pytest.raises(ap.PipelineError, match="不完整"):
        ap.stage_tts_corpus(c.post, c.get, "kangaroo")


def test_tts_corpus_error_field_fails():
    c = FakeClient()
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 0, "total": 20, "error": "TTS 挂了"}
    with pytest.raises(ap.PipelineError, match="TTS 挂了"):
        ap.stage_tts_corpus(c.post, c.get, "kangaroo")


def test_tts_corpus_export_zero_fails():
    c = FakeClient()
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 20, "total": 20}
    c.posts["/rvc/dataset/export?voice_id=kangaroo"] = {"ok": True, "copied": 0}
    with pytest.raises(ap.PipelineError, match="导出失败"):
        ap.stage_tts_corpus(c.post, c.get, "kangaroo")


def test_tts_corpus_happy(monkeypatch):
    c = FakeClient()
    c.posts["/rct/dataset/generate"] = {"ok": True}  # 干扰项：路径必须精确匹配
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 20, "total": 20}
    c.posts["/rvc/dataset/export?voice_id=kangaroo"] = {
        "ok": True,
        "copied": 20,
        "dest": "D:/RVC/dataset_raw/rvc_dataset",
    }
    monkeypatch.setattr(ap.time, "sleep", lambda s: None)
    r = ap.stage_tts_corpus(c.post, c.get, "kangaroo")
    assert r["exported"] == 20


# ---------------- stage_train ----------------


def test_train_fails_on_nonzero_rc():
    c = FakeClient()
    c.posts["/ft/train?voice_id=kangaroo&epochs=12"] = {"ok": True}
    c.gets["/ft/train_status?voice_id=kangaroo"] = {"running": False, "rc": 3, "error": "CUDA OOM"}
    with pytest.raises(ap.PipelineError, match="rc=3"):
        ap.stage_train(c.get, c.post, "kangaroo", 12)


def test_train_ok_on_rc0():
    c = FakeClient()
    c.posts["/ft/train?voice_id=kangaroo&epochs=12"] = {"ok": True}
    c.gets["/ft/train_status?voice_id=kangaroo"] = {"running": False, "rc": 0}
    r = ap.stage_train(c.get, c.post, "kangaroo", 12)
    assert r["done"] is True


def test_train_not_started_fails():
    c = FakeClient()
    c.posts["/ft/train?voice_id=kangaroo&epochs=12"] = {"ok": False}
    with pytest.raises(ap.PipelineError, match="训练未启动"):
        ap.stage_train(c.get, c.post, "kangaroo", 12)


# ---------------- stage_export_real_clips ----------------


class FakeCfg:
    """替换 ap.cfg：MEDIA_DIR 指向 tmp_path，RVC_EXPORT_DIR 指向其子目录。"""

    def __init__(self, tmp_path: Path):
        self.MEDIA_DIR = tmp_path / "media"
        self.RVC_EXPORT_DIR = tmp_path / "rvc" / "dataset_raw" / "rvc_dataset"


def test_export_real_clips_copies_ab_only(tmp_path, monkeypatch):
    fake = FakeCfg(tmp_path)
    monkeypatch.setattr(ap, "cfg", fake)
    clips = tmp_path / "media" / "clips"
    clips.mkdir(parents=True)
    # A、D 两级 + 一个质检缺失（不导）+ 一个只有质检记录、磁盘上没有（跳过不炸）
    for stem in ("a_A", "a_D", "a_none"):
        (clips / f"{stem}.wav").write_bytes(b"RIFF")
    c = FakeClient()
    c.gets["/clips"] = {
        "clips": [
            {"name": "a_A", "qc": {"grade": "A", "score": 90}},
            {"name": "a_D", "qc": {"grade": "D", "score": 10}},
            {"name": "a_none", "qc": {}},
            {"name": "a_ghost", "qc": {"grade": "A", "score": 95}},
        ]
    }
    r = ap.stage_export_real_clips(c.get, "kangaroo")
    assert r["exported"] == 1
    dest = fake.RVC_EXPORT_DIR.parent / "kangaroo"
    assert sorted(p.name for p in dest.glob("*.wav")) == ["a_A.wav"]


def test_export_real_clips_no_usable_fails(tmp_path, monkeypatch):
    fake = FakeCfg(tmp_path)
    monkeypatch.setattr(ap, "cfg", fake)
    c = FakeClient()
    c.gets["/clips"] = {"clips": [{"name": "a_D", "qc": {"grade": "D"}}]}
    with pytest.raises(ap.PipelineError, match="没有 A/B 级切片"):
        ap.stage_export_real_clips(c.get, "kangaroo")


def test_run_skip_tts_route_exports_real_clips(tmp_path, monkeypatch):
    c = _happy_client()
    fake = FakeCfg(tmp_path)
    monkeypatch.setattr(ap, "cfg", fake)
    (tmp_path / "media" / "clips").mkdir(parents=True)
    (tmp_path / "media" / "clips" / "x_A.wav").write_bytes(b"RIFF")
    c.gets["/clips"] = {"clips": [{"name": "x_A", "qc": {"grade": "A"}}]}
    args = {
        "file": ["a.mp4"],
        "voice_id": "auto_voice",
        "target_s": 30.0,
        "enhance": False,
        "with_spk": True,
        "skip_tts_corpus": True,
        "exp_name": "",
        "train": False,
        "epochs": 12,
        "dry_run": False,
        "timeout_s": 60,
    }
    report = ap.run_pipeline_tool(args, c.get, c.post, c.post_form)
    assert report["stages"]["export_real"]["exported"] == 1
    assert "tts_corpus" not in report["stages"]


# ---------------- run_pipeline_tool 整体 ----------------
def _happy_client():
    c = FakeClient()
    c.gets["/raw_videos"] = {"videos": [{"name": "a.mp4"}]}
    c.posts["/pipeline/run"] = {"ok": True, "started": True}
    c.gets["/pipeline/status"] = {"status": "done", "clips": 73, "message": "ok"}
    c.posts["/clips/qc?file=a.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 30, "B": 20, "C": 3, "D": 2},
    }
    c.posts["/clips/diarize?file=a.mp4"] = {"ok": True}
    return c


def test_run_dry_run_touches_nothing_but_raw_videos():
    c = _happy_client()
    seen = []

    def spy_post(path, payload=None):
        seen.append(path)
        return c.post(path, payload)

    args = {
        "file": None,
        "voice_id": "auto_voice",
        "target_s": 30.0,
        "enhance": False,
        "with_spk": True,
        "skip_tts_corpus": False,
        "exp_name": "",
        "train": False,
        "epochs": 12,
        "dry_run": True,
        "timeout_s": 60,
    }
    report = ap.run_pipeline_tool(args, c.get, spy_post, c.post_form)
    assert report["dry_run"] is True
    assert seen == []  # 没有任何 POST
    assert c.forms == []  # 没有建库


def test_run_aborts_before_voicebank_when_qc_zero():
    """A/B=0 → 建库阶段必须不被执行（宁可不产出，不可产出垃圾参考音）。"""
    c = _happy_client()
    c.posts["/clips/qc?file=a.mp4&spk=true&force=true"] = {
        "ok": True,
        "grades": {"A": 0, "B": 0, "C": 0, "D": 55},
    }
    args = {
        "file": ["a.mp4"],
        "voice_id": "auto_voice",
        "target_s": 30.0,
        "enhance": False,
        "with_spk": True,
        "skip_tts_corpus": True,
        "exp_name": "exp",
        "train": False,
        "epochs": 12,
        "dry_run": False,
        "timeout_s": 60,
    }
    with pytest.raises(ap.PipelineError, match="A/B=0"):
        ap.run_pipeline_tool(args, c.get, c.post, c.post_form)
    assert c.forms == []  # 没走到建库
    assert all("voicebank" not in p for p, _ in c.post_calls)


def test_run_happy_tts_route():
    c = _happy_client()
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 20, "total": 20}
    c.posts["/rvc/dataset/export?voice_id=auto_voice"] = {"ok": True, "copied": 20, "dest": "x"}
    args = {
        "file": ["a.mp4"],
        "voice_id": "auto_voice",
        "target_s": 30.0,
        "enhance": False,
        "with_spk": True,
        "skip_tts_corpus": False,
        "exp_name": "",
        "train": False,
        "epochs": 12,
        "dry_run": False,
        "timeout_s": 60,
    }
    report = ap.run_pipeline_tool(args, c.get, c.post, c.post_form)
    assert set(report["stages"]) == {"pipeline", "qc", "voicebank", "tts_corpus"}


def test_run_train_flag_invokes_ft_train():
    c = _happy_client()
    c.posts["/rvc/dataset/generate"] = {"ok": True, "started": True, "total": 20}
    c.gets["/rvc/dataset/status"] = {"running": False, "done": 20, "total": 20}
    c.posts["/rvc/dataset/export?voice_id=auto_voice"] = {"ok": True, "copied": 20, "dest": "x"}
    c.posts["/ft/train?voice_id=auto_voice&epochs=12"] = {"ok": True}
    c.gets["/ft/train_status?voice_id=auto_voice"] = {"running": False, "rc": 0}
    args = {
        "file": ["a.mp4"],
        "voice_id": "auto_voice",
        "target_s": 30.0,
        "enhance": False,
        "with_spk": True,
        "skip_tts_corpus": False,
        "exp_name": "",
        "train": True,
        "epochs": 12,
        "dry_run": False,
        "timeout_s": 60,
    }
    report = ap.run_pipeline_tool(args, c.get, c.post, c.post_form)
    assert report["stages"]["train"]["done"] is True

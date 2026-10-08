"""导出讲课视频（`m2_server/course_video.py`）单测 —— 2026-10-08。

覆盖：入口校验（没有批次 / 没有源课件 / 文本批次 / 正在跑 / 页全失败）、
编排（子进程命令的形状与顺序）、进度解析、失败与取消的落点、以及**磁盘才是事实**
（重开应用后靠 manifest + job_dir 仍能出片）。

合成与两个工具**全部打桩**：本文件不跑 PowerPoint、不跑 ffmpeg、不碰 GPU。
真链路（Office + 真 TTS + 真 ffmpeg）已手工验证：19 页课件导图 9.2s、
2 页端到端成片 17.29s，见逐日 2026-10-08 §第五轮。

⚠️ 这里守的是**不报错的错法**：
    · 贴文本的批次（没有源课件）若不拦，用户点「导出成片」会得到**没有画面**的片子
      或一个看不懂的 COM 报错；
    · 页数与停顿若读内存 STATE 而不读 manifest，重开应用后就"这批没合成过"——
      产物明明在磁盘上；
    · 工具失败时若只报"导出失败"，用户拿不到工具那一行真正的错（缺 Office /
      页序对不上 / 帧尺寸不一致全在那行里）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import course_api  # noqa: E402
import course_video as cv  # noqa: E402


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(cv.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """两个模块的 STATE 都复位；输出目录指到 tmp（产物目录由用例自己搭）。"""
    import config as cfg

    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    course_api.COURSE_STATE.update(
        running=False, status="idle", voice_id="", style_ref_voice="", done=0, total=0,
        percent=0, current_page=0, current_text="", job_dir="", gap_ms=350,
        source_pptx="", pages=[], error="",
    )
    cv.COURSE_VIDEO_STATE.update(
        running=False, status="idle", step="", percent=0, message="", job_dir="",
        out_url="", out_name="", duration_s=0.0, pages=0, slides=0, fps=30, gap_ms=0, error="",
    )
    cv._cv_cancel.clear()
    yield


def _mk_job(tmp_path: Path, *, job: str = "course_1700000000000", pages=2, failed=(),
            source=True, gap_ms=350, ok_url=True) -> Path:
    """搭一个"已合成完"的产物目录（manifest + 逐页 wav + 可选源课件）。"""
    job_dir = tmp_path / job
    job_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(1, pages + 1):
        bad = i in failed
        entries.append({"page": i, "text": f"第{i}页", "url": "" if (bad or not ok_url) else f"/u/{i}.wav",
                        "duration_s": 1.0, "failed": bad, "error": "x" if bad else ""})
    (job_dir / "manifest.json").write_text(json.dumps(
        {"total_pages": pages, "ok_pages": pages - len(failed), "gap_ms": gap_ms,
         "voice_id": "v1", "source_pptx": "source.pptx" if source else "",
         "pages": entries}, ensure_ascii=False), encoding="utf-8")
    if source:
        (job_dir / "source.pptx").write_bytes(b"PK\x03\x04 fake deck")
    return job_dir


def _fake_runner(record: list[list[str]], *, rc_map=None):
    """替掉 `_run_tool`：记下命令，按 label/命令内容决定成败，并喂几行假输出。"""

    def _run(cmd, label, on_line=None, timeout=cv._STEP_TIMEOUT):
        record.append(list(cmd))
        if on_line:
            if "pptx_to_png" in " ".join(cmd):
                on_line("[PowerPoint 16.0] demo.pptx: 2 页，导出 2 / 复用 0")
                on_line("  第 1 张 → page_001.png  (20 KB)")
                on_line("  第 2 张 → page_002.png  (13 KB)")
            else:
                on_line("[ok] out.mp4  2.00s  100 KB")
        want = (rc_map or {}).get(label, 0)
        if want != 0:
            raise cv.VideoError(f"{label}失败（退出码 {want}）：模拟错误行")
        if "lecture_video" in " ".join(cmd):
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_bytes(b"\x00" * 2048)
            out.with_suffix(".mp4.timeline.json").write_text(json.dumps(
                {"out": {"duration_s": 2.0}, "geometry": {"frames": 2}}), encoding="utf-8")

    return _run


def _wait_video_done(timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not cv.COURSE_VIDEO_STATE["running"]:
            return dict(cv.COURSE_VIDEO_STATE)
        time.sleep(0.02)
    raise AssertionError("导出任务没在 10s 内结束")


# ---------------- 入口校验 ----------------

class TestEntryValidation:
    def test_no_batch_is_400(self, client):
        r = client.post("/api/course/video", json={})
        assert r.status_code == 400
        assert "逐页合成" in r.json()["detail"]

    def test_unknown_job_dir_is_400(self, client, tmp_path):
        r = client.post("/api/course/video", json={"job_dir": "course_999"})
        assert r.status_code == 400
        assert "产物目录" in r.json()["detail"]

    def test_text_only_batch_explains_there_is_no_picture(self, client, tmp_path):
        """★ 贴文本的批次没有源课件 —— 必须说清"重传 .pptx"，而不是给个 COM 报错。"""
        job = _mk_job(tmp_path, source=False)
        r = client.post("/api/course/video", json={"job_dir": job.name})
        assert r.status_code == 400
        assert "源课件" in r.json()["detail"] and ".pptx" in r.json()["detail"]

    def test_all_pages_failed_is_400(self, client, tmp_path):
        job = _mk_job(tmp_path, pages=2, failed=(1, 2))
        r = client.post("/api/course/video", json={"job_dir": job.name})
        assert r.status_code == 400
        assert "没有合成成功的页" in r.json()["detail"]

    def test_conflict_while_synthesizing(self, client, tmp_path):
        _mk_job(tmp_path)
        course_api.COURSE_STATE["running"] = True
        course_api.COURSE_STATE["job_dir"] = "course_1700000000000"
        r = client.post("/api/course/video", json={})
        assert r.status_code == 409
        assert "逐页合成" in r.json()["detail"]

    def test_conflict_while_exporting(self, client, tmp_path):
        _mk_job(tmp_path)
        cv.COURSE_VIDEO_STATE["running"] = True
        r = client.post("/api/course/video", json={"job_dir": "course_1700000000000"})
        assert r.status_code == 409

    def test_cancel_when_idle_is_a_noop(self, client):
        r = client.post("/api/course/video/cancel")
        assert r.status_code == 200 and "没有正在跑" in r.json()["message"]


# ---------------- 编排 ----------------

class TestOrchestration:
    def test_happy_path_runs_slides_then_mux(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path, gap_ms=420)
        record: list[list[str]] = []
        monkeypatch.setattr(cv, "_run_tool", _fake_runner(record))

        r = client.post("/api/course/video", json={"job_dir": job.name})
        assert r.status_code == 200
        assert r.json()["gap_ms"] == 420  # 没给就用 manifest 里记的那个
        st = _wait_video_done()
        assert st["status"] == "done", st["error"]
        assert st["out_url"] == f"/api/media/outputs/{job.name}/course.mp4"
        assert st["duration_s"] == pytest.approx(2.0)
        assert st["pages"] == 2 and st["slides"] == 2

        # 两步的顺序与命令形状
        assert len(record) == 2
        slides_cmd, mux_cmd = record
        assert slides_cmd[1].endswith("pptx_to_png.py")
        assert slides_cmd[2] == str(job / "source.pptx")
        assert slides_cmd[slides_cmd.index("--out-dir") + 1] == str(job / "slides")
        assert mux_cmd[1].endswith("lecture_video.py")
        assert mux_cmd[mux_cmd.index("--pages") + 1] == str(job / "manifest.json")
        assert mux_cmd[mux_cmd.index("--slides") + 1] == str(job / "slides")
        assert mux_cmd[mux_cmd.index("--out") + 1] == str(job / "course.mp4")
        assert mux_cmd[mux_cmd.index("--gap-ms") + 1] == "420"
        assert mux_cmd[mux_cmd.index("--fps") + 1] == "30"
        assert "--force" not in slides_cmd  # 默认复用画面

    def test_force_and_fps_and_gap_overrides(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)
        record: list[list[str]] = []
        monkeypatch.setattr(cv, "_run_tool", _fake_runner(record))
        r = client.post("/api/course/video",
                        json={"job_dir": job.name, "force_slides": True, "fps": 24, "gap_ms": 0})
        assert r.status_code == 200 and r.json()["gap_ms"] == 0 and r.json()["fps"] == 24
        _wait_video_done()
        assert "--force" in record[0]
        assert record[1][record[1].index("--fps") + 1] == "24"
        assert record[1][record[1].index("--gap-ms") + 1] == "0"

    def test_current_batch_is_used_when_job_dir_omitted(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)
        course_api.COURSE_STATE.update(job_dir=job.name, gap_ms=350,
                                       pages=[{"page": 1, "url": "/u/1.wav", "failed": False}])
        monkeypatch.setattr(cv, "_run_tool", _fake_runner([]))
        r = client.post("/api/course/video", json={})
        assert r.status_code == 200 and r.json()["job_dir"] == job.name
        _wait_video_done()

    def test_slides_failure_reports_the_tool_line(self, client, tmp_path, monkeypatch):
        """★ 工具那一行的原文必须送到界面（缺 Office / 页序对不上全在那行里）。"""
        job = _mk_job(tmp_path)
        monkeypatch.setattr(cv, "_run_tool", _fake_runner([], rc_map={"导出画面": 1}))
        client.post("/api/course/video", json={"job_dir": job.name})
        st = _wait_video_done()
        assert st["status"] == "error"
        assert "导出画面失败" in st["error"] and "模拟错误行" in st["error"]

    def test_mux_failure_stops_before_done(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)
        monkeypatch.setattr(cv, "_run_tool", _fake_runner([], rc_map={"合成视频": 1}))
        client.post("/api/course/video", json={"job_dir": job.name})
        st = _wait_video_done()
        assert st["status"] == "error" and "合成视频失败" in st["error"]
        assert st["out_url"] == "" and not (job / "course.mp4").is_file()

    def test_worker_never_leaves_running_true(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)

        def _boom(*_a, **_kw):
            raise RuntimeError("模拟工具起不来")

        monkeypatch.setattr(cv, "_run_tool", _boom)
        client.post("/api/course/video", json={"job_dir": job.name})
        st = _wait_video_done()
        assert st["running"] is False and st["status"] == "error" and "起不来" in st["error"]


# ---------------- 进度与取消 ----------------

class TestProgressAndCancel:
    def test_slides_progress_lines_move_the_percent(self):
        cv.COURSE_VIDEO_STATE.update(percent=0, message="")
        on_line = cv._slides_progress([0])
        on_line("[PowerPoint 16.0] deck.pptx: 10 页，导出 10 / 复用 0")
        assert "10 页" in cv.COURSE_VIDEO_STATE["message"]
        on_line("  第 5 张 → page_005.png  (30 KB)")
        mid = cv.COURSE_VIDEO_STATE["percent"]
        assert 8 < mid < 68
        on_line("  第 10 张 → page_010.png  (30 KB)")
        assert cv.COURSE_VIDEO_STATE["percent"] == 68

    def test_cancel_between_steps_lands_as_cancelled(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)

        def _cancel_during_slides(cmd, label, on_line=None, timeout=cv._STEP_TIMEOUT):
            cv._cv_cancel.set()  # 模拟"用户在第一步行进中点取消"
            raise cv._Cancelled()

        monkeypatch.setattr(cv, "_run_tool", _cancel_during_slides)
        client.post("/api/course/video", json={"job_dir": job.name})
        st = _wait_video_done()
        assert st["status"] == "cancelled" and st["running"] is False

    def test_cancel_endpoint_sets_the_flag(self, client, tmp_path, monkeypatch):
        job = _mk_job(tmp_path)
        started = []

        def _hold(cmd, label, on_line=None, timeout=cv._STEP_TIMEOUT):
            started.append(label)
            while not cv._cv_cancel.is_set():
                time.sleep(0.02)
            raise cv._Cancelled()

        monkeypatch.setattr(cv, "_run_tool", _hold)
        client.post("/api/course/video", json={"job_dir": job.name})
        deadline = time.time() + 5
        while time.time() < deadline and not started:
            time.sleep(0.02)
        r = client.post("/api/course/video/cancel")
        assert r.status_code == 200 and "已发出" in r.json()["message"]
        st = _wait_video_done()
        assert st["status"] == "cancelled"


# ---------------- 与 course_api 的衔接 ----------------

class TestHandoffFromCourseApi:
    def test_manifest_records_gap_and_source(self, client_course, tmp_path):
        """manifest 必须记 gap_ms 与源课件 —— 出片的时间轴与画面都按它来。"""
        import io
        import zipfile

        import course_api as ca

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:  # 最小 pptx：1 页有备注
            z.writestr("ppt/slides/slide1.xml", "<xml/>")
            z.writestr("ppt/notesSlides/notesSlide1.xml", "<a:p><a:t>第一页备注</a:t></a:p>")
            z.writestr("ppt/slides/_rels/slide1.xml.rels",
                       '<Relationships><Relationship Target="../notesSlides/notesSlide1.xml"/></Relationships>')
        r = client_course.post("/api/course/parse",
                               files={"file": ("c.pptx", buf.getvalue(), "application/octet-stream")})
        assert r.status_code == 200
        src = r.json()["source_pptx"]
        assert src.startswith("course_src_") and src.endswith(".pptx"), src

        def _fake_ref(_v):
            return (Path("ref.wav"), "")

        import io as _io

        import numpy as np
        import qwen3_tts
        import soundfile as sf

        def _tts(text, **_kw):
            b = _io.BytesIO()
            sf.write(b, np.zeros(2400, dtype="float32"), 24000, format="WAV")
            return b.getvalue()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ca, "is_valid_voice_id", lambda v: v == "v1")
            mp.setattr(ca, "voice_ref", _fake_ref)
            mp.setattr(qwen3_tts, "tts", _tts)
            r = client_course.post("/api/course/run", json={
                "pages": [{"page": 1, "text": "第一页。"}], "voice_id": "v1",
                "gap_ms": 500, "source_pptx": src})
            assert r.status_code == 200, r.text
            job = r.json()["job_dir"]
            deadline = time.time() + 10
            while time.time() < deadline and ca.COURSE_STATE["running"]:
                time.sleep(0.02)
        mf = json.loads((tmp_path / job / "manifest.json").read_text("utf-8"))
        assert mf["gap_ms"] == 500
        assert mf["source_pptx"] == "source.pptx"
        assert (tmp_path / job / "source.pptx").is_file(), "源课件必须复制进产物目录"

    def test_traversal_source_name_is_rejected(self, client_course, tmp_path, monkeypatch):
        """请求里的名字是用户可控的 —— 带路径成分一律拒（不能让它指到别处）。"""
        import course_api as ca

        monkeypatch.setattr(ca, "is_valid_voice_id", lambda v: v == "v1")
        monkeypatch.setattr(ca, "voice_ref", lambda _v: (Path("ref.wav"), ""))
        for bad in ("../../secret.pptx", "course_src_x.pptx", "C:/tmp/a.pptx", "a/b.pptx"):
            r = client_course.post("/api/course/run", json={
                "pages": [{"page": 1, "text": "x"}], "voice_id": "v1", "source_pptx": bad})
            assert r.status_code == 400, f"{bad} 应被拒绝"
            assert "源课件" in r.json()["detail"]


@pytest.fixture
def client_course():
    app = FastAPI()
    app.include_router(course_api.router)
    return TestClient(app, raise_server_exceptions=False)

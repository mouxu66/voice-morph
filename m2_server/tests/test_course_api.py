"""讲稿驱动录课（course_api）的测试 —— 方案④，2026-10-05。

覆盖方案验收点：
    - pptx 解析：多页 / 中文 / 无备注页跳过 / 页序按 slide 序号（rels 映射，
      不按 notesSlide 文件名 —— 页序调整后不错位）/ 坏 zip
    - 纯文本兜底：`---` 分页 / 无标记整篇一页
    - 合成：mock qwen3_tts 端到端（逐页 wav + manifest）/ 单页失败可续跑（retry）
    - 任务互斥 409 / 入参校验
    - 插件清单红线（summary 措辞 + router 模块存在）

合成打桩说明：worker 里 `from qwen3_tts import tts` 是**函数内** import（可关插件
不做模块级依赖的契约），qwen3_tts 顶层全是标准库 —— `monkeypatch.setattr(
"qwen3_tts.tts", fake)` 即可整体替换合成入口，不碰真 worker / GPU。
"""

import io
import json
import re
import time
import zipfile

import course_api
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------- 工具 ----------------

def _mk_pptx(slides: dict[int, str | None]) -> bytes:
    """现场构造一个最小 pptx（内存 zip）。

    slides: {slide 序号: 备注文本}。None = 有 slide 没写备注。
    备注文件名**故意**与 slide 序号错一位（notesSlide{i+1} 对应 slide{i}），
    用于钉住「页序靠 rels 映射、不按文件名排序」——排错了断言当场红。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in sorted(slides):
            zf.writestr(f"ppt/slides/slide{i}.xml", "<xml/>")
            note = slides[i]
            if note is None:
                continue
            # notesSlide 文件名 = slide 序号 + 1（错位设计，见 docstring）
            nf = f"ppt/notesSlides/notesSlide{i + 1}.xml"
            paras = "".join(f"<a:p><a:r><a:t>{ln}</a:t></a:r></a:p>" for ln in note.split("\n"))
            zf.writestr(nf, f"<p:sld>{paras}</p:sld>")
            zf.writestr(
                f"ppt/slides/_rels/slide{i}.xml.rels",
                f'<Relationships><Relationship Target="../notesSlides/notesSlide{i + 1}.xml"/></Relationships>',
            )
    return buf.getvalue()


@pytest.fixture
def client():
    """只挂本插件的 router —— 开关层由 test_plugin_switch.py 管（同 preserve 的理由）。"""
    app = FastAPI()
    app.include_router(course_api.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def fake_voice(monkeypatch):
    """合法音色放行 + voice_ref 给假参考音（不碰 voicebank / worker）。"""
    monkeypatch.setattr(course_api, "is_valid_voice_id", lambda v: v == "v_test")
    monkeypatch.setattr(
        course_api, "voice_ref", lambda v: (__import__("pathlib").Path("ref.wav"), "")
    )


@pytest.fixture
def fake_tts(monkeypatch):
    """合成入口打桩：返回 0.1s 24k 单声道正弦 wav。

    `boom` 非空时，文本含该词的**句**抛异常 —— 造"单页失败"而不惊动其他页。
    返回一个可调的 calls 计数器供断言重试/续跑行为。
    """
    import soundfile as sf
    import numpy as np

    state = {"calls": 0, "boom": ""}

    def _fake(text, **_kw):
        state["calls"] += 1
        if state["boom"] and state["boom"] in text:
            raise RuntimeError(f"模拟合成失败：{text[:10]}")
        buf = io.BytesIO()
        sr = 24000
        sf.write(buf, np.zeros(int(sr * 0.1), dtype="float32"), sr, format="WAV")
        return buf.getvalue()

    monkeypatch.setattr("qwen3_tts.tts", _fake)
    return state


def _wait_done(timeout_s: float = 15.0) -> dict:
    """worker 是 daemon 线程 —— 轮询内部状态直到任务结束。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not course_api.COURSE_STATE["running"]:
            return dict(course_api.COURSE_STATE)
        time.sleep(0.05)
    raise AssertionError("任务 15s 未结束（可能死循环或线程没起来）")


def _reset_state():
    course_api.COURSE_STATE.update(
        running=False, status="idle", voice_id="", done=0, total=0,
        percent=0, current_page=0, current_text="", job_dir="", gap_ms=350,
        pages=[], error="",
    )


@pytest.fixture(autouse=True)
def _isolated_state():
    _reset_state()
    yield
    _reset_state()


# ---------------- pptx 解析 ----------------

class TestParsePptx:
    def test_multi_page_chinese_notes(self):
        pages, skipped = course_api.parse_pptx(
            _mk_pptx({1: "第一页讲：导课。", 2: "第二页讲：例题一。", 3: None})
        )
        assert [p["page"] for p in pages] == [1, 2]
        assert pages[0]["text"] == "第一页讲：导课。"
        assert skipped == 1  # 没写备注的页不计入，只报 skipped

    def test_page_order_follows_slide_number_not_note_filename(self):
        """页序必须按 slide 序号（rels 映射），不按 notesSlide 文件名。

        _mk_pptx 故意把 notesSlide{i+1} 对应 slide{i}：若实现按文件名排序，
        第 1 页会拿到第 2 页的备注 —— 当场红。
        """
        pages, _ = course_api.parse_pptx(
            _mk_pptx({1: "甲页备注", 2: "乙页备注"})
        )
        assert pages[0] == {"page": 1, "text": "甲页备注"}
        assert pages[1] == {"page": 2, "text": "乙页备注"}

    def test_xml_entities_unescaped(self):
        pages, _ = course_api.parse_pptx(_mk_pptx({1: "a&lt;b &amp; c"}))
        assert pages[0]["text"] == "a<b & c"

    def test_bad_zip_raises_value_error(self):
        with pytest.raises(ValueError, match="pptx"):
            course_api.parse_pptx(b"not a zip")

    def test_zip_without_slides_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ppt/notesSlides/notesSlide1.xml", "<x/>")
        with pytest.raises(ValueError, match="幻灯片"):
            course_api.parse_pptx(buf.getvalue())


# ---------------- 纯文本兜底 ----------------

class TestParseTextPages:
    def test_dash_separator(self):
        pages = course_api.parse_text_pages("第一页内容\n---\n第二页内容\n----\n第三页")
        assert [p["page"] for p in pages] == [1, 2, 3]
        assert pages[1]["text"] == "第二页内容"

    def test_no_separator_whole_text_one_page(self):
        pages = course_api.parse_text_pages("整篇没有分页标记的内容")
        assert len(pages) == 1 and pages[0]["page"] == 1

    def test_empty_chunks_dropped(self):
        pages = course_api.parse_text_pages("第一页\n---\n\n---\n第二页")
        assert [p["page"] for p in pages] == [1, 2]


# ---------------- parse 端点 ----------------

class TestParseEndpoint:
    def test_upload_pptx(self, client):
        r = client.post(
            "/api/course/parse",
            files={"file": ("course.pptx", _mk_pptx({1: "导课", 2: None}), "application/octet-stream")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "pptx" and body["total"] == 1 and body["skipped"] == 1
        assert body["pages"][0]["text"] == "导课"

    def test_pptx_all_notes_empty_is_a_clear_400(self, client):
        """全空备注是最常见的真实踩坑 —— 报错要把"写进备注"说清楚。"""
        r = client.post(
            "/api/course/parse",
            files={"file": ("course.pptx", _mk_pptx({1: None, 2: None}), "application/octet-stream")},
        )
        assert r.status_code == 400
        assert "备注" in r.json()["detail"]

    def test_text_fallback(self, client):
        r = client.post("/api/course/parse", data={"text": "第一页\n---\n第二页"})
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_nothing_given_is_400(self, client):
        assert client.post("/api/course/parse", data={"text": ""}).status_code == 400


# ---------------- 合成端到端（mock qwen3_tts） ----------------

class TestRunSynthesis:
    def test_end_to_end_pages_and_manifest(self, client, fake_voice, fake_tts, tmp_path):
        r = client.post(
            "/api/course/run",
            json={"pages": [{"page": 1, "text": "第一页，两句话。这是第二句。"},
                             {"page": 2, "text": "第二页内容。"}],
                  "voice_id": "v_test"},
        )
        assert r.status_code == 200
        assert r.json()["total"] == 2
        st = _wait_done()
        assert st["status"] == "done", st["error"]
        assert st["error"] == ""
        assert [p["failed"] for p in st["pages"]] == [False, False]
        assert all(p["duration_s"] > 0 and p["url"] for p in st["pages"])
        # job 目录里：每页一个 wav + manifest.json
        import config as cfg

        job = cfg.OUTPUTS_DIR / st["job_dir"]
        assert (job / "page_001.wav").is_file() and (job / "page_002.wav").is_file()
        manifest = json.loads((job / "manifest.json").read_text("utf-8"))
        assert manifest["ok_pages"] == 2 and manifest["total_pages"] == 2
        assert manifest["pages"][0]["page"] == 1

    def test_pages_sorted_by_page_number(self, client, fake_voice, fake_tts):
        """乱序提交按页号合成 —— 页号是"对齐回课件"的锚，顺序不该由提交顺序决定。"""
        client.post(
            "/api/course/run",
            json={"pages": [{"page": 2, "text": "乙"}, {"page": 1, "text": "甲"}],
                  "voice_id": "v_test"},
        )
        st = _wait_done()
        assert [p["page"] for p in st["pages"]] == [1, 2]

    def test_single_page_failure_does_not_sink_others(self, client, fake_voice, fake_tts):
        """★ 单页失败可续跑的前提：失败页标记 failed、其余页照常导出。"""
        fake_tts["boom"] = "炸"
        client.post(
            "/api/course/run",
            json={"pages": [{"page": 1, "text": "正常页。"},
                             {"page": 2, "text": "这一页要炸掉。"},
                             {"page": 3, "text": "正常页三。"}],
                  "voice_id": "v_test"},
        )
        st = _wait_done()
        by_page = {p["page"]: p for p in st["pages"]}
        assert by_page[2]["failed"] is True and by_page[2]["error"]
        assert by_page[1]["failed"] is False and by_page[3]["failed"] is False
        assert by_page[2]["url"] == "" and by_page[1]["duration_s"] > 0

    def test_retry_only_reruns_failed_pages(self, client, fake_voice, fake_tts):
        fake_tts["boom"] = "炸"
        client.post(
            "/api/course/run",
            json={"pages": [{"page": 1, "text": "正常页。"}, {"page": 2, "text": "要炸。"}],
                  "voice_id": "v_test"},
        )
        _wait_done()
        calls_after_run = fake_tts["calls"]
        assert calls_after_run > 0

        fake_tts["boom"] = ""  # "故障排除"，retry 应当恢复失败页
        r = client.post("/api/course/retry")
        assert r.status_code == 200
        st = _wait_done()
        assert st["status"] == "done" and st["error"] == ""
        assert all(not p["failed"] for p in st["pages"])
        # 只重跑失败页：新调用次数 = 失败页的句数（这里 1 页 1 句）
        assert fake_tts["calls"] == calls_after_run + 1

    def test_retry_without_failures_is_400(self, client, fake_voice, fake_tts):
        client.post("/api/course/run",
                    json={"pages": [{"page": 1, "text": "全好。"}], "voice_id": "v_test"})
        _wait_done()
        r = client.post("/api/course/retry")
        assert r.status_code == 400

    def test_run_conflict_while_running_is_409(self, client, fake_voice):
        course_api.COURSE_STATE["running"] = True  # _isolated_state 会复位
        r = client.post("/api/course/run",
                        json={"pages": [{"page": 1, "text": "x"}], "voice_id": "v_test"})
        assert r.status_code == 409

    def test_run_validates_pages_and_voice(self, client, fake_voice):
        # 全空文本页
        r = client.post("/api/course/run", json={"pages": [{"page": 1, "text": "  "}], "voice_id": "v_test"})
        assert r.status_code == 400
        # 非法音色
        r = client.post("/api/course/run", json={"pages": [{"page": 1, "text": "x"}], "voice_id": "nope"})
        assert r.status_code == 400


# ---------------- 插件清单红线 ----------------

class TestManifestGuard:
    def test_plugin_json_router_module_exists(self):
        """routers 指向的模块必须真实存在 —— manifest 与文件系统脱节时启动即 broken。"""
        from pathlib import Path

        m2 = Path(course_api.__file__).parent
        mf = json.loads((m2 / "plugins" / "sound.course" / "plugin.json").read_text("utf-8"))
        assert mf["routers"] == ["course_api"]
        assert (m2 / "course_api.py").is_file()

    def test_summary_wording_redline(self):
        """口径红线（同③的规矩）：说「拟应用/推广」，不说「已实现师范实训」，
        不出现医疗暗示词。summary 是设置页唯一可见文案，守它就是守口径。"""
        from pathlib import Path

        m2 = Path(course_api.__file__).parent
        raw = (m2 / "plugins" / "sound.course" / "plugin.json").read_text("utf-8")
        assert "拟应用" in raw  # 文案存在，守卫不空转
        assert not re.search(r"诊断|疗效|健康监测|已实现师范实训", raw)

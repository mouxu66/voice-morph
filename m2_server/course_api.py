"""讲稿驱动录课：PPT 备注页 → 逐页旁白（方案④，2026-10-05）。

用法：
    POST /api/course/parse     上传 .pptx（multipart）或贴纯文本（`---` 分页）→ 页列表
    POST /api/course/run       提交逐页合成任务（页内自动切句，句间停顿可调）
    GET  /api/course/status    轮询进度 + 逐页清单
    POST /api/course/cancel    取消当前任务
    POST /api/course/retry     只重跑上一任务的失败页（单页失败可续跑）

设计要点：
    - pptx 解析零新依赖：pptx 本质是 zip + XML（读 `ppt/notesSlides/*.xml`）。
      页序按 `ppt/slides/slideN.xml` 的 N；备注通过每页的 rels
      （Target=`../notesSlides/notesSlideM.xml`）映射 —— **不按 notesSlide 文件名
      排序**（页序调整/删页后的 pptx 会整体错位）。
    - 纯文本兜底：单独一行 `---`（3 个以上连字符）分页。
    - 逐页独立 wav：`outputs/course_<stamp>/page_001.wav` + `manifest.json`
      （页号/文本/时长）。**不拼整条大音频** —— 老师要的是"逐页对齐导出"，
      每页的音轨插回课件对应页；整段拼接是有声书（sound.audiobook）的事。
    - 单页失败可续跑：一页失败标记 failed、其余页照常导出；retry 只重跑失败页、
      原位更新清单。合成的每页重试一次（瞬时 OOM/worker 抖动不必劳用户重提）。
    - 同一时刻只允许一个任务（GPU 单路），重复提交 409（与 audiobook 同口径）。
    - 模块级**不 import audiobook**（可关插件；模块级拉依赖违反
      test_cross_plugin_import_isolation 锁的契约）—— 复用的切句/合成样板
      一律函数内延迟 import，文件常在、import 不会失败。
    - 产物不登记历史：与 audiobook 同理，落在本页供试听/下载，用户另存才进作品库。
"""

import re
import threading
import time
from html import unescape

from common import is_valid_voice_id, selected_voice, voice_ref
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/api")


# ---------------- pptx 解析（零新依赖：zipfile + 正则） ----------------

_PPTX_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_PPTX_RELS_RE = re.compile(r"ppt/slides/_rels/slide(\d+)\.xml\.rels$")
_PPTX_A_P_RE = re.compile(r"<a:p>.*?</a:p>", re.S)
_PPTX_A_T_RE = re.compile(r"<a:t>(.*?)</a:t>", re.S)
_PPTX_REL_TARGET_RE = re.compile(r'Target="[^"]*notesSlides/(notesSlide\d+\.xml)"')


def _notes_text(xml: str) -> str:
    """一张备注页 XML → 纯文本：按 `<a:p>` 分段（段间换行），段内拼接各 `<a:t>` run。"""
    paras: list[str] = []
    for p in _PPTX_A_P_RE.findall(xml):
        runs = [unescape(t) for t in _PPTX_A_T_RE.findall(p)]
        line = "".join(runs).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def parse_pptx(data: bytes) -> tuple[list[dict], int]:
    """pptx 字节 → (pages, skipped)。

    pages=[{page, text}]（text 非空的页，page 为 1 起的幻灯片序号）；
    没写备注的页不计入（skipped 计数）—— 老师不需要"第 7 页是空的"这种噪音。
    """
    import io
    import zipfile

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("这不是有效的 .pptx 文件（zip 结构打不开）") from e
    with zf:
        names = set(zf.namelist())
        slides = sorted(
            int(m.group(1))
            for n in names
            if (m := _PPTX_SLIDE_RE.match(n)) is not None
        )
        if not slides:
            raise ValueError("pptx 里没有幻灯片（没找到 ppt/slides/*.xml）")
        # slide 序号 → 备注页文件（经该页自己的 rels 映射，页序才可靠）
        notes_of: dict[int, str] = {}
        for n in names:
            m = _PPTX_RELS_RE.match(n)
            if m is None:
                continue
            tm = _PPTX_REL_TARGET_RE.search(zf.read(n).decode("utf-8", "replace"))
            if tm is not None:
                notes_of[int(m.group(1))] = f"ppt/notesSlides/{tm.group(1)}"
        pages: list[dict] = []
        skipped = 0
        for i in slides:
            nf = notes_of.get(i)
            text = _notes_text(zf.read(nf).decode("utf-8", "replace")) if nf and nf in names else ""
            if text.strip():
                pages.append({"page": i, "text": text.strip()})
            else:
                skipped += 1
    return pages, skipped


# ---------------- 纯文本兜底（`---` 分页） ----------------

_PAGE_SPLIT_RE = re.compile(r"^\s*-{3,}\s*$", re.M)


def parse_text_pages(text: str) -> list[dict]:
    """纯文本 → 页列表。没有分页标记时整篇算一页。

    空段丢弃后**按保留段重新编号**：文本路径的页号没有外部含义（pptx 才有
    slide 序号），用户眼里"第几页"就是"第几段有字的内容"。
    """
    chunks = [c.strip() for c in _PAGE_SPLIT_RE.split(text.replace("\r\n", "\n")) if c.strip()]
    return [{"page": i + 1, "text": c} for i, c in enumerate(chunks)]


# ---------------- 任务状态与后台线程 ----------------

COURSE_STATE: dict = {
    "running": False,
    "status": "idle",  # idle | running | done | cancelled | error
    "voice_id": "",
    "done": 0,
    "total": 0,
    "percent": 0,
    "current_page": 0,
    "current_text": "",
    "job_dir": "",  # 相对 outputs 的目录名（course_<stamp>）
    "gap_ms": 350,  # retry 续跑时要沿用上次的句间停顿
    "pages": [],  # [{page, text, url, duration_s, failed, error}]
    "error": "",
}
_course_cancel = threading.Event()
_course_lock = threading.Lock()

_PAGE_ATTEMPTS = 2  # 每页最多尝试次数（瞬时失败自动重试一次，不必劳用户重提）


class CoursePageIn(BaseModel):
    page: int
    text: str


class CourseRunRequest(BaseModel):
    pages: list[CoursePageIn]
    voice_id: str = ""
    gap_ms: int = 350


def _manifest(pages: list[dict]) -> dict:
    ok = [p for p in pages if not p["failed"] and p.get("url")]
    return {
        "generated_at": int(time.time() * 1000),
        "total_pages": len(pages),
        "ok_pages": len(ok),
        "total_duration_s": round(sum(p["duration_s"] for p in ok), 2),
        "pages": [
            {k: p[k] for k in ("page", "text", "url", "duration_s", "failed", "error") if k in p}
            for p in pages
        ],
    }


def _write_manifest(job_dir, pages) -> None:
    import json

    (job_dir / "manifest.json").write_text(
        json.dumps(_manifest(pages), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _page_url(job_name: str, page: int) -> str:
    return f"/api/media/outputs/{job_name}/page_{page:03d}.wav"


def _course_worker(job_dir, job_name: str, run_pages: list[dict], voice_id: str, gap_ms: int) -> None:
    """合成 run_pages 里的每一页（其余页的 state 原样保留 —— retry 只跑失败页的落点）。"""
    import soundfile as sf
    from pydub import AudioSegment
    from qwen3_tts import tts as qwen_tts

    import audiobook as ab  # 函数内延迟：可关插件不做模块级依赖（见模块注释）

    ref = voice_ref(voice_id)[0]
    gap = AudioSegment.silent(duration=gap_ms, frame_rate=24000)
    total = len(run_pages)

    try:
        for idx, page in enumerate(run_pages):
            if _course_cancel.is_set():
                COURSE_STATE.update(
                    running=False, status="cancelled", current_text="", error="已取消"
                )
                _write_manifest(job_dir, COURSE_STATE["pages"])
                return
            COURSE_STATE.update(
                current_page=page["page"],
                current_text=page["text"][:60],
                done=idx,
                percent=int(idx * 100 / total),
            )

            sents = ab.split_sentences(page["text"])
            pieces: list[AudioSegment] = []
            err = ""
            url = ""
            dur = 0.0
            for attempt in range(1, _PAGE_ATTEMPTS + 1):
                try:
                    pieces = []
                    for si, sent in enumerate(sents):
                        texts, after = ab._pieces_of(sent)
                        pcm, sr = ab._synth_pieces(texts, after, qwen_tts, ref, voice_id)
                        seg = AudioSegment(
                            pcm.tobytes(), frame_rate=sr, sample_width=pcm.dtype.itemsize, channels=1
                        )
                        pieces.append(seg)
                        if si < len(sents) - 1:
                            pieces.append(gap)
                    out = sum(pieces, AudioSegment.empty())
                    path = job_dir / f"page_{page['page']:03d}.wav"
                    out.export(str(path), format="wav")
                    d, sr2 = sf.read(str(path))
                    dur = round(len(d) / sr2, 2)
                    url = _page_url(job_name, page["page"])
                    err = ""
                    break
                except Exception as e:  # noqa: BLE001 —— 单页失败不拖垮其他页
                    err = f"{type(e).__name__}: {e}"

            for p in COURSE_STATE["pages"]:
                if p["page"] == page["page"]:
                    p.update(url=url, duration_s=dur, failed=not url, error=err)
                    break
            _write_manifest(job_dir, COURSE_STATE["pages"])
            COURSE_STATE.update(done=idx + 1, percent=int((idx + 1) * 100 / total))

        failed = [p for p in COURSE_STATE["pages"] if p["failed"]]
        COURSE_STATE.update(
            running=False,
            status="done",
            current_page=0,
            current_text="",
            error="" if not failed else f"{len(failed)} 页合成失败（可用「重试失败页」续跑）",
        )
    except Exception as e:  # noqa: BLE001 —— worker 顶层兜底，错误进状态而不是炸线程
        COURSE_STATE.update(running=False, status="error", current_text="", error=str(e))


@router.post("/course/parse")
async def course_parse(
    file: UploadFile | None = File(None),
    text: str = Form(""),
):
    """解析讲稿来源：.pptx 附件优先，否则按纯文本（`---` 分页）。"""
    fname = (file.filename or "").strip() if file is not None else ""
    if fname.lower().endswith(".pptx"):
        data = await file.read()
        try:
            pages, skipped = parse_pptx(data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not pages:
            raise HTTPException(
                status_code=400,
                detail=f"这个 pptx 的 {skipped} 页备注全是空的 —— 先把讲稿写进每页的「备注」再上传",
            )
        return {"source": "pptx", "total": len(pages), "skipped": skipped, "pages": pages}

    body = text
    if not body.strip() and file is not None and fname:
        body = (await file.read()).decode("utf-8", "replace")
    if not body.strip():
        raise HTTPException(status_code=400, detail="没有可解析的内容：上传 .pptx 或粘贴讲稿文本")
    pages = parse_text_pages(body)
    if not pages:
        raise HTTPException(status_code=400, detail="文本为空或全是分页标记")
    return {"source": "text", "total": len(pages), "skipped": 0, "pages": pages}


@router.post("/course/run")
async def course_run(req: CourseRunRequest):
    """提交逐页合成任务。"""
    with _course_lock:
        if COURSE_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有任务在跑，先取消或等待完成")
        pages = sorted(
            ({"page": p.page, "text": p.text.strip()} for p in req.pages if p.text.strip()),
            key=lambda p: p["page"],
        )
        if not pages:
            raise HTTPException(status_code=400, detail="没有可合成的页（每页文本不能全空）")
        voice_id = req.voice_id or selected_voice()
        if not voice_id or not is_valid_voice_id(voice_id):
            raise HTTPException(status_code=400, detail="请先指定合法音色")
        voice_ref(voice_id)  # 前置校验，避免起线程后才发现音色不存在

        import session_out  # noqa: F401 —— 会话目录语义的统一入口（本任务产物落 outputs 根子目录）

        from config import OUTPUTS_DIR

        stamp = int(time.time() * 1000)
        job_name = f"course_{stamp}"
        job_dir = OUTPUTS_DIR / job_name
        job_dir.mkdir(parents=True, exist_ok=True)

        state_pages = [
            {"page": p["page"], "text": p["text"], "url": "", "duration_s": 0.0, "failed": False, "error": ""}
            for p in pages
        ]
        _course_cancel.clear()
        COURSE_STATE.update(
            running=True,
            status="running",
            voice_id=voice_id,
            done=0,
            total=len(state_pages),
            percent=0,
            current_page=0,
            current_text="",
            job_dir=job_name,
            gap_ms=max(0, min(req.gap_ms, 3000)),
            pages=state_pages,
            error="",
        )

    threading.Thread(
        target=_course_worker,
        args=(job_dir, job_name, state_pages, voice_id, max(0, min(req.gap_ms, 3000))),
        daemon=True,
    ).start()
    return {"ok": True, "total": len(state_pages), "voice_id": voice_id, "job_dir": job_name}


@router.get("/course/status")
def course_status():
    return COURSE_STATE


@router.post("/course/cancel")
def course_cancel():
    if not COURSE_STATE["running"]:
        return {"ok": True, "message": "没有运行中的任务"}
    _course_cancel.set()
    return {"ok": True, "message": "取消指令已发出，当前页合成完即停"}


@router.post("/course/retry")
async def course_retry():
    """只重跑上一任务里失败的页（单页失败可续跑）。"""
    with _course_lock:
        if COURSE_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有任务在跑，先取消或等待完成")
        failed = [dict(p) for p in COURSE_STATE["pages"] if p["failed"]]
        if not failed:
            raise HTTPException(status_code=400, detail="上一任务没有失败页，不用重试")
        job_name = COURSE_STATE["job_dir"]
        if not re.fullmatch(r"course_\d+", job_name or ""):
            raise HTTPException(status_code=400, detail="没有可续跑的任务（目录名不合法）")
        from config import OUTPUTS_DIR

        job_dir = OUTPUTS_DIR / job_name
        if not job_dir.is_dir():
            raise HTTPException(status_code=400, detail="上次任务的产物目录已不在（可能被清理），请重新提交")
        voice_id = COURSE_STATE["voice_id"]
        if not voice_id or not is_valid_voice_id(voice_id):
            raise HTTPException(status_code=400, detail="上次的音色已失效，请重新提交任务")
        voice_ref(voice_id)

        gap_ms = COURSE_STATE["gap_ms"]
        # 先把失败页置回"待跑"态，worker 只跑这批页、原位更新
        for p in COURSE_STATE["pages"]:
            if p["failed"]:
                p.update(url="", duration_s=0.0, failed=False, error="")
        _course_cancel.clear()
        COURSE_STATE.update(
            running=True,
            status="running",
            done=0,
            total=len(failed),
            percent=0,
            current_page=0,
            current_text="",
            error="",
        )

    threading.Thread(
        target=_course_worker,
        args=(job_dir, job_name, failed, voice_id, gap_ms),
        daemon=True,
    ).start()
    return {"ok": True, "total": len(failed), "voice_id": voice_id, "job_dir": job_name}

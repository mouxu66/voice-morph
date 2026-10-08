"""讲稿驱动录课：PPT（备注页 / 幻灯片正文）→ 逐页旁白（方案④，2026-10-05）。

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
    - **讲稿来源备注优先、正文兜底**（2026-10-08）：某页没写备注时，自动取该页
      `ppt/slides/slideN.xml` 里可见的正文当讲稿草稿 —— 老师做 PPT 已经够累，
      不该再被要求"回头逐页补备注"（真实素材里 6 个 pptx 无一有备注）。
      两者都空的页才跳过。每页带 `source`（remark/body）供前端区分。
    - 纯文本兜底：单独一行 `---`（3 个以上连字符）分页。
    - 逐页独立 wav：`outputs/course_<stamp>/page_001.wav` + `manifest.json`
      （页号/文本/时长）。**不拼整条大音频** —— 老师要的是"逐页对齐导出"，
      每页的音轨插回课件对应页；整段拼接是有声书（sound.audiobook）的事。
    - 单页失败可续跑：一页失败标记 failed、其余页照常导出；retry 只重跑失败页、
      原位更新清单。合成的每页重试一次（瞬时 OOM/worker 抖动不必劳用户重提）。
    - 旁白可选**风格参考 ICL**（`style_ref_voice`，2026-10-08 接入 `outputs/lecture_demo`
      的实测成果）：纯声纹 x-vector 只取"这是谁"、丢掉"他怎么说的"，是录课旁白人机感
      的根因。⚠️ 但它是**整段克隆** —— 声音与语气都取自参考音，`voice_id` 那份音色
      不参与发声（实测：输出频谱与参考音相关 0.989、与旁白音色参考音 0.932）。
    - 同一时刻只允许一个任务（GPU 单路），重复提交 409（与 audiobook 同口径）。
    - 模块级**不 import audiobook**（可关插件；模块级拉依赖违反
      test_cross_plugin_import_isolation 锁的契约）—— 复用的切句/合成样板
      一律函数内延迟 import，文件常在、import 不会失败。
    - 产物不登记历史：与 audiobook 同理，落在本页供试听/下载，用户另存才进作品库。
"""

import re
import shutil
import threading
import time
from html import unescape
from pathlib import Path

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


def _slide_text(xml: str) -> str:
    """一页 XML → 纯文本：按 `<a:p>` 分段（段间换行），段内拼接各 `<a:t>` run。

    备注页（notesSlide）与幻灯片正文（slide）结构相同，共用这一个解析函数。
    """
    paras: list[str] = []
    for p in _PPTX_A_P_RE.findall(xml):
        runs = [unescape(t) for t in _PPTX_A_T_RE.findall(p)]
        line = "".join(runs).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


# 旧名保留：备注解析此前叫 _notes_text，测试/外部若还引用不至于断。
_notes_text = _slide_text


def parse_pptx(data: bytes) -> tuple[list[dict], int]:
    """pptx 字节 → (pages, skipped)。

    pages=[{page, text, source}]（text 非空的页，page 为 1 起的幻灯片序号）；
    `source` 标明这页的讲稿来自哪：`remark`（备注页）/ `body`（幻灯片正文兜底）。

    讲稿来源优先级：**备注优先、正文兜底**（2026-10-08 简化：老师做 PPT 已经够累，
    不该再被要求"回头逐页补备注"）。某页没写备注时，自动取该页幻灯片上可见的
    正文当讲稿草稿 —— 老师在上传后的页面里顺手改几句即可。
    连备注和正文都没有的页才计入 skipped（真正的全空页）。
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
            # 1) 备注优先
            nf = notes_of.get(i)
            remark = _slide_text(zf.read(nf).decode("utf-8", "replace")) if nf and nf in names else ""
            if remark.strip():
                pages.append({"page": i, "text": remark.strip(), "source": "remark"})
                continue
            # 2) 备注没有 → 幻灯片正文兜底
            sf = f"ppt/slides/slide{i}.xml"
            body = _slide_text(zf.read(sf).decode("utf-8", "replace")) if sf in names else ""
            if body.strip():
                pages.append({"page": i, "text": body.strip(), "source": "body"})
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
    "style_ref_voice": "",  # 风格参考 ICL 的音色 id（空=纯声纹）；retry 沿用，见 _course_worker
    "done": 0,
    "total": 0,
    "percent": 0,
    "current_page": 0,
    "current_text": "",
    "job_dir": "",  # 相对 outputs 的目录名（course_<stamp>）
    "gap_ms": 350,  # retry 续跑时要沿用上次的句间停顿
    "source_pptx": "",  # 源课件在 job 目录里的文件名（source.pptx；贴文本的批次为空）
    "pages": [],  # [{page, text, url, duration_s, failed, error}]
    "error": "",
}
_course_cancel = threading.Event()
_course_lock = threading.Lock()

_PAGE_ATTEMPTS = 2  # 每页最多尝试次数（瞬时失败自动重试一次，不必劳用户重提）

#: 风格参考 ICL 的段长（字符）。30 = `outputs/lecture_demo` 实测值（synth_lecture.py 的
#: `--seg-chars` 默认值就是 30，成片即按它出的）；worker 端还会先按句尾标点切，超长句才硬切。
_STYLE_SEG_CHARS = 30


class CoursePageIn(BaseModel):
    page: int
    text: str


class CourseRunRequest(BaseModel):
    pages: list[CoursePageIn]
    voice_id: str = ""
    gap_ms: int = 350
    # 风格参考 ICL（有情绪版旁白，2026-10-08 接入）：与 /api/tts 同口径 —— 只收
    # **音色 id** 走白名单，不收裸路径。空 = 不启用（纯声纹 x-vector，语调偏平）。
    # ⚠️ 启用后是**整段克隆**：声音与语气都取自参考音，`voice_id` 那份音色不参与发声。
    style_ref_voice: str = ""
    # parse 时暂存的源课件裸名（`course_src_<hash>.pptx`）：复制进 job 目录，
    # 导出成片时用它导画面。贴文本的批次为空串。
    source_pptx: str = ""


#: 源课件暂存名（会话目录里）：`course_src_<内容 sha1 前 10 位>.pptx`。
#: 内容寻址 ⇒ 同一份课件反复上传不会堆一堆副本；**裸名**（不带目录），与
#: `session_out` 的对外契约一致（消费侧显式拒绝含 `/` 的名字）。
_SOURCE_RE = re.compile(r"^course_src_[0-9a-f]{10}\.pptx$")


def save_source_pptx(data: bytes) -> str:
    """把上传的 .pptx 存进**会话目录**（退出即删），返回裸名。

    为什么必须留底：讲课视频的画面要从**原课件**导出来（`tools/pptx_to_png.py`）。
    此前 parse 只读备注文字、不看图形、也不留文件 —— 用户合成完再想导出成片就只能
    重传一次。存会话目录而不是 outputs 根：它是"本次会话的原料"；真出了片，源课件
    会随产物复制进 `outputs/course_*/source.pptx`，那才是该长期留着的那一份。
    """
    import hashlib

    import session_out

    name = f"course_src_{hashlib.sha1(data).hexdigest()[:10]}.pptx"
    (session_out.ensure() / name).write_bytes(data)
    return name


def resolve_source_pptx(name: str) -> Path | None:
    """暂存裸名 → 路径。名字不合法（含路径成分）或文件不在（会话被清过）返回 None。"""
    if not _SOURCE_RE.match(str(name or "").strip()):
        return None
    import session_out

    p = session_out.session_dir() / str(name).strip()
    return p if p.is_file() else None


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

    payload = _manifest(pages)
    # 出片要用的口径也落进 manifest：页间停顿此前只在内存的 STATE 里，重启后
    # "这一批到底留了多久的停顿"就无从考证，而视频正是按它排时间轴的。
    payload.update({
        k: COURSE_STATE.get(k) for k in ("gap_ms", "voice_id", "style_ref_voice", "source_pptx")
    })
    (job_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _page_url(job_name: str, page: int) -> str:
    return f"/api/media/outputs/{job_name}/page_{page:03d}.wav"


def _course_worker(
    job_dir,
    job_name: str,
    run_pages: list[dict],
    voice_id: str,
    gap_ms: int,
    style_ref: str = "",
    style_ref_text: str = "",
) -> None:
    """合成 run_pages 里的每一页（其余页的 state 原样保留 —— retry 只跑失败页的落点）。"""
    import soundfile as sf
    from pydub import AudioSegment
    from qwen3_tts import tts as qwen_tts

    import audiobook as ab  # 函数内延迟：可关插件不做模块级依赖（见模块注释）

    ref = voice_ref(voice_id)[0]
    gap = AudioSegment.silent(duration=gap_ms, frame_rate=24000)
    total = len(run_pages)
    # 风格参考只在真给了参考音时才组 kwargs —— 空字典 = 与接入前逐字相同的调用
    style_kw = (
        {"style_ref": style_ref, "style_ref_text": style_ref_text, "seg_chars": _STYLE_SEG_CHARS}
        if style_ref
        else {}
    )

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
                        pcm, sr = ab._synth_pieces(
                            texts, after, qwen_tts, ref, voice_id, **style_kw
                        )
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
                detail=(
                    f"这个 pptx 的 {skipped} 页既没有备注、幻灯片上也没有文字 —— "
                    "没有可当讲稿的内容（可以改用「粘贴文本」，用单独一行 --- 分页）"
                ),
            )
        return {"source": "pptx", "total": len(pages), "skipped": skipped, "pages": pages,
                # 留底：导出成片要从**原课件**导画面（贴文本的批次没有这一项）
                "source_pptx": save_source_pptx(data)}

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

        # 风格参考（可选）：先校验再起线程 —— 非法/不存在时线程里报错用户看不见
        style_ref_voice = req.style_ref_voice.strip()
        style_ref, style_ref_text = "", ""
        if style_ref_voice:
            if not is_valid_voice_id(style_ref_voice):
                raise HTTPException(status_code=400, detail="风格参考音色非法")
            sref, srtext = voice_ref(style_ref_voice)
            style_ref, style_ref_text = str(sref), srtext

        import session_out  # noqa: F401 —— 会话目录语义的统一入口（本任务产物落 outputs 根子目录）

        from config import OUTPUTS_DIR

        stamp = int(time.time() * 1000)
        job_name = f"course_{stamp}"
        job_dir = OUTPUTS_DIR / job_name
        job_dir.mkdir(parents=True, exist_ok=True)

        # 源课件：暂存裸名 → 复制进 job 目录（成片要从它导画面）。
        # 名字非法/文件不在都要**当场 400**：等出片时才发现没有课件，用户已经等了一轮合成。
        source_pptx = ""
        if req.source_pptx:
            src = resolve_source_pptx(req.source_pptx)
            if src is None:
                raise HTTPException(
                    status_code=400,
                    detail="源课件已不在（会话目录被清理过）—— 重新上传 .pptx 再合成一次",
                )
            shutil.copy2(src, job_dir / "source.pptx")
            source_pptx = "source.pptx"

        state_pages = [
            {"page": p["page"], "text": p["text"], "url": "", "duration_s": 0.0, "failed": False, "error": ""}
            for p in pages
        ]
        _course_cancel.clear()
        COURSE_STATE.update(
            running=True,
            status="running",
            voice_id=voice_id,
            style_ref_voice=style_ref_voice,
            done=0,
            total=len(state_pages),
            percent=0,
            current_page=0,
            current_text="",
            job_dir=job_name,
            gap_ms=max(0, min(req.gap_ms, 3000)),
            source_pptx=source_pptx,
            pages=state_pages,
            error="",
        )

    threading.Thread(
        target=_course_worker,
        args=(
            job_dir,
            job_name,
            state_pages,
            voice_id,
            max(0, min(req.gap_ms, 3000)),
            style_ref,
            style_ref_text,
        ),
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

        # 风格参考沿用上次任务：retry 出来的页要和同批其他页是同一个声音，
        # 否则「重试失败页」会悄悄换一个人念那几页。
        style_ref_voice = COURSE_STATE.get("style_ref_voice") or ""
        style_ref, style_ref_text = "", ""
        if style_ref_voice:
            if not is_valid_voice_id(style_ref_voice):
                raise HTTPException(status_code=400, detail="上次的风格参考音色已失效，请重新提交任务")
            sref, srtext = voice_ref(style_ref_voice)
            style_ref, style_ref_text = str(sref), srtext

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
        args=(job_dir, job_name, failed, voice_id, gap_ms, style_ref, style_ref_text),
        daemon=True,
    ).start()
    return {"ok": True, "total": len(failed), "voice_id": voice_id, "job_dir": job_name}

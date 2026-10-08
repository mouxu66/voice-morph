"""讲稿录课 · 导出成片：**原课件画面 + 逐页旁白 → mp4**（2026-10-08）。

把两个工具串成一条后台任务（都在 `tools/`，这里只做编排，不重写它们的逻辑）：

    tools/pptx_to_png.py     <job>/source.pptx ─▶ <job>/slides/page_%03d.png
    tools/lecture_video.py   <job>/manifest.json + slides ─▶ <job>/course.mp4

为什么走**子进程**而不是 import 进线程里跑
    · 画面那一步要 PowerPoint COM，只有装了 Office 的机器才有，而且 COM 要自己的
      apartment：在 FastAPI 进程里拉 COM，一旦卡在 PowerPoint 的模态框（受保护视图、
      许可提示）就既杀不掉也退不出；子进程超时可以连树一起收掉（`taskkill /T`）。
    · 缺 Office / 缺 comtypes 时子进程只是失败退出，**后端进程一根汗毛都不动**。
    · 工具本身就是 CLI（自带前置校验 + 出片后自检），这里只负责编排、进度与报错原文。
    ⚠️ 子进程一律 `CREATE_NO_WINDOW`：否则导出时会弹黑框，甚至盖住用户的微信窗口
      （速查表第 7 条那一族教训）。

与 `course_api` 的分工
    那边管「文字 → 逐页音频」（含失败页续跑）；这边管「画面 + 音频 → 视频」。
    两个 STATE、两个后台线程互不阻塞，但共用「同一时刻只跑一个任务」的规矩。
    入口会先查那边的状态：逐页合成还在跑时导片没有意义（页还在变）。

★ 「能出片」的判据一律**读磁盘**（manifest + source.pptx），不信内存
    重开应用后 `COURSE_STATE` 是空的，但 `outputs/course_*/` 还在 —— 所以本模块
    接受显式 `job_dir`，页数与页间停顿都从 manifest 里读（manifest 现在记着 gap_ms）。
"""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api")

_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _ROOT / "tools"
#: 子进程不要弹控制台窗口（否则盖住别的窗口，见模块注释）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
#: 单步超时：19 页真实课件实测 9.2s，但首次启动 PowerPoint / 超大课件会慢 —— 给足
_STEP_TIMEOUT = 1800
_JOB_RE = re.compile(r"^course_\d+$")

COURSE_VIDEO_STATE: dict = {
    "running": False,
    "status": "idle",  # idle | running | done | cancelled | error
    "step": "",  # slides | mux
    "percent": 0,
    "message": "",
    "job_dir": "",
    "out_url": "",
    "out_name": "",
    "duration_s": 0.0,
    "pages": 0,
    "slides": 0,
    "fps": 30,
    "gap_ms": 0,
    "error": "",
}
_cv_lock = threading.Lock()
_cv_cancel = threading.Event()


class CourseVideoRequest(BaseModel):
    """导出成片的参数。`gap_ms` 不给就用这一批的（manifest 里记着）。"""

    fps: int = 30
    gap_ms: int | None = None
    force_slides: bool = False  # 重导已有画面（默认复用：19 页要 ~9s，没必要每次重来）
    job_dir: str = ""  # 留空 = 当前这批（course_<stamp>）；给名字便于重开应用后重出片


class VideoError(RuntimeError):
    """业务错（文案直接给用户看）。"""


class _Cancelled(Exception):
    """用户取消（子进程已被收掉）。"""


def _set(**kw) -> None:
    COURSE_VIDEO_STATE.update(kw)


def _tool(name: str) -> Path:
    p = _TOOLS / name
    if not p.is_file():
        raise VideoError(f"工具缺失：{p}（安装副本没同步？跑 tools/sync_backend.ps1）")
    return p


def _kill_tree(pid: int) -> None:
    """连子进程一起收掉（工具自己会 Quit PowerPoint，但被强杀时那一步跑不到）。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001 —— 收尾失败不该盖住真正的错
        pass


def _run_tool(cmd: list[str], label: str, on_line=None, timeout: int = _STEP_TIMEOUT) -> None:
    """跑一个工具子进程，逐行喂给 `on_line`；非零退出抛业务错（带输出尾巴）。

    ⚠️ 不用 `subprocess.run(capture_output=True)`：那样要等进程结束才有输出，
    页数多时界面会长时间停在 0%，用户以为卡死。这里逐行读、边跑边报进度。
    ⚠️ 另有一个**看门狗**：子进程长时间一声不吭（典型是 PowerPoint 弹了受保护视图
    的模态框）时，光靠"逐行读"永远等不到下一行 —— 看门狗到点把它连树收掉，
    否则界面会永远停在 5%。
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    )
    tail: list[str] = []
    rc: int | None = None
    finished = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not finished.wait(timeout):
            timed_out.set()
            _kill_tree(proc.pid)

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        if proc.stdout is None:  # pragma: no cover —— PIPE 之下不会发生
            raise VideoError(f"{label}起不来（拿不到子进程输出管道）")
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                tail.append(line)
                del tail[:-6]
                if on_line is not None:
                    on_line(line)
            if _cv_cancel.is_set():
                _kill_tree(proc.pid)
                raise _Cancelled()
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired as e:  # pragma: no cover —— 输出读完还活着
            raise VideoError(f"{label}迟迟不退出（已收掉子进程）") from e
    finally:
        finished.set()
        if proc.poll() is None:
            _kill_tree(proc.pid)
    if timed_out.is_set():
        raise VideoError(
            f"{label}超时（{timeout}s 没结束）—— 多半卡在 PowerPoint 的弹窗上，已收掉子进程"
        )
    if rc != 0:
        raise VideoError(f"{label}失败（退出码 {rc}）：{' | '.join(tail[-4:]) or '没有任何输出'}")


def _slides_progress(total: list[int]):
    """解析 pptx_to_png 的输出 → 进度（total 用 list 当可变单元格）。"""

    def on_line(line: str) -> None:
        m = re.search(r":\s*(\d+)\s*页", line)
        if m:
            total[0] = int(m.group(1))
            _set(percent=8, message=f"课件共 {total[0]} 页，正在导出画面…")
            return
        m = re.search(r"第\s*(\d+)\s*张", line)
        if m and total[0]:
            done = int(m.group(1))
            _set(percent=min(68, 8 + int(60 * done / total[0])),
                 message=f"正在导出画面 {done}/{total[0]} 页…")

    return on_line


def _load_manifest(job_dir: Path) -> dict:
    p = job_dir / "manifest.json"
    if not p.is_file():
        raise VideoError("这一批的 manifest 不在（产物目录被清过？）—— 重新合成一次")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise VideoError(f"这一批的 manifest 读不了：{e}") from e


def _video_worker(job_dir: Path, job_name: str, gap_ms: int, fps: int, force: bool) -> None:
    """后台线程：导画面 → 拼片 → 复核。异常进 STATE，不炸线程。"""
    try:
        pptx = job_dir / "source.pptx"
        if not pptx.is_file():
            raise VideoError(
                "这一批没有源课件（讲稿是粘贴的文本，或合成时没带上文件）—— 画面导不出来。"
                "上传 .pptx 重新合成一次，成片才有画面"
            )
        slides_dir = job_dir / "slides"

        _set(step="slides", percent=3, message="正在从课件导出逐页画面…")
        total: list[int] = [0]
        cmd = [sys.executable, str(_tool("pptx_to_png.py")), str(pptx),
               "--out-dir", str(slides_dir)]
        if force:
            cmd.append("--force")
        _run_tool(cmd, "导出画面", on_line=_slides_progress(total))
        _set(slides=total[0], percent=70, message=f"画面就绪（{total[0]} 页）")

        out = job_dir / "course.mp4"
        _set(step="mux", percent=72, message="正在把画面与旁白合成视频…")
        _run_tool([sys.executable, str(_tool("lecture_video.py")),
                   "--pages", str(job_dir / "manifest.json"),
                   "--slides", str(slides_dir),
                   "--out", str(out),
                   "--gap-ms", str(gap_ms),
                   "--fps", str(fps)], "合成视频")

        if not out.is_file() or out.stat().st_size == 0:
            raise VideoError("合成视频没有产出文件（工具报成功但 mp4 不在）")
        # 出片自检的结论在 sidecar 里（时长/几何）—— 读得到就报给界面，读不到也不拦
        dur, frames = 0.0, 0
        side = out.with_suffix(out.suffix + ".timeline.json")
        if side.is_file():
            try:
                info = json.loads(side.read_text(encoding="utf-8"))
                dur = float(info.get("out", {}).get("duration_s") or 0.0)
                frames = int(info.get("geometry", {}).get("frames") or 0)
            except Exception:  # noqa: BLE001 —— 凭据读不了不影响成片本身
                pass
        _set(running=False, status="done", step="", percent=100,
             message=(f"成片完成：{dur:.1f}s" if dur else "成片完成"),
             out_url=f"/api/media/outputs/{job_name}/{out.name}",
             out_name=out.name, duration_s=dur, pages=frames, error="")
    except _Cancelled:
        _set(running=False, status="cancelled", step="", message="已取消", error="已取消")
    except Exception as e:  # noqa: BLE001 —— worker 顶层兜底，错误进状态而不是炸线程
        _set(running=False, status="error", step="", message="", error=str(e))


@router.post("/course/video")
async def course_video(req: CourseVideoRequest):
    """把这一批录课产物导成 mp4（课件画面 + 逐页旁白）。"""
    import course_api  # 同插件的兄弟模块：函数内 import，避免模块级相互牵引

    with _cv_lock:
        if COURSE_VIDEO_STATE["running"]:
            raise HTTPException(status_code=409, detail="导出成片已在跑，等它结束")
        if course_api.COURSE_STATE["running"]:
            raise HTTPException(status_code=409, detail="逐页合成还在跑，等它结束再导出成片")

        job_name = (req.job_dir or course_api.COURSE_STATE["job_dir"] or "").strip()
        if not _JOB_RE.match(job_name):
            raise HTTPException(status_code=400, detail="还没有可导出的批次，先跑一次逐页合成")
        from config import OUTPUTS_DIR

        job_dir = OUTPUTS_DIR / job_name
        if not job_dir.is_dir():
            raise HTTPException(status_code=400, detail="这一批的产物目录不见了，重新合成一次")
        mf = _load_manifest(job_dir)
        ok_pages = [p for p in (mf.get("pages") or []) if not p.get("failed") and p.get("url")]
        if not ok_pages:
            raise HTTPException(status_code=400, detail="这一批没有合成成功的页，导不出成片")
        if not (job_dir / "source.pptx").is_file():
            # 文案要指路：贴文本没有画面可导，重传 .pptx 才有
            raise HTTPException(
                status_code=400,
                detail="这一批没有源课件（讲稿是粘贴的文本）—— 上传 .pptx 重新合成一次，成片才有画面",
            )

        # 页间停顿：请求 > 本批 STATE > manifest 里记的 > 默认
        default_gap = None
        if course_api.COURSE_STATE["job_dir"] == job_name:
            default_gap = course_api.COURSE_STATE["gap_ms"]
        if default_gap is None:
            default_gap = mf.get("gap_ms", 350)
        gap_ms = max(0, min(int(default_gap if req.gap_ms is None else req.gap_ms), 3000))
        fps = max(1, min(int(req.fps), 60))

        _cv_cancel.clear()
        _set(running=True, status="running", step="", percent=0,
             message="准备中…" + ("（重新导出画面）" if req.force_slides else "（复用已有画面）"),
             job_dir=job_name, out_url="", out_name="", duration_s=0.0,
             pages=len(ok_pages), slides=0, fps=fps, gap_ms=gap_ms, error="")

    threading.Thread(
        target=_video_worker,
        args=(job_dir, job_name, gap_ms, fps, req.force_slides),
        daemon=True,
    ).start()
    return {"ok": True, "job_dir": job_name, "pages": len(ok_pages), "fps": fps, "gap_ms": gap_ms}


@router.get("/course/video/status")
def course_video_status():
    return COURSE_VIDEO_STATE


@router.post("/course/video/cancel")
def course_video_cancel():
    if not COURSE_VIDEO_STATE["running"]:
        return {"ok": True, "message": "没有正在跑的导出任务"}
    _cv_cancel.set()
    return {"ok": True, "message": "取消指令已发出，当前步骤结束即停"}

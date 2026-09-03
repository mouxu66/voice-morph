"""桌宠一键内录接口：WASAPI loopback 抓系统正在播的声音 → 解析挖掘。

自 server.py 拆出（行为不变）；复用 pipeline_api.pipeline_job 与 mine_api._mine_worker_thread。
app 装配见 server.py。
"""
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mine_api import _mine_worker_thread
from pipeline_api import pipeline_job
from runtime import (API_PREFIX, CAPTURE_STATE, PIPELINE_STATE, RAW_DIR,
                     pipeline_cancel, update_pipeline)

router = APIRouter(prefix=API_PREFIX)


class CaptureLoopbackRequest(BaseModel):
    seconds: float = 15.0   # 录制时长（3~120 秒）
    auto: bool = True       # 录完自动跑 流水线（demucs 去BGM+切片）+ 音色挖掘


def _capture_auto_worker(files: list[Path]):
    """内录后的自动流程：流水线 → 音色挖掘，进度走现有 PIPELINE_STATE / MINE_STATE。"""
    try:
        pipeline_job(files)
        if PIPELINE_STATE.get("status") == "done":
            _mine_worker_thread()
    except Exception as e:   # 后台流程：记录即可，不中断服务
        print(f"[capture] 自动解析/挖掘失败: {e}")


@router.post("/capture/loopback")
def capture_loopback(req: CaptureLoopbackRequest | None = None):
    """桌宠「录制当前声音」：WASAPI loopback 内录系统播出声 → 存 raw_videos →（可选）自动挖掘。

    同步录制 seconds 秒后返回（调用方 HTTP 超时要大于该时长）；auto 流程转后台，
    桌宠/前端通过 /pipeline/status 与 /mine/state 跟踪进度。
    """
    req = req or CaptureLoopbackRequest()
    seconds = max(3.0, min(float(req.seconds), 120.0))
    if CAPTURE_STATE["recording"]:
        raise HTTPException(400, "正在录制中，请稍候")
    if req.auto and PIPELINE_STATE["running"]:
        raise HTTPException(400, "流水线正在运行，稍后再录")
    from loopback_capture import LoopbackError, record_loopback

    name = f"capture_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    dest = RAW_DIR / name
    CAPTURE_STATE.update(recording=True, message=f"内录 {seconds:g} 秒…", file=name)
    try:
        record_loopback(seconds, dest)
    except LoopbackError as e:
        CAPTURE_STATE.update(recording=False, message=str(e), file="")
        raise HTTPException(500, f"内录失败: {e}")
    except Exception as e:
        CAPTURE_STATE.update(recording=False, message=str(e), file="")
        raise HTTPException(500, f"内录失败: {e}")
    CAPTURE_STATE.update(recording=False, message="录制完成", file=name)
    if not req.auto:
        return {"ok": True, "file": name, "seconds": seconds, "auto": False}
    if PIPELINE_STATE["running"]:
        return {"ok": True, "file": name, "seconds": seconds, "auto": False,
                "note": "流水线忙，已保存素材但未自动挖掘"}
    pipeline_cancel.clear()
    update_pipeline(running=True, status="running", step="prepare",
                    message="准备解析内录素材…", percent=1, clips=0, error="")
    threading.Thread(target=_capture_auto_worker, args=([dest],), daemon=True).start()
    return {"ok": True, "file": name, "seconds": seconds, "auto": True}

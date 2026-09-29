"""桌宠一键内录接口：WASAPI loopback 抓系统正在播的声音 → 解析挖掘。

自 server.py 拆出（行为不变）；复用 pipeline_api.pipeline_job 与 mine_api._mine_worker_thread。
app 装配见 server.py。
"""

import contextlib
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
import session_out
from runtime import (
    API_PREFIX,
    CAPTURE_STATE,
    PIPELINE_STATE,
    RAW_DIR,
    pipeline_cancel,
    update_pipeline,
)

router = APIRouter(prefix=API_PREFIX)


class CaptureLoopbackRequest(BaseModel):
    seconds: float = 15.0  # 录制时长（3~120 秒）
    auto: bool = True  # 录完自动跑 流水线（demucs 去BGM+切片）+ 音色挖掘


class MicStartRequest(BaseModel):
    #: 单次“按住”的硬上限（秒）。留给微信语音 60 秒上限一个余量。
    max_seconds: float = 59.0


class MicStopRequest(BaseModel):
    #: 换声用的音色（**音色库 id**，如 `kangaroo`）；留空 = 电脑上当前选中的音色。
    voice_id: str = ""
    #: 显式指定 RVC 实验名（一般留空 —— 由 resolve_rvc_voice 按约定推）。
    rvc_voice: str = ""
    pitch: int = 0
    index_rate: float = 0.5
    #: 只录音、不换声（排查用：确认录到的真的是你的声音、不是 CABLE）。
    raw: bool = False


def _decode_to_wav(raw: Path, dst: Path) -> None:
    """手机录的音频（webm/ogg/m4a/3gp/mp4…）→ 16k 单声道 wav。

    为什么不复用 `ab_chain._preprocess16k`：ab_chain 属 `sound.audition` 插件，
    本模块属 `pet.companion` —— 跨插件 import 会把两个**可关**能力绑在一起
    （关掉一个，另一个也跟着挂），正是 `tests/test_cross_plugin_import_isolation.py`
    钉的那类回归。这里只用 core 的 `find_ffmpeg`，命令与它逐字一致
    （`aresample=16000` + `-ac 1`），两处产物口径才一样。

    为什么是 16k：RVC 链路内部本来就把输入重采样到 16k 抽特征（ab_chain 的 RVC 对比
    也是喂 16k 进去的），先降到 16k 既省一次重采样，也不会丢 RVC 用得上的信息。
    """
    from common import find_ffmpeg

    cmd = [
        find_ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(raw),
        "-af",
        "aresample=16000",
        "-ac",
        "1",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(r.stderr.strip()[:800] or "ffmpeg 没有产出文件")


def _convert_for_send(
    path: Path,
    voice_id: str,
    rvc_voice: str,
    pitch: int,
    index_rate: float,
) -> dict:
    """把一段**本地录音**换声成可以直接发进微信的 wav，返回要并进响应的字段。

    两条入口共用：桌宠「按住说话」（`/capture/mic/stop`）与手机遥控页
    「手机当麦克风」（`/capture/mic/upload`）。抽出来不是因为代码长，而是因为里面那句
    「voicebank 用 `kangaroo`、RVC 实验是 `kangaroo_v2`」的命名换算**错了不会报错** ——
    只会静默退化成“没换声”或“找不到模型”。两个入口各抄一份，迟早漂掉一个。

    报错文案与按住说话链路完全一致（`docs/桌宠-按住说话-真机验收.md` 第五节按原文抄了
    这几句），所以别顺手改字。
    """
    # 延迟 import：rvc_convert 会拉起 RVC 子进程（与主服务 torch 隔离；
    # 同一套路见 wechat_voice.send_text 里的用法）。
    from common import selected_voice
    from rvc_convert import RvcError, resolve_rvc_voice, rvc_convert

    want = voice_id or selected_voice() or ""
    if not want:
        raise HTTPException(400, "没有可用音色 —— 先在电脑上选一个音色再按住说话")
    # ★ 两套命名不一样：voicebank 用 `kangaroo`，RVC 实验是 `kangaroo_v2`。
    #   把 voice_id 直接当 RVC 音色传下去会找不到模型（或退化成“不换声”）——
    #   这里必须与 send_text / preview_text 走同一个 resolve 约定。
    rvc_voice = rvc_voice or resolve_rvc_voice(want) or ""
    if not rvc_voice:
        raise HTTPException(
            400, f"音色「{want}」没有可用的 RVC 模型 —— 按住说话靠 RVC 定音色"
        )
    try:
        out = rvc_convert(path, rvc_voice, pitch, index_rate)
    except RvcError as e:
        raise HTTPException(400, f"换声失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"换声失败: {e}") from e
    return {
        "converted": True,
        "voice_id": want,
        "rvc_voice": rvc_voice,
        "file": out.name,
        "url": f"/api/media/outputs/{session_out.rel_url(out.name)}",
        "raw_file": path.name,
    }


def _capture_auto_worker(files: list[Path]):
    """内录后的自动流程：流水线 → 音色挖掘，进度走现有 PIPELINE_STATE / MINE_STATE。"""
    try:
        # 延迟 import：pipeline/mine 属可关插件（sound.workshop / sound.mine），
        # 且 pet.companion 的 requires 没声明它们 —— 模块级引用会把关掉的能力
        # 重新拉进来，其中一个坏掉还会直接拖垮整个 capture_api。
        from mine_api import _mine_worker_thread
        from pipeline_api import pipeline_job

        pipeline_job(files)
        if PIPELINE_STATE.get("status") == "done":
            _mine_worker_thread()
    except Exception as e:  # 后台流程：记录即可，不中断服务
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
        return {
            "ok": True,
            "file": name,
            "seconds": seconds,
            "auto": False,
            "note": "流水线忙，已保存素材但未自动挖掘",
        }
    pipeline_cancel.clear()
    update_pipeline(
        running=True,
        status="running",
        step="prepare",
        message="准备解析内录素材…",
        percent=1,
        clips=0,
        error="",
    )
    threading.Thread(target=_capture_auto_worker, args=([dest],), daemon=True).start()
    return {"ok": True, "file": name, "seconds": seconds, "auto": True}


# ---------------- 按住说话（桌宠）: 麦克风 start/stop ----------------
# 与上面的 loopback 内录是**互为镜像**的两件事：内录抓默认播放设备（系统在播的声音），
# 这里抓真实麦克风。为什么不抓“默认录音设备”：实时变声开着时它已被切到 CABLE Output，
# 按默认去录会录到**已经变好的声音**，再换一次声就是双重变声（见 mic_capture 模块注释）。
#
# 为什么要拆成 start/stop 而不是像内录那样“录 N 秒”：
#   这一条的手势是**按住说话**，松手就该停。固定时长要么切掉后半句，要么让人干等。
#   但“松开”不能是唯一出口 —— 到上限时采集线程自己停并落盘（手指滑出按钮导致 up
#   事件丢失、或按住不放，都不会留下一个永远在录的线程）。


@router.post("/capture/mic/start")
def mic_start(req: MicStartRequest | None = None):
    """开始录真实麦克风（一次只允许一个“按住”）。"""
    req = req or MicStartRequest()
    if CAPTURE_STATE["recording"]:
        raise HTTPException(400, "正在录音中（内录或按住说话）")
    import mic_capture

    out = session_out.new_path("mic_raw")
    try:
        info = mic_capture.start(out, req.max_seconds)
    except mic_capture.MicCaptureError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"麦克风启动失败: {e}") from e
    CAPTURE_STATE.update(recording=True, message="按住说话中…", file=out.name)
    return {"ok": True, "file": out.name, "max_seconds": info["max_seconds"], **info}


@router.post("/capture/mic/stop")
def mic_stop(req: MicStopRequest | None = None):
    """停止录音；默认接着换声（`rvc_convert`，用常驻 worker），返回可直接发送的 wav。

    桌宠只调这一次就能拿到“换好声的那句话” —— 不让前端去拼录音/换声/发送三步，
    否则这三步的失败分散在三处，用户看到的只会是“按了没反应”。
    """
    req = req or MicStopRequest()
    import mic_capture

    rec = mic_capture.current()
    if rec is None:
        raise HTTPException(400, "没有正在进行的录音")
    # 设备名要在 stop() 之前取：stop 会清掉单例，之后 current() 恒为 None
    device_name = rec.device_name
    try:
        path, duration, auto_stopped = mic_capture.stop()
    except mic_capture.MicCaptureError as e:
        CAPTURE_STATE.update(recording=False, message=str(e), file="")
        raise HTTPException(400, str(e)) from e
    finally:
        CAPTURE_STATE.update(recording=False, message="", file="")

    if duration < mic_capture.MIN_HOLD_SECONDS:
        raise HTTPException(
            400,
            f"录到的太短（{duration:.2f}s）—— 按住不放、说完再松手",
        )

    result = {
        "ok": True,
        "file": path.name,
        "url": f"/api/media/outputs/{session_out.rel_url(path.name)}",
        "duration_s": round(duration, 1),
        "auto_stopped": auto_stopped,
        "device": device_name,
    }
    if req.raw:
        result["converted"] = False
        return result

    result.update(_convert_for_send(path, req.voice_id, req.rvc_voice, req.pitch, req.index_rate))
    return result


# ---------------- 手机当麦克风（遥控页）: 上传一段录音 → 换声 ----------------
# 与桌宠「按住说话」**同一条产物口径**（录音 → rvc_convert → 可发送的 wav），
# 区别只在麦克风在哪：桌宠用 PC 的 WASAPI 输入设备，这里用手机。
#
# 为什么不复用 /capture/mic/start|stop：那两个端点的语义是"开/关一次本机采集会话"
# （单例 + 采集线程 + 设备挑选 + CAPTURE_STATE.recording），而手机这边音频已经在手机里
# 录完了 —— 对后端来说它只是一次**上传**，没有会话可开可关。硬塞进 start/stop 的话，
# "现在谁在录"这个状态要跨进程同步，而其中一个是随时会锁屏/断网的手机。


@router.post("/capture/mic/upload")
async def mic_upload(
    file: UploadFile = File(...),
    voice_id: str = Form(""),
    rvc_voice: str = Form(""),
    pitch: int = Form(0),
    index_rate: float = Form(0.5),
    raw: bool = Form(False),
):
    """手机遥控页「按住说话」：收一段**手机录的**音频，换声后返回可直接发送的 wav。

    参数与 /capture/mic/stop 同义（`voice_id` 留空 = 电脑上当前选中的音色），
    返回也同一组字段（`file` / `url` / `duration_s` / `voice_id`…）—— 手机拿到 `file`
    接着 POST `/api/wechat/send_voice {wav: file}` 就发进微信。刻意不把"上传+换声+
    发送"做成一个端点：发送要抢微信前台并切声卡，是一次可能失败的**动作**，
    换声却可能要等十几秒 —— 拆开才能让手机分开显示"正在换声"与"正在发"。
    """
    from common import MAX_UPLOAD_BYTES
    import mic_capture

    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"录音过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传内容为空 —— 手机那边可能没录上")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"录音过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")

    # 后缀只用来给 ffmpeg 一个提示（它本来就按内容识别）；手机给的可能是
    # webm/ogg（Android Chrome）或 m4a/mp4（iOS），也可能空。
    suffix = Path(file.filename or "phone.webm").suffix.lower() or ".webm"
    stamp = int(time.time() * 1000)
    raw_path = session_out.new_path(f"mic_phone_raw_{stamp}", suffix)
    raw_path.write_bytes(data)
    wav_path = session_out.new_path(f"mic_phone_{stamp}")
    try:
        _decode_to_wav(raw_path, wav_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            400, f"手机录音解不开（webm/m4a/ogg/wav 都支持）：{e}"
        ) from e
    finally:
        with contextlib.suppress(Exception):
            raw_path.unlink(missing_ok=True)

    duration = mic_capture.wav_duration(wav_path)
    if duration <= 0:
        wav_path.unlink(missing_ok=True)
        raise HTTPException(400, "手机录音解出来是空的 —— 重录一条试试")
    if duration < mic_capture.MIN_HOLD_SECONDS:
        wav_path.unlink(missing_ok=True)
        raise HTTPException(
            400, f"录到的太短（{duration:.2f}s）—— 按住不放、说完再松手"
        )
    if duration > mic_capture.MAX_HOLD_SECONDS:
        # 微信单条语音上限 60 秒，而发送链路对超长音频是**直接拒绝**（不截断）——
        # 与其让人等完换声（十几秒）才被拒，不如在这里就说清楚。
        wav_path.unlink(missing_ok=True)
        raise HTTPException(
            400,
            f"录了 {duration:.0f} 秒，超过微信单条语音上限（60 秒）—— 录短一点再来",
        )

    result = {
        "ok": True,
        "file": wav_path.name,
        "url": f"/api/media/outputs/{session_out.rel_url(wav_path.name)}",
        "duration_s": round(duration, 1),
        "source": "phone",
        "device": "手机麦克风（上传）",
    }
    if raw:
        result["converted"] = False
        return result

    result.update(_convert_for_send(wav_path, voice_id, rvc_voice, pitch, index_rate))
    return result

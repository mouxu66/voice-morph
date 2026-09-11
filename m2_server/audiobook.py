"""有声书工作台：长文 / SRT 逐句 TTS 拼接导出。

用法：
    POST /api/audiobook/run      提交任务（长文或 SRT 内容，指定音色）
    GET  /api/audiobook/status   轮询进度（第几句/共几句、逐句试听、最终成品）
    POST /api/audiobook/cancel   取消当前任务

设计要点：
    - 逐句走 x-vector 声纹克隆（ref_text 置空）：批任务求稳，且 8GB 显存下
      ICL 长参考会跌进 WDDM 共享内存慢路径；微调音色由 qwen3_tts.tts 自动分流。
    - 同一时刻只允许一个任务（GPU 单路），重复提交返回 409。
    - 每句的独立 wav 保留在 outputs/ 供逐句试听，最终拼接为单一成品 wav。
"""
import re
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config as cfg
from common import is_valid_voice_id, selected_voice, voice_ref

OUT = cfg.OUTPUTS_DIR
OUT.mkdir(exist_ok=True)

router = APIRouter(prefix="/api")


# ---------------- 文本切分 ----------------

# 中文按全角句末标点切；英文按 .!? 切（必须后跟空白，避免 U.S.A / 3.14 误切）。
# 注意：全角分支只能含全角标点——半角 !? 若混入会不检查后续空白就切分，
# 导致 "Hi!" 被拆成 "Hi"，碎片拼句时与下一句单词粘连（如 HiBye）。
_SENT_END = re.compile(r"(?<=[。！？；])|(?<=[.!?](?=\s))")
_MAX_SENT = 80    # 单句超过这个字数则在逗号处二次切分
_MIN_SENT = 4     # 碎片短于这个字数并入下一句
_SUB_SENT = re.compile(r"(?<=[，、,])")


def split_sentences(text: str) -> list[str]:
    """长文 → 适合单次合成的句子列表：按句末标点切，碎片合并、超长句在逗号处再切。"""
    raw = [s.strip() for s in _SENT_END.split(text.replace("\r\n", "\n"))]
    parts: list[str] = []
    buf = ""
    for s in raw:
        if not s:
            continue
        buf += s  # 无条件拼接：buf 未达到 _MIN_SENT 时绝不能丢句（旧实现会在此时静默丢弃 s）
        if len(buf) >= _MIN_SENT:
            parts.append(buf)
            buf = ""
    if buf:
        if parts:
            parts[-1] += buf  # 尾部碎片并入上一句
        else:
            parts.append(buf)
    # 超长句在逗号处二次切分（仍超长则硬切）
    out: list[str] = []
    for s in parts:
        if len(s) <= _MAX_SENT:
            out.append(s)
            continue
        piece = ""
        for seg in _SUB_SENT.split(s):
            if len(piece) + len(seg) > _MAX_SENT and piece:
                out.append(piece)
                piece = seg
            else:
                piece += seg
            while len(piece) > _MAX_SENT:  # 无标点的超长硬切
                out.append(piece[:_MAX_SENT])
                piece = piece[_MAX_SENT:]
        if piece:
            out.append(piece)
    return out


# ---------------- SRT 解析 ----------------

_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _srt_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def looks_like_srt(text: str) -> bool:
    return "-->" in text and bool(_SRT_TIME.search(text))


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    """SRT → [(start_s, end_s, 字幕文本)]，按开始时间排序。"""
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        start = _srt_seconds(*m.group(1, 2, 3, 4))
        end = _srt_seconds(*m.group(5, 6, 7, 8))
        lines = [ln.strip() for ln in block[m.end():].strip().splitlines()]
        body = " ".join(ln for ln in lines if ln and not ln.isdigit())
        if body:
            cues.append((start, end, body))
    cues.sort(key=lambda c: c[0])
    return cues


# ---------------- 任务状态与后台线程 ----------------

AUDIOBOOK_STATE: dict = {
    "running": False,
    "status": "idle",            # idle | running | done | cancelled | error
    "mode": "",                  # text | srt
    "voice_id": "",
    "done": 0,
    "total": 0,
    "percent": 0,
    "current_text": "",
    "url": "",
    "duration_s": 0.0,
    "error": "",
    "segments": [],              # [{i, text, url, duration_s, failed}]
}
_ab_cancel = threading.Event()
_ab_lock = threading.Lock()


class AudiobookRequest(BaseModel):
    text: str
    voice_id: str = ""
    gap_ms: int = 350             # 句间停顿（text 模式；srt 模式按时间轴对齐，此值为最短间隔）
    ref_text: str = ""            # 兼容字段，忽略


def _detect_lang(s: str) -> str:
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return "Chinese" if cjk * 2 >= len(s.strip()) else "English"


@router.post("/audiobook/run")
async def audiobook_run(req: AudiobookRequest):
    """提交有声书合成任务。文本自动识别 SRT（含 --> 时间轴）或普通长文。"""
    with _ab_lock:
        if AUDIOBOOK_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有任务在跑，先取消或等待完成")
        voice_id = req.voice_id or selected_voice()
        if not voice_id or not is_valid_voice_id(voice_id):
            raise HTTPException(status_code=400, detail="请先指定合法音色")
        voice_ref(voice_id)  # 前置校验，避免起线程后才发现音色不存在

        mode = "srt" if looks_like_srt(req.text) else "text"
        if mode == "srt":
            cues = parse_srt(req.text)
            if not cues:
                raise HTTPException(status_code=400, detail="SRT 解析失败：没有有效字幕条目")
            jobs = [(c[2], c[0], c[1]) for c in cues]
        else:
            sents = split_sentences(req.text)
            if not sents:
                raise HTTPException(status_code=400, detail="文本为空或无法切分出句子")
            jobs = [(s, None, None) for s in sents]

        _ab_cancel.clear()
        AUDIOBOOK_STATE.update(
            running=True, status="running", mode=mode, voice_id=voice_id,
            done=0, total=len(jobs), percent=0, current_text="",
            url="", duration_s=0.0, error="", segments=[],
        )

    threading.Thread(
        target=_audiobook_worker,
        args=(jobs, voice_id, max(0, min(req.gap_ms, 3000)), mode),
        daemon=True,
    ).start()
    return {"ok": True, "mode": mode, "total": len(jobs), "voice_id": voice_id}


def _audiobook_worker(jobs, voice_id: str, gap_ms: int, mode: str):
    from pydub import AudioSegment
    from qwen3_tts import tts as qwen_tts

    ref = voice_ref(voice_id)[0]
    stamp = int(time.time() * 1000)
    pieces: list[AudioSegment] = []
    gap = AudioSegment.silent(duration=gap_ms, frame_rate=24000)
    failed = 0
    total = len(jobs)

    try:
        for i, (text, start_s, _end_s) in enumerate(jobs):
            if _ab_cancel.is_set():
                AUDIOBOOK_STATE.update(running=False, status="cancelled",
                                       current_text="", error="已取消")
                return
            AUDIOBOOK_STATE.update(current_text=text, done=i,
                                   percent=int(i * 100 / total))
            seg_url = ""
            dur = 0.0
            try:
                wav = qwen_tts(text, ref_audio=str(ref), ref_text="",
                               language=_detect_lang(text), voice_id=voice_id)
                seg_path = OUT / f"audiobook_{stamp}_seg{i + 1:04d}.wav"
                seg_path.write_bytes(wav)
                import soundfile as sf
                d, sr = sf.read(str(seg_path))
                dur = round(len(d) / sr, 2)
                seg_url = f"/api/media/outputs/{seg_path.name}"
                pieces.append(AudioSegment.from_wav(str(seg_path)))
            except Exception:
                failed += 1
                pieces.append(AudioSegment.silent(duration=600, frame_rate=24000))
            AUDIOBOOK_STATE["segments"].append(
                {"i": i + 1, "text": text, "url": seg_url, "duration_s": dur,
                 "failed": seg_url == ""})
            AUDIOBOOK_STATE.update(done=i + 1, percent=int((i + 1) * 100 / total))

            # 句间停顿：text 模式固定 gap；srt 模式留给下方按时间轴对齐
            if i < total - 1 and mode == "text":
                pieces.append(gap)

        if failed >= total:
            raise RuntimeError(f"全部 {total} 句合成失败，请检查 worker / 显存状态")

        # srt 模式：按下一句开始时间补足静音（封顶 3s），保留原节奏
        if mode == "srt":
            aligned: list[AudioSegment] = []
            cursor = 0.0
            for i, (text, start_s, _e) in enumerate(jobs):
                seg = pieces[i]
                if i > 0 and start_s is not None:
                    wait = min(max(start_s - cursor, 0.0), 3.0)
                    if wait > 0.15:
                        aligned.append(AudioSegment.silent(
                            duration=int(wait * 1000), frame_rate=24000))
                    cursor += wait
                aligned.append(seg)
                if start_s is not None:
                    cursor = max(cursor, start_s) + len(seg) / 1000.0
                else:
                    cursor += len(seg) / 1000.0
            final = sum(aligned, AudioSegment.empty())
        else:
            final = sum(pieces, AudioSegment.empty())

        fname = f"audiobook_{stamp}.wav"
        final_path = OUT / fname
        final.export(str(final_path), format="wav")
        import soundfile as sf
        d, sr = sf.read(str(final_path))
        duration_s = round(len(d) / sr, 1)
        from history import register as history_register
        history_register("audiobook", voice_id, fname, f"/api/media/outputs/{fname}",
                         duration_s, input_text="")
        AUDIOBOOK_STATE.update(
            running=False, status="done", percent=100, current_text="",
            url=f"/api/media/outputs/{fname}",
            duration_s=duration_s,
            error="" if not failed else f"{failed} 句合成失败（以静音占位）",
        )
    except Exception as e:
        AUDIOBOOK_STATE.update(running=False, status="error",
                               current_text="", error=str(e))


@router.get("/audiobook/status")
def audiobook_status():
    return AUDIOBOOK_STATE


@router.post("/audiobook/cancel")
def audiobook_cancel():
    if not AUDIOBOOK_STATE["running"]:
        return {"ok": True, "message": "没有运行中的任务"}
    _ab_cancel.set()
    return {"ok": True, "message": "取消指令已发出，当前句合成完即停"}

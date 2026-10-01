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

import session_out
from common import is_valid_voice_id, selected_voice, voice_ref
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api")


# ---------------- 文本切分 ----------------

# 中文按全角句末标点切；英文按 .!? 切（必须后跟空白，避免 U.S.A / 3.14 误切）。
# 注意：全角分支只能含全角标点——半角 !? 若混入会不检查后续空白就切分，
# 导致 "Hi!" 被拆成 "Hi"，碎片拼句时与下一句单词粘连（如 HiBye）。
_SENT_END = re.compile(r"(?<=[。！？；])|(?<=[.!?](?=\s))")
_MAX_SENT = 80  # 单句超过这个字数则在逗号处二次切分
_MIN_SENT = 4  # 碎片短于这个字数并入下一句
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
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


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
        lines = [ln.strip() for ln in block[m.end() :].strip().splitlines()]
        body = " ".join(ln for ln in lines if ln and not ln.isdigit())
        if body:
            cues.append((start, end, body))
    cues.sort(key=lambda c: c[0])
    return cues


# ---------------- 任务状态与后台线程 ----------------

AUDIOBOOK_STATE: dict = {
    "running": False,
    "status": "idle",  # idle | running | done | cancelled | error
    "mode": "",  # text | srt
    "voice_id": "",
    "done": 0,
    "total": 0,
    "percent": 0,
    "current_text": "",
    "url": "",
    "duration_s": 0.0,
    "error": "",
    "segments": [],  # [{i, text, url, duration_s, failed}]
}
_ab_cancel = threading.Event()
_ab_lock = threading.Lock()


class AudiobookRequest(BaseModel):
    text: str
    voice_id: str = ""
    gap_ms: int = 350  # 句间停顿（text 模式；srt 模式按时间轴对齐，此值为最短间隔）
    ref_text: str = ""  # 兼容字段，忽略


def _detect_lang(s: str) -> str:
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return "Chinese" if cjk * 2 >= len(s.strip()) else "English"


# ---------------- 音效标记 `[爆炸]`（2026-09-25） ----------------
# 语义与 `wechat_voice._split_for_budget` / `tts_api._synth_marked` 一致：
# **音效跟在它前面那段文字之后**，标记本身不参与合成（不会被念出来）。
# 唯一解析实现在 `sfx_mark.py`，这里只做"哪一片之后插哪个"的编排。


def _reject_unknown_marks(sents: list[str]) -> None:
    """入口同步校验：句子里有 `[未知名]` 立刻 400，不留到后台线程里才发现。

    为什么必须在起线程之前：任务跑在 daemon 线程里，错误只能写进
    `AUDIOBOOK_STATE["error"]`，用户看到的是"跑完了但结果不对"。而写错音效名
    是用户当场能自己修的错，越早说越好（与微信路径同一文案、同一口径）。
    """
    import sfx_mark

    unknown: list[str] = []
    for s in sents:
        if not sfx_mark.has_mark(s):
            continue
        _segs, unk, _problems = sfx_mark.parse(s)
        unknown.extend(unk)
    if unknown:
        avail = "、".join(sfx_mark.available()[:12])
        raise HTTPException(
            status_code=400,
            detail=f"没有这个音效：{'、'.join(unknown)}。可用：{avail}",
        )


def _pieces_of(text: str) -> tuple[list[str | None], list[str | None]]:
    """一句文字 → (要念的片段, 每片之后要插的音效 id)。

    没有标记时返回 `([text], [None])` —— 调用方据此走与旧实现逐字相同的路径。
    片段为 `None` 表示"这里没有要念的字，但**要插一声**"（整片被标记吃掉，
    如 `[掌声]` 单独成句），由合成侧用静音占位。
    """
    import sfx_mark

    if not sfx_mark.has_mark(text):
        return [text], [None]
    segs, _unknown, _problems = sfx_mark.parse(text)
    texts: list[str | None] = []
    after: list[str | None] = []
    for t, sid in segs:
        if not t.strip():
            if sid is None:
                continue  # 首尾的空片（标记在开头/结尾产生的），丢掉
            texts.append(None)
        else:
            texts.append(t.strip())
        after.append(sid)
    if not texts:  # pragma: no cover —— 只有全空白输入才会走到，入口已挡
        return [text], [None]
    return texts, after


def _synth_pieces(texts, after, qwen_tts, ref, voice_id) -> tuple["object", int]:
    """逐片合成 →（带音效时）组装 → (PCM float32, sr)。

    无标记时只合成一次、结果原样返回（**不做峰值归一**）—— 现有产物的音量
    一个字节都不变，这是"无标记零回归"的落点。
    """
    import io

    import numpy as np
    import soundfile as sf

    raws: list[tuple[object, int]] = []
    for t in texts:
        if t is None:
            raws.append((None, 0))
            continue
        wav = qwen_tts(
            t,
            ref_audio=str(ref),
            ref_text="",
            language=_detect_lang(t),
            voice_id=voice_id,
        )
        data, psr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        raws.append((np.ascontiguousarray(data, dtype=np.float32), int(psr)))

    if len(raws) == 1 and not any(after):
        pcm, psr = raws[0]
        if pcm is not None:
            return pcm, psr

    from wechat_voice import SENT_GAP_S, SFX_GAP_S

    import sfx_lib

    sr = next((psr for pcm, psr in raws if pcm is not None), 24000)
    pieces = [
        np.zeros(max(1, int(0.01 * sr)), dtype=np.float32)
        if pcm is None
        else (pcm if psr == sr else sfx_lib._resample(pcm, psr, sr))
        for pcm, psr in raws
    ]
    out, _notes = sfx_lib.assemble(
        pieces, sr, after, sent_gap_s=SENT_GAP_S, sfx_gap_s=SFX_GAP_S
    )
    return out, sr


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
            _reject_unknown_marks(sents)  # 音效名写错要在起线程之前就说
            jobs = [(s, None, None) for s in sents]

        _ab_cancel.clear()
        AUDIOBOOK_STATE.update(
            running=True,
            status="running",
            mode=mode,
            voice_id=voice_id,
            done=0,
            total=len(jobs),
            percent=0,
            current_text="",
            url="",
            duration_s=0.0,
            error="",
            segments=[],
        )

    threading.Thread(
        target=_audiobook_worker,
        args=(jobs, voice_id, max(0, min(req.gap_ms, 3000)), mode),
        daemon=True,
    ).start()
    return {"ok": True, "mode": mode, "total": len(jobs), "voice_id": voice_id}


def _audiobook_worker(jobs, voice_id: str, gap_ms: int, mode: str):
    import soundfile as sf
    from pydub import AudioSegment
    from qwen3_tts import tts as qwen_tts

    ref = voice_ref(voice_id)[0]
    stamp = int(time.time() * 1000)
    pieces: list[AudioSegment] = []
    gap = AudioSegment.silent(duration=gap_ms, frame_rate=24000)
    failed = 0
    total = len(jobs)

    try:
        for i, (text, _, _) in enumerate(jobs):
            if _ab_cancel.is_set():
                AUDIOBOOK_STATE.update(
                    running=False, status="cancelled", current_text="", error="已取消"
                )
                return
            # 音效标记只认 text 模式：srt 模式是逐条字幕对齐时间轴，一句里再切片
            # 会把时间轴算乱，而字幕里的方括号更可能是原文内容。
            if mode == "text":
                texts, after = _pieces_of(text)
            else:
                texts, after = [text], [None]
            spoken = "".join(t for t in texts if t) or text
            AUDIOBOOK_STATE.update(current_text=spoken, done=i, percent=int(i * 100 / total))
            seg_url = ""
            dur = 0.0
            try:
                seg_path = session_out.new_path(f"audiobook_{stamp}_seg{i + 1:04d}")
                pcm, sr = _synth_pieces(texts, after, qwen_tts, ref, voice_id)
                sf.write(str(seg_path), pcm, sr, subtype="PCM_16")
                dur = round(pcm.size / sr, 2)
                seg_url = f"/api/media/outputs/{session_out.rel_url(seg_path.name)}"
                pieces.append(AudioSegment.from_wav(str(seg_path)))
            except Exception:
                failed += 1
                pieces.append(AudioSegment.silent(duration=600, frame_rate=24000))
            AUDIOBOOK_STATE["segments"].append(
                {
                    "i": i + 1,
                    "text": spoken,
                    "url": seg_url,
                    "duration_s": dur,
                    "failed": seg_url == "",
                }
            )
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
            for i, (_, start_s, _e) in enumerate(jobs):
                seg = pieces[i]
                if i > 0 and start_s is not None:
                    wait = min(max(start_s - cursor, 0.0), 3.0)
                    if wait > 0.15:
                        aligned.append(
                            AudioSegment.silent(duration=int(wait * 1000), frame_rate=24000)
                        )
                    cursor += wait
                aligned.append(seg)
                if start_s is not None:
                    cursor = max(cursor, start_s) + len(seg) / 1000.0
                else:
                    cursor += len(seg) / 1000.0
            final = sum(aligned, AudioSegment.empty())
        else:
            final = sum(pieces, AudioSegment.empty())

        final_path = session_out.new_path(f"audiobook_{stamp}")
        final.export(str(final_path), format="wav")
        import soundfile as sf

        d, sr = sf.read(str(final_path))
        duration_s = round(len(d) / sr, 1)
        # 不登记历史：产物只是本会话的试听结果，用户点「保存」时才进作品库
        # （默认不记，保存才留 —— 见 session_out 模块注释）。
        AUDIOBOOK_STATE.update(
            running=False,
            status="done",
            percent=100,
            current_text="",
            url=f"/api/media/outputs/{session_out.rel_url(final_path.name)}",
            duration_s=duration_s,
            error="" if not failed else f"{failed} 句合成失败（以静音占位）",
        )
    except Exception as e:
        AUDIOBOOK_STATE.update(running=False, status="error", current_text="", error=str(e))


@router.get("/audiobook/status")
def audiobook_status():
    return AUDIOBOOK_STATE


@router.post("/audiobook/cancel")
def audiobook_cancel():
    if not AUDIOBOOK_STATE["running"]:
        return {"ok": True, "message": "没有运行中的任务"}
    _ab_cancel.set()
    return {"ok": True, "message": "取消指令已发出，当前句合成完即停"}

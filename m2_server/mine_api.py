"""音色挖掘接口：上传切片 → 声纹聚类挖候选 → 试听 → 保存为正式音色。

自 server.py 拆出（行为不变）；共享状态 MINE_STATE / PREVIEW_TEXTS 在 runtime。
app 装配见 server.py。
"""
import json
import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import is_valid_voice_id
from history import register as history_register
from runtime import API_PREFIX, CLIPS_DIR, MINE_STATE, OUT, PREVIEW_TEXTS, VOICEBANK

router = APIRouter(prefix=API_PREFIX)


def _mine_worker_thread(params: dict | None = None):
    """后台挖掘线程：枚举切片 -> 调 worker /analyze。"""
    params = params or {}
    MINE_STATE.update(running=True, stage="running", message="正在转写与提取声纹…", kept=0, clusters=[])
    try:
        clips = [{"name": p.stem, "path": str(p)} for p in sorted(CLIPS_DIR.glob("*.wav"))]
        if not clips:
            raise RuntimeError("没有可用切片，请先在音色工坊解析视频")
        from qwen3_tts import analyze
        result = analyze(clips, sim_threshold=params.get("sim_threshold"),
                         min_cluster_size=params.get("min_cluster_size"))
        MINE_STATE.update(running=False, stage="done", message="挖掘完成",
                          kept=result.get("kept", 0), clusters=result.get("clusters", []),
                          errors=result.get("errors", []))
    except Exception as e:
        MINE_STATE.update(running=False, stage="error", message=str(e))


class MineRunRequest(BaseModel):
    sim_threshold: float | None = None   # 聚类相似度阈值，默认 0.5，调高挖出更多不同音色
    min_cluster_size: int | None = None  # 最小簇成员数，过滤零散噪声簇


@router.post("/mine/run")
def mine_run(req: MineRunRequest | None = None):
    """对当前全部切片跑音色挖掘（后台执行，前端轮询 /mine/state）。"""
    if MINE_STATE["running"]:
        return {"ok": True, "already_running": True}
    threading.Thread(target=_mine_worker_thread, daemon=True,
                     kwargs={"params": req.model_dump(exclude_none=True) if req else {}}).start()
    return {"ok": True}


@router.get("/mine/state")
def mine_state():
    return MINE_STATE


class MinePreviewRequest(BaseModel):
    clip: str            # 切片名（不含 .wav），即候选代表切片
    text: str = ""       # 试听文本；留空则从新句池轮换（保证与视频原话不同）


@router.post("/mine/preview")
def mine_preview(req: MinePreviewRequest):
    """用候选切片做参考，合成一句全新文本试听（ICL 模式，转写来自挖掘结果）。"""
    p = CLIPS_DIR / f"{req.clip}.wav"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"切片不存在: {req.clip}")
    # 从挖掘结果里取该切片的转写文字稿
    ref_text = ""
    for c in MINE_STATE["clusters"]:
        if c["rep"]["name"] == req.clip:
            ref_text = c["rep"]["text"]
            break
    text = req.text.strip()
    if not text:
        idx = (MINE_STATE.get("preview_seq", 0)) % len(PREVIEW_TEXTS)
        MINE_STATE["preview_seq"] = MINE_STATE.get("preview_seq", 0) + 1
        text = PREVIEW_TEXTS[idx]
    try:
        from qwen3_tts import tts as qwen_tts
        wav_bytes = qwen_tts(text, ref_audio=str(p), ref_text=ref_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听合成失败: {e}")
    fname = f"mine_{int(time.time() * 1000)}.wav"
    out = OUT / fname
    out.write_bytes(wav_bytes)
    import soundfile as sf
    d, sr = sf.read(str(out))
    duration_s = round(len(d) / sr, 1)
    history_register("mine", "", fname, f"/api/media/outputs/{fname}",
                     duration_s, input_text=text)
    return JSONResponse({
        "ok": True, "clip": req.clip, "text": text,
        "ref_text": ref_text,
        "url": f"/api/media/outputs/{fname}",
        "duration_s": duration_s,
    })


class MineSaveRequest(BaseModel):
    clip: str
    voice_id: str
    display_name: str = ""
    members: list[str] = []   # 可选：把同簇多条一起聚合为参考档案


@router.post("/mine/save")
def mine_save(req: MineSaveRequest):
    """把满意的候选保存为正式音色：代表切片(可含同簇成员)聚合为 reference.wav + ref_text.txt。

    参考音频只取簇内排名靠前的约 20 秒（ICL 克隆对超长参考既慢又会劣化），
    成员顺序即挖掘结果的质量排序。
    """
    if not is_valid_voice_id(req.voice_id):
        raise HTTPException(status_code=400, detail="音色 ID 非法")
    from pydub import AudioSegment

    REF_CAP_MS = 20000
    members = [req.clip] + [m for m in req.members if m != req.clip]
    merged = AudioSegment.silent(duration=300)
    texts = []
    rep_text = ""
    for name in members:
        p = CLIPS_DIR / f"{name}.wav"
        if not p.exists():
            continue
        seg = AudioSegment.from_wav(str(p))
        merged += seg.set_channels(1).set_frame_rate(22050) + AudioSegment.silent(duration=300)
        for c in MINE_STATE["clusters"]:
            rep_name = c.get("rep", {}).get("name")
            if rep_name == name:
                texts.append(c["rep"]["text"])
                if name == req.clip:
                    rep_text = c["rep"]["text"]
                break
        if len(merged) >= REF_CAP_MS:
            break
    if len(merged) <= 300:
        raise HTTPException(status_code=404, detail="候选切片不存在")

    out_dir = VOICEBANK / req.voice_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = out_dir / "reference.wav"
    merged.set_channels(1).set_frame_rate(22050).export(str(ref), format="wav")
    # 文字稿与参考音频逐句对齐（聚了多条切片就拼全部文字稿），ICL 克隆更准
    (out_dir / "ref_text.txt").write_text(" ".join(texts) or rep_text, encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps(
        {"display_name": req.display_name or req.voice_id, "source": "mine"}, ensure_ascii=False),
        encoding="utf-8")
    (out_dir / "clips.txt").write_text("\n".join(members), encoding="utf-8")
    # 保存后设为当前选中音色
    (VOICEBANK / "selected_voice.json").write_text(
        json.dumps({"voice_id": req.voice_id}), encoding="utf-8")
    return {"ok": True, "voice_id": req.voice_id, "duration_s": round(len(merged) / 1000, 1),
            "clips": len(members)}

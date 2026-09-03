"""A/B 音色对比接口：同一句双音色合成 + 声纹余弦相似度（盲听评分）。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common import voice_ref
from runtime import API_PREFIX, OUT, PREVIEW_TEXTS

router = APIRouter(prefix=API_PREFIX)

# 声纹嵌入缓存：path -> (mtime, emb)，同一参考音频不反复送 worker 提取
_EMB_CACHE: dict = {}


def _speaker_emb(path: Path) -> "list[float]":
    """取某 wav 的声纹嵌入（worker /emb，与克隆同一编码器），带 mtime 缓存。"""
    mtime = path.stat().st_mtime
    cached = _EMB_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    from qwen3_tts import post
    data = json.loads(post("/emb", {"path": str(path)}, timeout=120))
    if "emb" not in data:
        raise RuntimeError(f"声纹提取失败: {data.get('error')}")
    _EMB_CACHE[str(path)] = (mtime, data["emb"])
    return data["emb"]


class AbRunRequest(BaseModel):
    voice_a: str
    voice_b: str
    text: str = ""


@router.post("/ab/run")
def ab_run(req: AbRunRequest):
    """A/B 对比：同一句文本分别用两个音色合成，并计算各自与参考音频的声纹余弦相似度。
    顺序打乱交给前端做盲听，揭晓后再展示相似度分数。"""
    if req.voice_a == req.voice_b:
        raise HTTPException(status_code=400, detail="两个音色不能相同")
    text = req.text.strip() or PREVIEW_TEXTS[int(time.time()) % len(PREVIEW_TEXTS)]
    import numpy as np
    from qwen3_tts import tts as qwen_tts

    results = {}
    for tag, vid in (("A", req.voice_a), ("B", req.voice_b)):
        ref, _ = voice_ref(vid)
        # 强制 x-vector 声纹模式（ref_text 置空）：A/B 考察的是音色相似度本身，
        # 且长参考 ICL 在 8GB 卡上会跌进 WDDM 共享内存慢路径（20s 参考要 20 分钟+）
        wav = qwen_tts(text, ref_audio=str(ref), ref_text="", voice_id=vid)
        fname = f"ab_{int(time.time() * 1000)}_{tag}.wav"
        out = OUT / fname
        out.write_bytes(wav)
        emb_gen = np.asarray(_speaker_emb(out))
        emb_ref = np.asarray(_speaker_emb(ref))
        sim = float(np.dot(emb_gen, emb_ref) / (np.linalg.norm(emb_gen) * np.linalg.norm(emb_ref)))
        results[tag] = {"voice_id": vid, "url": f"/api/media/outputs/{fname}", "similarity": round(sim, 3)}
    return {"ok": True, "text": text, **results}

"""Qwen3-TTS 通用音色服务 · 常驻推理（venv312 / torch2.8cu129）。

职责（v2 重构：去袋鼠专用化）：
    1. /analyze  音色挖掘：对切片批量做 whisper 转写 + 说话人声纹提取 + 贪心聚类，
       按簇返回候选音色（每簇含最干净、最具代表性的切片），供前端迭代试听筛选
    2. /tts      按传入的参考音频+文字稿动态克隆合成（ICL 模式），prompt 按参考缓存
    3. /health   就绪探测

为什么独立成服务：
    m2_server 主进程运行在项目的 .venv(torch2.9+cu128，给 RVC 转换用)，
    而 Qwen3-TTS 必须跑在 venv312(torch2.8+cu129，支持 RTX5060 Blackwell)。
    两者 CUDA/torch 版本不兼容，不能同进程，故由 qwen3_tts.py 懒启动本服务常驻，
    监听 8001，通过 HTTP 转发请求，解耦互不干扰。

启动：
    D:/变声/tts_trial/venv312/Scripts/python.exe qwen3_tts_service.py
"""
import io
import os
import time

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Request
from fastapi.responses import Response
from qwen_tts import Qwen3TTSModel

try:
    import config as _cfg
    MODEL_DIR = str(_cfg.QWEN_MODEL_DIR)
except ImportError:  # 直接运行 worker（cwd 非 m2_server）时回退环境变量/相对默认
    MODEL_DIR = os.environ.get(
        "VM_QWEN_MODEL_DIR",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tts_models", "qwen3-tts-1.7b-base")))
WHISPER_SIZE = os.environ.get("VM_WHISPER_SIZE", "small")

app = FastAPI(title="Qwen3-TTS 通用音色 worker")
MODEL = None
WHISPER = None
WHISPER_SR = 16000
# prompt 缓存：key=(ref_audio_path, ref_text, x_vector_only) -> prompt 对象
_PROMPT_CACHE: dict = {}


@app.on_event("startup")
def _load():
    global MODEL
    MODEL = Qwen3TTSModel.from_pretrained(
        MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)


@app.get("/health")
def health():
    return {"status": "ok", "version": 5}


def _get_whisper():
    global WHISPER
    if WHISPER is None:
        from faster_whisper import WhisperModel
        WHISPER = WhisperModel(WHISPER_SIZE, device="cuda", compute_type="float16")
    return WHISPER


def _transcribe(path: str) -> dict:
    """转写一条切片，返回文字与质量指标（logprob 越接近 0 越好，no_speech 越低越好）。"""
    segments, _info = _get_whisper().transcribe(path, language="zh", vad_filter=True)
    texts, logprobs, nospeech = [], [], []
    for s in segments:
        texts.append(s.text)
        logprobs.append(s.avg_logprob)
        nospeech.append(s.no_speech_prob)
    text = "".join(texts).strip()
    quality = 0.0
    if logprobs:
        quality = float(np.mean(logprobs)) - float(np.mean(nospeech))
    return {"text": text, "quality": round(quality, 3)}


def _speaker_embedding(path: str) -> np.ndarray:
    """提取切片的说话人声纹（与克隆时同一个编码器，空间一致）。

    走 create_voice_clone_prompt(x_vector_only) 路径取 ref_spk_embedding——
    该路径在 A/B 试听脚本中实测稳定；直接调 model.extract_speaker_embedding
    会遇到 bf16 CUDA tensor 转 numpy 的兼容坑。
    """
    prompt = MODEL.create_voice_clone_prompt(ref_audio=path, ref_text=".", x_vector_only_mode=True)
    item = prompt[0] if isinstance(prompt, list) else prompt
    emb = item.ref_spk_embedding
    if torch.is_tensor(emb):
        emb = emb.detach().float().cpu().numpy()  # bf16 CUDA tensor -> fp32 numpy
    emb = np.asarray(emb, dtype=np.float32).flatten()
    n = np.linalg.norm(emb)
    return emb / n if n > 0 else emb


@app.post("/analyze")
async def analyze(req: Request):
    """音色挖掘：clips=[{name,path}] -> 转写+声纹+聚类 -> 候选簇列表。

    聚类：贪心归簇（余弦相似度 > 0.5 入簇，否则新建簇），簇按成员数降序。
    每簇代表切片 = 质量分最高且靠近簇质心的成员。
    """
    body = await req.json()
    clips: list = body.get("clips", [])
    t0 = time.time()
    entries = []
    errors: list[str] = []
    import traceback
    for c in clips:
        try:
            tr = _transcribe(c["path"])
            if not tr["text"] or len(tr["text"]) < 4:
                continue  # 无有效人声/太短的切片不参与
            emb = _speaker_embedding(c["path"])
            entries.append({"name": c["name"], "path": c["path"], "text": tr["text"],
                            "quality": tr["quality"], "emb": emb})
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[analyze] FAIL {c['name']}: {e}\n{tb}", flush=True)
            errors.append(f"{c['name']}: {e}")

    # 贪心聚类
    SIM_THRESHOLD = 0.5
    clusters: list[list[int]] = []
    for i, e in enumerate(entries):
        best, best_sim = None, SIM_THRESHOLD
        for ci, members in enumerate(clusters):
            center = np.mean([entries[j]["emb"] for j in members], axis=0)
            center /= max(np.linalg.norm(center), 1e-6)
            sim = float(np.dot(e["emb"], center))
            if sim > best_sim:
                best, best_sim = ci, sim
        if best is None:
            clusters.append([i])
        else:
            clusters[best].append(i)

    out = []
    for ci, members in enumerate(clusters):
        center = np.mean([entries[j]["emb"] for j in members], axis=0)
        center /= max(np.linalg.norm(center), 1e-6)
        ranked = sorted(members, key=lambda j: (
            -float(np.dot(entries[j]["emb"], center)), -entries[j]["quality"]))
        out.append({
            "cluster": ci,
            "size": len(members),
            "members": [entries[j]["name"] for j in ranked],
            "rep": {"name": entries[ranked[0]]["name"],
                    "path": entries[ranked[0]]["path"],
                    "text": entries[ranked[0]]["text"]},
        })
    out.sort(key=lambda c: -c["size"])
    return {"clusters": out, "kept": len(entries), "elapsed_s": round(time.time() - t0, 1),
            "errors": errors[:5]}


def _build_prompt(ref_audio: str, ref_text: str, xvec_only: bool):
    key = (ref_audio, ref_text, xvec_only)
    if key not in _PROMPT_CACHE:
        _PROMPT_CACHE[key] = MODEL.create_voice_clone_prompt(
            ref_audio=ref_audio, ref_text=ref_text or "占位", x_vector_only_mode=xvec_only)
    return _PROMPT_CACHE[key]


@app.post("/emb")
async def emb(req: Request):
    """声纹提取：path -> 归一化说话人嵌入（与克隆同一编码器）。用于相似度评估。"""
    body = await req.json()
    path = body.get("path", "")
    if not path.strip():
        return {"error": "no path"}
    try:
        e = _speaker_embedding(path)
        return {"dim": int(e.shape[0]), "emb": e.tolist()}
    except Exception as exc:
        import traceback
        print(f"[emb] FAIL {path}: {exc}\n{traceback.format_exc()}", flush=True)
        return {"error": str(exc)}


@app.post("/tts")
async def tts(req: Request):
    """动态克隆合成：text + language + ref_audio + ref_text(可空则 x-vector 模式) -> wav 字节。"""
    body = await req.json()
    text = body.get("text", "")
    language = body.get("language", "Chinese")
    ref_audio = body.get("ref_audio", "")
    ref_text = body.get("ref_text", "")
    if not text.strip() or not ref_audio:
        return Response(b"", status_code=400)
    xvec_only = not ref_text.strip()
    prompt = _build_prompt(ref_audio, ref_text, xvec_only)
    wavs, sr = MODEL.generate_voice_clone(
        text=[text], language=[language], voice_clone_prompt=prompt)
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)

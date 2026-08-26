"""Qwen3-TTS 袋鼠克隆 · 常驻推理服务（venv312 / torch2.8cu129）。

为什么独立成服务：
    m2_server 主进程运行在项目的 .venv（torch2.9+cu128，给 RVC 转换用），
    而 Qwen3-TTS 必须跑在 venv312（torch2.8+cu129，支持 RTX5060 Blackwell）。
    两者 CUDA/torch 版本不兼容，不能同进程，故由 qwen3_tts.py 懒启动本服务常驻，
    监听 8001，通过 HTTP 转发 TTS 请求，解耦互不干扰。

启动：
    D:/变声/tts_trial/venv312/Scripts/python.exe qwen3_tts_service.py
"""
import io

import torch
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.responses import Response
from qwen_tts import Qwen3TTSModel

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
REF_TEXT = "怕被其他人知道这家店给你一个"

app = FastAPI(title="Qwen3-TTS 袋鼠克隆 worker")


@app.on_event("startup")
def _load():
    global MODEL, PROMPT
    MODEL = Qwen3TTSModel.from_pretrained(
        MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
    PROMPT = MODEL.create_voice_clone_prompt(
        ref_audio=REF_AUDIO, ref_text=REF_TEXT, x_vector_only_mode=False)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tts")
async def tts(req: Request):
    """text + text_language(zh/en) -> audio/wav 字节流。"""
    body = await req.json()
    text = body.get("text", "")
    lang = str(body.get("text_language", "zh")).lower()
    language = "Chinese" if lang.startswith("zh") else "English"
    if not text.strip():
        return Response(b"", status_code=400)
    wavs, sr = MODEL.generate_voice_clone(
        text=[text], language=[language], voice_clone_prompt=PROMPT)
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)

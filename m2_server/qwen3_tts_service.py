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
import asyncio
import gc
import io
import os
import threading
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
# 微调模型缓存：同一时刻只驻留一个 custom_voice 模型（显存换模型走 LRU=1）
_ALT_MODEL: dict = {"dir": None, "model": None}
# CUDA Graph 加速引擎（fast_tts.py）：VM_FAST_TTS=0 关闭；请求体 {"fast": false} 单次关闭
FAST_TTS = os.environ.get("VM_FAST_TTS", "1") == "1"
# ICL 克隆参考音频时长上限：超长参考（>10s）既慢又会劣化，且文字稿与音频错位时
# 模型会照着长参考拖长输出、生成乱叫。超限自动截前段 + 文字稿按比例截断。
REF_MAX_S = float(os.environ.get("VM_REF_MAX_S", "10.0"))
_FAST: dict = {"eng": None, "model_id": None}
# GPU 串行锁：端点把推理放进线程池并行执行（避免堵死事件循环），GPU 调用必须互斥
_GPU_LOCK = threading.Lock()


def _get_fast_engine():
    """懒建 CUDA Graph 引擎；跟随当前驻留的基座 MODEL（换模型后自动重建）。"""
    if MODEL is None:
        return None
    if _FAST["eng"] is None or _FAST["model_id"] != id(MODEL):
        _release_fast_engine()
        try:
            from fast_tts import FastVoiceCloneEngine
            _FAST.update(eng=FastVoiceCloneEngine(MODEL), model_id=id(MODEL))
            print("[fast_tts] engine ready (CUDA Graph)", flush=True)
        except Exception as exc:
            import traceback
            print(f"[fast_tts] engine init FAIL: {exc}\n{traceback.format_exc()}",
                  flush=True)
            _FAST.update(eng=None, model_id=None)
    return _FAST["eng"]


def _release_fast_engine():
    """释放引擎（图与 StaticCache 会钉住模型权重，换驻留模型前必须调用）。"""
    if _FAST["eng"] is not None:
        _FAST.update(eng=None, model_id=None)
        gc.collect()
        torch.cuda.empty_cache()
        print("[fast_tts] engine released", flush=True)


@app.on_event("startup")
def _load():
    global MODEL
    MODEL = Qwen3TTSModel.from_pretrained(
        MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)


@app.get("/health")
def health():
    return {"status": "ok", "version": 6, "fast_tts": _FAST["eng"] is not None}


def _get_whisper():
    global WHISPER
    if WHISPER is None:
        from faster_whisper import WhisperModel
        WHISPER = WhisperModel(WHISPER_SIZE, device="cuda", compute_type="float16")
    return WHISPER


def _transcribe(path: str, vad_filter: bool = True, fast: bool = False) -> dict:
    """转写一条切片，返回文字与质量指标（logprob 越接近 0 越好，no_speech 越低越好）。

    vad_filter=False 供级联链路使用：子进程已用独立 VAD 分好块，
    whisper 内部过滤器再把短片段整段吞掉的话，调用方无法区分
    「用户真的没说话」和「被过滤器吃掉了」。

    fast=True 供级联实时链路：短句（0.5~6s）用 beam_size=1 + 免时间戳，
    解码耗时约降一半以上；切片转写/质检等离线场景仍走默认高质量参数。
    """
    kw = dict(language="zh", vad_filter=vad_filter)
    if fast:
        kw.update(beam_size=1, best_of=1, condition_on_previous_text=False,
                  without_timestamps=True)
    with _GPU_LOCK:
        segments, _info = _get_whisper().transcribe(path, **kw)
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
    with _GPU_LOCK:
        prompt = MODEL.create_voice_clone_prompt(ref_audio=path, ref_text=".",
                                                 x_vector_only_mode=True)
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
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: _analyze_blocking(body))


def _analyze_blocking(body: dict) -> dict:
    clips: list = body.get("clips", [])
    # 可调参数：sim_threshold 越高分簇越细（挖出更多不同音色）；min_cluster_size 过滤小簇噪声
    sim_threshold = float(body.get("sim_threshold") or 0.5)
    min_cluster_size = int(body.get("min_cluster_size") or 1)
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
    SIM_THRESHOLD = sim_threshold
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
    if min_cluster_size > 1:
        out = [c for c in out if c["size"] >= min_cluster_size]
    return {"clusters": out, "kept": len(entries), "elapsed_s": round(time.time() - t0, 1),
            "errors": errors[:5]}


def _trim_ref(ref_audio: str, ref_text: str) -> tuple:
    """ICL 模式参考音频超长时截前段，文字稿按比例截断（避免错位拖长/乱叫）。

    x-vector 模式只取声纹、与参考长度无关，不截。
    """
    if REF_MAX_S <= 0 or not os.path.isfile(ref_audio):
        return ref_audio, ref_text
    try:
        d, sr = sf.read(ref_audio)
    except Exception:
        return ref_audio, ref_text
    dur = len(d) / sr
    if dur <= REF_MAX_S:
        return ref_audio, ref_text
    n = int(REF_MAX_S * sr)
    tmp = os.path.join(os.path.dirname(ref_audio),
                       f"_trim_{int(time.time() * 1000)}_{os.getpid()}.wav")
    sf.write(tmp, d[:n], sr, format="WAV")
    if ref_text:
        keep = max(int(len(ref_text) * REF_MAX_S / dur), 4)
        ref_text = ref_text[:keep]
    print(f"[tts] 参考音频 {dur:.1f}s 超长，截断到 {REF_MAX_S:.1f}s（文字稿同步截断）", flush=True)
    return tmp, ref_text


def _build_prompt(ref_audio: str, ref_text: str, xvec_only: bool):
    key = (ref_audio, ref_text, xvec_only)
    if key not in _PROMPT_CACHE:
        if not xvec_only:
            ref_audio, ref_text = _trim_ref(ref_audio, ref_text)
        _PROMPT_CACHE[key] = MODEL.create_voice_clone_prompt(
            ref_audio=ref_audio, ref_text=ref_text or "占位", x_vector_only_mode=xvec_only)
    return _PROMPT_CACHE[key]


def _tts_blocking(text: str, language: str, ref_audio: str, ref_text: str,
                  gen_kwargs: dict, use_fast: bool):
    """同步 GPU 推理（由端点放进线程池执行）：fast 优先，失败回退原版 generate_voice_clone。"""
    with _GPU_LOCK:
        prompt = _build_prompt(ref_audio, ref_text, not ref_text.strip())
        if use_fast:
            eng = _get_fast_engine()
            if eng is not None:
                try:
                    wavs, sr = eng.generate(text=[text], language=[language],
                                            voice_clone_prompt=prompt, **gen_kwargs)
                    return wavs, sr, True
                except Exception as exc:
                    eng.stats["fallbacks"] += 1
                    print(f"[tts] fast path FAIL -> fallback: "
                          f"{type(exc).__name__}: {exc}", flush=True)
        wavs, sr = MODEL.generate_voice_clone(
            text=[text], language=[language], voice_clone_prompt=prompt, **gen_kwargs)
        return wavs, sr, False


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
    # 生成参数按需透传（性能调优用）。
    # do_sample=False 走贪心：省掉逐步 top-k 采样的 Python 开销；
    # 级联实时链路对首包延迟敏感时值得一试，代价是韵律多样性略降。
    gen_kwargs = {}
    for k in ("do_sample", "use_cache", "max_new_tokens", "top_k", "temperature"):
        if body.get(k) is not None:
            gen_kwargs[k] = body[k]
    use_fast = FAST_TTS and body.get("fast", True)
    # GPU 推理放线程池执行：async 端点里同步推理会堵死整个事件循环
    # （级联流式时 /health /transcribe /状态查询全卡死，坑 5）
    wavs, sr, fast_used = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _tts_blocking(text, language, ref_audio, ref_text,
                                    gen_kwargs, use_fast))
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"X-Fast-TTS": "1" if fast_used else "0"})


@app.post("/transcribe")
async def transcribe_ep(req: Request):
    """单文件转写：{path} -> {text, quality}。供微调工坊切片转写复用。"""
    body = await req.json()
    path = body.get("path", "")
    vad_filter = bool(body.get("vad_filter", True))
    fast = bool(body.get("fast", False))
    if not path or not os.path.isfile(path):
        return {"error": f"文件不存在: {path}"}
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: _transcribe(path, vad_filter, fast))
    except Exception as exc:
        import traceback
        print(f"[transcribe] FAIL {path}: {exc}\n{traceback.format_exc()}", flush=True)
        return {"error": str(exc)}


def _get_custom_model(model_dir: str):
    """加载（或取缓存的）custom_voice 微调模型。显存策略：与基座互斥驻留。

    8GB 卡上基座(3.6G)+微调模型(3.6G)+whisper 同时驻留太紧，切换时先释放另一个。
    """
    global MODEL
    if os.path.normpath(model_dir) == os.path.normpath(MODEL_DIR):
        if MODEL is None:
            MODEL = Qwen3TTSModel.from_pretrained(
                MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
        return MODEL
    if _ALT_MODEL["dir"] == os.path.normpath(model_dir) and _ALT_MODEL["model"] is not None:
        return _ALT_MODEL["model"]
    # 换模型：先卸掉现驻留的（无论基座还是旧微调）；fast 引擎钉着基座权重，必须先放
    _release_fast_engine()
    if _ALT_MODEL["model"] is not None:
        _ALT_MODEL.update(dir=None, model=None)
    if MODEL is not None:
        MODEL = None
    gc.collect()
    torch.cuda.empty_cache()
    m = Qwen3TTSModel.from_pretrained(model_dir, device_map="cuda:0", dtype=torch.bfloat16)
    _PROMPT_CACHE.clear()
    _ALT_MODEL.update(dir=os.path.normpath(model_dir), model=m)
    return m


def _tts_speaker_blocking(model_dir: str, speaker: str, text: str, language: str):
    """微调音色合成（同步 GPU 推理，线程池执行）。"""
    with _GPU_LOCK:
        m = _get_custom_model(model_dir)
        return m.generate_custom_voice(text=text, speaker=speaker, language=language)


@app.post("/tts_speaker")
async def tts_speaker(req: Request):
    """微调音色合成：{model_dir, speaker, text, language} -> wav 字节。

    与 /tts 互斥使用 GPU 模型槽位；返回头 X-Model-Slot 标明当前驻留模型。
    """
    body = await req.json()
    text = body.get("text", "")
    language = body.get("language", "Chinese")
    model_dir = body.get("model_dir", "")
    speaker = body.get("speaker", "")
    if not text.strip() or not model_dir or not speaker:
        return Response(b"", status_code=400)
    if not os.path.isdir(model_dir):
        return Response(f"model_dir 不存在: {model_dir}".encode(), status_code=400)
    wavs, sr = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _tts_speaker_blocking(model_dir, speaker, text, language))
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"X-Model-Slot": "custom" if _ALT_MODEL["dir"] else "base"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)

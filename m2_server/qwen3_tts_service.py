"""Qwen3-TTS 通用音色服务 · 常驻推理（venv312 / torch2.8cu129）。

职责（v3 重构：换 faster-qwen3-tts 真·流式后端）：
    1. /analyze  音色挖掘：对切片批量做 whisper 转写 + 说话人声纹提取 + 贪心聚类，
       按簇返回候选音色（每簇含最干净、最具代表性的切片），供前端迭代试听筛选
    2. /tts      按传入的参考音频+文字稿动态克隆合成（ICL 模式），prompt 由模型内部缓存
    3. /tts_stream 真·流式克隆合成：generate_voice_clone_streaming 逐块 yield 音频，
       首包延迟从「整句生成完」降到「首个 chunk_size 音频块」（≈chunk_size/12 秒）
    4. /health   就绪探测

为什么独立成服务：
    m2_server 主进程运行在项目的 .venv(torch2.9+cu128，给 RVC 转换用)，
    而 Qwen3-TTS 必须跑在 venv312(torch2.8+cu129，支持 RTX5060 Blackwell)。
    两者 CUDA/torch 版本不兼容，不能同进程，故由 qwen3_tts.py 懒启动本服务常驻，
    监听 8001，通过 HTTP 转发请求，解耦互不干扰。

v3 变更（2026-09-08）：
    官方 qwen_tts 0.1.1 库不暴露真·流式音频出口（non_streaming_mode 仅模拟流式文本输入）。
    改用社区加速器 faster-qwen3-tts（同 Qwen3-TTS 模型权重、Apache2.0 风格 MIT 许可）：
    其内部用 CUDA Graph 重写推理循环，generate_voice_clone_streaming 真·逐块 yield 音频，
    自带 warmup() 预热图。因此彻底移除我们自己的 fast_tts.py CUDA Graph 引擎（其依赖的
    旧 Qwen3TTSModel 内部结构在 transformers>=5 下已失效，且与 faster 的图重复冲突）。
    声纹提取改走 MODEL.model.create_voice_clone_prompt（base_model 封装层）。

启动：
    D:/变声/tts_trial/venv312/Scripts/python.exe qwen3_tts_service.py
"""
import asyncio
import gc
import io
import os
import re
import struct
import threading
import time

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from faster_qwen3_tts import FasterQwen3TTS

try:
    import config as _cfg
    MODEL_DIR = str(_cfg.QWEN_MODEL_DIR)
except ImportError:  # 直接运行 worker（cwd 非 m2_server）时回退环境变量/相对默认
    MODEL_DIR = os.environ.get(
        "VM_QWEN_MODEL_DIR",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tts_models", "qwen3-tts-1.7b-base")))
WHISPER_SIZE = os.environ.get("VM_WHISPER_SIZE", "small")

app = FastAPI(title="Qwen3-TTS 通用音色 worker (faster-qwen3-tts)")
MODEL = None
WHISPER = None
WHISPER_SR = 16000
# 微调模型缓存：同一时刻只驻留一个 custom_voice 模型（显存换模型走 LRU=1）
_ALT_MODEL: dict = {"dir": None, "model": None}
# ICL 克隆参考音频时长上限：超长参考（>10s）既慢又会劣化，且文字稿与音频错位时
# 模型会照着长参考拖长输出、生成乱叫。超限自动截前段 + 文字稿按比例截断。
REF_MAX_S = float(os.environ.get("VM_REF_MAX_S", "10.0"))
# 长文 ICL 分段上限（字符）：>0 时按句切段、逐段复用同一短风格参考合成再拼接，
# 规避"长文本单次 ICL 生成"在 8GB 显存上的崩溃/乱叫；VM_TTS_SEG_CHARS=0 关闭。
_SEG_MAX_CHARS = int(os.environ.get("VM_TTS_SEG_CHARS", "60"))
# GPU 串行锁：端点把推理放进线程池并行执行（避免堵死事件循环），GPU 调用必须互斥
_GPU_LOCK = threading.Lock()
# faster 流式 chunk 大小（codec steps）：越小首包越早，默认 12 ≈ 1s 音频/块
_STREAM_CHUNK = int(os.environ.get("VM_TTS_CHUNK", "12"))


@app.on_event("startup")
def _load():
    global MODEL
    MODEL = FasterQwen3TTS.from_pretrained(
        MODEL_DIR, device="cuda", dtype=torch.bfloat16,
        attn_implementation="sdpa", max_seq_len=4096, qwentts_use_fa=False)
    # 捕获 predictor/talker CUDA 图，消除首句/首包开销（等价于旧 fast_tts 引擎的作用）
    MODEL.warmup()


@app.get("/health")
def health():
    warmed = bool(MODEL is not None and getattr(MODEL, "_warmed_up", False))
    return {"status": "ok", "version": 7, "tts_engine": "faster-qwen3-tts",
            "warmed_up": warmed}


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
    kw = dict(language="zh", vad_filter=vad_filter,
              initial_prompt="以下是简体中文。\n")
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

    faster-qwen3-tts 把声纹 API 收进 base_model（MODEL.model.create_voice_clone_prompt），
    旧 MODEL.create_voice_clone_prompt 公开方法已移除。x_vector_only 模式只取说话人向量、
    与参考长度无关，ref_text 被忽略。
    """
    with _GPU_LOCK:
        items = MODEL.model.create_voice_clone_prompt(
            ref_audio=path, ref_text=".", x_vector_only_mode=True)
    item = items[0] if isinstance(items, (list, tuple)) else items
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


def _gen_kwargs(body: dict) -> dict:
    """透传生成参数白名单：faster API 不支持 use_cache 等旧字段，只放它认的。"""
    out = {}
    for k in ("do_sample", "max_new_tokens", "top_k", "temperature",
              "top_p", "repetition_penalty"):
        if body.get(k) is not None:
            out[k] = body[k]
    return out


def _tts_blocking(text: str, language: str, ref_audio: str, ref_text: str,
                  gen_kwargs: dict) -> tuple:
    """同步 GPU 推理（由端点放进线程池执行）：直接走 faster 原生 generate_voice_clone。

    faster 后端自带 CUDA Graph 加速（warmup 已捕获），等价旧 fast_tts 引擎且零额外依赖。
    xvec_only 模式（ref_text 为空）只取说话人声纹，与参考文字稿无关。
    """
    with _GPU_LOCK:
        wavs, sr = MODEL.generate_voice_clone(
            text=text, language=language, ref_audio=ref_audio, ref_text=ref_text,
            xvec_only=not (ref_text or "").strip(), **gen_kwargs)
        return wavs, sr, True


def _split_segments(text: str, max_chars: int = _SEG_MAX_CHARS) -> list:
    """把长文按句尾标点切分成适合单次 ICL 生成的段，超长句再硬切。

    - 参考音频是"短风格参考"，每段都复用同一个参考，保证整段音色/节奏一致；
    - 单段不过长，避免一次生成过大导致 8GB 显存崩溃或 WDDM 慢路径。
    """
    if max_chars <= 0:
        return [text]
    norm = re.sub(r"\s+", " ", (text or "")).strip()
    if not norm:
        return []
    parts = re.split(r"(?<=[。！？!?…；;,])", norm)
    segs, cur = [], ""
    for p in parts:
        if not p:
            continue
        if cur and len(cur) + len(p) > max_chars:
            segs.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        segs.append(cur)
    # 无标点的超长句：按字符硬切，保证单段长度可控
    out = []
    for s in segs:
        while len(s) > max_chars:
            out.append(s[:max_chars])
            s = s[max_chars:]
        if s:
            out.append(s)
    return out


def _tts_style_blocking(text: str, language: str, style_audio: str,
                        style_text: str, gen_kwargs: dict,
                        seg_chars: int = _SEG_MAX_CHARS):
    """风格参考 ICL 合成：音频未带文字稿则先 whisper 转写，再按段复用同一
    短风格参考逐段生成并拼接。返回 (wav, sr, n_segs, fast_used)。

    注意：不要在这里整体包 _GPU_LOCK——_transcribe 与 _tts_blocking 内部
    各自会再取 _GPU_LOCK（threading.Lock 不可重入），外层再取会死锁。
    """
    if not (style_text or "").strip():
        tr = _transcribe(style_audio)  # 自带 _GPU_LOCK
        style_text = (tr.get("text") or "").strip()
        if not style_text:
            style_text = "占位"
    sa, st = _trim_ref(style_audio, style_text)  # ≤REF_MAX_S，规避长参考 ICL 崩溃
    segs = _split_segments(text, seg_chars)
    sr = None
    wavs_all = []
    for seg in segs:
        if seg.strip() == "":
            continue
        w, s, _fu = _tts_blocking(seg, language, sa, st, gen_kwargs)
        wavs_all.append(w[0])
        sr = s
    if sr is None:
        return [np.zeros(0, dtype=np.float32)], 24000, 0, False
    # 返回 list（包一层），与端点 sf.write(buf, wavs[0], sr) 的取数组约定对齐
    return [np.concatenate(wavs_all)], sr, len(wavs_all), True


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
    """动态克隆合成：text + language + ref_audio + ref_text(可空则 x-vector 模式) -> wav 字节。

    新增「风格参考 ICL」：style_ref 提供一段≤10s 风格音频 + 可选 style_ref_text(缺省自动
    whisper 转写)，走 ICL 并按 seg_chars 分段逐段合成后拼接——用于让长文也带上指定
    情感/节奏，同时规避长文本单次 ICL 的 8GB 崩溃/乱叫。
    """
    body = await req.json()
    text = body.get("text", "")
    language = body.get("language", "Chinese")
    ref_audio = body.get("ref_audio", "")
    ref_text = body.get("ref_text", "")
    style_ref = body.get("style_ref", "")
    style_text = body.get("style_ref_text", "")
    seg_chars = int(body.get("seg_chars") or 0)
    if not text.strip():
        return Response(b"", status_code=400)
    if not (ref_audio or style_ref):
        return Response(b"", status_code=400)
    gen_kwargs = _gen_kwargs(body)
    if style_ref.strip() and os.path.isfile(style_ref):
        seg = seg_chars if seg_chars > 0 else _SEG_MAX_CHARS
        wavs, sr, n_segs, fast_used = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _tts_style_blocking(text, language, style_ref, style_text,
                                              gen_kwargs, seg))
    else:
        # GPU 推理放线程池执行：async 端点里同步推理会堵死整个事件循环
        # （级联流式时 /health /transcribe /状态查询全卡死，坑 5）
        wavs, sr, fast_used = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _tts_blocking(text, language, ref_audio, ref_text, gen_kwargs))
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"X-Fast-TTS": "1" if fast_used else "0"})


def _frame_pcm(arr: "np.ndarray") -> bytes:
    """把一段 float32 音频打成「4 字节小端长度 + 原始 float32 PCM」帧。

    流式端点逐段 yield 这种帧；客户端按长度前缀切回 float32 数组即可拼接。
    Qwen3-TTS（含 faster 后端）输出恒为 24kHz 单声道 float32，采样率写在响应头 X-Sample-Rate。
    """
    a = np.ascontiguousarray(arr, dtype=np.float32)
    return struct.pack("<I", a.nbytes) + a.tobytes()


@app.post("/tts_stream")
async def tts_stream(req: Request):
    """真·流式克隆合成：与 /tts 同入参，但 generate_voice_clone_streaming 逐块 yield 音频。

    为什么是真·流式（v3 升级）：
        官方 qwen_tts 库整段生成完才回包；faster-qwen3-tts 的 generate_voice_clone_streaming
        内部按 chunk_size（codec steps）自回归，每产出一块音频就 yield 一次——首包延迟从
        「整句生成完」降到「首个 ~chunk_size/12 秒音频块」，实时级联（relay）首句出声提前 1~2s。

    协议：application/octet-stream，每帧 = uint32(小端)长度 + float32 PCM；
        响应头 X-Sample-Rate=24000、X-Format=f32le。风格参考(style_ref)模式同样走真流式
        （ref_audio=style_ref，ref_text=style_text；style_text 空则退化为 x-vector 克隆）。
    """
    body = await req.json()
    text = body.get("text", "")
    language = body.get("language", "Chinese")
    ref_audio = body.get("ref_audio", "")
    ref_text = body.get("ref_text", "")
    style_ref = body.get("style_ref", "")
    style_text = body.get("style_ref_text", "")
    ref = ref_audio or style_ref
    rt = ref_text or style_text
    if not text.strip():
        return Response(b"", status_code=400)
    if not ref:
        return Response(b"", status_code=400)
    gen_kwargs = _gen_kwargs(body)
    # 流式块大小：优先 chunk_size，兼容旧 seg_chars（文本分段语义本版已不再适用）
    chunk_size = int(body.get("chunk_size") or body.get("seg_chars") or _STREAM_CHUNK)
    xvec_only = not (rt or "").strip()

    def gen_sync():
        with _GPU_LOCK:
            for audio_chunk, _sr, _timing in MODEL.generate_voice_clone_streaming(
                    text=text, language=language, ref_audio=ref, ref_text=rt,
                    chunk_size=chunk_size, xvec_only=xvec_only, **gen_kwargs):
                yield _frame_pcm(audio_chunk)

    return StreamingResponse(
        gen_sync(), media_type="application/octet-stream",
        headers={"X-Sample-Rate": "24000", "X-Format": "f32le",
                 "X-Fast-TTS": "1", "Cache-Control": "no-store"})


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
    faster 后端加载后需 warmup 捕获 CUDA 图。
    """
    global MODEL
    if os.path.normpath(model_dir) == os.path.normpath(MODEL_DIR):
        if MODEL is None:
            MODEL = FasterQwen3TTS.from_pretrained(
                MODEL_DIR, device="cuda", dtype=torch.bfloat16,
                attn_implementation="sdpa", max_seq_len=4096, qwentts_use_fa=False)
            MODEL.warmup()
        return MODEL
    if _ALT_MODEL["dir"] == os.path.normpath(model_dir) and _ALT_MODEL["model"] is not None:
        return _ALT_MODEL["model"]
    # 换模型：先卸掉现驻留的（无论基座还是旧微调）
    if _ALT_MODEL["model"] is not None:
        _ALT_MODEL.update(dir=None, model=None)
    if MODEL is not None:
        MODEL = None
    gc.collect()
    torch.cuda.empty_cache()
    m = FasterQwen3TTS.from_pretrained(
        model_dir, device="cuda", dtype=torch.bfloat16,
        attn_implementation="sdpa", max_seq_len=4096, qwentts_use_fa=False)
    m.warmup()
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

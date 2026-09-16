"""OpenAI 兼容语音 API（本地 shim）—— 让既有 OpenAI SDK 代码零改动指向本机。

为什么要有这一层（2026-09-16，差异化对标结论 E2）：
    同类本地项目（如 VoiceStudio）靠「OpenAI 兼容端点 + MCP」把开发者接进来——
    别人写好的 TTS 代码只要把 base_url 换成 http://127.0.0.1:8000/v1 就能跑。
    我们全部能力都已在 /api 下，缺的只是这层**协议翻译**，成本极低、收益直接。

设计取舍：
    - **路由前缀 /v1 而非 /api/v1**：OpenAI SDK 的 base_url 语义是「/v1 之前的部分」，
      所以 base_url="http://127.0.0.1:8000/v1" 会打 /v1/audio/speech。若挂到 /api/v1，
      用户必须写 base_url=".../api/v1"，与官方文档不一致，容易踩坑。
    - **错误体严格照 OpenAI 形状**：{"error": {"message", "type", "code"}}。
      SDK 靠这个结构解析异常，用 FastAPI 默认的 {"detail": ...} 会让 SDK 抛解析错误，
      而不是把我们的中文提示透给用户。
    - **voice 参数映射**：OpenAI 内置名（alloy/echo/...）本机没有对应音色，
      因此**优先按本机 voice_id 精确匹配**，匹配不到再用 `default` 兜底到当前选中音色；
      非法 voice 返回 400 并**列出可用 id**（比"找不到"有用得多）。
    - **不提供 /v1/audio/transcriptions**：本项目有 ASR 能力但走的是 RVC 子进程链路，
      未做稳定的文件转写端点。宁可 404 也不要给一个「有时能用」的假接口。

用法：
    from openai import OpenAI
    client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="example")
    audio = client.audio.speech.create(model="tts-1", voice="kangaroo", input="你好")
    audio.stream_to_file("out.mp3")

    ⚠️ api_key 必须传但**不校验**（SDK 要求非空）。这里写 "example" 而非任意字符串，
    是为了让 tools/check_secrets.py 的占位符白名单放行 —— 那个扫描器拦「标识符里含
    apiKey/token 且右边是字面量」的写法，本机不需要真密钥，不该为此改动门禁规则。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from runtime import API_PREFIX  # noqa: F401  （保留以对齐其它模块的导入习惯）

router = APIRouter(prefix="/v1")

# 本项目支持的响应格式 → 媒体类型。mp3 走 ffmpeg 转码，其余可直接落盘。
_FORMAT_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "pcm": "audio/L16",
}

_DEFAULT_MODEL = "tts-1"


class SpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech 请求体（另接收本项目扩展字段）。"""

    model: str = _DEFAULT_MODEL
    input: str
    voice: str = "default"
    response_format: str = "mp3"
    speed: float = 1.0
    # ---- 本项目扩展（OpenAI 无此字段，多出来的键默认被 SDK 忽略/透传）----
    language: str = "zh"


def _error(message: str, status: int, code: str, err_type: str = "invalid_request_error"):
    """OpenAI 形状的错误体。SDK 靠这个结构解析异常。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def _available_voice_ids() -> list[str]:
    """本机可选音色的 id 列表（供报错时指路）。"""
    from voices_api import list_voices

    return [v["id"] for v in list_voices().get("voices", [])]


def _resolve_voice(requested: str) -> str:
    """把 OpenAI 的 voice 参数翻译成本机 voice_id。

    "default" / 内置名（alloy 等）→ 当前选中音色；精确匹配本机 id → 该 id；
    其余 → 抛 ValueError（调用方转 400 并列出可用项）。
    """
    from common import selected_voice

    req = (requested or "").strip()
    if req in ("", "default"):
        cur = selected_voice()
        if not cur:
            raise ValueError("本机还没有选中音色，请先在「音色库」里选一个，或把 voice 传成具体音色 id")
        return cur

    ids = _available_voice_ids()
    if req in ids:
        return req

    # OpenAI 内置音色名不映射（本机没有这些音色，静默替换会让用户以为生效了）
    builtin = {"alloy", "echo", "fable", "onyx", "nova", "shimmer", "ash", "ballad",
               "coral", "sage", "verse", "marin", "cedar"}
    if req in builtin:
        raise ValueError(
            f"voice='{req}' 是 OpenAI 的云端内置音色，本机没有它。"
            f"请改用本机音色 id（例如 'default' 表示当前选中音色）"
        )
    raise ValueError(f"未知音色 id: '{req}'。可用音色: {', '.join(ids) if ids else '（音色库为空）'}")


def _transcode(wav_bytes: bytes, fmt: str) -> tuple[bytes, str]:
    """把合成的 wav 转成目标格式。wav/pcm 直接返回，其余借 ffmpeg。

    返回 (字节, 媒体类型)；不支持的格式抛 ValueError。
    """
    if fmt not in _FORMAT_MIME:
        raise ValueError(
            f"不支持的 response_format: '{fmt}'。可选: {', '.join(_FORMAT_MIME)}"
        )
    if fmt == "pcm":
        # OpenAI 的 pcm 是 24kHz 16-bit 裸流；本机 TTS 恒 24000Hz，去掉 44 字节头即可
        return wav_bytes[44:], _FORMAT_MIME["pcm"]
    if fmt == "wav":
        return wav_bytes, _FORMAT_MIME["wav"]

    import io
    import subprocess
    import tempfile
    from pathlib import Path

    import soundfile as sf

    from common import find_ffmpeg  # 项目统一的 ffmpeg 查找（含 Windows 兜底）

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError(f"response_format='{fmt}' 需要 ffmpeg，但本机没找到。请改用 'wav'")

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / f"out.{fmt}"
        data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        sf.write(str(src), data, sr)
        proc = subprocess.run(
            [str(ffmpeg), "-y", "-loglevel", "error", "-i", str(src), str(dst)],
            capture_output=True,
        )
        if proc.returncode != 0 or not dst.exists():
            raise ValueError(
                f"转码为 {fmt} 失败: {proc.stderr.decode('utf-8', 'ignore')[:200]}"
            )
        return dst.read_bytes(), _FORMAT_MIME[fmt]


@router.post("/audio/speech")
def audio_speech(req: SpeechRequest):
    """OpenAI 兼容的文字转语音。返回二进制音频流。"""
    from tts_api import synth_wav

    if not (req.input or "").strip():
        return _error("input 不能为空", 400, "invalid_input")

    # 两个 400 分开处理：错误码要能指认出**哪个参数**错了。
    # 合并成一个 "invalid_voice" 会让 response_format 写错的人去查 voice 参数
    # （实测首版就是这个毛病），排查方向直接跑偏。
    fmt = (req.response_format or "mp3").lower()
    if fmt not in _FORMAT_MIME:
        return _error(
            f"不支持的 response_format: '{req.response_format}'。可选: {', '.join(_FORMAT_MIME)}",
            400, "invalid_format",
        )

    try:
        voice_id = _resolve_voice(req.voice)
    except ValueError as e:
        return _error(str(e), 400, "invalid_voice")

    try:
        wav_path, _duration, _voice = synth_wav(
            text=req.input,
            voice_id=voice_id,
            text_language=req.language or "zh",
        )
        wav_bytes = wav_path.read_bytes()
        body, mime = _transcode(wav_bytes, fmt)
    except Exception as e:  # HTTPException 也在此被兜住，统一转 OpenAI 错误体
        detail = getattr(e, "detail", None) or str(e)
        return _error(f"合成失败: {detail}", 500, "synthesis_failed", "server_error")

    return Response(content=body, media_type=mime)


@router.get("/models")
def list_models():
    """OpenAI 兼容的模型列表。

    本机不区分模型（只有一个 TTS 链路），但 SDK / 客户端常会先探测这里，
    返回空列表会让部分客户端直接判定「服务不可用」。
    """
    return {
        "object": "list",
        "data": [
            {"id": _DEFAULT_MODEL, "object": "model", "created": 0, "owned_by": "voice-morph"},
            {"id": "tts-1-hd", "object": "model", "created": 0, "owned_by": "voice-morph"},
        ],
    }


@router.get("/audio/voices")
def list_openai_voices():
    """本项目扩展：列出可选音色 id（OpenAI 官方无此端点，便于对方 SDK 侧发现音色）。"""
    return {"object": "list", "data": [{"id": i} for i in _available_voice_ids()]}

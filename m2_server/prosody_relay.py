"""语气中转（prosody relay）：把源音频「换个说法」再交给 RVC。

为什么要这一层：
    RVC 是音色替换器——它保留输入音频的音高曲线（抑扬顿挫）、节奏、能量，
    只替换音色。所以输出里听到的「语气」100% 来自源音频：说话人的口音、
    犹豫、"嗯""那个"这类口头禅、甚至环境噪声都会被原样带进去。
    想让输出干净标准，必须先换掉韵律：转文字 → 由 TTS 用目标音色的参考音
    重新说一遍 → 再交给 RVC 换音色。业界称 TTS→VC 级联。

链路：源 wav --ASR--> 文本 --TTS(ref=目标音色参考音)--> 中继 wav --RVC--> 目标音色
（本模块只负责到「中继 wav」，RVC 环节由 offline_vc / cascade 各自完成）

代价：ASR + TTS 串行约 1~3s，且 ASR 出错会直接改变说话内容（实时链路已按
句切块，说错一句只影响那一句）。
"""
from pathlib import Path

import config as cfg
import qwen3_tts
from runtime import OUT, VOICEBANK

# 兜底参考音：音色没有自己的参考音频（市场下载的多数如此）时用它，
# 保证中转链路永远出声，而不是静默失败。
DEFAULT_REF = Path(cfg.ROOT) / "tts_models" / "ref" / "meituan_rat_002.wav"

# 转写文本短于此长度视为无效（whisper 对噪声可能吐出零碎字），
# 直接跳过中转走原音频，避免把一句噪声放大成一段胡话。
MIN_TEXT_CHARS = 2


def resolve_ref_audio(voice_id: str) -> str:
    """目标音色的 TTS 参考音路径（决定中转后说话的腔调）。

    优先级：音色库 reference.wav > 内置默认参考音。
    """
    if voice_id:
        ref = VOICEBANK / voice_id / "reference.wav"
        if ref.is_file():
            return str(ref)
    return str(DEFAULT_REF)


def resolve_ref_text(voice_id: str) -> str:
    """参考音对应的转写文本（有则让 TTS 对齐更准，无则走 x-vector 声纹模式）。"""
    if not voice_id:
        return ""
    meta = VOICEBANK / voice_id / "meta.json"
    try:
        import json

        return str(json.loads(meta.read_text(encoding="utf-8")).get("ref_text") or "")
    except Exception:
        return ""


def transcribe(path: str | Path) -> str:
    """ASR 转写，返回去空白后的文本；失败抛 RuntimeError（上层决定降级策略）。"""
    res = qwen3_tts.transcribe(str(path))
    if not isinstance(res, dict):
        raise RuntimeError(f"ASR 返回异常：{res!r}")
    if res.get("error"):
        raise RuntimeError(str(res["error"]))
    return str(res.get("text") or "").strip()


def relay(input_path: str | Path, voice_id: str, out_path: str | Path | None = None,
          language: str = "Chinese") -> Path:
    """把源音频重说一遍：ASR → TTS，输出中继 wav（音色仍是 TTS 的，待 RVC 转换）。

    返回输出路径；转写为空或 TTS 失败时抛 RuntimeError，由调用方决定是报错
    还是降级为「保留原语气」。
    """
    import io

    import soundfile as sf

    text = transcribe(input_path)
    if len(text) < MIN_TEXT_CHARS:
        raise RuntimeError(f"转写文本过短（{len(text)} 字），跳过语气中转")

    ref_audio = resolve_ref_audio(voice_id)
    ref_text = resolve_ref_text(voice_id)
    wav_bytes = qwen3_tts.tts(text, ref_audio, ref_text, language=language,
                              voice_id=voice_id)
    if not wav_bytes:
        raise RuntimeError("TTS 返回空音频")

    out = Path(out_path) if out_path else OUT / f"relay_{Path(input_path).stem}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    data, sr = sf.read(io.BytesIO(wav_bytes))
    sf.write(str(out), data, sr)
    return out

"""TTS 接口：Qwen3-TTS 文字→语音（克隆选中音色）。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import selected_voice, voice_ref
from history import register as history_register
from runtime import API_PREFIX, OUT

router = APIRouter(prefix=API_PREFIX)


class TTSRequest(BaseModel):
    text: str
    text_language: str = "zh"
    voice_id: str = ""
    # 风格参考 ICL + 长文分段：
    #   style_ref_voice=用哪个音色的 reference 作风格参考(安全白名单，server 解析为磁盘路径)
    #   style_ref=直接给风格音频路径(可选)；seg_chars>0 时按句分段合成
    style_ref_voice: str = ""
    style_ref: str = ""
    style_ref_text: str = ""
    seg_chars: int = 0


def synth_wav(text: str, voice_id: str = "", text_language: str = "zh",
              style_ref_voice: str = "", style_ref: str = "", style_ref_text: str = "",
              seg_chars: int = 0) -> tuple[Path, float, str]:
    """合成到 outputs/tts_*.wav，返回 (路径, 时长秒, 实际 voice_id)。

    供 `/api/tts` 与微信一键发送（`/api/wechat/send_text`）共用，避免两处各写一遍。
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    voice_id = voice_id or selected_voice()
    if not voice_id:
        raise HTTPException(status_code=400, detail="请先选择音色")
    ref, _ref_text = voice_ref(voice_id)
    try:
        from qwen3_tts import tts as qwen_tts
        # 优先 ICL 语气克隆(ref_text 有内容即走 ICL，音色/语气最贴原视频)；
        # ref_text 为空才回退纯声纹(x-vector)模式。6.4s 参考音 + 真实文字稿在 8GB 显存已验证可跑。
        # 传了 style_ref 时改用风格参考 ICL + 长文分段(seg_chars>0)，见 worker /tts。
        kw = dict(text=text, ref_audio=str(ref), ref_text=_ref_text,
                  language="Chinese" if text_language.startswith("zh") else "English",
                  voice_id=voice_id)
        if style_ref_voice:
            sref, srtext = voice_ref(style_ref_voice)  # 安全白名单：非法/不存在抛 400/404
            kw["style_ref"] = str(sref)
            if srtext:
                kw["style_ref_text"] = srtext
            if seg_chars > 0:
                kw["seg_chars"] = seg_chars
        elif style_ref:
            kw["style_ref"] = style_ref
            if style_ref_text:
                kw["style_ref_text"] = style_ref_text
            if seg_chars > 0:
                kw["seg_chars"] = seg_chars
        wav_bytes = qwen_tts(**kw)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS 失败: {e}")
    fname = f"tts_{int(time.time() * 1000)}.wav"
    out = OUT / fname
    out.write_bytes(wav_bytes)
    import soundfile as sf
    d, sr = sf.read(str(out))
    duration_s = round(len(d) / sr, 1)
    return out, duration_s, voice_id


@router.post("/tts")
def tts_endpoint(req: TTSRequest):
    """文字→语音：按 voice_id 音色克隆合成（不传 voice_id 则用当前选中音色，都没有则报错）。
    结果保存为 outputs/tts_*.wav 并返回 URL，便于前端下载与历史持久化。"""
    out, duration_s, voice_id = synth_wav(
        req.text, req.voice_id, req.text_language,
        req.style_ref_voice, req.style_ref, req.style_ref_text, req.seg_chars,
    )
    fname = out.name
    history_register("tts", voice_id, fname, f"/api/media/outputs/{fname}",
                     duration_s, input_text=req.text)
    return JSONResponse({
        "ok": True,
        "voice_id": voice_id,
        "url": f"/api/media/outputs/{fname}",
        "duration_s": duration_s,
    })

"""TTS 接口：Qwen3-TTS 文字→语音（克隆选中音色）+ 输字变声链路自检。

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


# ---------------- 输字变声链路自检（与 /audio/send_chain 同规格） ----------------
# 为什么 TTS 侧也要一份：首页 STEP 0 首推的就是「输字让它说」，而这条链路
# 此前没有任何自检 —— 用户被推荐去的那条路，恰恰是故障时最没有指引的那条。
#
# TTS 的故障面与实时变声完全不同（后者查声卡，这里查引擎/参考音/磁盘）：
#   1. TTS 模型与分词器是否就位（缺了 worker 起不来，报错还要等 30 秒）
#   2. worker 进程是否在跑（不在 = 首次合成要等加载，说明不是故障但要知道）
#   3. 当前音色是否有参考音（市场装的 RVC 权重**没有** reference，不能 TTS）
#   4. 输出目录是否可写（磁盘满/权限问题时合成会失败在最后一步）
# 只读检查，不起 worker（探测进程是为了给出准确提示，不为自检付 30 秒加载代价）。


def build_tts_chain_report(model_ok: bool, model_detail: str, tokenizer_ok: bool,
                           worker_alive: bool, voice_id: str, ref_ok: bool,
                           ref_detail: str, out_writable: bool, out_detail: str,
                           tokenizer_detail: str = "") -> dict:
    """把各项探测结果转成检查项（纯函数，可单测）。

    返回 {"ok","all_ok","items"}，items 与 /audio/send_chain 同构
    （key/ok/warn/label/detail/hint），前端可复用同一套渲染。

    detail 的拼装规则：全好时用 model_detail（正常路径）；
    否则**由本函数**拼出缺什么 —— 不让调用方决定，避免出现
    「分词器缺失但 detail 显示的是模型正常路径」这种自相矛盾的输出。
    tokenizer_detail 可选，用于告知分词器的期望路径。
    """
    items: list[dict] = []

    # 1) 引擎模型（缺失 = 硬阻断，合成一定失败）
    if model_ok and tokenizer_ok:
        items.append({
            "key": "tts_models", "ok": True, "label": "语音引擎模型",
            "detail": model_detail, "hint": "",
        })
    else:
        miss = []
        if not model_ok:
            miss.append("模型缺失或没有 config.json")
        if not tokenizer_ok:
            miss.append("分词器缺失" + (f"（{tokenizer_detail}）" if tokenizer_detail else ""))
        items.append({
            "key": "tts_models", "ok": False, "label": "语音引擎模型",
            "detail": "；".join(miss),
            "hint": "到设置 →「模型与引擎配置」里检查语音合成模型目录；缺文件时按「下载指引」补齐。",
        })

    # 2) 引擎进程（不在跑不是故障：首次合成会自动加载，只是要等）
    if not (model_ok and tokenizer_ok):
        # 模型都没有，讨论进程没意义 —— 上面的红项已经给出唯一该做的动作
        pass
    elif worker_alive:
        items.append({
            "key": "tts_worker", "ok": True, "label": "语音引擎进程",
            "detail": "已加载并常驻，合成可直接开始", "hint": "",
        })
    else:
        items.append({
            "key": "tts_worker", "ok": False, "warn": True, "label": "语音引擎进程",
            "detail": "尚未加载（首次合成时自动加载，约需 20~30 秒）",
            "hint": "首次使用慢是正常的；加载完成后同一次会话内后续合成立刻返回。",
        })

    # 3) 当前音色的参考音（市场模型没有 reference → 这条链路用不了）
    if not voice_id:
        items.append({
            "key": "voice_ref", "ok": False, "label": "当前音色",
            "detail": "还没有选中任何音色",
            "hint": "到「音色库」挖掘并保存一个音色，或在首页试听预置音色后安装（预置音色是实时变声模型，不能输字合成）。",
        })
    elif ref_ok:
        items.append({
            "key": "voice_ref", "ok": True, "label": f"当前音色（{voice_id}）",
            "detail": ref_detail, "hint": "",
        })
    else:
        items.append({
            "key": "voice_ref", "ok": False, "label": f"当前音色（{voice_id}）",
            "detail": ref_detail or "该音色没有参考音频",
            "hint": "输字合成需要音色自带参考音频（reference.wav）。从市场安装的音色是实时变声模型，"
                    "只能开麦变声；想输字合成请先在「音色库」采集或挖掘一个音色。",
        })

    # 4) 输出目录可写（合成最后一步落盘，磁盘满时在这里失败）
    if out_writable:
        items.append({
            "key": "out_dir", "ok": True, "label": "结果保存目录",
            "detail": out_detail, "hint": "",
        })
    else:
        items.append({
            "key": "out_dir", "ok": False, "label": "结果保存目录",
            "detail": out_detail or "目录不可写",
            "hint": "检查磁盘剩余空间；或在设置 →「存储占用与清理」里清理后重试。",
        })

    return {"ok": True, "all_ok": all(i["ok"] for i in items), "items": items}


def _probe_out_dir() -> tuple[bool, str]:
    """探输出目录是否可写（真写一个临时文件再删，比 os.access 可靠）。"""
    probe = OUT / ".tts_write_probe"
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink()
        return True, f"{OUT}（可写）"
    except Exception as e:
        return False, f"{OUT} 不可写：{e}"


@router.get("/tts/send_chain")
def tts_send_chain():
    """输字变声链路自检（只读）：引擎模型 / 引擎进程 / 当前音色参考音 / 输出目录。"""
    import config as cfg

    model_ok = cfg.QWEN_MODEL_DIR.exists() and (cfg.QWEN_MODEL_DIR / "config.json").exists()
    tokenizer_ok = cfg.QWEN_TOKENIZER_DIR.exists()
    # 全好时 detail 显示正常路径；缺项时的说明由 build_tts_chain_report 自己拼
    model_detail = str(cfg.QWEN_MODEL_DIR) if model_ok else ""

    try:
        import qwen3_tts
        worker_alive = bool(qwen3_tts.worker_alive())
    except Exception:
        # 依赖缺失（如干净 runner 没有 fastapi 之外的组件）时按「未加载」上报，
        # 而不是让整个自检 500 —— 自检本身不该成为新的故障点。
        worker_alive = False

    voice_id = selected_voice()
    ref_ok, ref_detail = False, ""
    if voice_id:
        try:
            ref, ref_text = voice_ref(voice_id)
            ref_ok = True
            ref_detail = f"参考音就位：{ref.name}（{ref.parent}）"
            if ref_text:
                ref_detail += " · 带文字稿，语气克隆效果更好"
        except HTTPException as e:
            ref_ok = False
            ref_detail = str(e.detail)

    out_writable, out_detail = _probe_out_dir()
    return build_tts_chain_report(
        model_ok, model_detail, tokenizer_ok, worker_alive,
        voice_id, ref_ok, ref_detail, out_writable, out_detail,
        tokenizer_detail=str(cfg.QWEN_TOKENIZER_DIR),
    )

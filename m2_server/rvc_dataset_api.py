"""RVC 训练集接口：Qwen3-TTS 批量生成语料 / 列表 / 导出到整合包 / 模型状态。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""
import re
import shutil
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config as cfg
from common import selected_voice, voice_ref
from rvc_common import find_pth
from runtime import API_PREFIX, pipeline_lock

router = APIRouter(prefix=API_PREFIX)

RVC_DATASET_DIR = cfg.MEDIA_DIR / "rvc_dataset"
# RVC 整合包与训练产物路径集中由 config 定义（环境变量可覆盖）
RVC_EXPORT_DIR = cfg.RVC_EXPORT_DIR
RVC_DEFAULT_EXP = cfg.RVC_DEFAULT_EXP

# 训练语料模板：默认从 data/rvc_texts.txt 读取（可经 VM_RVC_TEXTS_FILE 覆盖为任意音色专用语料）；
# 文件缺失时回退内置 20 句，保证历史行为不变。
RVC_TEXTS = cfg.load_rvc_texts()

RVC_GEN_STATE = {"running": False, "total": len(RVC_TEXTS), "done": 0,
                 "current": "", "error": ""}


@router.get("/rvc/dataset")
def list_rvc_dataset(voice_id: str | None = None):
    """列出已生成的训练语料；传 voice_id 时只看该音色的语料。"""
    import soundfile as sf
    prefix = f"rvc_{re.sub(r'[^0-9A-Za-z_-]', '_', voice_id)}_" if voice_id else None
    items = []
    if RVC_DATASET_DIR.exists():
        files = sorted(RVC_DATASET_DIR.glob("*.wav"))
        for f in files if not prefix else [f for f in files if f.name.startswith(prefix)]:
            try:
                d, sr = sf.read(str(f))
                items.append({"name": f.name, "duration_s": round(len(d) / sr, 2),
                              "size_kb": round(f.stat().st_size / 1024, 1)})
            except Exception:
                continue
    return {"items": items, "export_dir": str(RVC_EXPORT_DIR)}


class RvcGenerateReq(BaseModel):
    """指定用哪个音色生成语料；不传则用当前选中音色。"""
    voice_id: str | None = None


@router.post("/rvc/dataset/generate")
def rvc_generate_dataset(req: RvcGenerateReq | None = None):
    """后台线程：用 Qwen3-TTS 按指定音色逐句生成 20 句语料到 media/rvc_dataset/。

    语料文件名前缀取音色 ID（安全化），便于多音色区分。
    """
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料正在生成中，请稍候")
    body = req or RvcGenerateReq()
    voice_id = (body.voice_id or selected_voice()).strip()
    if not voice_id:
        raise HTTPException(400, "未指定 voice_id，且没有已选中的音色")
    ref_audio, ref_text = voice_ref(voice_id)
    prefix = "rvc_" + re.sub(r"[^0-9A-Za-z_-]", "_", voice_id)
    RVC_DATASET_DIR.mkdir(parents=True, exist_ok=True)

    def _job():
        RVC_GEN_STATE.update(running=True, done=0, current="", error="")
        try:
            from qwen3_tts import tts as qwen_tts
            for i, text in enumerate(RVC_TEXTS, 1):
                RVC_GEN_STATE["current"] = text
                wav = qwen_tts(text, ref_audio=str(ref_audio), ref_text=ref_text)
                (RVC_DATASET_DIR / f"{prefix}_{i:03d}.wav").write_bytes(wav)
                RVC_GEN_STATE["done"] = i
        except Exception as e:
            RVC_GEN_STATE["error"] = str(e)
        finally:
            RVC_GEN_STATE["running"] = False
            RVC_GEN_STATE["current"] = ""

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "started": True, "total": len(RVC_TEXTS), "voice_id": voice_id}


@router.get("/rvc/dataset/status")
def rvc_dataset_status():
    with pipeline_lock:
        return dict(RVC_GEN_STATE)


@router.post("/rvc/dataset/export")
def export_rvc_dataset(exp_name: str | None = None, voice_id: str | None = None):
    """把语料同步到 RVC 整合包（dataset_raw/<exp>/ 与训练读取的 dataset/<exp>/）。

    传 voice_id 时只导出该音色自己的语料（按生成时的文件名前缀过滤），
    避免把 A 音色的语料混进 B 音色的训练集 —— 这是"选了音色却不像"的常见根因。
    """
    if RVC_GEN_STATE["running"]:
        raise HTTPException(400, "语料生成进行中，完成后才能导出")
    if not RVC_DATASET_DIR.exists():
        raise HTTPException(400, "还没有训练语料，请先生成")
    exp = exp_name or voice_id or RVC_DEFAULT_EXP
    # 前缀后必须紧跟 "_"，否则 rvc_mei 会误匹配到 rvc_meituan_rat 的语料
    prefix = f"rvc_{re.sub(r'[^0-9A-Za-z_-]', '_', voice_id)}_" if voice_id else None
    files = [f for f in RVC_DATASET_DIR.glob("*.wav")
             if not prefix or f.name.startswith(prefix)]
    if not files:
        raise HTTPException(400, f"音色 [{voice_id}] 还没有语料，请先生成" if voice_id
                            else "还没有训练语料，请先生成")

    dest = RVC_EXPORT_DIR.parent / exp
    # 训练驱动默认读取 dataset/<exp>/（与 logs/<exp>/ 同级，见 cfg.rvc_exp_dirs）
    train_ds = cfg.rvc_exp_dirs(exp)[1]
    for d in (dest, train_ds):
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, d / f.name)
    return {"ok": True, "copied": len(files), "dest": str(dest),
            "train_dataset_dir": str(train_ds), "exp": exp}


@router.get("/rvc/model")
def rvc_model_status(exp_name: str | None = None):
    """RVC 模型训练完成状态（供前端「模型已就绪」卡片展示）；可按音色 ID 查询。"""
    exp = exp_name or RVC_DEFAULT_EXP
    weights_dir = cfg.rvc_exp_dirs(exp)[0]
    pth = find_pth(exp, weights_dir)
    idx = next(weights_dir.glob("added_*.index"), None) if weights_dir.exists() else None
    trained = pth is not None and idx is not None
    # 语料数按该实验自己的训练集目录统计（与 /rvc/voices 口径一致），
    # 否则多个音色共用 media/rvc_dataset 会互相虚报。
    ds_dir = cfg.rvc_exp_dirs(exp)[1]
    dataset_count = len(list(ds_dir.glob("*.wav"))) if ds_dir.exists() else 0
    return {
        "exp": exp,
        "trained": trained,
        "pth_exists": pth is not None,
        "index_exists": idx is not None,
        "dataset_count": dataset_count,
        "weights_dir": str(weights_dir),
        "dataset_dir": str(ds_dir),
    }

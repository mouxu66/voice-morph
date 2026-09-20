"""音色库接口：音色清单 / 建库（含自动优选）/ 删除 / 音色包导出导入。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""

import asyncio
import contextlib
import json
import logging
import re
import shutil
from pathlib import Path

import config as cfg
from common import MAX_UPLOAD_BYTES, is_valid_voice_id, selected_voice
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from runtime import API_PREFIX, CLIPS_DIR, RAW_DIR, VIDEO_SUFFIXES, VOICEBANK, clip_prefix
from rvc_common import exp_display_name, exp_license, exp_snapshot, exp_source

router = APIRouter(prefix=API_PREFIX)
LOG = logging.getLogger(__name__)


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text("utf-8"))
    except Exception:
        return {}


def _market_preview_url(exp: str) -> str:
    """市场音色已生成试听时返回播放地址（outputs/market/<id>_preview.wav）。"""
    try:
        if (cfg.OUTPUTS_DIR / "market" / f"{exp}_preview.wav").exists():
            return f"/api/media/outputs/market/{exp}_preview.wav"
    except Exception:  # noqa: BLE001
        pass
    return ""


@router.get("/voices")
def list_voices():
    """音色库清单（合并两个来源，与 /rvc/voices 保持一致）：

    1. media/voicebank/<id>/reference.wav —— 音色库档案（有参考音频）；
    2. <RVC_ROOT>/logs/<exp>/ —— RVC 实验目录（有训练产物或语料的）。

    前端音色页据此展示统一视图；RVC 模型音色额外携带 model_ready / trained_at 等字段。
    """
    from pydub import AudioSegment

    items: dict[str, dict] = {}

    # 来源 1：音色库档案
    for d in VOICEBANK.iterdir():
        ref = d / "reference.wav"
        if not d.is_dir() or not ref.exists():
            continue
        # 中文名优先级：音色库 meta.json > 市场 source.json > 实验 meta.json > 目录名
        meta = _read_meta(meta_path) if (meta_path := d / "meta.json").exists() else {}
        display = str(meta.get("display_name") or "") or exp_display_name(d.name)
        items[d.name] = {
            "id": d.name,
            "display_name": display,
            "reference": ref.name,
            "duration_s": round(len(AudioSegment.from_wav(str(ref))) / 1000, 1),
            "kind": meta.get("kind") or "clone",
            "has_reference": True,
            "source": exp_source(d.name),
            **exp_license(d.name),
            **exp_snapshot(d.name),
        }

    # 来源 2：RVC 实验目录（有模型/语料但不在音色库里的）
    rvc_logs = cfg.RVC_ROOT / "logs"
    if rvc_logs.exists():
        for d in rvc_logs.iterdir():
            if not d.is_dir() or d.name in items:
                continue
            snap = exp_snapshot(d.name)
            # 无训练产物也无语料的噪音目录不展示
            if not (snap["pth_exists"] or snap["index_exists"] or snap["dataset_count"]):
                continue
            items[d.name] = {
                "id": d.name,
                "display_name": exp_display_name(d.name),
                "reference": "",
                "duration_s": 0,
                "kind": "rvc_model",
                "has_reference": False,
                "source": exp_source(d.name),
                "preview_url": _market_preview_url(d.name),
                **exp_license(d.name),
                **snap,
            }

    voices = sorted(
        items.values(),
        key=lambda v: (not v.get("model_ready"), not v.get("has_reference", True), v["id"]),
    )
    return {"voices": voices}


def _read_qc_json(exp: str):
    """读取该音色的质检结果（outputs/qc/<exp>.json）；没有或损坏时返回 None。"""
    f = cfg.OUTPUTS_DIR / "qc" / f"{exp}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        LOG.warning("[qc] 读取质检结果 %s 失败: %s", f, e)
        return None


# /rvc/voices 从 rvc_live.py 下沉到这里（B 类·公共端点归宿，设计稿 §4.1）：
# 它被三个页面当「RVC 音色候选」消费（实时变声页、离线变声选音色、试音间的音色
# 下拉会连同市场音色一起用），实现却一直住在 sound.rvc-live 里 —— 关掉实时变声后
# 这条公共数据端点跟着消失：离线变声静默没音色可选，试音间刷新音色整片报错。
# 音色数据层的归属写在 §4.1：core.voices =「音色库是几乎所有能力的公共数据层」。
@router.get("/rvc/voices")
def rvc_voices():
    """RVC 变声可选音色清单（合并两个来源，与原 rvc_live.py 实现等价）：

    1. 音色库 media/voicebank/<id>/reference.wav —— 能生成语料、能训练的音色；
    2. RVC 整合包 logs/<exp>/ 下训练出权重+索引的实验 —— 能直接实时变声的模型。

    两者以「音色 ID == 实验名」对齐，前端据此展示每个音色走到哪一步。
    """
    items: dict[str, dict] = {}

    bank = VOICEBANK
    if bank.exists():
        for d in bank.iterdir():
            if not d.is_dir() or not (d / "reference.wav").exists():
                continue
            # 中文名来源：meta.json 的 display_name；没有/损坏时回退目录名
            meta = _read_meta(d / "meta.json")
            display = d.name
            if meta:
                display = str(meta.get("display_name") or "") or exp_display_name(d.name)
            items[d.name] = {
                "id": d.name,
                "display_name": display,
                "has_reference": True,
                "qc": _read_qc_json(d.name),
                "source": exp_source(d.name),
                **exp_snapshot(d.name),
            }

    logs = cfg.RVC_ROOT / "logs"
    if logs.exists():
        for d in logs.iterdir():
            if not d.is_dir() or d.name in items:
                continue
            snap = exp_snapshot(d.name)
            # 音色库里没有、又没训练产物也没语料的目录属于噪音，不展示
            if not (snap["pth_exists"] or snap["index_exists"] or snap["dataset_count"]):
                continue
            items[d.name] = {
                "id": d.name,
                "display_name": exp_display_name(d.name),
                "has_reference": False,
                "qc": _read_qc_json(d.name),
                "source": exp_source(d.name),
                **snap,
            }

    voices = sorted(
        items.values(), key=lambda v: (not v["model_ready"], not v["has_reference"], v["id"])
    )
    # active_exp 的真相在 rvc_live 的内存态（最近训练/刚启动的实时变声实验），无落盘。
    # 尽力向它打听：实时变声开着时模块必然已加载（零成本）；实时变声被关/依赖缺失时
    # import 失败就回退默认实验 —— 反正没人在跑实时变声，这个字段没有用户可感知的语义。
    try:
        from rvc_live import _active_exp

        active_exp = _active_exp()
    except Exception:
        active_exp = cfg.RVC_DEFAULT_EXP
    return {
        "voices": voices,
        "active_exp": active_exp,
        "default_exp": cfg.RVC_DEFAULT_EXP,
        "rvc_root": str(cfg.RVC_ROOT),
        "rvc_ready": cfg.RVC_ROOT.exists()
        and (cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe").exists(),
    }


@router.delete("/voicebank/{voice_id}")
async def delete_voice(voice_id: str):
    """删除音色：音色库档案不在时，回退删 RVC 实验目录。

    音色列表有两个来源（见 list_voices）：voicebank/<id>/ 档案、
    RVC_ROOT/logs/<id>/ 实验目录（训练产物/语料）。旧实现只删 voicebank，
    删 RVC 模型条目（如 kangaroo_clean/mute）必 404——前端只见"删除失败"。
    Windows 坑：目录内文件被 worker/播放器占用时 rmtree 会 PermissionError，
    短重试（句柄释放有延迟）+ 失败返回 500 并点名被锁文件。
    """
    if not is_valid_voice_id(voice_id):
        raise HTTPException(400, "音色 ID 非法")
    source = "voicebank"
    d = VOICEBANK / voice_id
    if not d.exists():
        d = cfg.RVC_ROOT / "logs" / voice_id
        if not d.exists():
            raise HTTPException(404, f"音色 [{voice_id}] 不存在")
        source = "rvc_logs"
    # 删除的是当前选中音色 → 先清选中记录，避免 selected_voice.json 悬空引用
    if selected_voice() == voice_id:
        with contextlib.suppress(OSError):
            (VOICEBANK / "selected_voice.json").unlink()
    # rmtree 是阻塞 IO，丢进线程池避免卡住事件循环；Windows 句柄释放有延迟，重试 3 次
    last_err: OSError | None = None
    for attempt in range(3):
        try:
            await run_in_threadpool(shutil.rmtree, d)
            return {"ok": True, "source": source}
        except OSError as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(0.6)
    leftovers = sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))[:3]
    raise HTTPException(
        500,
        f"删除失败：{last_err}。文件可能正被占用（如 {', '.join(leftovers)}），"
        "请先停止相关页面的播放/生成，或重启桌面端后重试。",
    )


def _voicebank_guidance() -> str:
    """建库自动优选拿不到切片时，结合已有质检结果给「原因 + 下一步建议」（P2-1）。"""
    try:
        import clip_qc

        items = clip_qc.load_all().values()
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return "没有可自动优选的切片：请先对素材运行流水线，再对它做一次切片质检。"
    total = len(items)
    from collections import Counter

    grades = Counter(it.get("grade") for it in items)
    ok = grades.get("A", 0) + grades.get("B", 0)
    if ok == 0:
        return (
            f"质检无可用切片（共 {total} 条，D 级 {grades.get('D', 0)} 条，"
            f"多为他人声/伴奏残留）：建议换一段目标说话人清晰的素材后重试。"
        )
    dur = sum(it.get("duration_s", 0.0) for it in items if it.get("grade") in ("A", "B"))
    return (
        f"质检可用切片仅 {ok}/{total} 条，总时长 {dur:.1f}s，不足以自动优选："
        f"建议增加素材时长，或调低自动优选目标秒数后重试。"
    )


@router.post("/voicebank")
async def create_voicebank(voice_id: str, request: Request):
    """用勾选的片段生成参考音频。body: clips=name1&clips=name2...

    自动优选：不传 clips 而传 auto=1 时，按切片质检分数从高到低自动挑够
    target_s 秒（默认 30，精细档建议 60）——对应 P1-1 的"建库自动优选"。
    """
    from pydub import AudioSegment

    form = await request.form()
    clips = form.getlist("clips")
    auto = str(form.get("auto") or "") in ("1", "true", "True")
    enhance = str(form.get("enhance") or "") in ("1", "true", "True")
    target_s = float(form.get("target_s") or 0) or 30.0
    if not clips and auto:
        try:
            import clip_qc

            clips = clip_qc.recommend(target_s)[0]  # 只要片段名，推荐总时长此处不用
        except Exception:  # noqa: BLE001
            clips = []
        if not clips:
            raise HTTPException(400, _voicebank_guidance())
    if not clips:
        raise HTTPException(400, "请至少提供一个片段名")
    if not is_valid_voice_id(voice_id):
        raise HTTPException(400, "音色 ID 非法")

    out_dir = VOICEBANK / voice_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "reference.wav"

    merged = AudioSegment.silent(duration=300)
    for name in clips:
        p = CLIPS_DIR / f"{name}.wav"
        if not p.exists():
            raise HTTPException(404, f"片段不存在: {name}")
        if enhance:
            # P2-5：可选 DeepFilterNet 模型增强，进一步清掉切片残留噪声/BGM
            try:
                from audio_enhance import enhance_file

                tmp = out_dir / f"_enh_{name}.wav"
                enhance_file(p, tmp)
                seg = AudioSegment.from_wav(str(tmp))
                with contextlib.suppress(Exception):
                    tmp.unlink()
            except Exception:
                seg = AudioSegment.from_wav(str(p))
        else:
            seg = AudioSegment.from_wav(str(p))
        merged += seg + AudioSegment.silent(duration=300)

    merged = merged.set_channels(1).set_frame_rate(22050)
    merged.export(str(out), format="wav")

    # 记录片段清单（供 demo_convert 按片段提取音色向量）
    (out_dir / "clips.txt").write_text("\n".join(clips), encoding="utf-8")
    # 把来源素材写进 meta（素材库「已用于」徽标的持久依据）
    meta_p = out_dir / "meta.json"
    meta: dict = {}
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if RAW_DIR.exists():
        sources = sorted(
            f.name
            for f in RAW_DIR.iterdir()
            if f.suffix.lower() in VIDEO_SUFFIXES
            and any(
                name.startswith(clip_prefix(f.stem)) or name.startswith(f.stem[:12])
                for name in clips
            )
        )
        if sources:
            meta["sources"] = sources
    with contextlib.suppress(Exception):
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # 删除旧的音色向量缓存，下次自动重新提取
    (out_dir / "reference_se.npy").unlink(missing_ok=True)

    return {
        "ok": True,
        "voice_id": voice_id,
        "duration_s": round(len(merged) / 1000, 1),
        "auto": auto,
        "picked": len(clips),
    }


# ---------------- 音色包导出 / 导入 ----------------

# 音色包内允许携带的档案文件（白名单，杜绝 zip-slip 与超大缓存）
_PACK_FILES = ("reference.wav", "ref_text.txt", "meta.json", "clips.txt")


@router.get("/voicebank/{voice_id}/export")
def export_voice_pack(voice_id: str, include_rvc: bool = False):
    """打包音色档案为 zip（reference.wav + 文字稿 + meta + 片段清单）。
    include_rvc=true 时附带该音色的 RVC 权重与索引（若已训练），导入端可直接实时变声。"""
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="音色 ID 非法")
    d = VOICEBANK / voice_id
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"音色 [{voice_id}] 不存在")
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # meta.json 里补写 voice_id，导入端据此还原档案
        meta = _read_meta(d / "meta.json")
        meta["voice_id"] = voice_id
        zf.writestr("voicepack/meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for name in _PACK_FILES:
            if name == "meta.json":
                continue
            p = d / name
            if p.exists():
                zf.write(p, f"voicepack/{name}")
        if include_rvc:
            log_dir = cfg.RVC_ROOT / "logs" / voice_id
            if log_dir.exists():
                pth = log_dir / f"{voice_id}.pth"
                if not pth.exists():  # 回退训练自动产出的 G_*.pth（与 rvc_live 的规则一致）
                    pth = next(log_dir.glob("G_*.pth"), None)
                if pth is not None and pth.exists():
                    zf.write(pth, f"voicepack/rvc/{pth.name}")
                for idx in log_dir.glob("added_*.index"):
                    zf.write(idx, f"voicepack/rvc/{idx.name}")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="voicepack_{voice_id}.zip"'},
    )


@router.post("/voicebank/import")
async def import_voice_pack(file: UploadFile = File(...), overwrite: bool = False):
    """导入音色包 zip：还原为 voicebank/<voice_id>/ 音色档案。同名默认拒绝，overwrite=1 覆盖。
    注意：微调模型（ft_model，含 3.6G 基座硬链接）不随包携带，导入后自动降级为 x-vector 声纹克隆。"""
    import io
    import zipfile

    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"音色包过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝导入"
        )
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"音色包过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝导入"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip 音色包")
    names = zf.namelist()
    if "voicepack/reference.wav" not in names or "voicepack/meta.json" not in names:
        raise HTTPException(
            status_code=400, detail="缺少 voicepack/reference.wav 或 meta.json，不是音色包"
        )
    try:
        meta = json.loads(zf.read("voicepack/meta.json").decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="meta.json 解析失败")
    voice_id = str(meta.get("voice_id") or "").strip()
    if not is_valid_voice_id(voice_id):
        raise HTTPException(status_code=400, detail="包内 voice_id 非法")
    dest = VOICEBANK / voice_id
    if dest.exists() and not overwrite:
        raise HTTPException(
            status_code=409, detail=f"音色 [{voice_id}] 已存在；如需覆盖请勾选覆盖导入"
        )

    await run_in_threadpool(lambda: shutil.rmtree(dest, True) if dest.exists() else None)
    # 包内 rvc/ 权重还原到 RVC 整合包 logs/<voice_id>/（实时变声直接可用）；整合包缺失则跳过并提示
    rvc_log_dir = cfg.RVC_ROOT / "logs" / voice_id
    rvc_restored = 0
    for name in names:
        if not name.startswith("voicepack/") or name.endswith("/"):
            continue
        rel = name[len("voicepack/") :]
        # 逐项防路径穿越：档案文件白名单 + rvc/ 一层子目录
        is_rvc = bool(re.match(r"^rvc/[A-Za-z0-9_.-]+$", rel))
        if not is_rvc and rel not in _PACK_FILES:
            continue
        target = (rvc_log_dir / rel[len("rvc/") :]) if is_rvc else (dest / rel)
        if is_rvc:
            if not cfg.RVC_ROOT.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(name))
        if is_rvc:
            rvc_restored += 1

    kind = str(meta.get("kind") or "clone")
    if kind == "finetuned" and not (dest / "ft_model" / "model.safetensors").exists():
        # 包里没带微调权重：降级为普通声纹音色，避免 /tts 去加载不存在的模型目录
        meta["kind"] = "clone"
        meta.pop("model_dir", None)
        meta["import_note"] = "微调权重未随包携带，已降级为声纹克隆"
    kind = str(meta.get("kind") or "clone")
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "voice_id": voice_id,
        "kind": kind,
        "rvc_files": rvc_restored,
        "display_name": meta.get("display_name") or voice_id,
    }

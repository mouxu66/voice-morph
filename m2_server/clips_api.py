"""切片接口：切片列表 / 说话人分离 / 质量打分 / 导出 RVC 训练集 zip。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""

from pathlib import Path

import config as cfg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from runtime import API_PREFIX, CLIPS_DIR, clip_prefix

router = APIRouter(prefix=API_PREFIX)


@router.get("/clips")
def list_clips():
    from pydub import AudioSegment

    try:
        import clip_qc

        qc = clip_qc.load_all()
    except Exception:  # 质检结果读不到不影响列表本身（无质检时前端退化为按响度筛选）
        qc = {}
    clips = []
    for f in sorted(CLIPS_DIR.glob("*.wav")):
        try:
            a = AudioSegment.from_wav(str(f))
        except Exception:
            continue
        item = {
            "name": f.stem,
            "duration_s": round(len(a) / 1000, 1),
            "loudness_dbfs": round(a.max_dBFS, 1),
        }
        if f.stem in qc:
            item["qc"] = qc[f.stem]
        clips.append(item)
    return {"clips": clips}


def _locate_vocals(stem: str) -> Path | None:
    """定位某素材的纯人声轨：demucs 分离产物优先，其次 vocals/ 下的 44.1k 升轨。

    素材 stem 与切片前缀可能不同（去掉非法字符/截断），用多候选匹配。
    """
    cands = {stem, clip_prefix(stem), stem[:12]} if stem else set()
    demucs = cfg.MEDIA_DIR / "demucs_out"
    if demucs.is_dir():
        for model in demucs.iterdir():
            if not model.is_dir():
                continue
            for sub in model.iterdir():
                if not sub.is_dir():
                    continue
                if any(sub.name == c or sub.name.startswith(c) for c in cands):
                    v = sub / "vocals.wav"
                    if v.exists():
                        return v
    for c in cands:
        v = cfg.MEDIA_DIR / "vocals" / f"{c}.wav"
        if v.exists():
            return v
    return None


@router.post("/clips/diarize")
def diarize_clips(file: str = Query(..., description="素材文件名或切片前缀，用于定位该素材的切片")):
    """对指定素材做说话人分离，推荐主说话人（辅助从多人/BGM 混音素材挑音色）。

    有纯人声轨时走 CAM++ 完整 diarization（VAD+分块声纹+HDBSCAN 聚类，输出按时间的说话人
    分段，并把切片分派到对应说话人）；无音轨时回退为纯切片声纹聚类。

    纯本地、Apache-2.0、免认证，首次运行自动下载模型并缓存。
    """
    import speaker_sep

    stem = Path(file).stem if file and file != "/" else ""
    prefixes = {stem[:12] if stem else ""}
    if stem:
        prefixes.add(clip_prefix(stem))
    prefixes.discard("")
    paths = [f for f in CLIPS_DIR.glob("*.wav") if any(f.stem.startswith(p) for p in prefixes)]
    if not paths:
        raise HTTPException(404, f"「{file}」没有可分析的切片，请先对其运行流水线。")
    try:
        vocal = _locate_vocals(stem)
        if vocal:
            return speaker_sep.analyze_audio(vocal, paths)
        # 无纯人声轨时回退为纯切片声纹聚类；素材无 BGM 分离产物属正常（短素材/纯人声视频）
        return speaker_sep.analyze_clips(paths)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # P2-1：给可读原因 + 下一步建议，不再裸抛内部异常
        ename = type(e).__name__
        if ename == "RuntimeError" and str(e):
            raise HTTPException(500, f"说话人分离失败：{e}")
        raise HTTPException(
            500,
            f"说话人分离失败：{e}。建议：确认素材已跑过流水线、切片可正常读取后重试；"
            f"若为模型首次下载类错误，请检查网络。",
        )


@router.post("/clips/qc")
def qc_clips(
    file: str = Query(..., description="素材文件名或切片前缀"),
    spk: bool = Query(True, description="是否计算说话人一致性（需跑一次 diarization，较慢）"),
    force: bool = Query(False, description="忽略缓存重算"),
):
    """给某素材的切片做质量打分（P1-1）。

    维度：时长 / 响度 / 削波 / 中段静音 / 底噪 SNR，可选「说话人一致性」（复用 CAM++
    声纹，需先定位纯人声轨跑 diarization，较慢，但能抓住他人声与 BGM 残留）。

    越过硬判废线的切片直接判 D 并给出人话原因；结果落盘
    `outputs/clip_qc/<prefix>.json`，并合并进 `GET /clips` 的返回。
    """
    import clip_qc
    import numpy as np
    import speaker_sep

    stem = Path(file).stem if file and file != "/" else ""
    prefix = clip_prefix(stem) if stem else ""
    prefixes = {p for p in (prefix, stem[:12] if stem else "") if p}
    paths = [f for f in CLIPS_DIR.glob("*.wav") if any(f.stem.startswith(p) for p in prefixes)]
    if not paths:
        raise HTTPException(404, f"「{file}」没有可质检的切片，请先对其运行流水线。")

    center = None
    main_spk = None
    if spk:
        cached = clip_qc.load_material(prefix or stem)
        if cached and cached.get("center") and not force:
            center = np.asarray(cached["center"], dtype=float)
            main_spk = cached.get("main_spk")
        else:
            vocal = _locate_vocals(stem)
            if vocal:
                main_spk, center, _meta = speaker_sep.main_center(vocal)
    try:
        payload = clip_qc.score_material(
            prefix or stem, paths, spk_center=center, force=force, main_spk=main_spk
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"切片质检失败：{e}")
    return {
        "ok": True,
        "prefix": payload["prefix"],
        "count": payload["count"],
        "grades": payload["grades"],
        "ok_count": payload["ok_count"],
        "has_spk": payload["has_spk"],
        "main_spk": payload["main_spk"],
        "updated_at": payload["updated_at"],
    }


@router.get("/export/rvc")
def export_rvc(request: Request):
    """把片段打包成 RVC 训练集 zip（可选 ?clips=a&clips=b 只导出指定片段）。
    RVC 训练只需要一个装 wav 的文件夹，自带说明文件。"""
    import io
    import zipfile

    wanted = request.query_params.getlist("clips")
    files = sorted(CLIPS_DIR.glob("*.wav"))
    if wanted:
        allowed = {f.stem for f in files}
        files = [CLIPS_DIR / f"{c}.wav" for c in wanted if c in allowed]
    if not files:
        raise HTTPException(400, "没有可导出的片段，请先跑素材流水线")

    readme = (
        "RVC 训练集使用说明\n"
        "================\n"
        f"共 {len(files)} 个干净人声片段（已去背景音、已按静音切分）。\n"
        "1. 解压后把整个 rvc_dataset 文件夹放到 RVC 整合包的 assets/datasets/ 下\n"
        "   （或在训练界面直接选择本文件夹）。\n"
        "2. 采样率不用管，RVC 会自动重采样；建议 40k 底模。\n"
        "3. 训练参数建议：提取算法 rmvpe，epoch 200~300（8G 显存可全默认）。\n"
        "4. 素材越多越像：3 分钟起步，10 分钟效果明显更稳。\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"rvc_dataset/{f.name}")
        z.writestr("rvc_dataset/README.txt", readme)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rvc_dataset.zip"},
    )


# ---------------- 音色体检「三灯」（2026-09-29 新增） ----------------
# 数据来自 tools/voice_metrics.py（嗓音客观指标）+ tools/voice_report.py（三灯合成）。
# 为什么放在 clips_api（sound.workshop 插件）而不是新开 router：
#   三灯是「素材/音色质量」这件事的另一个视图，与 /clips/qc 同属一个概念域；
#   新开 router 要动 plugin.json 清单，而清单是随包发的静态文件、有
#   tests/test_plugin_manifest.py 全程校验 —— 为一个只读接口扩清单不划算。

_VOCAL_SAMPLE_MAX = 20   # 与 tools/voice_qc.py 的取样上限保持一致（20 条中位数已足够稳）


def _voice_qc_json(voice: str) -> dict:
    """读 outputs/qc/<voice>.json（tools/voice_qc.py 的落盘）。读不到给空 dict。"""
    import json

    f = cfg.OUTPUTS_DIR / "qc" / f"{voice}.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


@router.get("/voice/lamps")
def voice_lamps(voice: str = Query(..., description="音色名（对应 outputs/qc/<voice>.json）")):
    """音色体检三灯（只读）：相似度 / 自然度 / 夹嗓子风险。

    **不现场计算**，只读 `tools/voice_qc.py` 已经落盘的结果 —— 现场算 20 条切片的
    parselmouth 指标要几秒，而这是一个会被音色卡片在列表里逐张调的接口，
    现场算会让页面卡住。写入路径是：训练后 `rvc_live` 自动触发质检（已有），
    或用户/脚本显式跑 `python tools/voice_qc.py --voice <名>` /
    `--vocal-preview <数据集目录>`。

    返回 data 为空表示还没跑过质检 —— 前端据此显示「未体检」，**不要**当成不合格。
    """
    data = _voice_qc_json(voice)
    if not data:
        return {"voice": voice, "lamps": None, "verdict": None,
                "tested_count": 0, "source": None,
                "hint": "该音色还没跑过质检：python tools/voice_qc.py --voice " + voice}
    # 变声验收的 lamps 优先（含相似度灯）；否则退到训练前体检（只有两个灯）
    for section in ("voice", "vocal_preview"):
        sec = data.get(section) or {}
        if sec.get("lamps"):
            return {"voice": voice, "lamps": sec["lamps"]["lamps"],
                    "verdict": sec["lamps"]["verdict"],
                    "tested_count": sec["lamps"]["tested_count"],
                    "source": section,
                    "vocal": {k: sec.get(k) for k in
                              ("hnr", "h1_h2", "spectral_tilt", "jitter_local",
                               "shimmer_local", "clips_measured", "sampled")},
                    "created_at": data.get("created_at")}
    return {"voice": voice, "lamps": None, "verdict": None,
            "tested_count": 0, "source": None,
            "hint": "质检结果里没有三灯（可能是旧版本 voice_qc 落盘的），重跑一次即可"}


@router.post("/voice/lamps/scan")
async def voice_lamps_scan(request: Request):
    """现场体检一份数据集目录（训练前用）：不依赖 worker 8001，秒级返回。

    body: {"dir": "D:/RVC/dataset_raw/<名>"}，缺省用 RVC dataset_raw 下第一个目录。
    返回与 /voice/lamps 同构的 lamps，外加原始指标。
    """
    import sys

    try:
        body = await request.json()
    except Exception:
        body = {}
    target = (body or {}).get("dir") or ""

    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        import voice_metrics
        import voice_report
    except Exception as e:
        raise HTTPException(500, f"嗓音指标模块不可用: {type(e).__name__}: {e}")

    ok, why = voice_metrics.metrics_available()
    if not ok:
        raise HTTPException(500, why)

    d = Path(target) if target else None
    if d is None or not d.exists():
        root = cfg.RVC_ROOT / "dataset_raw"
        cands = sorted([p for p in root.iterdir() if p.is_dir()]) if root.exists() else []
        if not cands:
            raise HTTPException(404, f"目录不存在且 dataset_raw 下没有可体检的目录: {target}")
        d = cands[0]
    wavs = sorted(d.glob("*.wav"))
    if not wavs:
        raise HTTPException(404, f"目录里没有 wav: {d}")

    sample = wavs if len(wavs) <= _VOCAL_SAMPLE_MAX else \
        wavs[:: max(1, len(wavs) // _VOCAL_SAMPLE_MAX)][:_VOCAL_SAMPLE_MAX]
    metrics = []
    for f in sample:
        m = voice_metrics.extract(f)
        if not m.get("error"):
            metrics.append(m)
    if not metrics:
        raise HTTPException(500, "没有一条切片能算出嗓音指标")

    def _med(key):
        vals = sorted(m[key] for m in metrics if m.get(key) is not None)
        return round(vals[len(vals) // 2], 3) if vals else None

    agg = {k: _med(k) for k in ("hnr", "h1_h2", "spectral_tilt",
                                "jitter_local", "shimmer_local", "f0_median")}
    lamps = voice_report.three_lamps(
        emb_sim=None, hnr=agg["hnr"], h1_h2=agg["h1_h2"],
        spectral_tilt=agg["spectral_tilt"])
    return {"dir": str(d), "clips": len(wavs), "sampled": len(sample),
            "metrics": agg, "lamps": lamps["lamps"],
            "verdict": lamps["verdict"], "tested_count": lamps["tested_count"]}

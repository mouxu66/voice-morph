"""M1 素材流水线接口：后台跑 提轨→去BGM→切片，前端轮询进度。

自 server.py 拆出（行为不变）；capture（桌宠内录）复用本模块的 pipeline_job。
app 装配见 server.py。
"""

import importlib.util
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from runtime import (
    API_PREFIX,
    AUDIO_SUFFIXES,
    CLIPS_DIR,
    PIPELINE_STATE,
    RAW_DIR,
    ROOT,
    VIDEO_SUFFIXES,
    clip_prefix,
    pipeline_cancel,
    pipeline_lock,
    update_pipeline,
)

router = APIRouter(prefix=API_PREFIX)


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("pipeline", ROOT / "m1_workshop" / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pipeline_error_hint(e: Exception, step: str = "", file_name: str = "") -> str:
    """把流水线异常转成「可读原因 + 下一步建议」（P2-1 失败可诊断）。

    step 取值：extract 提音轨 / separate 去BGM / slice 切分。
    """
    subject = f"「{file_name}」" if file_name else "该素材"
    ename = type(e).__name__
    if ename == "CalledProcessError":
        if step == "extract":
            return (
                f"{subject} 提取音轨失败：视频可能无音轨、已损坏，或 ffmpeg 不可用。"
                f"建议：换一段包含人声的视频素材；或在系统里安装完整版 ffmpeg 后重试。"
            )
        if step == "separate":
            return (
                f"{subject} 去除背景音乐失败：首次运行需联网下载 demucs 模型（约 300MB），"
                f"网络中断或显存不足都会导致中断。建议：检查网络后重试；"
                f"若反复失败，可能是显卡显存不足。"
            )
        return f"{subject} 处理失败（{step or '未知步骤'}）：{e}。建议：换一段素材重试。"
    if ename == "FileNotFoundError":
        return (
            f"{subject} 分离后未生成人声文件：{e}。建议：重新运行该素材，或检查磁盘空间是否充足。"
        )
    if ename == "PipelineCancelled":
        return "流水线已取消，已完成的片段会保留。"
    return f"{subject} 流水线出错：{e}。建议：按上方信息排查；" f"若为模型下载/网络类错误，请重试。"


def _attach_verdict(**kw) -> None:
    """算出诊断结论并挂到 PIPELINE_STATE["verdict"]（P2-1）。

    ★ 这个函数**永不抛**：诊断器自己崩掉比没有诊断器更糟 —— 用户会以为整个后端挂了，
    而实际上素材早就处理完了。所以 import/计算/落盘全在同一个 try 里兜底。

    与 `_pipeline_error_hint` 的分工：那个翻译**抛出来的**异常（ffmpeg 挂了），
    这个解释**不抛异常的**失败（素材是纯伴奏、切片全是 D 级）。
    """
    try:
        import quality_verdict

        # 写进 dict 而不是整体重新赋值：PIPELINE_STATE 是**原地共享**的 dict
        # （见 runtime 模块注释），整体赋值会断开其他地方持有的引用。
        PIPELINE_STATE["verdict"] = quality_verdict.build_quality_verdict(**kw)
    except Exception:  # noqa: BLE001
        PIPELINE_STATE["verdict"] = {}


def pipeline_job(videos: list[Path]):
    """流水线任务体：逐个素材 提轨→去BGM→切片，进度写 PIPELINE_STATE。

    独立成模块级函数，便于 /pipeline/run 与桌宠内录（capture）复用。"""
    try:
        mod = _load_pipeline_module()
        total = len(videos)
        clips = 0
        for i, v in enumerate(videos):
            if pipeline_cancel.is_set():
                raise mod.PipelineCancelled()
            update_pipeline(file=v.name)
            base = round((i / total) * 88)
            update_pipeline(
                step="extract", percent=base + 2, message=f"({i + 1}/{total}) 提取音轨：{v.name}"
            )
            wav = mod.step1_extract(v, pipeline_cancel)
            update_pipeline(
                step="separate",
                percent=base + 12,
                message=f"({i + 1}/{total}) 去除背景音乐：{v.name}（首次需下载模型，较慢）",
            )
            vocal = mod.step2_separate(wav, pipeline_cancel)
            update_pipeline(
                step="slice", percent=base + 24, message=f"({i + 1}/{total}) 静音检测切分：{v.name}"
            )
            n = mod.step3_slice(vocal, clip_prefix(v.stem), pipeline_cancel)
            clips += n
            update_pipeline(clips=clips)
        update_pipeline(
            status="done",
            step="",
            percent=100,
            message=f"流水线完成，共切出 {clips} 个片段（正在质检…）",
        )
        # 切片质检：后台静默跑，失败不影响流水线结果
        prefixes = [clip_prefix(v.stem) for v in videos]
        # 先给一份「还没质检」的结论：质检线程要几十秒（逐条算 SNR），
        # 这段时间里界面不该空白 —— 先告诉用户"切出来了但还没判定好坏"
        _attach_verdict(
            summary={"total": clips, "ok": 0, "grades": {"A": 0, "B": 0, "C": 0, "D": 0}},
            status="running" if clips else "done",
        )
        threading.Thread(target=_qc_after_pipeline, args=(prefixes,), daemon=True).start()
    except Exception as e:
        if type(e).__name__ == "PipelineCancelled":
            update_pipeline(status="cancelled", step="", message="已取消，已完成的片段会保留")
            _attach_verdict(status="cancelled")
        else:
            hint = _pipeline_error_hint(
                e, PIPELINE_STATE.get("step", ""), PIPELINE_STATE.get("file", "")
            )
            update_pipeline(status="error", step="", message="流水线出错", error=hint)
            _attach_verdict(status="error", error=hint)
    finally:
        update_pipeline(running=False, file="")


def _qc_after_pipeline(prefixes: list[str]):
    """流水线跑完后自动给切片打分（后台线程，静默失败）。

    只算客观指标，不含"说话人一致性"——那需要 diarization，较重；用户点了
    「说话人分离」之后再跑 `POST /clips/qc` 会补上这个最强维度并覆盖缓存。
    """
    try:
        import clip_qc

        summary = clip_qc.score_prefixes(prefixes, CLIPS_DIR)
        update_pipeline(qc=summary)
        # P2-1：质检结果一出来就把「结论 + 下一步」挂上去，供前端直接渲染。
        # 这里**不再自己拼 msg 的建议文案** —— 那套 if/elif 与 quality_verdict 重复，
        # 且两处口径分开演化（比如"可用切片偏少"的阈值）必然打架。
        # message 只留客观数字，解释全权交给 verdict。
        _attach_verdict(summary=summary, status="done")
        if summary.get("total"):
            total = summary["total"]
            ok = summary.get("ok", 0)
            grades = summary.get("grades", {}) or {}
            update_pipeline(
                message=(
                    f"流水线完成，切片 {total} 条，质检可用（A/B）{ok} 条"
                    f"（A {grades.get('A', 0)} / B {grades.get('B', 0)} / "
                    f"C {grades.get('C', 0)} / D {grades.get('D', 0)}）"
                )
            )
    except Exception:  # noqa: BLE001
        pass


@router.post("/pipeline/run")
def run_pipeline(file: list[str] | None = Query(default=None)):
    """后台启动 M1 流水线，前端轮询 /pipeline/status 获取分步进度。

    file 可传一个或多个素材文件名，指定时只处理这些素材（文件名相对 media/raw_videos，
    允许用 URL 编码或原始中文名）；不传则处理目录内全部视频/音频素材。"""
    if PIPELINE_STATE["running"]:
        raise HTTPException(400, "流水线正在运行中，请稍候")
    if file:
        videos: list[Path] = []
        for name in file:
            p = RAW_DIR / Path(name).name
            if not p.exists():
                raise HTTPException(404, f"素材不存在：{name}")
            if p.suffix.lower() not in VIDEO_SUFFIXES and p.suffix.lower() not in AUDIO_SUFFIXES:
                raise HTTPException(400, f"仅支持视频/音频格式：{name}")
            videos.append(p)
        scope = "、".join(v.name for v in videos)
    else:
        # 视频与音频素材统一进流水线（音频文件 ffmpeg -vn 提轨同样有效，可来自上传/录音等多种渠道）
        videos = [
            f
            for f in RAW_DIR.iterdir()
            if f.suffix.lower() in VIDEO_SUFFIXES or f.suffix.lower() in AUDIO_SUFFIXES
        ]
        scope = f"全部 {len(videos)} 个素材"
    if not videos:
        raise HTTPException(400, "media/raw_videos/ 里没有视频/音频素材，请先上传或放入素材")

    pipeline_cancel.clear()
    update_pipeline(
        running=True,
        status="running",
        step="prepare",
        message=f"准备处理 {scope}…",
        percent=1,
        clips=0,
        error="",
    )

    threading.Thread(target=pipeline_job, args=(videos,), daemon=True).start()
    return {"ok": True, "started": True, "scope": scope}


@router.get("/pipeline/status")
def pipeline_status():
    with pipeline_lock:
        return dict(PIPELINE_STATE)


@router.post("/pipeline/cancel")
def cancel_pipeline():
    pipeline_cancel.set()
    return {"ok": True}

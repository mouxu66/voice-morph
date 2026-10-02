# -*- coding: utf-8 -*-
"""声音保存的 HTTP 接口（`sound.preserve` 插件）。

    GET    /api/preserve/guide                          录音引导（脚本 / 档位 / 复现文本 / 同意书）
    GET    /api/preserve/summary                        档案库总览
    GET    /api/preserve/profiles                       档案列表
    POST   /api/preserve/profiles                       建档 {label, note}
    GET    /api/preserve/profiles/{pid}                 档案详情（profile + 事件数）
    DELETE /api/preserve/profiles/{pid}                 删除档案（含录音、同意书、日志）
    POST   /api/preserve/profiles/{pid}/consent         记录知情同意
    POST   /api/preserve/profiles/{pid}/gate            质量门禁（**会算指标**，故 POST）
    GET    /api/preserve/profiles/{pid}/events          操作日志
    POST   /api/preserve/profiles/{pid}/references      上传一档参考音
    POST   /api/preserve/profiles/{pid}/truth           上传一段真实录音（听测对照）
    GET    /api/preserve/profiles/{pid}/reference       取参考音（**给现有 TTS 链路用**）

★ 本模块**不实现合成**
------------------------
「复现」= 拿档案里的参考音 → 走现有 TTS → 经虚拟声卡输出。合成那条链路
（`qwen3_tts` / `tts_api` / `hook.wechat` 的发送）已经存在且经过验收，
在这里重写一遍只会造出**第二条会漂移的实现**。
所以本模块只负责一件事：**告诉调用方「该用哪一段参考音」**（`/reference`），
剩下的交给现有链路。这也是 `plugin.json` 的 `requires` 刻意留空的原因 ——
后端一个别的插件都不依赖，页面上那个「去复现」的入口由前端按能力门控。

★ `gate` 为什么是 POST
----------------------
它会**真的去算**客观指标（parselmouth，单个文件约 1 秒），不是纯读。
与 `docs/音色体检三灯.md` §5 的 GET 只读 / POST 现算 是同一套约定 ——
让"看一眼"和"算一次"在 HTTP 语义上分得开，前端才不会在渲染时意外触发重算。
"""

from pathlib import Path

import config as cfg
import voicepreserve as vp
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)


# ---------------- 请求体 ----------------

class CreateReq(BaseModel):
    label: str
    note: str = ""


class ConsentReq(BaseModel):
    scope: str = "self"
    statement: str = ""
    operator: str = ""


# ---------------- 小工具 ----------------

def _pid_or_400(pid: str) -> str:
    """路径参数校验。id 非法一律 400 —— 它会拼进路径，不能放进业务层再拦。"""
    if not vp.is_valid_profile_id(pid):
        raise HTTPException(400, "档案 id 非法")
    return pid


def _profile_or_404(pid: str) -> dict:
    p = vp.get_profile(pid)
    if p is None:
        raise HTTPException(404, f"档案不存在: {pid}")
    return p


def _media_url(path: Path) -> str:
    """把档案里的 wav 转成前端能播的 URL（`core.media` 的静态白名单已含 `preserve`）。"""
    try:
        rel = path.resolve().relative_to(Path(cfg.MEDIA_DIR).resolve())
        return f"{API_PREFIX}/media/{rel.as_posix()}"
    except Exception:
        return ""


# ---------------- 引导与总览 ----------------

@router.get("/preserve/guide")
def preserve_guide() -> dict:
    """录音引导：脚本、时长档、复现文本、同意书。

    由后端现算再吐给前端（**不让前端手抄一份**）—— 与 `remote_api` 的
    `capabilities` 同一个手法：只在一处声明的东西才算接上了。
    """
    return vp.guide()


@router.get("/preserve/summary")
def preserve_summary() -> dict:
    """档案库总览：几份档案、几份有同意、几份有参考音。"""
    return vp.summary()


# ---------------- 档案 CRUD ----------------

@router.get("/preserve/profiles")
def preserve_list() -> dict:
    return {"profiles": vp.list_profiles()}


@router.post("/preserve/profiles")
def preserve_create(req: CreateReq) -> dict:
    """建一个空档案。**不写 consent** —— 同意是用户下一步显式做的动作。"""
    try:
        p = vp.create_profile(req.label, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "profile": p}


@router.get("/preserve/profiles/{pid}")
def preserve_get(pid: str) -> dict:
    """档案详情。**不在这里跑门禁** —— 那要算指标，由 `POST /gate` 显式触发。"""
    p = _profile_or_404(_pid_or_400(pid))
    events = vp.read_events(pid, limit=20)
    return {"profile": p, "events": events, "events_total": len(vp.read_events(pid, limit=0))}


@router.delete("/preserve/profiles/{pid}")
def preserve_delete(pid: str) -> dict:
    """删除档案。**不可逆** —— 会连同录音、同意书、操作日志一起删掉。"""
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    vp.log_event(pid, "delete", note="档案连同录音与日志一并删除")
    if not vp.delete_profile(pid):
        raise HTTPException(404, f"档案不存在: {pid}")
    return {"ok": True, "deleted": pid}


# ---------------- 知情同意 ----------------

@router.post("/preserve/profiles/{pid}/consent")
def preserve_consent(pid: str, req: ConsentReq) -> dict:
    """记录知情同意。**这是复现路径的前置条件**（见 `voicepreserve.gate`）。"""
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    try:
        consent = vp.record_consent(
            pid, scope=req.scope, statement=req.statement, operator=req.operator)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if consent.get("error"):
        raise HTTPException(400, consent["error"])
    return {"ok": True, "consent": consent}


# ---------------- 质量门禁与日志 ----------------

@router.post("/preserve/profiles/{pid}/gate")
def preserve_gate(pid: str) -> dict:
    """质量门禁：这份档案现在能不能用来复现？

    **会算指标**（单个参考音约 1 秒），所以是 POST。verdict 只看结构性事实
    （有无参考音 / 有无同意 / 指标算不算得出）；三灯只作信息呈现，不参与判定 ——
    它的阈值锚在另一批素材上，拿它否决别人的档案是错的（见 `voicepreserve.gate` 的注释）。
    """
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    return vp.gate(pid)


@router.get("/preserve/profiles/{pid}/events")
def preserve_events(pid: str, limit: int = 200) -> dict:
    """操作日志（谁、何时、做了什么）。只追加，不可改 —— 可审计的前提。"""
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    return {"events": vp.read_events(pid, limit=max(1, min(int(limit), 2000)))}


# ---------------- 录音上传 ----------------

async def _save_upload(file: UploadFile) -> Path:
    """把上传的音频落到临时文件，返回路径。

    ★ `await file.read()` **必须在路由内**（也就是在请求还活着的时候）：FastAPI 0.141
    在端点返回后会自动关闭表单文件句柄，把 `file` 丢进后台线程再读会报
    `I/O operation on closed file`（2026-10-01 实测抓到过，见
    `docs/犯错指南.md` 速查表第 123 条）。所以这里一次读完、写完再返回路径 ——
    后续的 ffmpeg 转换拿的是**磁盘上的临时文件**，与请求生命周期无关了。
    """
    import tempfile

    from common import MAX_UPLOAD_BYTES

    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"录音过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传内容为空")
    suffix = Path(file.filename or "").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        return Path(tmp.name)


@router.post("/preserve/profiles/{pid}/references")
async def preserve_add_reference(
    pid: str,
    file: UploadFile = File(...),
    duration_s: float = Form(...),
):
    """上传一档参考音。

    前端录出来的是 **webm/opus**（`MediaRecorder`），这里统一经 ffmpeg 归一成
    单声道 wav 再入库 —— 直接把 webm 字节命名成 `.wav` 存下来**不会报错**，
    只会在几秒后以"读不出浊音段"的形式暴露（见 `voicepreserve._to_wav` 的注释）。

    同一档重传 = **替换**，不是追加（否则门禁会拿一堆重复档去算，中位数失去意义）。
    """
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    src = await _save_upload(file)
    try:
        rec = vp.add_reference(pid, src, duration_s)
    finally:
        src.unlink(missing_ok=True)
    if rec.get("error"):
        raise HTTPException(400, rec["error"])
    return {"ok": True, "reference": rec}


@router.post("/preserve/profiles/{pid}/truth")
async def preserve_add_truth(
    pid: str,
    file: UploadFile = File(...),
    condition: str = Form(...),
):
    """上传一段「真实录音」（听测 ABX 的对照）。条件名见 `/guide` 的 `conditions`。"""
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    src = await _save_upload(file)
    try:
        rec = vp.add_truth(pid, condition, src)
    finally:
        src.unlink(missing_ok=True)
    if rec.get("error"):
        raise HTTPException(400, rec["error"])
    return {"ok": True, "truth": rec}


# ---------------- 取参考音（给现有 TTS 链路用） ----------------

@router.get("/preserve/profiles/{pid}/reference")
def preserve_reference(pid: str, duration_s: float | None = None) -> dict:
    """取一档参考音的路径与可播放 URL。

    `duration_s` 省略时取**最长档**（质量最好的那一档）。
    调用方拿到 `path` 后走现有 TTS / 发送链路 —— 本模块不合成。
    """
    pid = _pid_or_400(pid)
    _profile_or_404(pid)
    path = vp.reference_path(pid, duration_s)
    if path is None:
        want = "任意档" if duration_s is None else f"{vp._dur_tag(duration_s)} 档"
        raise HTTPException(404, f"这份档案还没有{want}的参考音")
    return {
        "path": str(path),
        "url": _media_url(path),
        "file": path.name,
        "duration_s": duration_s,
    }

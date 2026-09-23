"""特效声板（`sound.fx-board`）：把一条短音效**实时**播进虚拟声卡。

它解决的是这个场景（`docs/特效声板设计.md` 的用户原话）：

    文字转语音 → 播到虚拟声卡 → 按住 Alt 录制期间点一下「爆炸」
    → 系统把 TTS 人声与爆炸混成一条 → 微信录走 → 一条语音里两声都有

关键点：**声板不碰微信发送链路的任何一行代码**。它只做一件事 —— 把声音写进
CABLE 的渲染端；微信从采集端录到的就是混好的结果（Windows 共享模式自动混音）。
这也是本模块刻意**不 import `wechat_voice`** 的原因：微信模块是全仓坑最密集的地方，
耦合它等于把声板拖进那堆坑里。设备关键字与 RVC venv 解释器各自读同一组环境变量
（`VM_LIVE_OUTPUT_DEVICE` / `VM_RVC_ROOT`），语义一致而无导入依赖。

与 `_send_lock` 的关系（设计稿 §四第 3 条，**别改**）
    `_send_lock` 是"发送流程"的锁：一段 TTS 正在播到声卡时它一直被持有。
    声板**不进那把锁** —— 否则"录制中点格子"要干等到播放结束，用户场景直接失效。
    声板自己只需要一把**轻量互斥**（`_CMD_LOCK`）：worker 的行协议是「一条命令一行
    响应」，两条命令同时在管道上飞会让响应错位（谁读到谁的行就乱了）。

素材从哪来
    · 出厂 6 条：`plugins/sound.fx-board/samples/*.wav`（`tools/gen_sfx.py` 程序化合成，
      可复现、零第三方版权）；
    · 用户导入：`<media>/soundboard/*.wav`（`/import` 写入；`DELETE` 只能删这些）。
    文件名 stem 即素材 id，出厂 id 与导入 id 不许重名（导入时直接拒，比"谁覆盖谁"
    这种隐式规则好查）。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from pathlib import Path

import config as cfg
import numpy as np
import soundfile as sf
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/api/soundboard", tags=["soundboard"])

#: 出厂素材（随插件走，`tools/gen_sfx.py` 生成）
SAMPLES_DIR = Path(__file__).resolve().parent / "plugins" / "sound.fx-board" / "samples"
#: 用户导入（跟 media 走，测试里被 VM_MEDIA_DIR 重定向，所以用例不会污染真素材）
IMPORT_DIR = cfg.MEDIA_DIR / "soundboard"
#: 播放计数（用户最初想要的"使用热度"；本地零依赖）
STATS_FILE = cfg.OUTPUTS_DIR / "soundboard_stats.json"

RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
BOARD_WORKER = Path(__file__).resolve().parent / "board_worker.py"

#: 与 wechat_voice.OUTPUT_DEVICE_KEYWORD 同义同默认值（见文件头：不 import 它）
DEVICE_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "VB-Audio Virtual Cable|CABLE Input")

_MAX_IMPORT_BYTES = 2 * 1024 * 1024  # 2MB：one-shot 素材不该有更大的
_MAX_IMPORT_S = 5.0  # 5s：微信语音是"叠加进人声"，太长的音效会把整条消息搞糊

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCK = threading.Lock()  # 起/停 worker
_CMD_LOCK = threading.Lock()  # 命令串行（行协议是「一条命令一行响应」）
_STATS_LOCK = threading.Lock()  # 计数的读-改-写（FastAPI 的同步端点跑在线程池里，会并发）
_proc: subprocess.Popen | None = None


# --------------------------------------------------------------- 素材目录


def _sanitize_stem(raw: str) -> str:
    """把上传文件名收成安全的 stem：去目录、拒穿越、限长、只留常规字符。"""
    stem = Path(raw or "").name  # 去掉任何目录成分（../x.wav → x.wav）
    stem = Path(stem).stem
    bad = [c for c in stem if c in '\\/:*?"<>|' or ord(c) < 32]
    if bad or stem in ("", ".", ".."):
        raise HTTPException(status_code=400, detail=f"文件名不合法：{raw!r}")
    if len(stem) > 40:
        raise HTTPException(status_code=400, detail="文件名太长（限 40 字符）")
    return stem


def _meta() -> dict:
    """出厂素材的显示名/标签：`samples/manifest.json`（gen_sfx.py 写）。"""
    f = SAMPLES_DIR / "manifest.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _duration_s(path: Path) -> float:
    try:
        return round(float(sf.info(str(path)).duration), 2)
    except Exception:
        return 0.0


def _load_stats() -> dict:
    """读播放计数。坏了就当空 —— 计数是锦上添花，不该让它拖垮 catalog。"""
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bump(sid: str) -> None:
    """播放计数 +1（尽力而为：写不进去也不能影响播放）。"""
    with _STATS_LOCK:
        stats = _load_stats()
        stats[sid] = stats.get(sid, 0) + 1
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 只读环境/盘满：计数丢了就丢了，播放不受影响


def _items() -> list[dict]:
    """出厂 + 导入的全部素材（导入在后，名字冲突时**导入被拒**，所以不会真冲突）。"""
    meta = _meta()
    stats = _load_stats()
    out: list[dict] = []
    for d, builtin in ((SAMPLES_DIR, True), (IMPORT_DIR, False)):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.wav")):
            sid = f.stem
            m = meta.get(sid) if builtin else None
            out.append(
                {
                    "id": sid,
                    "name": (m or {}).get("name") or sid,
                    "tags": (m or {}).get("tags") or (["导入"] if not builtin else []),
                    "duration_s": _duration_s(f),
                    "count": stats.get(sid, 0),
                    "builtin": builtin,
                }
            )
    return out


def _resolve_path(sample_id: str) -> tuple[Path, bool]:
    """素材 id → 文件路径 + 是否出厂。只认这两个目录里的文件名，拒绝一切路径花样。

    穿越防护用「拼接后必须仍在目标目录内」而不是字符串黑名单：后者永远漏
    （`....//`、`%2e%2e`、NT 的 `\\?\\` 前缀…），而 `is_relative_to` 是判据本身。
    """
    if not sample_id or sample_id in (".", "..") or any(c in sample_id for c in "/\\"):
        raise HTTPException(status_code=400, detail=f"非法的音效 id：{sample_id!r}")
    for d, builtin in ((SAMPLES_DIR, True), (IMPORT_DIR, False)):
        p = (d / f"{sample_id}.wav").resolve()
        if p.is_relative_to(d.resolve()) and p.is_file():
            return p, builtin
    raise HTTPException(status_code=404, detail=f"没有这个音效：{sample_id}")


# --------------------------------------------------------------- 播放 worker


def _spawn_worker() -> subprocess.Popen:
    """启动常驻播放 worker（懒启动，成功后复用）。

    `VM_SOUNDBOARD=0` 时**拒播**：测试环境不该往真声卡/CABLE 上放声音 ——
    与 `VM_WARMUP=0` / `VM_WECHAT_RESTART=0` 同一套隔离惯例（conftest 会设 0）。
    """
    if os.environ.get("VM_SOUNDBOARD", "1") == "0":
        raise RuntimeError("声板播放已被 VM_SOUNDBOARD=0 禁用（测试环境）")
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    if not BOARD_WORKER.exists():
        raise RuntimeError(f"找不到声板播放器: {BOARD_WORKER}")
    proc = subprocess.Popen(
        [str(RVC_VENV_PY), str(BOARD_WORKER), DEVICE_KEYWORD, str(SAMPLES_DIR), str(IMPORT_DIR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=_NO_WINDOW,  # 与发送链路同理：弹出控制台会遮挡微信窗口
    )
    line = proc.stdout.readline() if proc.stdout else ""
    try:
        msg = json.loads(line or "{}")
    except Exception:
        msg = {}
    if msg.get("type") != "ready":
        detail = msg.get("msg") or (line or "").strip()[:200] or "无输出"
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError(f"声板播放器未就绪：{detail}")
    return proc


def _ensure_worker() -> subprocess.Popen:
    global _proc
    with _LOCK:
        if _proc is not None and _proc.poll() is None:
            return _proc
        if _proc is not None:  # 死掉的残留：关掉管道再重开
            with contextlib.suppress(Exception):
                _proc.kill()
        _proc = _spawn_worker()
        return _proc


def _command(payload: dict) -> dict:
    """发一条命令并读**恰好一行**响应（行协议不变量：一命令一响应）。

    读不到行（空串）= worker 已退出（管道关闭）—— 与 `play_worker._PlayWorkerHandle`
    的判据一致：不要把"管道关了"和"响应内容不对"混成一类。
    """
    with _CMD_LOCK:
        proc = _ensure_worker()
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline() if proc.stdout else ""
        except Exception as e:
            raise RuntimeError(f"声板播放命令发送失败：{e}") from e
    if not line:
        raise RuntimeError("声板播放器已退出（管道关闭）")
    try:
        msg = json.loads(line)
    except Exception:
        raise RuntimeError(f"声板播放器响应无法解析：{line.strip()[:200]}")
    if msg.get("type") == "error":
        raise RuntimeError(str(msg.get("msg") or "未知错误"))
    return msg


def _stop_worker() -> None:
    global _proc
    with _LOCK:
        proc, _proc = _proc, None
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.kill()
    except Exception:
        pass


# --------------------------------------------------------------- 路由


@router.get("/catalog")
def soundboard_catalog():
    """素材目录（格子面板的渲染依据）。不含绝对路径 —— 目录枚举不给文件系统信息。"""
    return {"ok": True, "items": _items()}


class PlayReq(BaseModel):
    id: str
    gain: float | None = None


@router.post("/play")
def soundboard_play(req: PlayReq):
    """播一条音效到 CABLE。**立即返回**（one-shot 语义：点击即响，不等播完）。

    注意它**不获取 `_send_lock`** —— 详见模块头注释：录制窗口内点格子必须畅通。
    """
    path, _builtin = _resolve_path(req.id)
    payload = {"wav": str(path), "id": req.id, "gain": 0.9 if req.gain is None else req.gain}
    try:
        msg = _command(payload)
    except (RuntimeError, OSError) as e:
        # OSError 也归 503：Popen 失败（解释器不可执行/被占用）与"设备找不到"
        # 对用户是同一件事 —— 声板现在不能响，而不是"服务器写错代码"。
        raise HTTPException(status_code=503, detail=str(e))
    _bump(req.id)
    return {"ok": True, "playing": True, "id": req.id, "duration_s": msg.get("duration_s")}


@router.post("/stop")
def soundboard_stop():
    try:
        _command({"stop": True})
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


@router.post("/warm")
def soundboard_warm():
    """预热：把 worker 与素材全读进内存（冷启动 ~2-3s 的 import 只付一次）。

    面板一挂载就调它，用户点第一格时就已经是热的 —— 不然第一次点击要等冷导入，
    听感上像"没反应"。
    """
    try:
        proc = _ensure_worker()
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "ready": proc.poll() is None, "samples": len(_items())}


@router.post("/import")
async def soundboard_import(file: UploadFile = File(...)):
    """导入用户自己的音效（wav/flac/ogg…）：≤5s、≤2MB，转单声道 PCM16 wav。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > _MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件过大：>{_MAX_IMPORT_BYTES // (1024 * 1024)}MB 拒绝"
        )
    import io

    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"音频读取失败：{e}")
    dur = len(data) / int(sr or 1)
    if dur > _MAX_IMPORT_S:
        raise HTTPException(status_code=400, detail=f"太长了：{dur:.1f}s > {_MAX_IMPORT_S:.0f}s")
    if dur < 0.05:
        raise HTTPException(status_code=400, detail="太短了（<0.05s）")
    if data.ndim > 1:
        data = data.mean(axis=1)

    sid = _sanitize_stem(file.filename or "imported")
    if (SAMPLES_DIR / f"{sid}.wav").is_file():
        raise HTTPException(status_code=400, detail=f"名字与出厂音效重复：{sid}（请改名）")

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = IMPORT_DIR / f"{sid}.wav"
    sf.write(str(out), np.asarray(data, dtype=np.float32), int(sr), subtype="PCM_16")
    return {"ok": True, "id": sid, "duration_s": round(dur, 2)}


@router.delete("/{sample_id}")
def soundboard_delete(sample_id: str):
    """删除**导入**的素材（出厂素材不许删 —— 删了重装才有，这不该由用户承担）。"""
    path, builtin = _resolve_path(sample_id)
    if builtin:
        raise HTTPException(status_code=400, detail="出厂音效不可删除")
    path.unlink()
    stats = _load_stats()
    if stats.pop(sample_id, None) is not None:
        with contextlib.suppress(OSError):
            STATS_FILE.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return {"ok": True, "id": sample_id}

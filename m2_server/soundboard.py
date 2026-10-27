"""特效声板（`sound.fx-board`）：把一条短音效**播进虚拟声卡**（实时）或**混进一条音频**（预混）。

它解决的是这个场景（`docs/交接-AI必读.md` 的用户原话）：

    文字转语音 → 播到虚拟声卡 → 按住 Alt 录制期间点一下「爆炸」
    → 系统把 TTS 人声与爆炸混成一条 → 微信录走 → 一条语音里两声都有

关键点：**声板不碰微信发送链路的任何一行代码**。它只做一件事 —— 把声音写进
CABLE 的渲染端；微信从采集端录到的就是混好的结果（Windows 共享模式自动混音）。
这也是本模块刻意**不 import `wechat_voice`** 的原因：微信模块是全仓坑最密集的地方，
耦合它等于把声板拖进那堆坑里。设备关键字与 RVC venv 解释器各自读同一组环境变量
（`VM_LIVE_OUTPUT_DEVICE` / `VM_RVC_ROOT`），语义一致而无导入依赖。

两条路径（同一份素材库 `sfx_lib.py`）
------------------------------------
    /play    实时：点一下即响，与正在播的 TTS / 正在跑的实时变声**系统混音**。
             这是主场景（"按住 Alt 说话时来一炮"）。
    /premix  预混：把勾选的音效**离线**混进一条合成产物，产出新 wav，发送目标换成它。
             存在的理由是一条真实的物理限制 —— 点格子时鼠标焦点会离开微信，
             **按住的录音有没有被取消**尚未真机验证（设计稿 §五）。预混不依赖任何
             实时交互，所以它是那条路走不通时的确定性兜底。

与 `_send_lock` 的关系（设计稿 §四第 3 条，**别改**）
    `_send_lock` 是"发送流程"的锁：一段 TTS 正在播到声卡时它一直被持有。
    声板**不进那把锁** —— 否则"录制中点格子"要干等到播放结束，用户场景直接失效。
    声板自己只需要一把**轻量互斥**（`_CMD_LOCK`）：worker 的行协议是「一条命令一行
    响应」，两条命令同时在管道上飞会让响应错位（谁读到谁的行就乱了）。
    预混同样不进那把锁：它是纯文件加工，与发送互不干扰（写的是另一个文件名）。

素材见 `sfx_lib.py`（目录、读取缓存、混音 DSP 都在那里 —— 效果链里的「插入音效」
用的是同一份，所以不能复制成本模块的私有常量）。

素材的三种来源（出厂 / 单条导入 / **成套音效包**）
    出厂随插件走；`/import` 收单条；`/packs/*` 管**成套**的安装与卸载
    （布局与安全解压都在 `sfx_packs.py`，它也是叶子模块、无路由）。
    三条来源在格子里是同一件事 —— 用户看到的都是"一条能点的音效"，
    区别只在 `builtin`/`pack` 两个字段上（决定能不能删、删的是哪一层）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import config as cfg
import numpy as np
import session_out
import sfx_lib
import sfx_packs
import soundfile as sf
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sfx_lib import SfxError

router = APIRouter(prefix="/api/soundboard", tags=["soundboard"])

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


# --------------------------------------------------------------- 素材（薄封装到 sfx_lib）

# `sfx_lib` 才是素材库的 owner。本模块只做两件事：
#   ① 把它的 SfxError 翻译成 HTTPException（路由层才关心 HTTP 语义）；
#   ② 叠上"播放计数"这层运营数据（成品目录 ≠ 素材库）。
# 目录常量一律**用 `sfx_lib.X` 现读**，不许在导入期复制成模块级变量 ——
# 早绑定的副本会让 monkeypatch 静默失效，写入落回用户的真实 media/outputs
# （`docs/犯错档案-工程.md` §8.36 / §8.37）。


def _http(e: SfxError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _resolve_path(sample_id: str) -> tuple[Path, bool]:
    try:
        return sfx_lib.resolve_path(sample_id)
    except SfxError as e:
        raise _http(e) from e


def _sanitize_stem(raw: str) -> str:
    try:
        return sfx_lib.sanitize_stem(raw)
    except SfxError as e:
        raise _http(e) from e


def _load_stats() -> dict:
    """读播放计数。坏了就当空 —— 计数是锦上添花，不该让它拖垮 catalog。"""
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bump(sid: str) -> None:
    """计数 +1（点一次格子 / 混一次都算"用过一次"；尽力而为，写不进去也不影响功能）。"""
    with _STATS_LOCK:
        stats = _load_stats()
        stats[sid] = stats.get(sid, 0) + 1
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 只读环境/盘满：计数丢了就丢了，播放不受影响


def _items() -> list[dict]:
    """素材目录 + 播放计数（格子面板的渲染依据）。

    附一个 `removable`：**单条**可以删的才为真。包内素材与出厂素材都是 `False` ——
    前者要删得整包删（`DELETE /packs/{id}`），后者删了得重装才有。
    前端不该自己拿 `builtin/pack` 推这条规则，因为"谁能删"是会变的产品决定，
    多一处推断就多一处会漂的地方。
    """
    stats = _load_stats()
    return [
        {
            **it,
            "count": stats.get(it["id"], 0),
            "removable": (not it.get("builtin")) and not it.get("pack"),
        }
        for it in sfx_lib.list_samples()
    ]


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
        [
            str(RVC_VENV_PY),
            str(BOARD_WORKER),
            DEVICE_KEYWORD,
            str(sfx_lib.SAMPLES_DIR),  # 现读：测试/多环境里目录可能被改
            str(sfx_lib.IMPORT_DIR),
        ],
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


# --------------------------------------------------------------- 音频解析（预混用）


def _outputs_wav(name: str) -> Path:
    """把请求里的 wav 名解析成**会话目录或 outputs 根**下的文件。**只认裸文件名**。

    与 `wechat_voice._resolve_wav` 的差别（刻意）：那边放行绝对路径，因为它还要
    处理发送链路内部生成的临时文件；这里只服务"把某条合成产物混一下"这一件事，
    所以直接拒掉任何带目录成分的名字 —— 请求体里的路径不该能指向 outputs 之外。

    两处都查（而不是只查根）的理由：合成产物默认落在 `outputs/.session/`
    （`session_out`），只查根目录会让"刚合成完就混音"报找不到文件。
    """
    p = session_out.find(name)
    if p is None:
        n = str(name or "").strip()
        # 名字本身非法（带路径成分）与"两处都没有"是两回事，报错要分开
        if not n or n in (".", "..") or any(c in n for c in "/\\"):
            raise HTTPException(status_code=400, detail=f"非法的音频名：{name!r}")
        raise HTTPException(status_code=404, detail=f"找不到音频：{n}")
    return p


def _latest_tts() -> Path:
    """不给 wav 名时的默认目标：最近一次合成产物（与发送链路的默认口径一致）。"""
    p = session_out.newest_tts()
    if p is None:
        raise HTTPException(
            status_code=404, detail="还没有 TTS 产物，先在网页上合成一条语音"
        )
    return p


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"音频读取失败：{e}") from e
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), int(sr or 1)


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
        raise HTTPException(status_code=503, detail=str(e)) from e
    _bump(req.id)
    return {"ok": True, "playing": True, "id": req.id, "duration_s": msg.get("duration_s")}


@router.post("/stop")
def soundboard_stop():
    try:
        _command({"stop": True})
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
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
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "ready": proc.poll() is None, "samples": len(_items())}


class PremixInsert(BaseModel):
    sample: str
    mode: str = "layer"  # layer | prepend | append（sfx_lib.MODES）
    at_s: float = 0.0
    gain: float = 0.9


class PremixReq(BaseModel):
    wav: str | None = None
    inserts: list[PremixInsert] = []


@router.post("/premix")
def soundboard_premix(req: PremixReq):
    """把勾选的音效**离线混进**一条合成产物，产出新 wav（预混模式）。

    分工见模块头：**实时是主场景，预混是确定性兜底**。要它是因为一条尚未真机验证的
    物理限制 —— 点格子时鼠标焦点离开微信，按住的录音可能被取消。预混不依赖任何
    实时交互：发送目标直接换成这条混好的文件。

    输出名 `sfxmix_<原名>`，理由见本文件 `/premix` 那段的注释（不劫持"最近一条
    TTS 产物"的口径 + 同名原子替换，反复试混不堆垃圾也不让播放读到半截文件）。
    """
    inserts = list(req.inserts or [])
    if not inserts:
        raise HTTPException(status_code=400, detail="没有勾选任何音效")
    if len(inserts) > sfx_lib.MAX_INSERTS:
        raise HTTPException(
            status_code=400, detail=f"一次最多混 {sfx_lib.MAX_INSERTS} 条音效"
        )
    # 前置校验（与效果链的"尽力而为"相反）：`/premix` 是一次**明确的用户动作**，
    # 差一条音效就该当场说清楚是哪条，而不是安静地发一条少了那声响的语音 ——
    # 那种失败在界面上看不出任何异常，是最难被发现的一类。
    bad_mode = [str(i.mode) for i in inserts if str(i.mode).lower() not in sfx_lib.MODES]
    if bad_mode:
        raise HTTPException(
            status_code=400,
            detail=f"未知位置：{bad_mode[0]!r}（可选 {'/'.join(sfx_lib.MODES)}）",
        )
    for ins in inserts:
        try:
            sfx_lib.resolve_path(ins.sample)
        except SfxError as e:
            raise _http(e) from e

    src = _outputs_wav(req.wav) if req.wav else _latest_tts()
    voice, sr = _read_mono(src)
    dur = voice.size / max(1, sr)
    if dur > sfx_lib.MAX_SECONDS:
        raise HTTPException(status_code=400, detail=f"音频太长：{dur:.0f}s > {sfx_lib.MAX_SECONDS:.0f}s")

    mixed, notes = sfx_lib.mix_into(voice, sr, [i.model_dump() for i in inserts])

    # 预混产物也是**会话产物**（退出即删）：它只是"发送时用的那条音频"，不是作品。
    # 与源同目录 —— 源在会话目录时产物也在会话目录（`derived_dir` 的同一口径）。
    out = session_out.derived_dir(src) / f"sfxmix_{src.stem}.wav"
    # ★ 临时名必须**每次唯一**：源相同 → `out` 相同（同一条 TTS + 同一组音效再混一次
    # 是常态，用户在反复试"加多少"），若 tmp 固定成 `out.name + ".tmp"`，两次并发的
    # 预混会**共写同一个文件**：A 写完 B 覆盖，再各自 `os.replace` —— 落地的成品可能
    # 是两者交织的半截 wav（时长/音量都对不上，且不报错）。固定名还会让"上一次崩溃
    # 留下的 .tmp"与"这一次正在写的 .tmp"无法区分。
    # 用 pid+uuid 而不是时间戳：时间戳在同毫秒并发下照样撞，且不如 uuid 直观。
    tmp = out.with_name(f".{out.stem}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 显式给 `format="WAV"`：临时名以 `.tmp` 结尾（故意不叫 .wav，免得被
        # `glob("*.wav")` 扫到半截文件），而 soundfile 靠扩展名推不出格式。
        sf.write(str(tmp), mixed, sr, subtype="PCM_16", format="WAV")
        os.replace(tmp, out)  # 原子替换：正在播的那条读到的要么旧版、要么新版
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise HTTPException(status_code=503, detail=f"混音结果写不进去：{e}") from e

    for ins in inserts:
        _bump(ins.sample)  # 与 /play 同一口径：混一次也算"用过一次"
    return {
        "ok": True,
        "wav": out.name,
        "seconds": round(mixed.size / max(1, sr), 2),
        "inserts": len(inserts),
        "skipped": notes,  # 上一步已校验过 id/位置，这里通常是空的（除非素材是空音频）
    }


# --------------------------------------------------------------- 歌声垫底（BGM）

# 「边唱边说」三档的后端（调研：docs/交接-AI必读.md）：
#   · /bgm/upload + /bgm/premix   —— A 档「垫歌预混」：把一首歌全程混进一条合成产物，
#     产出 bgmmix_*.wav，发送目标换成它（发送链路零改动）；
#   · /bgm/start /bgm/stop /bgm/gain —— C 档「实时垫歌」：往 CABLE 垫一层长 BGM，
#     走声板 worker 的「垫底层」（独立流，不抢占 one-shot，见 board_worker.py）。
# 歌的来源：**会话目录**（这里上传的、翻唱链路的成品）+ **常驻歌单目录** `media/songs`
# （用户自己丢进去的歌，跨会话保留）。两条都用「裸文件名」定位，**不收路径**。

#: BGM 素材上限（秒）：垫底是"一首歌"的量级，10 分钟以上的多半是下错了东西
MAX_BGM_S = 600.0
#: 上传 BGM 的大小上限（与翻唱上传同一口径；10 分钟 44.1k 单声道 PCM16 ≈ 50MB）
_MAX_BGM_BYTES = 50 * 1024 * 1024
#: 实时垫底的默认增益（≈ -10dB）：与 sfx 的 0.9 不是一回事——BGM 是长播放，
#: 要一直压在人声（实时变声/说话）下面，默认就按配平目标给。
BGM_DEFAULT_GAIN = 0.3


def _songs_dir() -> Path:
    """常驻歌单目录（跨会话保留，不随会话清空）。

    ⚠️ 必须**每次现读** `cfg.MEDIA_DIR`，不许在导入期绑成模块常量 —— 早绑定的副本
    会让测试的目录隔离静默失效，写入落回用户的真实 `media/`
    （`docs/犯错档案-工程.md` §8.36 / §8.37）。
    """
    return cfg.MEDIA_DIR / "songs"


def _bgm_last_file() -> Path:
    """「上次放的那首」指针（跨会话保留）。

    歌本身在会话目录里、退出即删，所以指针**可能指向一个已经不存在的歌**：
    读到就交叉核对 `_bgm_candidates()`，对不上当没有（`/bgm/recent` 的 last 字段）。
    宁可面板回落到第一首，也别让它指着一个点了 404 的名字。
    """
    return cfg.OUTPUTS_DIR / "soundboard_bgm_last.json"


#: 面板「换歌」列表上限：204px 的下拉里超过 8 行就只剩滚动了
BGM_RECENT_LIMIT = 8

#: 能**直接播**的扩展名（soundfile 能读的）。m4a/aac 刻意不在里面 —— 它们要先经
#: ffmpeg 转码（`/bgm/upload` 做的就是这件事），列出来只会点了报「读不出来」。
_BGM_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg")

#: 会话目录里**算歌**的文件名口径（白名单，不是黑名单）。
#:   同一个目录里还躺着 `tts_*.wav`（合成语音）、`sfxmix_*`（预混产物）、
#:   `rvc_*`（换声产物）—— 黑名单漏一个就会把一句 5 秒的人声当"歌"列给用户，
#:   而他只有点下去才知道选错了。所以只认两种：
#:     · `bgm_*`                 —— `/bgm/upload` 解码后的歌；
#:     · `cover_<毫秒>_<毫秒>.wav` —— 翻唱链路的**成品**（`cover_api.py:526-528`
#:       的 `cover_in_*` / `cover_voice_*` 是同一条链路的中间件，必须排除：
#:       voice 只有人声、in 是原始输入）。
_COVER_FINAL_RE = re.compile(r"^cover_\d+_\d+\.wav$", re.IGNORECASE)


def _is_song_name(name: str) -> bool:
    """会话目录里的文件名是不是"一首歌"（`media/songs` 常驻目录不做这层过滤 ——
    用户放进去的就是歌，认扩展名即可）。"""
    low = str(name or "").lower()
    if not low.endswith(_BGM_SUFFIXES):
        return False
    return low.startswith("bgm_") or bool(_COVER_FINAL_RE.match(name))


def _song_dirs() -> list[tuple[Path, str]]:
    """候选目录 + 来源标记。**会话在前** —— 与 `session_out.find` 的查找顺序一致，
    重名时列表里先出现的就是 `_bgm_path` 真会解析到的那一个。"""
    return [(session_out.session_dir(), "session"), (_songs_dir(), "library")]


def _song_item(p: Path, source: str) -> dict:
    """一条候选歌。时长读不到不算失败（面板只是不显示时长，照样能播）。"""
    dur = None
    with contextlib.suppress(Exception):
        dur = round(float(sf.info(str(p)).duration), 2)
    return {
        "name": p.name,
        "id": p.stem,          # 与 `/bgm/start` 返回的 id 同一口径（board_worker 的 sid）
        "duration_s": dur,
        "source": source,
        "mtime": int(p.stat().st_mtime),
    }


def _bgm_candidates(limit: int = BGM_RECENT_LIMIT) -> list[dict]:
    """最近能放的歌（按修改时间倒序）。只读：不碰 worker、不碰声卡。"""
    seen: set[str] = set()
    found: list[tuple[float, dict]] = []
    for d, source in _song_dirs():
        if not d.is_dir():
            continue
        try:
            files = [p for p in d.iterdir() if p.is_file()]
        except OSError:      # 目录刚被清掉 / 权限问题：当作没有，别让整个端点 500
            continue
        for p in files:
            if p.name in seen:
                continue
            if source == "session" and not _is_song_name(p.name):
                continue
            if source == "library" and not p.name.lower().endswith(_BGM_SUFFIXES):
                continue
            seen.add(p.name)
            try:
                found.append((p.stat().st_mtime, _song_item(p, source)))
            except OSError:
                continue
    found.sort(key=lambda t: t[0], reverse=True)
    return [it for _, it in found[: max(1, int(limit))]]


def _bgm_last() -> str | None:
    try:
        data = json.loads(_bgm_last_file().read_text(encoding="utf-8"))
        name = str(data.get("name") or "").strip()
    except Exception:
        return None
    return name or None


def _remember_bgm(name: str) -> None:
    """记住这次放的是哪首（尽力而为：写不进去不影响播放）。"""
    try:
        dst = _bgm_last_file()
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_text(json.dumps({"name": name, "at": int(time.time())}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, dst)
    except OSError:
        pass


def _bgm_path(bgm: str) -> Path:
    """歌的裸名 → 绝对路径（预混与实时垫底共用）。找不到/非法都当场报错。

    查找顺序：会话目录 → `outputs` 根（`session_out.find` 的老口径）→ **常驻歌单**
    `media/songs`。最后这一处是 2026-10-22 加的：面板的「换歌」列表里就有常驻歌，
    只查会话目录的话，列表点得中、播放却 404。
    """
    p = session_out.find(bgm)
    if p is not None:
        return p
    n = str(bgm or "").strip()
    if not n or n in (".", "..") or any(c in n for c in "/\\"):
        raise HTTPException(status_code=400, detail=f"非法的音频名：{bgm!r}")
    q = _songs_dir() / n
    if q.is_file():
        return q
    raise HTTPException(
        status_code=404,
        detail=f"找不到这首歌：{n}（本次导入/翻唱的歌退出应用会清空；常驻歌放在 media/songs 里）",
    )


class BgmPremixReq(BaseModel):
    wav: str | None = None  # 要垫的人声（缺省=最近一次合成产物，与 /premix 同口径）
    bgm: str  # 会话里的歌（裸名）
    lead_db: float = sfx_lib.BGM_LEAD_DB  # BGM 比人声低多少 dB
    fade_in_s: float = sfx_lib.BGM_FADE_DEFAULTS[0]
    fade_out_s: float = sfx_lib.BGM_FADE_DEFAULTS[1]
    start_s: float = 0.0  # 从歌的哪一秒开始垫（跳前奏直取副歌）


@router.post("/bgm/upload")
async def bgm_upload(file: UploadFile = File(...)):
    """上传一首歌当 BGM 素材：解码成单声道 PCM16 wav 落会话目录，返回裸名。

    为什么先转码而不是存原始文件：垫底/垫层两侧都直接读数组或喂 worker，
    mp3/m4a 的解码能力不能赌用户机器上的 libsndfile —— ffmpeg 兜一次底就稳了
    （与翻唱链路 `_cover_worker` ① 同一做法）。失败时 soundfile 能读的格式
    （wav/flac/ogg）不依赖 ffmpeg 也能过。
    """
    import io

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > _MAX_BGM_BYTES:
        raise HTTPException(status_code=413, detail="文件过大：>50MB 拒绝")
    data = sr = None
    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:
        pass  # soundfile 不认识的格式 → 落到 ffmpeg 兜底
    if data is None:
        import common
        import subprocess as _sp

        ff = common.find_ffmpeg()
        if not ff:
            raise HTTPException(status_code=400, detail="音频解码失败：既不是 wav/flac/ogg，也没装 ffmpeg")
        tmp = session_out.new_path("bgm_dec", Path(file.filename or "s.mp3").suffix or ".mp3")
        dst = session_out.new_path("bgm_dec", ".wav")
        try:
            tmp.write_bytes(raw)
            r = _sp.run(
                [ff, "-y", "-loglevel", "error", "-i", str(tmp), "-ar", "44100", "-ac", "1", str(dst)],
                capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0 or not dst.exists():
                raise HTTPException(
                    status_code=400, detail="音频解码失败：" + (r.stderr or "").strip()[:300]
                )
            data, sr = sf.read(str(dst), dtype="float32", always_2d=False)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                dst.unlink(missing_ok=True)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    dur = len(data) / int(sr or 1)
    if dur < 1.0:
        raise HTTPException(status_code=400, detail=f"太短了：{dur:.1f}s（垫底至少要 1s）")
    if dur > MAX_BGM_S:
        raise HTTPException(
            status_code=400,
            detail=f"太长了：{dur:.0f}s > {MAX_BGM_S:.0f}s —— 先用翻唱页/别的工具截一段",
        )

    out = session_out.new_path("bgm")
    sf.write(str(out), np.asarray(data, dtype=np.float32), int(sr), subtype="PCM_16")
    return {
        "ok": True,
        "name": out.name,
        "duration_s": round(dur, 2),
        "url": f"/api/media/outputs/{session_out.rel_url(out.name)}",
    }


@router.post("/bgm/premix")
def bgm_premix(req: BgmPremixReq):
    """把一首歌**全程垫进**一条合成产物（A 档「歌声垫底预混」）。

    输出 `bgmmix_<原名>.wav`，发送目标换成它 —— 与 `/premix` 同一套产物语义
    （不劫持"最近一条 TTS 产物"的口径 + 原子替换）。混音本体在 `sfx_lib.mix_bgm`：
    人声是时间轴（输出长度=人声长度）、RMS 自动配平、首尾淡入淡出。

    与 `/premix` 的一个刻意差别：这是**明确的用户动作**但 DSP 侧没有"部分失败"的
    形态（一首歌要么垫得上要么整单失败），所以不需要 `skipped` 列表——notes 里的
    是**提示**（循环补满/电平量不出），不是失败。
    """
    lead_db = min(30.0, max(0.0, float(req.lead_db)))
    fade_in = min(10.0, max(0.0, float(req.fade_in_s)))
    fade_out = min(10.0, max(0.0, float(req.fade_out_s)))
    start_s = min(3600.0, max(0.0, float(req.start_s)))

    bgm_path = _bgm_path(req.bgm)
    bgm, bgm_sr = _read_mono(bgm_path)
    if bgm.size / max(1, bgm_sr) > MAX_BGM_S:
        raise HTTPException(status_code=400, detail="这首歌太长，先截一段再用（≤600s）")

    src = _outputs_wav(req.wav) if req.wav else _latest_tts()
    voice, sr = _read_mono(src)
    dur = voice.size / max(1, sr)
    if dur > sfx_lib.MAX_SECONDS:
        raise HTTPException(status_code=400, detail=f"音频太长：{dur:.0f}s > {sfx_lib.MAX_SECONDS:.0f}s")

    try:
        mixed, notes = sfx_lib.mix_bgm(
            voice, sr, bgm, bgm_sr,
            lead_db=lead_db, fade_in_s=fade_in, fade_out_s=fade_out, start_s=start_s,
        )
    except SfxError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    out = session_out.derived_dir(src) / f"bgmmix_{src.stem}.wav"
    tmp = out.with_name(f".{out.stem}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp")
    try:
        sf.write(str(tmp), mixed, sr, subtype="PCM_16", format="WAV")
        os.replace(tmp, out)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise HTTPException(status_code=503, detail=f"混音结果写不进去：{e}") from e
    return {
        "ok": True,
        "wav": out.name,
        "seconds": round(mixed.size / max(1, sr), 2),
        "url": f"/api/media/outputs/{session_out.rel_url(out.name)}",
        "notes": notes,  # 提示（循环补满/电平量不出），不是失败——如实带给前端
    }


class BgmStartReq(BaseModel):
    bgm: str  # 会话里的歌（裸名）
    gain: float = BGM_DEFAULT_GAIN
    loop: bool = True  # 歌比说话短时循环补满（与预混同一语义）


@router.get("/bgm/recent")
def bgm_recent():
    """面板「放歌」页的换歌列表 + 上次放的那首。**只读**（不碰 worker、不碰声卡）。

    为什么会话目录也要列：桌宠面板是「此刻放给对面听」的遥控器，用户在通话前刚
    导入/翻唱的那首歌，必须能在面板里直接点到 —— 让他为此切回主界面就失去意义了。
    """
    items = _bgm_candidates()
    last = _bgm_last()
    if last and not any(it["name"] == last for it in items):
        last = None   # 指针指向会话里的歌、而会话已被清空 → 当作没有
    return {"ok": True, "items": items, "last": last}


@router.post("/bgm/start")
def bgm_start(req: BgmStartReq):
    """往 CABLE **实时垫一层 BGM**（C 档）：说话/实时变声的同时垫歌。

    走声板 worker 的「垫底层」——独立音频流，与 one-shot 音效互不抢占：
    垫上歌之后点"爆炸"，炸的是音效，歌继续（这正是"垫"与"插播"的区别，
    见 board_worker.py 头注释）。不获取 `_send_lock`，理由与 /play 相同。
    """
    path = _bgm_path(req.bgm)
    gain = min(1.5, max(0.0, float(req.gain)))
    try:
        msg = _command({"bgm": str(path), "id": path.stem, "gain": gain, "loop": bool(req.loop)})
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    _remember_bgm(path.name)   # 面板下次悬停/重启后回到这首（尽力而为，失败不影响播放）
    return {
        "ok": True,
        "playing": True,
        "id": path.stem,
        "duration_s": msg.get("duration_s"),
        "loop": bool(req.loop),
    }


@router.post("/bgm/stop")
def bgm_stop():
    """停掉垫底层（one-shot 音效不受影响 —— 它们是两条独立的流）。"""
    try:
        _command({"bgm_stop": True})
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True}


class BgmGainReq(BaseModel):
    gain: float


@router.post("/bgm/gain")
def bgm_gain(req: BgmGainReq):
    """实时调垫底层的音量（不用重开 —— 流里读的是同一个变量）。"""
    gain = min(1.5, max(0.0, float(req.gain)))
    try:
        _command({"bgm_gain": gain})
    except (RuntimeError, OSError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "gain": gain}


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
        raise HTTPException(status_code=400, detail=f"音频读取失败：{e}") from e
    dur = len(data) / int(sr or 1)
    if dur > _MAX_IMPORT_S:
        raise HTTPException(status_code=400, detail=f"太长了：{dur:.1f}s > {_MAX_IMPORT_S:.0f}s")
    if dur < 0.05:
        raise HTTPException(status_code=400, detail="太短了（<0.05s）")
    if data.ndim > 1:
        data = data.mean(axis=1)

    sid = _sanitize_stem(file.filename or "imported")
    if (sfx_lib.SAMPLES_DIR / f"{sid}.wav").is_file():
        raise HTTPException(status_code=400, detail=f"名字与出厂音效重复：{sid}（请改名）")

    sfx_lib.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = sfx_lib.IMPORT_DIR / f"{sid}.wav"
    sf.write(str(out), np.asarray(data, dtype=np.float32), int(sr), subtype="PCM_16")
    return {"ok": True, "id": sid, "duration_s": round(dur, 2)}


def _purge_stats(ids) -> list[str]:
    """把若干素材 id 从计数里清掉（返回真清掉的那些）。

    为什么必须清：计数是**按 id 字符串**存的，而 id 会跟着素材消失。
    不清就会出现两种错觉 —— ① 重装同名包，"用过 7 次"凭空继承；
    ② 反复装卸之后 `soundboard_stats.json` 里攒一堆永远不会再被读到的键。
    """
    with _STATS_LOCK:
        stats = _load_stats()
        gone = [sid for sid in ids if stats.pop(sid, None) is not None]
        if gone:
            with contextlib.suppress(OSError):
                STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATS_FILE.write_text(
                    json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        return gone


@router.delete("/{sample_id}")
def soundboard_delete(sample_id: str):
    """删除**导入**的素材（出厂素材不许删 —— 删了重装才有，这不该由用户承担）。"""
    path, builtin = _resolve_path(sample_id)
    if builtin:
        raise HTTPException(status_code=400, detail="出厂音效不可删除")
    # ❗包内素材（id 形如 `arcade/coin`）**根本走不到这里**（去 2026-09-24 真机验证：
    # `DELETE /api/soundboard/arcade/coin` 得到 405，不是这里写的 400）。原因：
    # 这条路由只有**一段** `{sample_id}`，而含 `/` 的 id 是两段，压根不匹配 ——
    # 真环境里它落到 SPA 的 GET 兜底上，于是回"方法不允许"。
    # 一度在这里写了句 `if split_id(...)` 的友好报错，**那是死代码**（只能靠直接
    # 调函数碰到），已删：留着会让人以为它挡了什么事。
    #
    # 那要不要把路由改成 `{sample_id:path}`？**不要** —— 声明顺序是行为：
    # 那条通配会先吃掉 `DELETE /packs/{pack_id}`，于是"卸载整包"变成"这是包内素材"，
    # 卸载功能直接死掉（正是 `plugin_manifest.mount_plan` 注释里说的那类遮蔽）。
    # 真正的防护在别处：界面只给 `removable` 的素材（即导入的）删除入口，
    # 后端也用 `removable=False` 明确告诉它包内素材不能单条删。
    path.unlink()
    _purge_stats([sample_id])
    return {"ok": True, "id": sample_id}


# --------------------------------------------------------------- 音效包（成套素材）


def _pack_http(e: sfx_packs.PackError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@router.get("/packs")
def soundboard_packs():
    """已安装的音效包（含坏包，带原因 —— 东西消失了就该能被问出来）。"""
    packs = sfx_packs.list_packs()
    return {
        "ok": True,
        "packs": packs,
        "samples": sum(int(p.get("count") or 0) for p in packs if not p.get("broken")),
    }


@router.get("/packs/available")
def soundboard_packs_available():
    """货架：清单里可以装的包（未配清单时 `items` 为空 + 一句 `note`，**不是错误**）。

    单独一条端点（而不是并进 `/packs`）：一个问"我装了什么"，一个问"外面有什么"，
    后者会真的去取网络或读清单文件，不该拖慢前者。
    """
    return {"ok": True, **sfx_packs.load_index()}


@router.post("/packs/install")
async def soundboard_pack_install(
    file: UploadFile = File(...),
    pack_id: str = Form(""),
    overwrite: bool = Form(False),
):
    """装一个本地 zip 音效包（离线路子：别人发给你的包、或你自己打的包）。

    `pack_id` 留空时按包内的 `pack.json` / zip 目录名推（见 `sfx_packs.install_zip`）。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    # id 回落链：表单显式指定 > zip 里的路径/pack.json > **上传的文件名**。
    # 最后那一跳是实际最常用的：用户手上就是 `街机音效.zip`，不该逼他再想个 id
    # （少了它，一个没写 id 又没套目录的包会直接报「给不出合法的 id」）。
    pid = pack_id or sfx_packs.clean_stem(file.filename or "") or None
    try:
        res = sfx_packs.install_zip(raw, pid, overwrite=overwrite)
    except sfx_packs.PackError as e:
        raise _pack_http(e) from e
    return {"ok": True, **res}


class PackFetchReq(BaseModel):
    id: str
    overwrite: bool = False


@router.post("/packs/download")
def soundboard_pack_download(req: PackFetchReq):
    """从清单里的地址下载并安装（市场路子，带 sha256 校验与域名白名单）。"""
    try:
        res = sfx_packs.install_from_index(req.id, overwrite=req.overwrite)
    except sfx_packs.PackError as e:
        raise _pack_http(e) from e
    return {"ok": True, **res}


@router.delete("/packs/{pack_id}")
def soundboard_pack_uninstall(pack_id: str):
    """卸载一个音效包：素材与计数一起没了（计数见 `_purge_stats` 的理由）。"""
    try:
        res = sfx_packs.uninstall(pack_id)
    except sfx_packs.PackError as e:
        raise _pack_http(e) from e
    return {"ok": True, "purged": _purge_stats(res["ids"]), **res}

"""快捷短语板：课堂高频用语一键合成/重放 —— 直播、录课时让老师嗓子歇一歇。

场景（2026-10-03 与用户对齐的「方案③基础版」）：老师直播/录网课时总有那么几句
要说几十遍的话（「我们来看这道题」「大家先思考一分钟」）。把它们预设下来，
点一下就用**当前选中的音色**（含声音保存的克隆音色）念出来 —— 那一句就不用
老师自己开口了。

设计取舍
--------
· **复用 `tts_api.synth_wav`**：音效标记、ICL、报错口径全部与「输字变声」一致，
  不另写一条合成链路。
· **产物持久缓存**（`outputs/phrase_cache/<sha>.wav`），**不**走会话目录：
  短语板的用法是同一句话反复点（一节课点几十次），每次都重新合成（冷态还可能
  等 30s+）就失去意义了。缓存键 = sha1(voice_id|text) —— 换音色、改文字都会
  自然 miss，不需要任何失效逻辑。
  为什么不怕占磁盘：短语都是几秒钟的话，一条 ~100-250KB；另有 `MAX_CACHE_FILES`
  按 mtime 淘汰兜底。`storage.py` 的清理只 glob outputs 根（不递归），不会误删。
· **状态文件** `outputs/voice_phrases.json`，与 plugins.json 等同处 outputs/
  （后端必须能脱离 Electron 独立跑，见 plugin_manifest.py 的同款论证）。
  ⚠️ 路径每次现读 `cfg.OUTPUTS_DIR`，不许模块级早绑定（本仓 §8.36/§8.37 连环坑）。

锁的口径
--------
`_STORE_LOCK` 保护「读-改-写」状态文件这一整段；`_SYNTH_LOCK` 串行化真合成
（TTS worker 单进程，并发点两条只会互相拖慢）。**持 `_STORE_LOCK` 期间只许调
`_read_unlocked()`** —— `load_items()` 会再拿同一把锁，`threading.Lock` 不可
重入，持锁重入 = 永久死锁（这类锁规约错误测试抓得住：CRUD 用例都会真实持锁跑）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

import config as cfg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)

MAX_PHRASES = 50
MAX_TEXT_LEN = 200
#: 磁盘兜底：缓存 wav 超过这个数就按 mtime 淘汰最旧的（正常教学用法远到不了）。
MAX_CACHE_FILES = 500

#: 出厂短语：首次使用自动铺好，老师开箱即有可点的东西（都是课堂真实高频句）。
DEFAULT_PHRASES = (
    "同学们，我们来看这道题。",
    "大家先思考一分钟。",
    "请把这一页记到笔记本上。",
    "这道题的答案是B。",
    "下课时间到了，同学们再见。",
)

_STORE_LOCK = threading.Lock()
#: 合成互斥：TTS worker 是单进程串行，并发点两条短语只会互相拖慢还可能打爆队列。
#: 命中缓存不需要这把锁（读文件是无害并发）。
_SYNTH_LOCK = threading.Lock()


def _state_file() -> Path:
    return cfg.OUTPUTS_DIR / "voice_phrases.json"


def _cache_dir() -> Path:
    return cfg.OUTPUTS_DIR / "phrase_cache"


def _cache_key(voice_id: str, text: str) -> str:
    return hashlib.sha1(f"{voice_id}|{text}".encode("utf-8")).hexdigest()[:20]


#: speak() 返回的产物 URL 前缀（/play 换算真实文件用；与 media 静态路由同源）
_MEDIA_OUTPUTS_PREFIX = "/api/media/outputs/"


def _url_to_path(url: str) -> Path | None:
    """speak() 产物 URL → outputs/ 下的真实文件路径（播进声卡前必须拿到实体）。

    只认本模块自己拼出的前缀；带 `..` / 空段的成分一律拒掉 —— 与
    `soundboard._outputs_wav` 同一警惕：换算结果不许逃出 outputs/。
    （`.session` 这种点开头的目录是 speak() 的合法兜底产物，放行。）
    """
    if not url.startswith(_MEDIA_OUTPUTS_PREFIX):
        return None
    parts = url[len(_MEDIA_OUTPUTS_PREFIX) :].split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    return cfg.OUTPUTS_DIR.joinpath(*parts)


def _write(items: list[dict]) -> None:
    """落盘（调用方必须已持 `_STORE_LOCK`）。"""
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), "utf-8")


def _read_unlocked() -> list[dict]:
    """读短语列表，**不拿锁、不播种**；文件缺失/损坏时返回出厂短语（内存值）。

    文件缺失时返回默认值而不是空表：用户没点开过面板（没触发过 GET 的播种），
    直接对一条出厂短语调 speak/update 也不该 404。
    """
    f = _state_file()
    if f.is_file():
        try:
            raw = json.loads(f.read_text("utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, list):
                return [
                    {"id": str(i.get("id") or ""), "text": str(i.get("text") or "")}
                    for i in items
                    if isinstance(i, dict) and i.get("id")
                ]
        except Exception:
            pass  # 状态文件坏了不该把整块功能带走：当默认短语，下次保存覆写回来
    return [{"id": uuid.uuid4().hex[:8], "text": t} for t in DEFAULT_PHRASES]


def load_items() -> list[dict]:
    """读全部短语；文件不存在时**落盘播种**出厂短语（幂等：有文件就不再动）。"""
    with _STORE_LOCK:
        items = _read_unlocked()
        if not _state_file().is_file():
            _write([{**it, "created_at": int(time.time())} for it in items])
        return items


def _find(items: list[dict], pid: str) -> dict:
    for it in items:
        if it["id"] == pid:
            return it
    raise HTTPException(status_code=404, detail="没有这条短语（可能已被删除，刷新一下）")


def _validate_text(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="短语内容不能为空")
    if len(t) > MAX_TEXT_LEN:
        raise HTTPException(status_code=400, detail=f"短语最长 {MAX_TEXT_LEN} 字（长句请走「输字变声」）")
    return t


def _trim_cache() -> None:
    """缓存文件数超上限时按 mtime 淘汰最旧的（尽力而为，删不掉的跳过）。"""
    d = _cache_dir()
    files = [p for p in d.glob("*.wav") if p.is_file()]
    if len(files) <= MAX_CACHE_FILES:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files[: len(files) - MAX_CACHE_FILES]:
        try:
            p.unlink()
        except OSError:
            continue


def _duration_s(p: Path) -> float:
    try:
        import soundfile as sf

        return round(float(sf.info(str(p)).duration), 1)
    except Exception:
        return 0.0


def speak(text: str, voice_id: str) -> dict:
    """把一句话用指定音色念出来（带持久缓存），返回可直接播放的 dict。

    缓存键 = sha1(voice_id|text)：换音色 / 改文字自然 miss，无失效逻辑可漏。
    """
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{_cache_key(voice_id, text)}.wav"
    url = f"/api/media/outputs/phrase_cache/{dst.name}"

    if dst.is_file():
        return {"url": url, "duration_s": _duration_s(dst), "cached": True, "voice_id": voice_id}

    with _SYNTH_LOCK:
        if dst.is_file():  # double-checked：排队等锁期间别人可能已经合成完同一条
            return {"url": url, "duration_s": _duration_s(dst), "cached": True, "voice_id": voice_id}
        from tts_api import synth_wav  # 延迟导入：避免模块级依赖环

        out, _dur, _vid = synth_wav(text, voice_id)
        try:
            shutil.copy2(out, dst)
        except OSError:
            # 缓存写不进去不算硬失败：synth_wav 的会话产物还在，直接播它，
            # 只是这条下次还得重新合成。cached=False 让前端能照常显示"刚合成"。
            return {
                "url": f"/api/media/outputs/{out.parent.name}/{out.name}",
                "duration_s": _dur,
                "cached": False,
                "voice_id": voice_id,
            }
    _trim_cache()
    return {"url": url, "duration_s": _duration_s(dst), "cached": False, "voice_id": voice_id}


class PhraseBody(BaseModel):
    text: str


class SpeakBody(BaseModel):
    voice_id: str = ""


@router.get("/voice-phrases")
def list_phrases():
    """全部快捷短语（前端面板的渲染依据；首次调用时播种出厂短语）。"""
    return {"ok": True, "items": load_items()}


@router.post("/voice-phrases")
def add_phrase(body: PhraseBody):
    text = _validate_text(body.text)
    with _STORE_LOCK:
        items = _read_unlocked()
        if len(items) >= MAX_PHRASES:
            raise HTTPException(status_code=400, detail=f"最多 {MAX_PHRASES} 条，先删一条再加")
        item = {"id": uuid.uuid4().hex[:8], "text": text, "created_at": int(time.time())}
        items.append(item)
        _write(items)
    return {"ok": True, "item": {"id": item["id"], "text": item["text"]}}


@router.put("/voice-phrases/{pid}")
def update_phrase(pid: str, body: PhraseBody):
    text = _validate_text(body.text)
    with _STORE_LOCK:
        items = _read_unlocked()
        target = _find(items, pid)
        # 改文字后旧缓存自然失效（键含 text），无需清理。
        target["text"] = text
        _write(items)
    return {"ok": True, "item": {"id": pid, "text": text}}


@router.delete("/voice-phrases/{pid}")
def delete_phrase(pid: str):
    with _STORE_LOCK:
        items = _read_unlocked()
        _find(items, pid)
        _write([it for it in items if it["id"] != pid])
    return {"ok": True}


@router.post("/voice-phrases/{pid}/speak")
def speak_phrase(pid: str, body: SpeakBody):
    """点一下：用指定音色（缺省=当前选中）念这条短语；第二次点击零等待。"""
    with _STORE_LOCK:
        phrase = _find(_read_unlocked(), pid)
    voice_id = body.voice_id or _selected_voice()
    if not voice_id:
        raise HTTPException(status_code=400, detail="请先选择音色")
    r = speak(phrase["text"], voice_id)
    return {"ok": True, **r}


@router.post("/voice-phrases/{pid}/play")
def play_phrase(pid: str, body: SpeakBody):
    """全局热键链路（Ctrl+Alt+1~5）：念这条短语并**直接播进虚拟声卡**。

    与 /speak 的分工：speak 只产出 URL（前端浏览器播，走本机扬声器，窗口藏着
    就听不见）；play 复用同一条持久缓存（speak() 命中即零合成），再把 wav 写进
    CABLE 渲染端 —— PPT 放映/直播/视频通话里按热键，对面那头才收得到。
    **立即返回**（one-shot 语义，与声板 /play 一致）；连按 = 抢占重播，不排队。

    ★ 出口按**真声直讲是否在跑**分流（2026-10-04）：
      - 直讲开着 → `passthrough.play_overlay`：把这条短语**叠加**到老师的真声上，
        **不打断讲课**（两个流各走各的，CABLE 里混音）。这是"老师真声讲课 +
        偶尔插一句 AI 音"场景的正解。
      - 直讲没开 → `soundboard._command`：维持原行为（`sd.play` 独占播放）。
        **零行为变化** —— 不开直讲的用户完全感知不到这次改动。

    环境不支持时 503（VM_SOUNDBOARD=0 / VM_PASSTHROUGH=0 / 找不到播放设备）：
    热键侧静默，与级联热键对后端离线的处理同一口径。
    """
    with _STORE_LOCK:
        phrase = _find(_read_unlocked(), pid)
    voice_id = body.voice_id or _selected_voice()
    if not voice_id:
        raise HTTPException(status_code=400, detail="请先选择音色")
    r = speak(phrase["text"], voice_id)
    wav = _url_to_path(r["url"])
    if wav is None or not wav.is_file():
        raise HTTPException(status_code=500, detail="合成产物不存在，请重试")

    # worker 按这个 id 记解码缓存：同音色同文的短语重播零 IO
    clip_id = f"phrase_{_cache_key(voice_id, phrase['text'])}"
    try:
        # 延迟导入：passthrough 是独立功能域（与 soundboard 同理，不建模块级依赖）
        import passthrough

        if passthrough.is_running():
            # 叠加：gain 留余量（真声也在里面，给两路相加留空间防削波）
            msg = passthrough.play_overlay(str(wav), clip_id, gain=0.9)
            return {
                "ok": True,
                "playing": True,
                "mixed": True,
                "duration_s": msg.get("duration_s"),
                "cached": r["cached"],
            }
        from soundboard import _command  # 延迟导入：声板是独立功能域，不建模块级依赖

        msg = _command(
            {
                "wav": str(wav),
                "id": clip_id,
                # 1.0：走这条时这条语音就是此刻的全部内容（不像音效要与人声叠加留余量）
                "gain": 1.0,
            }
        )
    except (RuntimeError, OSError) as e:
        # 与声板 /play 同口径：Popen 失败与"设备找不到"都是"现在播不了"→ 503
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "playing": True, "duration_s": msg.get("duration_s"), "cached": r["cached"]}


def _selected_voice() -> str:
    from common import selected_voice

    return selected_voice()

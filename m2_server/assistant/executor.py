"""执行器：把一条**已被确认**的提案变成实际动作。

唯一入口是 `/assistant/proposals/{id}/confirm`。本模块不判断"要不要发"，
也不做任何自动触发 —— 那是 `api.py` 的护栏，也是这个功能存在的全部理由。

执行形态（为什么是后台线程而不是同步返回）
------------------------------------------
发一首歌的真实耗时是"下载 10~40s + 转码 2s + 发送（=音频时长+3s）"，
一条 54 秒的语音条端到端要 1~2 分钟。同步 HTTP 会让前端转圈转到用户以为卡死，
而这条链路里**每一步都有可读的进度**（正在下载 / 正在转码 / 正在发送）。
所以 confirm 立即返回 `running`，前端轮询提案状态。

复用而不是重写
--------------
- 取流：`ytdlp_fetch.fetch`（白名单、取消票、兜底全在那里）
- 转码：`assistant.audio`（只做"变 PCM + 截段"，判据长度来自 `wechat_voice`）
- 换声：`rvc_convert.rvc_convert`（常驻 worker）
- 发送：`wechat_voice.send_voice` / `send_text`（预算护栏、UIA、降级全在那里）

这里**一行都不重写**：那几条链路的护栏（60s 预算、录音环境、发送后校验）是踩过
真机事故才长出来的，抄一份就等于把事故也抄一份。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

try:
    import config as cfg
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config as cfg

from . import audio, search, store
from .schema import STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, Proposal

#: 正在跑的提案 id → 线程。防止同一条被点两次（store 的状态机是第一道，这是第二道）。
_INFLIGHT: dict[str, threading.Thread] = {}
_INFLIGHT_LOCK = threading.Lock()


class ExecError(RuntimeError):
    """执行失败。消息**直接给人看**，不要包成英文堆栈。"""


def _out_dir() -> Path:
    d = Path(cfg.OUTPUTS_DIR) / "assistant"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _step(prop: Proposal, steps: list[str], line: str) -> None:
    """记一步进度：既进**局部列表**（最终写进 result），也立刻落盘（前端轮询能看到）。

    ★ 必须写同一个列表。早期版本只写 store、局部另有一份 `steps`，结果 `run()`
    最后用局部列表覆盖 `prop.result` —— 中间步骤在 UI 上凭空消失（实测少两步）。
    """
    steps.append(line)
    prop.result = {"steps": list(steps)}
    store.update(prop)


# ---------------------------------------------------------------- 各 kind 的执行


def _run_song(prop: Proposal) -> dict:
    """搜 → 下载 → 截段 →（可选换声）→ 发语音条。"""
    p = prop.params
    steps: list[str] = []

    url = (p.get("url") or "").strip()
    title = p.get("title") or p.get("query") or ""
    if not url:
        _step(prop, steps, f"搜索「{p['query']}」")
        rows = search.search_song(p["query"], limit=5)
        best = search.pick_best(rows)
        url = best["url"]
        title = best["title"]
        steps.append(f"选中：{title}")

    _step(prop, steps, f"取回音频：{title or url}")
    import ytdlp_fetch

    info = ytdlp_fetch.fetch(url)
    src = Path(info["path"])
    steps.append(f"已下载 {src.name}（{info['bytes'] / 2**20:.1f} MB，来自 {info['site']}）")

    wav = _out_dir() / f"assistant_{prop.id}.wav"
    _step(prop, steps, "转码截段")
    audio.clip_for_wechat(
        src,
        wav,
        start_s=(float(p["start_s"]) if p.get("start_s") not in (None, "") else None),
    )
    dur = audio.probe_duration(wav)
    steps.append(f"片段 {dur:.1f}s → {wav.name}" if dur else f"片段 → {wav.name}")

    voice = (p.get("voice") or "").strip()
    if voice:
        _step(prop, steps, f"换声：{voice}")
        import rvc_convert

        rvc_id = rvc_convert.resolve_rvc_voice(voice)
        if not rvc_id:
            raise ExecError(f"音色「{voice}」没有可用的 RVC 模型，不能换声（要不改成不换声再发）")
        wav = rvc_convert.rvc_convert(wav, rvc_id)
        steps.append(f"换声完成：{wav.name}")

    _step(prop, steps, "发送到微信")
    import wechat_voice

    resp = wechat_voice.send_voice(wechat_voice.SendVoiceReq(wav=str(wav.resolve())))
    body = _json_of(resp)
    if not body.get("ok"):
        raise ExecError(f"微信没发出去：{body.get('error') or body.get('outcome') or '未知原因'}")
    steps.append("已发送")
    return {"steps": steps, "wav": wav.name, "title": title, "url": url, "send": body}


def _run_voice(prop: Proposal) -> dict:
    """文字 → TTS（可选 RVC）→ 发语音条。整条链路交给 `wechat_voice.send_text`。"""
    p = prop.params
    steps: list[str] = []
    dialect = (p.get("dialect") or "").strip()
    if dialect:
        # 诚实地拒绝，而不是"用普通话念一遍然后说这是粤语"。
        raise ExecError(
            f"当前 TTS 只会普通话和英文，做不了「{dialect}」发音。"
            "可行的替代：①把词写成方言字（如粤语「我唔知」）再发，TTS 念出来会带味道；"
            "②先用 TTS 生成，再用一个方言音色做 RVC 换声。"
        )

    voice = (p.get("voice") or "").strip()
    rvc_voice = (p.get("rvc_voice") or "").strip()
    steps.append(f"合成 {len(p['text'])} 字")
    import wechat_voice

    req = wechat_voice.SendTextReq(
        text=p["text"], voice_id=voice, rvc_voice=rvc_voice, no_rvc=not bool(rvc_voice)
    )
    resp = wechat_voice.send_text(req)
    body = _json_of(resp)
    if not body.get("ok"):
        raise ExecError(f"没发出去：{body.get('error') or body.get('outcome') or '未知原因'}")
    steps.append("已发送")
    return {"steps": steps, "send": body}


def _run_reply(prop: Proposal) -> dict:
    """回复草稿：只回文本，永不发送。

    这里有副作用才是 bug —— 用户要的是"分析完我自己发"，替他按发送就是越界。
    """
    return {"steps": ["已生成草稿（未发送）"], "draft": prop.params.get("text", "")}


_RUNNERS = {
    "song.send": _run_song,
    "voice.say": _run_voice,
    "reply.draft": _run_reply,
}


def _json_of(resp) -> dict:
    """把 FastAPI 端点可能返回的 `dict` / `JSONResponse` 统一成 dict。"""
    if isinstance(resp, dict):
        return resp
    body = getattr(resp, "body", None)
    if isinstance(body, (bytes, bytearray)):
        import json

        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "发送接口返回了无法解析的内容"}
    return {"ok": False, "error": f"发送接口返回了意外的类型 {type(resp).__name__}"}


# ---------------------------------------------------------------- 对外入口


def run(prop: Proposal) -> Proposal:
    """同步执行一条提案，并把结果写回 store。异常一律转成 failed（不往外抛）。"""
    runner = _RUNNERS.get(prop.kind)
    if runner is None:
        prop.touch(STATUS_FAILED)
        prop.error = f"没有这个动作：{prop.kind}"
        store.update(prop)
        store.append_history(prop)
        return prop

    prop.touch(STATUS_RUNNING)
    store.update(prop)
    t0 = time.perf_counter()
    try:
        result = runner(prop)
        result["elapsed_s"] = round(time.perf_counter() - t0, 1)
        prop.result = result
        prop.touch(STATUS_DONE)
    except Exception as exc:  # noqa: BLE001 —— 执行器的职责就是"别让一条失败拖垮整个后端"
        prop.result = prop.result if isinstance(prop.result, dict) else {}
        prop.result.setdefault("steps", []).append(f"失败：{exc}")
        prop.error = str(exc)
        prop.touch(STATUS_FAILED)
        # 堆栈只进后端日志，不进 UI（用户要看的是"哪一步、为什么"）
        traceback.print_exc()
    store.update(prop)
    store.append_history(prop)
    return prop


def start(prop: Proposal) -> tuple[bool, str]:
    """后台执行。返回 `(ok, 拒绝原因)`。

    状态机与线程表**双保险**：单看状态机，两次几乎同时的 confirm 可能都读到
    pending（先读后写）；单看线程表，进程重启后线程表是空的。两个都查才稳。
    """
    ok, why = prop.usable()
    if not ok:
        return False, why
    with _INFLIGHT_LOCK:
        if prop.id in _INFLIGHT and _INFLIGHT[prop.id].is_alive():
            return False, "这条已经在执行了"
        th = threading.Thread(target=run, args=(prop,), name=f"assistant-{prop.kind}", daemon=True)
        _INFLIGHT[prop.id] = th
        th.start()
    return True, ""


def wait(prop_id: str, timeout_s: float = 5.0) -> bool:
    """等某条跑完（测试用；生产路径靠轮询）。返回是否已结束。"""
    with _INFLIGHT_LOCK:
        th = _INFLIGHT.get(prop_id)
    if th is None:
        return True
    th.join(timeout_s)
    return not th.is_alive()

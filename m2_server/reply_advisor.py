"""本地 Qwen3-VL-4B（GGUF）生成「该怎么回」—— 只读消息，绝不发送。

为什么是它
----------
本机唯一的**能写中文**的模型。其余模型全是音频：Qwen3-TTS 只能把字读成声音、
Whisper 只能听写、RVC/HuBERT/Demucs 只能变声和分离 —— 没有一个会「想一句话」。
Qwen3-VL-4B 虽然是视觉模型，但纯文本推理完全可用，而且**不需要 mmproj**
（那是图片投影器）：只传文字时省掉 433MiB 和整条视觉链路。

实测（2026-10-24，RTX 5060 8G / Ryzen 7 8840HX，见 docs/实测-4B视觉与TTS-RVC共存）：
    GPU(-ngl 99, ctx 2048)  启动 4.6s + 推理 0.73s
    CPU(-ngl 0,   ctx 2048) 启动 2.0s + 推理 5.90s
纯文本 prompt 只有 ~160 token（整窗截图是 3037），所以比文档里的视觉延迟低一个档。

生命周期：**懒加载 + 空闲 90 秒自杀**。它只在用户点「怎么回 / 换一批」时活着，
绝不常驻 —— 实时变声要求显存余量 >2GB，而 4B 全量要 ~3.5GB。空闲退出后下次再拉起
（模型已在系统缓存里，实测启动 4.6s）。

失败必须降级而不是报错
----------------------
未装模型 / 端口被占 / 进程崩 / 输出不是合法 JSON —— 一律退回 `_rule_replies()`
的模板建议，并在 `note` 里说清原因。桌宠只负责把话显示出来，不该出现「点了没反应」。
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 环境变量（全部可选，默认值就是本机实测的那套）
ENV_EXE = "VM_REPLY_LLAMA_SERVER"
ENV_MODEL = "VM_REPLY_MODEL"
ENV_PORT = "VM_REPLY_PORT"
ENV_NGL = "VM_REPLY_NGL"
ENV_CTX = "VM_REPLY_CTX"
ENV_IDLE = "VM_REPLY_IDLE_S"
ENV_TIMEOUT = "VM_REPLY_TIMEOUT_S"
ENV_BOOT = "VM_REPLY_BOOT_S"

#: 默认模型：Q4_K_M 2.33GiB。不传 --mmproj —— 纯文本用不上视觉投影器。
DEFAULT_MODEL = r"D:\Qwen3VL-4B-Instruct-Q4_K_M.gguf"
MODEL_LABEL = "qwen3vl-4b"

#: 推理超时。CPU 档实测 5.9s，留 10 倍余量给「三者同卡」那种极端情况。
CHAT_TIMEOUT = 90.0

#: 生成参数：2-3 条短句，别让它写小作文。
MAX_TOKENS = 220
TEMPERATURE = 0.4

#: 一次喂给模型的最大消息条数（去掉时间行后）。20 条肉眼够判断语境，
#: prompt 也只有 ~300 token —— 再长对 4B 只会更慢更糊涂。
FEED_MESSAGES = 14

_SYSTEM = (
    "你是微信聊天助手，帮用户决定「该怎么回」。"
    "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码块。格式："
    '{"should_reply":true,"why":"一句话理由（不超过20字）",'
    '"replies":["回复1","回复2","回复3"]}'
    "replies 给 2-3 条，每条不超过 30 个汉字，必须是可以直接发出去的口语短句，"
    "像真人随手打的；不要复述对方原话，不要写「建议你」这种旁白，不要加引号。"
    "如果最后一条是用户自己发的、没有新问题，should_reply 为 false，replies 给空数组。"
)

_KIND_LABEL = {"voice": "语音", "media": "图片/文件"}

#: 自动托管用的系统提示：**只出一条**、**不许旁白**。
#: 与 `_SYSTEM` 分开而不是复用：列表里给 3 条时模型会倾向「给选项」，
#: 托管要的是「现在就把这句话发出去」，多给一条反而增加发错的风险。
_SYSTEM_ONE = (
    "你是微信聊天助手。读下面的聊天记录，写**一条**现在就能直接发出去的中文口语回复。"
    "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码块。格式："
    '{"reply":"要发出去的那句话","why":"一句话理由（不超过20字）"}'
    "reply 不超过 30 个汉字，像真人随手打的；不要复述对方原话，"
    "不要写「建议你」「你可以」「你可以回」这类旁白，不要加引号，不要加句号以外的装饰。"
)

MAX_ONE_TOKENS = 96
ONE_TEMPERATURE = 0.5

_LOCK = threading.RLock()
_PROC: subprocess.Popen | None = None
_PORT: int = 0
_LAST_USED: float = 0.0
_WATCHER: threading.Thread | None = None
_PORT_OVERRIDE: int | None = None  # 测试用：指定已启动的端口


# ------------------------------------------------------------------ 定位


def _find_exe() -> str | None:
    """llama-server.exe：环境变量优先，否则在本机几个 llama.cpp 构建里挑最新的。

    不能按目录名字典序 —— `b8833` 会排在 `b10488` 后面（"8" > "1"），
    挑到的反而是旧包。按 mtime 取最新。
    """
    env = os.environ.get(ENV_EXE)
    if env and Path(env).is_file():
        return env
    cands = glob.glob(r"D:\llama-*\llama-server.exe")
    if not cands:
        return None
    return max(cands, key=lambda p: Path(p).stat().st_mtime)


def _find_model() -> str | None:
    env = os.environ.get(ENV_MODEL)
    if env and Path(env).is_file():
        return env
    return DEFAULT_MODEL if Path(DEFAULT_MODEL).is_file() else None


def available() -> tuple[bool, str]:
    """能不能用本地模型。返回 (能否, 不能的人话原因)。"""
    if not _find_exe():
        return False, "没找到 llama-server.exe（可用 VM_REPLY_LLAMA_SERVER 指定）"
    if not _find_model():
        return False, f"没找到本地模型 {DEFAULT_MODEL}（可用 VM_REPLY_MODEL 指定）"
    return True, ""


# ------------------------------------------------------------------ 进程生命周期


def _port() -> int:
    if _PORT_OVERRIDE:
        return _PORT_OVERRIDE
    return int(os.environ.get(ENV_PORT, "8093"))


def _health(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def _kill_locked() -> None:
    global _PROC
    proc, _PROC = _PROC, None
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _watch_idle() -> None:
    """空闲自杀。5 秒粒度足够 —— 目的不是省显存到极致，而是「不用时别赖着」。"""
    while True:
        time.sleep(5)
        idle_s = float(os.environ.get(ENV_IDLE, "90"))
        with _LOCK:
            if _PROC is not None and time.time() - _LAST_USED >= idle_s:
                _kill_locked()


def _start_locked(port: int) -> None:
    """拉起 llama-server 并等它 ready。调用方必须持锁。"""
    global _PROC
    exe, model = _find_exe(), _find_model()
    assert exe and model, "available() 已保证两者存在"
    ngl = os.environ.get(ENV_NGL, "99")
    ctx = os.environ.get(ENV_CTX, "2048")
    boot_s = float(os.environ.get(ENV_BOOT, "90"))
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _PROC = subprocess.Popen(
        [
            exe, "-m", model,
            "--host", "127.0.0.1", "--port", str(port),
            "-ngl", str(ngl), "-c", str(ctx), "-t", "8", "--no-warmup",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    deadline = time.time() + boot_s
    while time.time() < deadline:
        if _PROC.poll() is not None:
            raise RuntimeError(f"llama-server 启动即退出（code {_PROC.returncode}）")
        if _health(port):
            return
        time.sleep(0.4)
    _kill_locked()
    raise RuntimeError(f"llama-server 在 {boot_s:.0f}s 内没 ready")


def _ensure_server() -> int:
    """拿到一个可用的端口。已活着就复用；死了就重启。"""
    global _LAST_USED, _WATCHER, _PROC
    port = _port()
    with _LOCK:
        _LAST_USED = time.time()
        if _PROC is not None and _PROC.poll() is None and _health(port):
            return port
        if _PROC is None and _health(port):
            # 端口上已有一个（上次后端留下的）llama-server：直接复用，不重复加载。
            return port
        _kill_locked()
        _start_locked(port)
        if _WATCHER is None:
            _WATCHER = threading.Thread(target=_watch_idle, name="reply-idle", daemon=True)
            _WATCHER.start()
        return port


def shutdown() -> None:
    """后端退出时顺手带走子进程（否则任务管理器里躺一个 2.3GB 的 llama-server）。"""
    with _LOCK:
        _kill_locked()


atexit.register(shutdown)


# ------------------------------------------------------------------ 提示词与解析


def _history_lines(messages: list[dict]) -> list[str]:
    """把 UIA 消息压成模型好读的几行。时间行扔掉（只会白占 token）。"""
    out: list[str] = []
    for m in messages:
        kind = (m or {}).get("kind") or "text"
        text = str((m or {}).get("text") or "").strip()
        if not text or kind in ("time", "system"):
            continue
        if kind == "voice":
            out.append(f"[语音 {m.get('seconds', '?')} 秒]")
        elif kind == "media":
            out.append(f"[{text}]")
        else:
            out.append(text)
    return out[-FEED_MESSAGES:]


def build_prompt(messages: list[dict], chat: str = "", last_side: str | None = None) -> str:
    """拼用户消息。`last_side` 是雷达给的确定信号：right = 最后一条是我发的。

    为什么不给每条消息标发送方：UIA 的 ListItem Name **不带发送方**（群聊也一样），
    逐条猜左右是编造。只有雷达的几何信号能确定「最后一条是谁发的」，就只用它。
    """
    lines = _history_lines(messages)
    head = f"当前会话：{chat or '未知'}\n" if chat else ""
    if last_side == "right":
        tail = "\n（注：最后一条是用户自己发的，对方还没回。）"
    elif last_side == "left":
        tail = "\n（注：最后一条是对方发的。）"
    else:
        tail = ""
    body = "\n".join(lines) if lines else "（没有读到文字消息）"
    return f"{head}最近的聊天记录（按时间顺序）：\n{body}{tail}\n\n该怎么回？"


def parse_reply_json(text: str) -> dict | None:
    """从模型输出里抠出 {why, replies}。校验不通过返回 None（调用方降级）。

    模型偶尔会加 ```json 围栏或前后废话，所以取第一个 { 到最后一个 }。
    严格校验是刻意的：宁可退回模板，也不把「建议你…」这种旁白塞进输入框。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("replies")
    if not isinstance(raw, list):
        return None
    replies: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip().strip('"“”').strip()
        if not s or len(s) > 40:
            continue
        if s not in replies:
            replies.append(s)
    if not replies:
        return None
    why = data.get("why")
    why = why.strip()[:40] if isinstance(why, str) else ""
    should = data.get("should_reply")
    return {"why": why, "replies": replies[:3], "should_reply": should is not False}


def parse_one_json(text: str) -> dict | None:
    """从模型输出里抠出**一条**可直接发送的 `{reply, why}`。校验不过返回 None。

    校验比 `parse_reply_json` 更严：托管是**真的会发出去**，所以
    「建议你…」「你可以回…」这类旁白、带换行的长文、引号包裹、空串都要拦下。
    调用方拿到 None 必须**放弃本次回复**（不是退回模板再发 —— 模板是给人点的，
    自动发出去就成了复读机）。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("reply")
    if not isinstance(raw, str):
        return None
    reply = raw.strip().strip('"“”').strip()
    if not reply or len(reply) > 40:
        return None
    if "\n" in reply or "\r" in reply:
        return None
    for bad in ("建议你", "你可以回", "你可以回复", "回复建议", "这是一条回复"):
        if reply.startswith(bad):
            return None
    why = data.get("why")
    why = why.strip()[:40] if isinstance(why, str) else ""
    return {"reply": reply, "why": why}


def _rule_replies(messages: list[dict], last_side: str | None = None) -> list[str]:
    """模型不在时的兜底。只求「能直接点着发」，不假装懂语境。"""
    if last_side == "right":
        return ["等对方回就行"]
    texts = [m for m in _history_lines(messages) if not m.startswith("[")]
    last = texts[-1] if texts else ""
    if last:
        from chat_read import is_question

        if is_question(last):
            return ["在的，怎么了？", "刚看到，你说"]
    return ["收到", "哈哈哈", "好嘞"]


def _chat(
    port: int,
    prompt: str,
    timeout: float,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    payload = {
        "model": "qwen3vl",
        "messages": [
            {"role": "system", "content": system or _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "temperature": TEMPERATURE if temperature is None else temperature,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ 对外入口


def suggest(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict:
    """读最近消息 → 2-3 条可直接发的回复。永远返回可渲染的 dict，不抛异常。

    engine=local 表示真模型给的；engine=rule 是降级模板（note 里写原因）。
    """
    start = time.perf_counter()
    ok, why_not = available()
    if not ok:
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": why_not,
        }
    try:
        port = _ensure_server()
        content = _chat(port, build_prompt(messages, chat, last_side), CHAT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 —— 本地推理失败一律降级
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": f"本地模型没跑起来：{exc}",
        }
    parsed = parse_reply_json(content)
    if parsed is None:
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": "模型输出没通过校验，先给模板建议",
        }
    return {
        "engine": "local",
        "model": MODEL_LABEL,
        "replies": parsed["replies"],
        "why": parsed["why"],
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def suggest_one(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict | None:
    """自动托管用：**一条**可直接发送的回复；任何一步不成立都返回 None。

    为什么不像 `suggest()` 那样降级到规则模板：模板是给用户点的备选，
    自动发出去会变成复读机（「收到」「哈哈哈」），比不回更糟。
    """
    ok, _why_not = available()
    if not ok:
        return None
    try:
        port = _ensure_server()
        content = _chat(
            port,
            build_prompt(messages, chat, last_side),
            CHAT_TIMEOUT,
            system=_SYSTEM_ONE,
            max_tokens=MAX_ONE_TOKENS,
            temperature=ONE_TEMPERATURE,
        )
    except Exception:  # noqa: BLE001 —— 托管侧只关心「有没有可用的一条」
        return None
    return parse_one_json(content)


def status() -> dict:
    """诊断用：模型/可执行文件是否在位、服务是否活着。"""
    ok, why = available()
    return {
        "ok": ok,
        "note": why,
        "engine": "local",
        "model": MODEL_LABEL,
        "model_path": _find_model(),
        "server": _find_exe(),
        "running": _PROC is not None and _PROC.poll() is None,
    }


def _set_port_for_test(port: int | None) -> None:
    """测试夹具：把请求指向一个假 llama-server（不 spawn 真进程）。"""
    global _PORT_OVERRIDE
    _PORT_OVERRIDE = port

"""本地 Qwen3.5-9B（GGUF）生成「该怎么回」—— 只读消息，绝不发送。

为什么是它
----------
本机能写中文、又能塞进 8G 显存的模型里，它答得最像人。其余模型全是音频：
Qwen3-TTS 只能把字读成声音、Whisper 只能听写、RVC/HuBERT/Demucs 只能变声和分离。

**换过模型**（2026-10-24）：原先用 Qwen3-VL-4B。用 117 群 9 组真实语料同题横评，
4B 的通病是「只接情绪不接事实」还老替用户答应（「我帮你买一瓶荔枝水」）；
9B 会先问清再答（「行，买什么牌子？」），9 组里没有一组再乱许诺。
代价：单次 0.6s → 4.7s，显存 2.5G → 5.4G，冷启 4.6s → 10.7s。

★ Qwen3.5 是**思考型**模型：不开 `enable_thinking=False` 时答案全落在
`reasoning_content`、`content` 是空串（实测 9 条全空，看着像模型挂了）。
`_chat()` 默认关思考；要让它们想，设 `VM_REPLY_THINK=1`。

生命周期：**懒加载 + 空闲 90 秒自杀**。它只在用户点「怎么回 / 换一批」时活着，
绝不常驻 —— 实时变声要求显存余量 >2GB，而 9B Q4 要 ~5.4GB。空闲退出后下次再拉起。

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
import re
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

#: 默认模型：Qwen3.5-9B Q4_K_M（5.4GiB）。**换过**：原先用 Qwen3VL-4B，
#: 2026-10-24 用 117 群 9 组真实语料同题横评 —— 9B 明显更接得住事，且**不再替用户答应**
#: （4B「我帮你买一瓶荔枝水」→ 9B「行，买什么牌子？」），代价是 0.6s → 4.7s、显存 2.5G → 5.4G。
#: 4B 仍在盘里，VM_REPLY_MODEL 指回去即可（快但答得糙）。
DEFAULT_MODEL = r"D:\llama-b9660-bin-win-cuda-13.3-x64\Qwen3.5-9B-Q4_K_M.gguf"
MODEL_LABEL = "qwen3.5-9b"

#: 9B Q4 常驻时的显存占用（2026-10-24 实测 5.4G）。**只用于日志里说人话**，
#: 不参与任何判断 —— 要不要让路看的是「谁在跑」，不是这个估算值。
MODEL_VRAM_MB = 5400

#: 推理超时。CPU 档实测 5.9s，留 10 倍余量给「三者同卡」那种极端情况。
CHAT_TIMEOUT = 90.0

#: 生成参数：2-3 条短句，别让它写小作文。
MAX_TOKENS = 220
TEMPERATURE = 0.4

#: 一次喂给模型的最大消息条数（去掉时间行后）。20 条肉眼够判断语境，
#: prompt 也只有 ~300 token —— 再长对 4B 只会更慢更糊涂。
FEED_MESSAGES = 14

#: 关不关「思考链」。思考型模型（Qwen3.5 / Qwen3-*-Thinking）不关的话，
#: 答案会全部落在 `reasoning_content`、`content` 是空串 —— 2026-10-24 实测
#: 9 条测例全空，看起来像模型挂了。设 VM_REPLY_THINK=1 才会放它想。
NO_THINK = os.environ.get("VM_REPLY_THINK", "0") != "1"

_SYSTEM = (
    "你是微信聊天助手，帮用户决定「该怎么回」。"
    "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码块。格式："
    '{"should_reply":true,"why":"一句话理由（不超过20字）",'
    '"replies":["回复1","回复2","回复3"]}'
    "\n内容按这个优先级来："
    "①先看对方的实质诉求 —— 问位置/时间/买什么/怎么操作，就把答案或明确态度说出来，"
    "三条里**至少一条**必须落到这件事上；"
    "②再看口气 —— 像真人随手打的，可以有梗、可以接话茬，但**梗不能替代答案**；"
    "③对方只是在起哄、发表情、说闲话时，才可以只接梗。"
    "\n分寸是硬约束：不要替用户答应任何事、不要承诺（请客/帮忙/马上办）、"
    "不要道谢道贺、不要替用户做决定 —— 需要答应时写成问一句，不要写成已经答应了。"
    "不要复述对方原话，不要写「建议你」「你可以回」这类旁白，不要加引号。"
    "每条不超过 30 个汉字。"
    "如果最后一条标着「我：」（用户自己发的）、没有新问题，should_reply 为 false，replies 给空数组。"
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
    # 自动托管是**真的发出去**，所以分寸比建议模式更硬：一句「我马上帮你买」就是替用户
    # 许下了承诺（2026-10-24 评测里模型确实这么写过）。
    "优先回答对方问的那件具体事；可以有梗，但梗不能替代答案。"
    "绝不替用户答应、承诺、道谢或做决定 —— 需要答应时写成问一句。"
    "最后一条标着「我：」时不要回。"
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


def is_running() -> bool:
    """子进程是否活着。纯内存态，不探端口 —— 调用方只想问「现在占没占着显存」。"""
    proc = _PROC
    return proc is not None and proc.poll() is None


def _realtime_busy() -> str:
    """实时变声是否在跑；在跑返回人话原因，不在跑返回空串。

    为什么本模块要主动让路：9B 一进显存就是 5.4G，而 RVC 实时链路要 1.37G、
    且要求余量 ≥2G（`rvc_live.MIN_LIVE_FREE_VRAM_MB`）。2026-10-25 实测：
    9B 起来后整卡只剩 ~780MB —— 此时再开变声就是 OOM（无声 + 声卡残留）。
    与其让变声跑到一半崩，不如这条建议先给模板。

    探测按「查不到就当不忙」处理：`rvc_live` 导入失败、探测本身抛异常，
    都不该让人点不动按钮。
    """
    try:
        from rvc_live import _live_proc_alive

        if _live_proc_alive():
            return "实时变声正在运行（显存让它先占着），先给模板建议"
    except Exception:  # noqa: BLE001 —— 探测失败不是「忙」
        return ""
    return ""


atexit.register(shutdown)


# ------------------------------------------------------------------ 提示词与解析


def _history_lines(messages: list[dict]) -> list[str]:
    """把 UIA 消息压成模型好读的几行。时间行扔掉（只会白占 token）。

    ⚠️ 2026-10-24 实测过「每行前面标 我：/对方：」，**不要再加回来**：
    同一批 9 组真实语料上，带标记的 9 组没有一组比不带更好，还反复把
    「我：」原样写进回复正文（那条一旦被点选就真的发出去）。
    模型的问题不是「不知道谁说的」，而是接不住具体诉求 —— 详见
    `outputs/评测-117群-回复质量-2026-10-24.md` 与 `_COMMIT_RE` 的说明。
    """
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

    逐条发送方（`side`）**不进提示词**（理由见 `_history_lines`）；它只随
    `/api/radar/chat` 一起返回，给面板和托管判定用。
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


def close_unbalanced_json(text: str) -> str | None:
    """把「只差几个收尾括号」的 JSON 补齐；补不了返回 None。

    为什么需要：Ornith-1.5-9B 有 ~40% 的输出会**少一个结尾 `}`**
    （`finish_reason=stop`、token 也没用完，纯粹是没写），
    实测 27 次里 11 次因此被判成「输出非法」，白降级成模板。
    这里只做一件事：从第一个 `{` 扫到底，数清还没闭合的 `{` / `[`，按栈逆序补上。
    字符串没闭合、括号提前闭合（比开的多）、超长，一律返回 None —— 不做任何猜测，
    「宁可退回模板，也不把旁白塞进输入框」这条不变。
    """
    start = (text or "").find("{")
    if start < 0:
        return None
    body = text[start:]
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None          # 结构本来就乱了，不硬修
    if in_str or not stack or len(stack) > 3:
        return None
    return body + "".join(reversed(stack))


def parse_reply_json(text: str) -> dict | None:
    """从模型输出里抠出 {why, replies}。校验不通过返回 None（调用方降级）。

    模型偶尔会加 ```json 围栏或前后废话，所以取第一个 { 到最后一个 }。
    括号没闭合的（Ornith 常犯）先尝试补齐一次；补不了就放弃。
    其余校验**刻意严格**：宁可退回模板，也不把「建议你…」这种旁白塞进输入框。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            data = None
    if data is None:
        repaired = close_unbalanced_json(text)
        if repaired is None:
            return None
        try:
            data = json.loads(repaired)
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
    why = data.get("why")
    why = why.strip()[:40] if isinstance(why, str) else ""
    should = data.get("should_reply")
    if not replies:
        # 「不用回」是模型的**结论**，不是解析失败。以前一律当失败 → 退回模板，
        # 于是用户点了「怎么回」收到「收到/哈哈哈/好嘞」，而模型其实在说「这条不用回」。
        if should is False:
            return {"why": why, "replies": [], "should_reply": False}
        return None
    return {"why": why, "replies": replies[:3], "should_reply": should is not False}


#: 「替你答应」的句式。4B 在真实语料上反复写这类话（2026-10-24 评测：
#: 「我帮你买一瓶荔枝水」「好的，我马上帮你买！」「收到红包，谢谢！」），
#: 提示词里写了「不要承诺」它照写 —— 所以必须在**输出侧**兜一道。
#: 这是确定性过滤，不依赖模型听话：这些话一旦被点一下就真的发出去，
#: 代价是替用户许下承诺，或者把别人的红包当成自己的。
_COMMIT_RE = re.compile(
    # 帮他做某事：动词表要够宽 —— 2026-10-24 实测漏过「我帮你拿」「我去买一瓶」
    r"我(?:来|去)?(?:帮|给|替)(?:你|他|她|大家|他们)[^，。]{0,4}"
    r"(?:买|下单|付|转|处理|搞定|安排|拿|带|送|取|借|订|叫|问|发你)"
    r"|我(?:马上|立刻|这就)[^，。]{0,6}(?:买|下单|付|转|办|弄|拿|送)"
    r"|我(?:去|来)(?:买|拿|带|送|取|办|弄|订|叫)"
    r"|我(?:来)?(?:请客|买单|付款|请你)"
    r"|收到(?:红包|转账)"
    r"|包在我身上"
)


def looks_like_promise(text: str) -> bool:
    """这句是不是在**替用户**答应/承诺（含「收到红包谢谢」这种张冠李戴）。"""
    return bool(_COMMIT_RE.search(text or ""))


def drop_promises(replies: list[str]) -> list[str]:
    """去掉替用户点头的候选。全被去掉时返回空 —— 调用方决定怎么降级。"""
    return [r for r in replies if not looks_like_promise(r)]


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
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            data = None
    if data is None:
        # 与 parse_reply_json 同一套补齐：Ornith 常少写结尾的 `}`
        repaired = close_unbalanced_json(text)
        if repaired is None:
            return None
        try:
            data = json.loads(repaired)
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
    if NO_THINK:
        # Qwen3.5 这类**思考型**模型：不关思考时，答案全在 reasoning_content 里，
        # content 是空的（2026-10-24 实测：9 条测例全空，看着像模型挂了）。
        # 这里对非思考模型是空操作 —— 模板里没有这个变量，llama.cpp 直接忽略。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    # ⚠️ 不要拿 reasoning_content 兜底：那是思考过程（还常常是英文），
    # 里面的格式样例 `"replies":["reply1","reply2"]` 会被当成回复真发出去
    # （2026-10-24 用 Qwopus3.5-9B 实测，9 条里 2 条吐了 reply1/reply2）。
    # 空就让它空，调用方会降级成模板。
    return content


# ------------------------------------------------------------------ 对外入口


def suggest(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict:
    """读最近消息 → 2-3 条可直接发的回复。永远返回可渲染的 dict，不抛异常。

    engine=local 表示真模型给的；engine=rule 是降级模板（note 里写原因）。
    """
    start = time.perf_counter()
    busy = _realtime_busy()
    if busy:
        # 与实时变声互斥：不拉起模型，但仍要给能点的东西（桌宠不该「点了没反应」）。
        return {"engine": "rule", "replies": _rule_replies(messages, last_side), "note": busy}
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
    raw_replies = parsed["replies"]
    if not raw_replies:
        # 模型明确判定「这条不用回」：别拿模板凑数 —— 用户点了按钮却收到
        # 「收到 / 哈哈哈 / 好嘞」，比看到「这条不用回」更糟（也更假）。
        return {
            "engine": "local",
            "model": MODEL_LABEL,
            "replies": [],
            "why": parsed["why"],
            "note": parsed["why"] or "模型认为这条不用回",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    replies = drop_promises(raw_replies)
    note = ""
    if not replies:
        # 三条都在替用户点头 —— 宁可退回模板，也不能摆出「点了就替他答应」的选项
        replies = _rule_replies(messages, last_side)
        note = "模型那几条都在替你答应，已换成安全的模板"
    elif len(replies) < len(raw_replies):
        note = f"有 {len(raw_replies) - len(replies)} 条在替你答应，已去掉"
    return {
        "engine": "local",
        "model": MODEL_LABEL,
        "replies": replies,
        "why": parsed["why"],
        "note": note,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def suggest_one(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict | None:
    """自动托管用：**一条**可直接发送的回复；任何一步不成立都返回 None。

    为什么不像 `suggest()` 那样降级到规则模板：模板是给用户点的备选，
    自动发出去会变成复读机（「收到」「哈哈哈」），比不回更糟。
    """
    if _realtime_busy():
        # 托管是**真发**：没有模型就不发，绝不拿模板顶出去（与 suggest 的降级不同）。
        return None
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
    out = parse_one_json(content)
    if out is None:
        return None
    # 托管是**真的发出去**：模型一旦写了「我帮你买」，发出去就是替用户许了承诺。
    # 这里不return，直接丢弃 —— 宁可这条不回，也不能替用户答应（2026-10-24 评测）。
    if looks_like_promise(out.get("reply") or ""):
        return None
    return out


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

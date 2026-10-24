"""微信「全局托管」：替用户盯着白名单会话，判断该回，写一条并**发出去**。

这是本项目第一个**会替用户发消息**的功能，所以边界要写在最前面
-----------------------------------------------------------------
默认关、必须显式授权（`start(consent=True)`）、只在本次进程有效（重启即失效）。
每条自动回复都必须同时满足：

    ① 会话在**用户挑的白名单**里（不是「所有未读」）；
    ② 有未读（说明确实来了新消息；用户正开着看的会话不会被打扰）；
    ③ 最后一条是**文字**、且是**对方**发的（左右由像素雷达判定，UIA 拿不到发送方）；
    ④ 是提问或 @了用户（`question_only`，用户拍板的口径）；
    ⑤ 群聊另外要求**必须 @用户**（群里一句泛泛的提问不该由机器人接）；
    ⑥ 免打扰会话默认跳过；
    ⑦ 不在静默时段、不超频控、不重复处理同一条消息；
    ⑧ 本地模型给得出**一条**合格短句（给不出就不发 —— 绝不退回模板复读）。

不做什么
--------
不发语音 / 文件 / 图片 / 红包 / 转账 / 链接，不点「+」面板，不改用户的草稿，
不翻聊天历史（`history_pages=0`；开了会抢一次前台滚轮），不碰现有语音发送链路。

为什么「只发文字」
------------------
语音要切虚拟声卡 + 按住 Alt + 前台真实点击（`wechat_voice` 那条链路），每一步都可能
把用户正在做的事打断；而文本走 ValuePattern + InvokePattern，**全程后台**。
托管是「用户不在电脑前」的场景，后台静默比拟真更重要。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import config as cfg

CONFIG_FILE = cfg.OUTPUTS_DIR / "autopilot.json"
LOG_FILE = cfg.OUTPUTS_DIR / "autopilot_log.jsonl"

#: 日志文件上限，超了就轮转一份 `.1`（托管可能连跑一整天，不能无限长）
LOG_MAX_BYTES = 512 * 1024
LOG_KEEP = 60

#: 同一指纹（会话 + 未读条数 + 最后一条原文）在多长时间内算「已处理」
SEEN_TTL_S = 1800

#: 连续失败多少次自动停机（微信被关、UIA 挂了、模型崩了都算）
MAX_FAIL_STREAK = 3

#: 显存预检：与实时变声共用一张卡时，余量不足就跳过生成（不是跳过整轮）
MIN_FREE_VRAM_MB = int(os.environ.get("VM_AUTOPILOT_MIN_FREE_VRAM_MB", "2048"))

DEFAULTS: dict = {
    "whitelist": [],
    "groups": [],
    "me_names": [],
    "question_only": True,
    "skip_muted": True,
    "poll_s": 20,
    "min_gap_s": 45,
    "max_per_chat_per_hour": 3,
    "max_per_run": 20,
    "quiet_hours": [23, 7],
    "history_pages": 0,
    "reply_max_chars": 30,
}


# ---------------------------------------------------------------- 配置


def _as_str_list(raw, key: str, limit: int, max_len: int = 40) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError(f"{key} 必须是字符串数组")
    out: list[str] = []
    for x in raw:
        s = x.strip()
        if not s:
            continue
        if len(s) > max_len:
            raise ValueError(f"{key} 里有一项超过 {max_len} 字：{s[:12]}…")
        if s not in out:
            out.append(s)
    if len(out) > limit:
        raise ValueError(f"{key} 最多 {limit} 项")
    return out


def _as_bool(raw, key: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValueError(f"{key} 必须是 true/false")
    return raw


def _as_int(raw, key: str, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} 必须是整数")
    if not lo <= raw <= hi:
        raise ValueError(f"{key} 必须在 {lo}~{hi} 之间")
    return raw


def normalize(raw: dict | None) -> dict:
    """用户配置 → 校验过的完整配置。任何一处不合法都**当场抛 ValueError**（中文原因）。

    为什么不「尽量修」：托管会自动发消息，一个被静默纠正的白名单
    （比如把拼错的会话名悄悄丢掉）会让用户以为在托管、其实什么都没发生。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("配置必须是对象")
    out = {
        "whitelist": _as_str_list(raw.get("whitelist"), "whitelist", 20),
        "groups": _as_str_list(raw.get("groups"), "groups", 20),
        "me_names": _as_str_list(raw.get("me_names"), "me_names", 5, max_len=20),
        "question_only": _as_bool(raw.get("question_only"), "question_only", True),
        "skip_muted": _as_bool(raw.get("skip_muted"), "skip_muted", True),
        "poll_s": _as_int(raw.get("poll_s"), "poll_s", DEFAULTS["poll_s"], 5, 600),
        "min_gap_s": _as_int(raw.get("min_gap_s"), "min_gap_s", DEFAULTS["min_gap_s"], 0, 3600),
        "max_per_chat_per_hour": _as_int(
            raw.get("max_per_chat_per_hour"), "max_per_chat_per_hour", DEFAULTS["max_per_chat_per_hour"], 1, 20
        ),
        "max_per_run": _as_int(raw.get("max_per_run"), "max_per_run", DEFAULTS["max_per_run"], 1, 200),
        "history_pages": _as_int(raw.get("history_pages"), "history_pages", 0, 0, 3),
        "reply_max_chars": _as_int(raw.get("reply_max_chars"), "reply_max_chars", DEFAULTS["reply_max_chars"], 10, 40),
    }
    qh = raw.get("quiet_hours", DEFAULTS["quiet_hours"])
    if not isinstance(qh, list) or len(qh) != 2 or any(
        isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 23 for x in qh
    ):
        raise ValueError("quiet_hours 必须是 [起始小时, 结束小时]（0~23）")
    out["quiet_hours"] = list(qh)
    return out


def load_config() -> dict:
    """读配置。文件缺失 / 坏掉 → 默认值（默认白名单为空 = 什么都不会回）。"""
    try:
        data = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    except Exception:
        return normalize({})
    try:
        return normalize(data)
    except ValueError:
        return normalize({})


def save_config(raw: dict) -> dict:
    """校验并落盘（原子替换）。返回写入的完整配置。"""
    data = normalize(raw)
    path = Path(CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return data


# ---------------------------------------------------------------- 判断（纯函数）


def _at_me(text: str, names: list[str]) -> bool:
    """正文里是否 @ 了用户。微信的 @ 后面跟着 U+2005（四分之一空格），先归一。"""
    raw = text or ""
    t = raw.replace("\u2005", "").replace("\u200b", "")
    for n in names:
        if not n:
            continue
        if f"@{n}" in t or f"@ {n}" in raw.replace("\u2005", " "):
            return True
    return False


def is_quiet(cfg_in: dict, hour: int) -> bool:
    start, end = cfg_in["quiet_hours"]
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨零点，如 [23, 7]


def _last_text(messages: list[dict]) -> dict | None:
    for m in reversed(messages or []):
        if (m or {}).get("kind") not in ("time", "system"):
            return m
    return None


def precheck(
    *,
    chat: str,
    unread: int,
    muted: bool,
    cfg_in: dict,
    now_ts: float,
    hour: int,
    sent_at: dict[str, list[float]],
) -> tuple[bool, str]:
    """切会话**之前**就能否掉的条件（省一次 UIA 操作）。"""
    if chat not in cfg_in["whitelist"]:
        return False, "不在白名单"
    if muted and cfg_in["skip_muted"]:
        return False, "会话免打扰"
    if unread <= 0:
        return False, "没有新消息"
    if is_quiet(cfg_in, hour):
        return False, "静默时段"
    mine = sent_at.get(chat) or []
    if mine and now_ts - mine[-1] < cfg_in["min_gap_s"]:
        return False, f"距上次回复不足 {cfg_in['min_gap_s']}s"
    recent = [t for t in mine if now_ts - t < 3600]
    if len(recent) >= cfg_in["max_per_chat_per_hour"]:
        return False, "本会话一小时内回得太多"
    return True, ""


def decide(
    *,
    chat: str,
    unread: int,
    muted: bool,
    messages: list[dict],
    side: str,
    cfg_in: dict,
    now_ts: float,
    hour: int,
    sent_at: dict[str, list[float]],
    seen: dict[str, float],
) -> tuple[bool, str, str]:
    """该不该回。返回 `(可回, 原因, 指纹)`；不可回时原因写日志用。

    纯函数（时间/频控数据从参数进来），这样规则可以逐条被测试钉住 ——
    「什么情况下会替用户说话」是这个功能唯一值得反复验证的部分。
    """
    ok, reason = precheck(
        chat=chat, unread=unread, muted=muted, cfg_in=cfg_in, now_ts=now_ts, hour=hour, sent_at=sent_at
    )
    if not ok:
        return False, reason, ""
    last = _last_text(messages)
    if last is None:
        return False, "没读到消息", ""
    if last.get("kind") != "text":
        return False, f"最后一条是{last.get('kind')}（只回文字）", ""
    text = str(last.get("text") or "").strip()
    if not text:
        return False, "最后一条是空文本", ""
    if side != "left":
        return False, "最后一条不是对方发的（或左右识别不到）", ""

    mentions = _at_me(text, cfg_in["me_names"])
    from chat_read import is_question  # 惰性：本模块要能在无 UIA 环境里被测试导入

    question = is_question(text)
    if cfg_in["question_only"] and not (question or mentions):
        return False, "既不是提问也没 @你", ""
    if chat in cfg_in["groups"] and not mentions:
        return False, "群聊里没 @你", ""

    fp = f"{chat}|{unread}|{text[:60]}"
    last_seen = seen.get(fp)
    if last_seen and now_ts - last_seen < SEEN_TTL_S:
        return False, "这条刚处理过", ""
    return True, "该回", fp


# ---------------------------------------------------------------- 引擎


def _default_ops() -> dict:
    """真实依赖。全部惰性 import —— 引擎模块本身可以在无微信/无模型的环境里被测试。"""

    def alive() -> tuple[bool, str]:
        import ctypes

        import wechat_proc

        hwnd = wechat_proc.find_wechat_hwnd()
        if not hwnd:
            return False, "微信没开"
        if ctypes.windll.user32.IsIconic(hwnd):
            return False, "微信最小化了"
        return True, ""

    def read_chat(limit: int = 20) -> dict:
        import chat_read

        return chat_read.read_chat(limit=limit)

    def select_session(title: str) -> dict:
        import wechat_text

        return wechat_text.select_session(title)

    def selected_chat() -> str:
        import wechat_text

        return wechat_text.selected_chat()

    def peek_side() -> dict:
        import radar_api

        return radar_api.peek_side()

    def suggest_one(messages: list[dict], chat: str, side: str | None) -> dict | None:
        import reply_advisor

        return reply_advisor.suggest_one(messages, chat, side)

    def send_text(text: str, chat: str) -> dict:
        import wechat_text

        return wechat_text.send_text(text, expect_chat=chat)

    def scroll(pages: int) -> dict:
        import wechat_text

        return wechat_text.scroll_messages(pages)

    return {
        "wechat_alive": alive,
        "read_chat": read_chat,
        "select_session": select_session,
        "selected_chat": selected_chat,
        "peek_side": peek_side,
        "suggest_one": suggest_one,
        "send_text": send_text,
        "scroll": scroll,
        "now": time.time,
    }


def _free_vram_mb() -> int | None:
    """nvidia-smi 查空闲显存（MB）。没装 / 无 GPU 返回 None（视为不限制）。"""
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        vals = [int(x.strip()) for x in (out.stdout or "").splitlines() if x.strip().isdigit()]
        return min(vals) if vals else None
    except Exception:
        return None


class Engine:
    """单线程轮询引擎。所有状态都在锁内读写；`tick()` 可单独调用（测试用）。"""

    def __init__(self, ops: dict | None = None):
        self._ops = ops or {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._status = "off"  # off / running / paused / stopping
        self._cfg = load_config()
        self._stats = {"ticks": 0, "sent": 0, "skipped": 0, "errors": 0}
        self._run_sent = 0
        self._last_error = ""
        self._last_tick_at = 0.0
        self._sent_at: dict[str, list[float]] = {}
        self._seen: dict[str, float] = {}
        self._fail_streak = 0
        self._vram_cache = (0.0, None)  # (ts, mb)
        self._recent: deque[dict] = deque(maxlen=LOG_KEEP)

    # -- 依赖 ----------------------------------------------------------

    def ops(self) -> dict:
        with self._lock:
            if not self._ops:
                self._ops = _default_ops()
            return self._ops

    # -- 状态 ----------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "stats": dict(self._stats),
                "run_sent": self._run_sent,
                "last_error": self._last_error,
                "last_tick_at": self._last_tick_at,
            }

    def config(self) -> dict:
        with self._lock:
            return dict(self._cfg)

    def set_config(self, data: dict) -> None:
        """把已校验的配置换进内存快照（只在停机状态调用；运行中改会造成新旧混用）。"""
        with self._lock:
            self._cfg = dict(data)

    def recent(self, limit: int = 40) -> list[dict]:
        with self._lock:
            items = list(self._recent)[-limit:]
        return items or read_log(limit)

    # -- 生命周期 ------------------------------------------------------

    def start(self, consent: bool) -> dict:
        """开启托管。`consent` 必须是 True —— 前端勾过风险告知才会传。"""
        if consent is not True:
            raise PermissionError("需要显式授权（consent=true）")
        with self._lock:
            self._cfg = load_config()
            if not self._cfg["whitelist"]:
                raise ValueError("白名单是空的：先选至少一个会话")
            if self._status in ("running", "paused"):
                return self.status()
            self._stop.clear()
            self._pause.clear()
            self._run_sent = 0
            self._fail_streak = 0
            self._status = "running"
            self._thread = threading.Thread(target=self._loop, name="chat-autopilot", daemon=True)
            self._thread.start()
            return self.status()

    def pause(self) -> dict:
        with self._lock:
            if self._status == "running":
                self._status = "paused"
                self._pause.set()
            return self.status()

    def resume(self) -> dict:
        with self._lock:
            if self._status == "paused":
                self._status = "running"
                self._pause.clear()
            return self.status()

    def stop(self, join: bool = True) -> dict:
        with self._lock:
            self._stop.set()
            self._pause.clear()
            if self._status in ("running", "paused"):
                self._status = "stopping"
        t = self._thread
        if join and t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)
        with self._lock:
            self._status = "off"
            return self.status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(1.0)
                continue
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 —— 单轮异常不该让线程静默死掉
                self._record_error(f"托管轮次异常：{exc}")
            with self._lock:
                poll = self._cfg["poll_s"]
            self._stop.wait(poll)
        with self._lock:
            if self._status in ("running", "paused", "stopping"):
                self._status = "off"

    # -- 单轮 ----------------------------------------------------------

    def tick(self) -> dict:
        """跑一轮：读会话列表 → 逐个白名单未读 → 判断 → 生成 → 发送。返回摘要。"""
        ops = self.ops()
        now = float(ops["now"]())
        with self._lock:
            cfg_in = dict(self._cfg)
            sent_at = {k: list(v) for k, v in self._sent_at.items()}
            seen = dict(self._seen)
        hour = int(time.localtime(now).tm_hour)

        ok_alive, why_dead = ops["wechat_alive"]()
        if not ok_alive:
            self._record_error(why_dead)
            return {"ok": False, "error": why_dead, "sent": 0, "skipped": 0}

        snap = ops["read_chat"](limit=8)
        if not snap.get("ok"):
            msg = snap.get("error") or "读微信会话失败"
            self._record_error(msg)
            return {"ok": False, "error": msg, "sent": 0, "skipped": 0}

        original = snap.get("chat") or ""
        sessions = [s for s in (snap.get("sessions") or []) if s.get("chat") in cfg_in["whitelist"]]
        sessions = [s for s in sessions if int(s.get("unread") or 0) > 0]
        sessions.sort(key=lambda s: -int(s.get("unread") or 0))

        sent = skipped = 0
        try:
            for s in sessions:
                if self._stop.is_set():
                    break
                with self._lock:
                    if self._run_sent >= cfg_in["max_per_run"]:
                        break
                chat = str(s["chat"])
                allowed, reason = precheck(
                    chat=chat,
                    unread=int(s.get("unread") or 0),
                    muted=bool(s.get("muted")),
                    cfg_in=cfg_in,
                    now_ts=now,
                    hour=hour,
                    sent_at=sent_at,
                )
                if not allowed:
                    skipped += 1
                    self._log("skip", chat, reason=reason)
                    continue

                sel = ops["select_session"](chat)
                if not sel.get("ok"):
                    skipped += 1
                    msg = f"切会话失败：{sel.get('error')}"
                    self._log("error", chat, reason=msg)
                    self._record_error(msg)
                    continue
                try:
                    if cfg_in["history_pages"]:
                        ops["scroll"](cfg_in["history_pages"])
                    detail = ops["read_chat"](limit=20)
                    side_info = ops["peek_side"]()
                    if cfg_in["history_pages"]:
                        ops["scroll"](-cfg_in["history_pages"])
                    if not detail.get("ok"):
                        skipped += 1
                        self._log("error", chat, reason=detail.get("error") or "读消息失败")
                        continue
                    allowed, reason, fp = decide(
                        chat=chat,
                        unread=int(s.get("unread") or 0),
                        muted=bool(s.get("muted")),
                        messages=detail.get("messages") or [],
                        side=str(side_info.get("side") or ""),
                        cfg_in=cfg_in,
                        now_ts=now,
                        hour=hour,
                        sent_at=sent_at,
                        seen=seen,
                    )
                    if not allowed:
                        skipped += 1
                        self._log("skip", chat, reason=reason)
                        continue

                    can_gen, why_not = self._generation_ready()
                    if not can_gen:
                        skipped += 1
                        self._log("skip", chat, reason=why_not)
                        continue

                    draft = ops["suggest_one"](detail.get("messages") or [], chat, side_info.get("side"))
                    if not draft or not (draft.get("reply") or "").strip():
                        skipped += 1
                        self._log("skip", chat, reason="本地模型没给出可用回复")
                        continue
                    reply = str(draft["reply"]).strip()
                    if len(reply) > cfg_in["reply_max_chars"]:
                        skipped += 1
                        self._log("skip", chat, reason=f"模型回复过长（{len(reply)} 字）")
                        continue

                    res = ops["send_text"](reply, chat)
                    if res.get("ok"):
                        sent += 1
                        with self._lock:
                            self._run_sent += 1
                            self._sent_at.setdefault(chat, []).append(now)
                            self._seen[fp] = now
                            self._stats["sent"] += 1
                            self._fail_streak = 0
                        self._log("sent", chat, reply=reply, why=draft.get("why", ""), method=res.get("method", ""))
                    else:
                        skipped += 1
                        msg = f"发送失败：{res.get('error')}"
                        self._log("error", chat, reason=msg)
                        self._record_error(msg)
                except Exception as exc:  # noqa: BLE001 —— 单个会话炸掉要记账后继续下一个
                    skipped += 1
                    self._log("error", chat, reason=f"处理会话异常：{exc}")
                    self._record_error(f"处理会话异常：{exc}")
        finally:
            self._restore(ops, original, "")

        with self._lock:
            self._stats["ticks"] += 1
            self._stats["skipped"] += skipped
            self._last_tick_at = now
            self._prune_locked(now)
        return {"ok": True, "sent": sent, "skipped": skipped, "error": ""}

    @staticmethod
    def _restore(ops: dict, original: str, current: str) -> None:
        """把会话切回用户原本在看的那一个。失败静默（下一次 select 会再校验）。"""
        if not original or original == current:
            return
        try:
            if ops.get("selected_chat") and ops["selected_chat"]() == original:
                return
            ops["select_session"](original)
        except Exception:
            pass

    def _prune_locked(self, now: float) -> None:
        for fp, ts in list(self._seen.items()):
            if now - ts > SEEN_TTL_S * 2:
                self._seen.pop(fp, None)
        for chat, arr in list(self._sent_at.items()):
            keep = [t for t in arr if now - t < 3600]
            if keep:
                self._sent_at[chat] = keep
            else:
                self._sent_at.pop(chat, None)

    def _generation_ready(self) -> tuple[bool, str]:
        """生成前的环境预检：本地模型是否在位、显存余量是否够。

        显存与实时变声共用一张卡：4B 约 3.5GB，余量不足时硬拉会把 RVC 挤崩。
        所以这里**只跳过生成**（本轮算 skip），既不停机也不发空消息。
        """
        try:
            import reply_advisor

            ok, why = reply_advisor.available()
            if not ok:
                return False, f"本地模型不可用：{why}"
        except Exception as exc:  # noqa: BLE001
            return False, f"本地模型不可用：{exc}"
        with self._lock:
            ts, mb = self._vram_cache
            if time.time() - ts > 30:
                mb = _free_vram_mb()
                self._vram_cache = (time.time(), mb)
        if mb is not None and mb < MIN_FREE_VRAM_MB:
            return False, f"显存余量 {mb}MB < {MIN_FREE_VRAM_MB}MB（实时变声在跑），暂不生成"
        return True, ""

    # -- 日志 ----------------------------------------------------------

    def _record_error(self, msg: str) -> None:
        with self._lock:
            self._last_error = msg
            self._stats["errors"] += 1
            self._fail_streak += 1
            too_many = self._fail_streak >= MAX_FAIL_STREAK
        self._log("error", "", reason=msg)
        if too_many:
            self._stop.set()

    def _log(self, action: str, chat: str, **kw) -> None:
        rec = {"ts": round(time.time(), 3), "time": time.strftime("%H:%M:%S"), "action": action, "chat": chat}
        rec.update(kw)
        with self._lock:
            self._recent.append(rec)
        _append_log(rec)

    def log(self, action: str, chat: str, **kw) -> None:
        """公开的记账入口（API 的测试发送也写同一份日志）。"""
        self._log(action, chat, **kw)


def _append_log(rec: dict) -> None:
    try:
        path = Path(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不影响发消息


def read_log(limit: int = 40) -> list[dict]:
    """从日志文件尾部取最近若干条（重启后仍能看见上一轮的记录）。"""
    path = Path(LOG_FILE)
    if not path.exists():
        return []
    try:
        lines = deque(path.read_text(encoding="utf-8").splitlines(), maxlen=limit)
    except Exception:
        return []
    out: list[dict] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


_ENGINE: Engine | None = None
_ENGINE_LOCK = threading.Lock()


def engine() -> Engine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
        return _ENGINE


def reset_engine_for_test() -> None:
    """丢弃单例 —— 只给测试用。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            _ENGINE.stop(join=False)
        _ENGINE = None

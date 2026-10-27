"""微信「该不该回」状态雷达的 API 面（chat_radar 的 HTTP 壳）。

为什么只是一层薄壳
------------------
感知（像素行投影）与判断（规则引擎）全部在 `chat_radar/` 包里，且刻意**不依赖
FastAPI**（CLI、桌面角标、测试都能直接用）。本模块只做三件事：

    ① 持有一个进程级 `Radar` 单例（懒加载 + 锁）——桌面端可能开多个页面同时
       轮询，但 GDI 抓图这一步必须串行，否则两个 tick 互相抢窗口句柄没有意义；
    ② 把 `Tick`（信号 + 结论）序列化成 JSON；
    ③ 观察失败不当 500：微信没开 / 最小化是**正常状态**而不是服务坏了，
       返回 200 + `ok:false` + verdict=unknown，前端据此显示「观察中」。
       真正的服务器错误（如序列化炸了）才用 500。

为什么「该不该回」不给前端自由发挥的空间
----------------------------------------
`verdict.action` 是后端判定的枚举（none/wait/soon/now/unknown），前端只做展示。
判断层的规则与文案都钉在 chat_radar.judge 里 —— 桌宠改文案不改语义。

轮询建议：`poll_hint_ms` 由后端给出（1500ms）。理由在 radar.py 的模块注释：
500ms 轮询是「不漏正在输入」的上限，但桌面角标场景 1.5s 足够且 CPU 减半；
单次 tick 实测 ~100ms（p95 121ms），两个页面同时轮询也不会互相饿死。
"""

from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/radar", tags=["radar"])

#: 前端轮询间隔建议。写进响应而不是让前端写死：以后 L0 优化了或加了推送，
#: 只改这里一处，老前端自动跟上。
POLL_HINT_MS = 1500

#: 单例与锁。懒加载：import 本模块（含测试收集期）绝不去碰 GDI / user32。
#: RLock 而不是 Lock：`_tick_serialized()` 持锁调 `_radar()`，后者要再拿同一把锁
#: 才能建单例 —— 不可重入锁会当场死锁（probe/reset 两处同理）。
_RADAR = None
_RADAR_LOCK = threading.RLock()


def _radar():
    """进程级单例。首次调用才 import chat_radar —— 失败的信息原样抛给调用方。"""
    global _RADAR
    if _RADAR is None:
        with _RADAR_LOCK:
            if _RADAR is None:
                from chat_radar import Radar  # 惰性：CLI/测试收集期不付 GDI 成本

                _RADAR = Radar()
    return _RADAR


def reset_singleton() -> None:
    """丢弃单例 —— 只给测试用（用例之间必须各自拿到干净的 Radar）。"""
    global _RADAR
    with _RADAR_LOCK:
        _RADAR = None


def _tick_serialized() -> dict:
    """串行跑一次 tick 并序列化。锁住整个 tick：抓图 ~100ms，串行足够。"""
    start = time.perf_counter()
    with _RADAR_LOCK:
        tick = _radar().tick()
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    out: dict = {
        "ok": tick.ok,
        "verdict": tick.verdict.as_dict(),
        "signals": tick.signals.as_dict(),
        "elapsed_ms": elapsed_ms,
        "poll_hint_ms": POLL_HINT_MS,
    }
    if not tick.ok:
        out["error"] = tick.error
    return out


@router.get("/state")
def state():
    """抓一帧、算一次「该不该回」。微信没开 / 最小化时返回 `ok:false` + 人话原因。"""
    try:
        return _tick_serialized()
    except Exception as exc:  # noqa: BLE001 —— 单例构建/序列化的意外才到这
        raise HTTPException(status_code=500, detail=f"雷达状态计算失败：{exc}") from exc


@router.post("/reset")
def reset():
    """清空观察状态（换会话 / 想重新观察时用）。下次 /state 从冷启动重新累计。"""
    with _RADAR_LOCK:
        _radar().reset()
    return {"ok": True}


@router.get("/chat")
def chat(limit: int = 20):
    """当前会话的只读视图 + 本地规则分析（桌宠「分析聊天」按钮）。

    为什么不放进雷达的轮询链路：读消息列表要遍历 UIA 树（~50ms/次），
    只在用户点按钮时跑；雷达那 1.5s 的轮询仍然只走像素投影。
    微信没开 / 最小化是正常状态：200 + ok:false + 人话原因，与 /state 同一约定。
    """
    from chat_read import analyze, read_chat  # 惰性：不点按钮不碰 UIA

    try:
        snap = read_chat(limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 只读视图不该把接口打成 500
        return {"ok": False, "error": f"读微信会话失败：{exc}"}
    if not snap.get("ok"):
        return snap
    # 雷达结论是「锦上添花」：拿不到也照样有消息与未读分析。
    try:
        tick = _tick_serialized()
        snap["verdict"] = tick.get("verdict")
        snap["signals"] = tick.get("signals")
    except Exception as exc:  # noqa: BLE001
        snap["verdict_note"] = f"雷达不可用：{exc}"
    snap["analysis"] = analyze(snap, snap.get("verdict"))
    return snap


#: 认名的串行锁。**Lock 而不是 RLock**：这一路不重入自己。
#: 为什么需要：一次认名要拉起视觉模型并占 6G 显存（实测 2.7s/批），
#: 双击按钮或两个窗口同时点，就会排队两次一模一样的模型调用。
_NAMES_LOCK = threading.Lock()


@router.post("/names")
def names(timeout: float = 90.0):
    """读一遍群昵称：像素指纹分组 → Ornith 视觉 → 回填缓存。

    为什么要单独一个端点、而且必须是 POST
    --------------------------------------
    ① 它**不是只读**：会拉起视觉模型（占 6G 显存）、写进程内缓存。放轮询里会和
       实时变声抢显卡 —— 所以只能由用户点「认人」触发（前端那个按钮就是这个）。
    ② POST 而不是 GET：GET 允许浏览器/代理随意预取和重试，「预取一次 = 跑一次模型」
       的代价不能这么花；POST 天然挡掉这一类。

    和 `/chat` 的分工：`/chat` 的每条消息已经带 `speaker`（像素指纹 id，毫秒级），
    但 `name` 要等这一步读过才有值。所以面板先显示「群友3」，认完变「张三」。

    降级：认不出名字是**常态**（单聊没有昵称条、模型没权重、这个群全在发表情），
    一律 200 + `ok:true` + `learned:0` + `note`，与 `/state` 同一约定；只有意外异常
    才 `ok:false`（也不 500 —— 一个锦上添花的能力不该把面板打成错误页）。
    """
    # 上限钉死：调用方传 10000 会一直占着锁，后面的点击全部排队。
    timeout = max(5.0, min(float(timeout), 180.0))
    if not _NAMES_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "正在认人，等这一批读完"}
    try:
        from chat_read import read_names  # 惰性：不点「认人」就不碰 UIA / 模型

        return read_names(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 认不出名字不该把接口打成 500
        return {"ok": False, "error": f"认人失败：{exc}"}
    finally:
        _NAMES_LOCK.release()


@router.get("/replies")
def replies(limit: int = 14, last_side: str = "", avoid: str = ""):
    """读当前会话 → 本地 Qwen3-VL-4B（llama-server）给 2-3 条可直接发的回复。

    为什么单独一个端点、不和 /chat 合并：/chat 是 50ms 的 UIA 只读，
    /replies 要拉起 4B（实测首启 4.6s + 推理 0.7s）。合并会让「分析聊天」等模型，
    而结论其实早就出来了 —— 前端先画结论、再异步拉这一条。

    `last_side` 由前端从 /chat 的 signals 带过来（left/right）。不给也行：
    模型只按最后一条像不像提问来写，给了则能避免「催用户回自己刚发的话」。

    `avoid` 是「换一批」带回来的上一批：JSON 数组（也可以退化成 `|` 分隔）。
    不给就是第一次要，给了就要求换角度 —— 以前这个参数不存在，那个按钮
    只是原样再问一次，temperature 0.4 下多半给出几乎一样的句子。
    """
    from chat_read import read_chat  # 惰性：不点按钮不碰 UIA
    from reply_advisor import suggest

    try:
        snap = read_chat(limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 只读视图不该把接口打成 500
        return {"ok": False, "error": f"读微信会话失败：{exc}"}
    if not snap.get("ok"):
        return snap
    side = last_side if last_side in ("left", "right") else None
    out = suggest(snap.get("messages") or [], snap.get("chat") or "", side,
                  avoid=_parse_avoid(avoid))
    return {"ok": True, "chat": snap.get("chat"), **out}


def _parse_avoid(raw: str) -> list[str] | None:
    """把 `avoid` 查询参数解成列表。**故意两种格式都收**：前端拼 JSON 最省事，
    但手工 curl 调试时 `?avoid=666|收到` 更好打。解不出来就当没给 ——
    这个参数只影响措辞，不该让整个接口 400。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    items: list[str] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = [str(x) for x in parsed]
    except Exception:  # noqa: BLE001 —— 不是 JSON 就走分隔符那条路
        items = raw.split("|")
    items = [x.strip()[:30] for x in items if x and x.strip()]
    return items[:6] or None


#: 每个会话里用户**亲手给过**的检索词。
#: 「117」「3864区」这类群名给不出实体，只能靠用户说一次；
#: 说一次就该记住 —— 否则每点一次「查」都在问同一个问题。
_Q_HINT: dict[str, str] = {}


@router.get("/digest")
def digest(q: str = "", chat: str = "", limit: int = 4):
    """按当前会话的最后一个问题去搜攻略/数值，结果**只给用户看，不进群**。

    为什么单独一个端点、不和 /replies 合并：检索要 2~3 秒，而「该不该回」的结论
    150ms 就该出来。更重要的是这一步要**用户自己按** —— 原话是「我看着攻略，
    再让他做更多额外的事」；自动搜会把面板塞满，还没人看。

    `q` 给了就用给的（前端可以改词再搜一次）；不给就从当前会话里挑**最像问题的
    那条**对方消息当检索词。挑法是 `reply_advisor.pick_question` —— 不能取最后一条：
    群里问完之后总跟着「在线等」，实测拿它搜回来的是《无尽冬日攻略详解大全》
    这种泛泛的通关指南，而真正那句「打熊该上什么英雄」能搜到三条对口的打熊阵容。
    剪法是 `web_digest.build_query`，很保守 —— 搜狗对长句直接返回 0 条，宁可短。
    """
    from web_digest import build_query, search, useful_frag

    used_chat = chat
    frag = ""
    # 就算用户自己给了词，也要把会话名读出来 —— `_Q_HINT` 是按会话存的，
    # 拿不到会话名就会把词存进空 key，下次自动拼词时对不上（2026-10-25 实测）。
    if not q or not used_chat:
        try:
            from chat_read import read_chat  # 惰性：不点按钮不碰 UIA
            from reply_advisor import pick_question

            snap = read_chat(limit=10)
            msgs = snap.get("messages") or []
            used_chat = used_chat or (snap.get("chat") or "")
            if not q:
                frag = pick_question(msgs, span=6)
                if not frag:
                    # 挑不出来（对方只在起哄/发表情）就退回最后一条，让 build_query
                    # 自己去判断这段词够不够格 —— 它宁可返回空也不猜一个。
                    for m in reversed(msgs):
                        if (m.get("kind") or "text") != "text" or m.get("side") != "left":
                            continue
                        frag = str(m.get("text") or "").strip()
                        if frag:
                            break
        except Exception:  # noqa: BLE001 —— 读不到微信也能搜，只是只剩群名
            pass
    if not q:
        q = build_query(used_chat, None, frag)
        if not q:
            # 自动拼不出来（群名是「117」、对方只报了个数）：用这台机器上
            # 这个会话里用户自己说过的词。仍然没有就让 search() 去说「说个词」。
            q = build_query("", None, _Q_HINT.get(used_chat, ""))
    elif useful_frag(q):
        _Q_HINT[used_chat] = q.strip()
    out = search(q, limit=max(1, min(8, limit)))
    return {
        "ok": out["ok"],
        "chat": used_chat,
        "q": out["q"],
        "items": out["items"],
        "weak": out["weak"],
        "note": out["note"],
        "hint": _Q_HINT.get(used_chat, ""),
        "elapsed_ms": out.get("elapsed_ms", 0),
    }


@router.post("/replies/from-digest")
def replies_from_digest(body: dict | None = None):
    """拿刚搜到的那份攻略，写 2~3 句能直接点发的话。

    检索结果由前端回传而不是后端自己缓存：用户可能连搜三次、只挑其中一条看，
    这条链路要用的必须是**他正在看的那份**。
    """
    from chat_read import read_chat
    from reply_advisor import suggest_from_digest

    body = body or {}
    digest_doc = body.get("digest") or {}
    try:
        snap = read_chat(limit=14)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读微信会话失败：{exc}"}
    if not snap.get("ok"):
        return snap
    side = body.get("last_side") if body.get("last_side") in ("left", "right") else None
    out = suggest_from_digest(
        snap.get("messages") or [], digest_doc, snap.get("chat") or "", side
    )
    return {"ok": True, "chat": snap.get("chat"), **out}


@router.post("/replies/from-image")
def replies_from_image(body: dict | None = None):
    """看群里那张图 → 给「怎么打」的要点 + 能直接发的话。

    这是用户原话的正解：「他问了一句话，比如说这个活动怎么打呀，我希望他能上网
    搜索，给我点建议，我拿着去生成回复给他。」

    所以一条链路里做三件事，顺序不能换：
      ① 把图**放大**读出来（缩略图读不了，见 `vision_grab`）
      ② 拿「群名实体 + 对方问的那句」去搜 —— 注意搜的是**那句话**，不是图。
         图只回答「这一期排了什么」，不回答「这类活动怎么打」；反过来用图当
         检索词会拼出一串活动名，搜狗返回的全是日程表，没有打法。
      ③ 图和资料一起喂模型，出 `advice`（给本人看）+ `replies`（能发）。

    `search=0` 可以跳过第 ②步 —— 有时候用户只想让它读图，不想等那 2 秒。
    """
    from chat_read import read_chat
    from reply_advisor import suggest_from_digest
    from vision_grab import grab_biggest_image

    body = body or {}
    try:
        snap = read_chat(limit=14)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读微信会话失败：{exc}"}
    if not snap.get("ok"):
        return snap
    msgs = snap.get("messages") or []
    chat = snap.get("chat") or ""
    side = body.get("last_side") if body.get("last_side") in ("left", "right") else None

    start = time.perf_counter()
    img_path, img_note = grab_biggest_image()

    digest: dict = {"q": "", "items": []}
    if img_path and body.get("search", True):
        from web_digest import build_query, search  # 惰性：不点按钮不联网

        # 检索词取**最后一条对方说的、有信息量的那句**。逐条往回找而不是只看最后一条：
        # 「这个活动怎么打呀」前面常常只隔一个表情或一句「+1」。
        frag = ""
        for m in reversed(msgs):
            if (m.get("kind") or "text") != "text" or m.get("side") != "left":
                continue
            t = str(m.get("text") or "").strip()
            if len(t) >= 3 and not t.startswith("["):
                frag = t
                break
        q = build_query(chat, None, frag)
        if q:
            digest = search(q, limit=3)
            digest["q"] = q

    out = suggest_from_digest(msgs, digest, chat, side, image=img_path)
    return {
        "ok": True,
        "chat": chat,
        "image": {"ok": bool(img_path), "note": img_note},
        "digest": digest,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        **out,
    }


def peek_side() -> dict:
    """**只看最后一条是谁发的**（左=对方 / 右=自己），不推进观察状态机。

    为什么自动托管要这个：UIA 的消息 ListItem **不带发送方**，群聊里也没有可靠的
    几何信息（每个 ListItem 的矩形都是整行）。而「对方最后发言」是托管敢回的前提，
    只有雷达的像素左右分栏能确定。走 `peek()` 而不是 `tick()`：托管每轮只取一个值，
    不该喂状态机、更不该把用户的「等了多久」算乱。
    """
    try:
        with _RADAR_LOCK:
            frame, layout, err = _radar().peek()
    except Exception as exc:  # noqa: BLE001 —— 观察失败是常态（微信没开/最小化）
        return {"ok": False, "side": "", "kind": "", "n_blocks": 0, "error": f"雷达不可用：{exc}"}
    if err:
        return {"ok": False, "side": "", "kind": "", "n_blocks": 0, "error": err}
    assert layout is not None  # err 为空蕴含非空
    last = layout.last_chat
    return {
        "ok": last is not None,
        "side": last.side if last is not None else "",
        "kind": last.kind if last is not None else "",
        "n_blocks": len(layout.blocks),
        "error": "" if last is not None else "聊天区没有识别到消息块",
    }


@router.get("/probe")
def probe():
    """诊断：窗口矩形 + 聊天区 ROI + 最近一次识别出的块数。**不推进观察状态。**

    为什么不用 `tick()`（2026-10-23 修正）：tick 会喂状态机 —— 推进 `frames_seen`、
    覆盖上一帧指纹、还可能重置 `_stable` 与 `_observed_since`（冷启动的
    `gap_lower_bound` 就靠后者算）。结果就是「一诊断，gap 就从 0 重算」，
    排查问题时反而把现场搅乱。这里改走 `Radar.peek()`：抓图 + 解析，不喂状态机。
    """
    start = time.perf_counter()
    with _RADAR_LOCK:
        frame, layout, err = _radar().peek()
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    out: dict = {"ok": err == "", "elapsed_ms": elapsed_ms}
    if err:
        out["error"] = err
        return out
    assert frame is not None and layout is not None  # err 为空蕴含两者非空
    win, roi = frame.window, frame.roi
    out["window"] = {"title": win.title, "rect": list(win.rect)}
    out["roi"] = [roi.left, roi.top, roi.right, roi.bottom]
    out["n_blocks"] = len(layout.blocks)
    return out

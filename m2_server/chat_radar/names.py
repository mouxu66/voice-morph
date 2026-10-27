"""群聊里「谁说的」：UIA 拿不到，只能从昵称条读。

为什么需要这个模块
------------------
`chat_read._item_rows()` 的 docstring 已经写明：UIA 的 ListItem **没有发送方**
（2026-10-24 实测 4.1.15.13，群聊也一样），连子节点都没有。2026-10-27 又把
这棵树翻了一遍确认：8 条消息 0 子节点，除正文外唯一属性是 `AutomationId`
（所有气泡共用 `chat_bubble_item_view`），`HelpText` / `ItemStatus` /
`LegacyIAccessible.Description` 全空 —— 昵称根本没进 accessibility tree。

但同一时刻的截图给出了另一条路：**每条消息的 UIA 矩形完整覆盖「昵称+头像+气泡」**，
昵称就画在矩形顶部、头像右侧那一条灰字里。所以归属是「有像素、无文字」。

三条判据的分工（关键，别混）
--------------------------
1. **是不是同一个人 —— 像素指纹**。同一个人在同一缩放下昵称渲染完全一致
   （实测「红姜、」两条消息的条带字节级相同）。指纹相同就是同一人，
   **完全不依赖任何模型认对字**。
2. **叫什么 —— Ornith 视觉**。把本轮所有「没见过的」昵称条拼成一张带序号的图，
   一次调用读完（实测 7 条 / 2.7s / 6 个真昵称全对）。比专用 OCR 准：
   同一批条带 RapidOCR 4/6（`Lnlion` 读空、`红姜、` 丢顿号）。
3. **OCR 只在没有视觉权重时兜底**（可选依赖 rapidocr_onnxruntime，18ms/条）。
   没有它也不影响归属，只是名字显示不出来。

为什么必须带序号
----------------
让模型按数组顺序返回是危险的：只要它漏一条/多一条，后面**整排错位**，
结果是「把 A 的名字安到 B 头上」——比不写名字糟得多。带序号 + 按号回填后，
漏一条只影响那一条（2026-10-27 实测 7/7 对齐）。

性能（本机 RTX 5060 8G / 微信 4.1.x / 125% 缩放）
------------------------------------------------
    指纹分组：<1ms/条（纯 numpy + hash）
    视觉读名：2.7s/批（只在新昵称出现时付一次，之后走缓存 0ms）
    OCR 兜底：18ms/条（仅在没有 mmproj 时用）

只读边界
--------
只裁已经抓下来的窗口位图，不点击、不切会话、不滚动、不碰微信进程内存。
抓图失败 / 模型不可用 / 置信度不够时一律留空，**不猜**。
"""

from __future__ import annotations

import base64
import io
import threading

import numpy as np

#: 昵称条相对消息矩形的几何（**逻辑像素**，用前乘 DPI 比例）。
#: dx 是「跳过头像」的宽度：微信 4.x 的头像比想象中大（实测 125% 缩放下约
#: 77px 宽），dx=44 会切进头像右缘 —— 头像的深色会把二值化阈值拉低到 144，
#: 灰色昵称（~160）被当成背景，指纹只哈希到头像，同一个人被拆成两个 speaker
#: （2026-10-27 实测「巴黎罗」两条被拆成 id 2/3）。
#: 80 = 头像宽 77 + 3px 间隙；再大就会切掉昵称首字。
NICK_DX = 80
#: 昵称条高度：要**完整盖住昵称那一行字**（实测字形约 10px，但从 rect.top 到
#: 字形起点有 12~14px 偏移，24 会把底部切掉一行 —— 而两条消息切的量可能不同，
#: 于是同一个「巴黎罗」一条剩 10 行、一条剩 9 行，指纹就分叉了）。
#: 34 = 偏移 14 + 字形 10 + 上下余量，仍不到气泡顶边。
NICK_H = 34
NICK_W = 200

#: 特征图尺寸与匹配阈值（2026-10-27 实测分布）：
#:   同一个人：0.772（1px 裁切差）/ 1.000（完全一致）
#:   不同的人：最高 0.425（「巴黎罗」vs「Lnlion」，都是三字昵称）
#: 取 0.60 —— 两侧各留 0.17 以上余量。**宁可拆成两个人，也不能把两个人并成一个**
#: （合并错 = 把 A 的立场安到 B 头上，比显示两个同名条目糟得多）。
NORM_W, NORM_H = 64, 16
MATCH_IOU = 0.60
#: 平移搜索半径：1~2px 的裁切差异靠它吸收，再大就说明不是同一套渲染。
SHIFT = 2

#: 一次拼图最多几条。实测 7 条 2.7s 且全对；给到 12 是上限，
#: 再多模型容易漏行（漏行有防护，但会白跑一趟）。
SHEET_MAX = 12

#: 昵称最长字数。超过多半是引用卡片或系统提示，不是名字。
MAX_LEN = 14

#: 引用/转发卡片：`Lnlion: [图片](共3条) >` 这种矩形顶部是卡片本身，
#: OCR/视觉都会吐带方括号冒号的串，不能当昵称用。
_REJECT_CHARS = ("[", "]", "【", "】", "：", ":", "】")

PROMPT = (
    "这张图是若干条微信群聊消息的**昵称条**，从上到下排列，每条左侧有一个红色序号。"
    "请逐条读出昵称文字，按序号返回。只输出 JSON："
    '{"items":[{"n":1,"name":"第1条的名字"},{"n":2,"name":"..."}]}。'
    "序号必须照抄图上的红色数字，不要自己重排；读不出的那条 name 写空串。"
    "不要解释、不要代码块。"
)

_LOCK = threading.RLock()
#: speaker id → 特征图（NORM_H×NORM_W bool）。1..n，按首次出现顺序。
_SIG: dict[int, np.ndarray] = {}
#: speaker id → 昵称。用户改过的名字也存这里。
_NAMES: dict[int, str] = {}
_NEXT = [1]
_MAX_SPEAKERS = 512

_OCR = None


# ---------------------------------------------------------------- 分组


def signature(band: np.ndarray) -> np.ndarray | None:
    """昵称条 → 二值特征图（NORM_H×NORM_W）。判不出返回 None。

    为什么是「相似度匹配」而不是「精确哈希」：同一昵称在不同消息里会被 Qt 以
    亚像素方式渲染，加上 UIA 矩形取整方向不同，两条「巴黎罗」**永远差 1px**。
    精确哈希会把它们判成两个人（实测发生）；相关度又分不开（不同人 0.82、
    同人 0.917，余量只有 0.08）。二值 IoU + 小范围平移搜索把差距拉到
    0.425 vs 0.772，这才是安全的判据。

    裁剪比例按 band 高度算，不写死像素：band 高 = NICK_H × DPI 比例，
    昵称落在上部约 23%~80% 之间（下方可能带气泡顶边，留着不影响 ——
    同一条消息的气泡边位置是固定的，同人两张图会一起对上）。
    """
    from PIL import Image

    h = int(band.shape[0])
    if h < 8:
        return None
    top = int(h * 0.23)
    bot = min(h, top + int(h * 0.57))
    a = np.asarray(Image.fromarray(band[top:bot]).convert("L"), dtype=np.uint8)
    bg = int(np.median(a))
    mask = (a < bg - 20) if bg > 128 else (a > bg + 20)
    if not mask.any():
        return None
    small = Image.fromarray((mask * 255).astype(np.uint8)).resize(
        (NORM_W, NORM_H), Image.LANCZOS
    )
    return np.asarray(small) > 96


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = int((a | b).sum())
    return float((a & b).sum()) / union if union else 0.0


def _best_match(sig: np.ndarray) -> tuple[int, float]:
    """与已登记的每个 speaker 比，返回 (id, 最大 IoU)。0 表示没有匹配。"""
    best_id, best = 0, 0.0
    for sid, ref in _SIG.items():
        quick = _iou(sig, ref)
        if quick >= MATCH_IOU:  # 快速通道：没偏移的直接过
            return sid, quick
        for dy in range(-SHIFT, SHIFT + 1):
            for dx in range(-SHIFT, SHIFT + 1):
                v = _iou(np.roll(np.roll(sig, dy, axis=0), dx, axis=1), ref)
                if v > best:
                    best, best_id = v, sid
    return (best_id, best) if best >= MATCH_IOU else (0, best)


def has_ink(band: np.ndarray, min_px: int = 8) -> bool:
    """这条带上有没有字。

    头像右侧到正文之间若是纯背景（用户自己发的消息、或被折叠的系统行），
    就不必送去识别 —— 空白条带会让 OCR 返回 0.4~0.5 的随机短串。

    判据是**绝对像素数**而不是占比：两个字的昵称（「红姜」）在 200×35 的带里
    只占 0.86%，按 1% 比例会被误判成空白（2026-10-27 单测抓到）。
    """
    if band is None or band.size == 0:
        return False
    a = band.mean(axis=2) if band.ndim == 3 else band
    bg = float(np.median(a))
    mask = (a < bg - 20) if bg > 128 else (a > bg + 20)
    return int(mask.sum()) >= min_px


def _identify_one(band: np.ndarray) -> tuple[int, str]:
    """一条昵称条 → (speaker id, 已知名字)。调用方必须已持有 _LOCK。"""
    if band is None or band.size == 0 or not has_ink(band):
        return 0, ""
    sig = signature(band)
    if sig is None:
        return 0, ""
    sid, _score = _best_match(sig)
    if not sid:
        if len(_SIG) >= _MAX_SPEAKERS:
            _reset_locked()
        sid = _NEXT[0]
        _NEXT[0] += 1
        _SIG[sid] = sig
    return sid, _NAMES.get(sid, "")


def identify(bands: list[np.ndarray]) -> list[tuple[int, str]]:
    """昵称条 → [(speaker id, 已知名字)]，一次扫描，纯指纹不调模型。

    这是主入口：面板刷新走它（<1ms/条）。名字此时多半是空串（还没读过），
    但 id 已经能用来分组 —— 「3 个人在说话」这件事不需要等模型。
    """
    out: list[tuple[int, str]] = []
    with _LOCK:
        for band in bands:
            out.append(_identify_one(band))
    return out


def assign(bands: list[np.ndarray]) -> list[int]:
    """只要 id（0 = 这条没字/认不出人）。"""
    return [sid for sid, _name in identify(bands)]


def names(bands: list[np.ndarray]) -> list[str]:
    """只要已知名字（没读过的一律空串，不等待）。"""
    return [name for _sid, name in identify(bands)]


def name_of(sid: int) -> str:
    with _LOCK:
        return _NAMES.get(sid, "")


def set_name(sid: int, name: str) -> None:
    """用户手工纠正名字（指纹不变，只是换显示名）。"""
    clean = _clean(name)
    if not sid or not clean:
        return
    with _LOCK:
        _NAMES[sid] = clean


# ---------------------------------------------------------------- 读名字


def _clean(text: str) -> str:
    """模型/OCR 文本 → 昵称。判不出来就返回空串，**绝不猜**。"""
    t = (text or "").strip().strip("、，,。.；;~～-—_ \t")
    if not t or len(t) > MAX_LEN:
        return ""
    if any(ch in t for ch in _REJECT_CHARS):
        return ""
    return t


def _font(size: int = 22):
    from PIL import ImageFont

    for path in (
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _sheet(bands: list[np.ndarray]) -> bytes:
    """昵称条 → 带红色序号的竖排拼图（PNG bytes）。"""
    from PIL import Image, ImageDraw

    pad = 8
    boxes = [Image.fromarray(b) for b in bands]
    width = max(b.width for b in boxes) + 56
    height = sum(b.height + pad for b in boxes) + pad
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    font = _font(22)
    y = pad
    for i, box in enumerate(boxes):
        draw.text((6, y + 2), str(i + 1), fill=(220, 0, 0), font=font)
        canvas.paste(box, (52, y))
        y += box.height + pad
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def refresh(bands: list[np.ndarray], *, timeout: float = 90.0) -> tuple[int, str]:
    """把本轮**还没名字**的昵称条拼一张图，让 Ornith 一次读完。

    返回 `(新认出的个数, 失败原因)`。失败不抛 —— 名字是锦上添花，
    读不到不该让「分析聊天」整个失败。

    同一 speaker 只要一条：连发 5 条的人，拼图里只出现一次（去重后按 id 回填）。
    """
    pairs = identify(bands)
    todo: dict[int, np.ndarray] = {}
    for (sid, name), band in zip(pairs, bands, strict=False):
        if sid and not name and sid not in todo:
            todo[sid] = band
    if not todo:
        return 0, ""

    # 一次最多 SHEET_MAX 条：剩下的留到下一次刷新（不在这一趟硬塞）
    keys = list(todo)[:SHEET_MAX]
    payload = _sheet([todo[k] for k in keys])

    try:
        import reply_advisor as ra
    except Exception as exc:  # noqa: BLE001
        return 0, f"视觉模型不可用：{exc}"
    ok, why = ra.vision_ready()
    if not ok:
        return 0, why
    try:
        port = ra._ensure_server()
        content = ra._chat(
            port,
            PROMPT,
            timeout,
            system=None,
            max_tokens=60 + 40 * len(keys),
            image_b64=base64.b64encode(payload).decode(),
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        return 0, f"读昵称失败：{exc}"

    fixed = ra.close_unbalanced_json(content) or content
    import json

    try:
        data = json.loads(fixed)
    except Exception:
        return 0, "模型输出不是 JSON"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0, "模型输出缺少 items"

    got = 0
    with _LOCK:
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except Exception:
                continue
            if not (1 <= n <= len(keys)):
                continue
            name = _clean(str(item.get("name") or ""))
            if not name:
                continue
            sid = keys[n - 1]
            if not _NAMES.get(sid):
                _NAMES[sid] = name
                got += 1
    return got, ""


# ---------------------------------------------------------------- OCR 兜底


def ocr_available() -> bool:
    """可选依赖是否装了。没装不影响归属，只是名字显示不出来。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:
        return False


def _ocr_engine():
    global _OCR
    with _LOCK:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR = RapidOCR()
        return _OCR


def recognize(band: np.ndarray) -> str:
    """单条昵称条 → 名字（RapidOCR，18ms）。

    只作为**没有视觉权重时**的兜底，不是主路径：同一批条带它 4/6，
    Ornith 6/6（`Lnlion` 读空、`红姜、` 丢顿号）。别拿它当默认。
    """
    if band is None or band.size == 0 or not has_ink(band):
        return ""
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        eng = _ocr_engine()
        # img 是 RGB，RapidOCR 内部按 BGR 处理 → 翻转
        bgr = np.ascontiguousarray(band[:, :, ::-1])
        # 昵称位置已知，检测模型纯属浪费：det 全开 1.6s/条，只留 rec 是 18ms。
        res, _ = eng(bgr, use_det=False, use_cls=False, use_rec=True)
        if res:
            item = res[0]
            raw = str(item[0] or "")
            score = float(item[1]) if len(item) > 1 else 1.0
            if score >= 0.60:
                return _clean(raw)
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------- 维护


def _reset_locked() -> None:
    _SIG.clear()
    _NAMES.clear()
    _NEXT[0] = 1


def reset() -> None:
    """清空全部身份（测试与微信重启后用）。"""
    with _LOCK:
        _reset_locked()


def snapshot() -> dict[int, str]:
    """当前 id → 名字（面板显示用）。"""
    with _LOCK:
        return dict(_NAMES)

"""按屏收割当前微信会话的历史消息（小步滚动 + RuntimeId 精确去重）。

为什么用 RuntimeId 而不是文本对齐：微信群聊里「图片」「动画表情」「666」这类
重复文本极多，按文本求最长后缀-前缀会把不同时间的同一句话对齐错位，结果整屏
重复追加或整屏丢弃（2026-10-25 实测：文本法对齐成功率只有 20%）。微信每个
ListItem 都带唯一 RuntimeId（Qt 对象 ID），**滚动前后同一条消息的 RuntimeId
不变**（实测 8 行里 6 行原样保留），用它做锚点可以精确到条。

为什么要小步（2 格 ≈ 161px）：滚轮位移不是线性的 —— 实测 1 格 55px、2 格 161px、
3 格 301px、4 格 823px。位移一旦超过消息区高度（约 1181px），两屏之间就没有
共同消息，中间那段永久丢失。固定 2 格，保证同一屏里 6~10 条能原样对上。

用法：
    python tools/harvest_chat.py --target 无尽冬日3864区ALL天地联盟 \
        --max-rounds 6000 --out outputs/eval-3864/harvest_3864.json

前置：微信停在目标会话，录制中不要动鼠标键盘（脚本自己会前台化微信）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "m2_server"))

import chat_read  # noqa: E402
import wechat_uia  # noqa: E402
import wechat_voice  # noqa: E402

MOUSEEVENTF_WHEEL = 0x0800
WHEEL_NOTCH = 120     # 正 = 往上翻（看更早的消息）
STEP_NOTCHES = 2      # 每次 2 格 ≈ 161px，实测同屏保留 6~10 条

#: 这几类不是「人说的话」，拼进来只会干扰评测。
_SYS_HINTS = (
    "撤回了一条消息",
    "加入了群聊",
    "加入了群",
    "被添加为群管理员",
    "从群管理员中被移除",
    "邀请",
    "修改群名",
    "退出了群聊",
    "移出了群聊",
    "领取了",
    "拍了拍",
    "群公告",
    "开启了",
    "关闭了",
)


def _key(m: dict) -> str:
    return (m.get("text") or "").strip()


def _side_rank(s: str) -> int:
    """合并同一行时谁说了算：判出来的 left/right > center > unknown。

    左右是**像素**判的，同一条消息翻页后换了位置，`_row_side` 会抖
    （实测「最近这开箱子太过分了」一次 left、一次 unknown）。所以 side 只在
    已经对齐的重叠区里择优合并，不参与对齐。
    """
    return {"left": 3, "right": 3, "center": 2, "unknown": 1}.get(s, 0)


def _foreground() -> bool:
    """把微信顶到前台 —— 滚轮是发给「光标下的窗口」的，不在前台会滚错对象。"""
    hwnd, _pid = wechat_uia._wechat_hwnd_pid()
    if not hwnd:
        return False
    try:
        user32 = wechat_voice._user32()
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        return False
    time.sleep(0.3)
    return True


def _wheel_at(x: int, y: int, notches: int) -> None:
    wechat_voice._mouse_move_abs(x, y)
    time.sleep(0.04)
    step = WHEEL_NOTCH if notches > 0 else -WHEEL_NOTCH
    for _ in range(abs(notches)):
        wechat_voice._send_input_mouse(MOUSEEVENTF_WHEEL, mouse_data=step)
        time.sleep(0.05)


def _snap(chat) -> list[dict]:
    """读一屏：RuntimeId + 文本 + 屏幕矩形。顺序即视觉顺序（上→下）。"""
    try:
        children = chat.GetChildren()
    except Exception:
        return []
    out: list[dict] = []
    for c in children:
        try:
            if c.ControlTypeName != "ListItemControl":
                continue
            rid = c.GetRuntimeId()
            r = c.BoundingRectangle
            out.append({
                "rid": rid[-1] if rid else None,
                "text": (c.Name or "").strip(),
                "top": r.top,
                "bottom": r.bottom,
            })
        except Exception:
            continue
    return out


def _label(rows: list[dict]) -> None:
    """就地补 side（right=我 / left=对方 / center=时间 / unknown）。"""
    if not rows:
        return
    try:
        sides = chat_read.label_sides(rows)
    except Exception:
        sides = ["unknown"] * len(rows)
    for r, s in zip(rows, sides, strict=False):
        r["side"] = s


class Collector:
    """从「最新」往「更早」累积。acc 恒为**时间正序**（上早下晚），新内容前插。

    对齐用「文本 + 像素位置」双条件，**不能只用文本也不能只用 RuntimeId**：
    - 只用文本：「图片」「动画表情」「666」在群里重复率极高，最长后缀-前缀会撞车
      （实测对齐成功率只有 20%，整屏重复追加或整屏丢弃）。
    - 只用 RuntimeId：那是 **Qt 控件**的 id 不是消息 id。RecyclerListView 只有
      十来个控件反复复用，滚远之后控件 id 不变、内容全换 —— 用它去重会把所有
      新消息当成旧消息（实测 80 轮只收到 30 条就误判到顶）。
    - 两个一起：同一条消息在相邻两屏的 top 位移是**同一个常数**（滚轮位移很稳，
      实测 2 格 = 160~161px、3 格 = 301~302px），拿它校正文本匹配就不会错位。

    只拿**上一屏快照**参与对齐，不在 acc 上比对：acc 里不同批次的 top 来自不同
    时刻的屏幕坐标，跨屏后已不可比。
    """

    #: 允许的滚动位移范围（px）。滚 1 格 55、2 格 161、3 格 301、4 格 823。
    DY_MIN, DY_MAX = 40, 1100
    #: 同一条消息在两屏的 (top - dy) 与 top 允许的误差（渲染取整/子像素）。
    TOP_TOL = 8
    #: 至少连续对上几条才认这个位移，低于此判为「没对齐」。
    MIN_OVERLAP = 2

    def __init__(self) -> None:
        self.acc: list[dict] = []
        self.prev: list[dict] = []
        self.gaps = 0
        self.last_dy: int | None = None

    def _match(self, prev: list[dict], new: list[dict]) -> tuple[int, int, int | None]:
        """找「new 的尾部 == prev 的头部」的位移。返回 (新增条数 shift, 重叠条数 k, 位移)。"""
        best = (0, 0, None)
        for shift in range(len(new)):
            space = min(len(prev), len(new) - shift)
            if space < self.MIN_OVERLAP:
                break
            dy = new[shift]["top"] - prev[0]["top"]
            if not (self.DY_MIN <= dy <= self.DY_MAX):
                continue
            k = 0
            while (
                k < space
                and prev[k]["text"] == new[shift + k]["text"]
                and abs(new[shift + k]["top"] - prev[k]["top"] - dy) <= self.TOP_TOL
            ):
                k += 1
            if k > best[1]:
                best = (shift, k, dy)
        return best

    def add(self, new: list[dict]) -> tuple[int, int]:
        """把一屏并进来，返回 (新增条数, 重叠条数)。"""
        if not new:
            return 0, 0
        if not self.acc:
            self.acc.extend(new)
            self.prev = new
            return len(new), 0

        shift, k, dy = self._match(self.prev, new)
        if k >= self.MIN_OVERLAP:
            # 重叠区里 side 取判得更清楚的那个；文本取更长的（表情占位符会变）
            for i in range(k):
                old, cand = self.prev[i], new[shift + i]
                if _side_rank(cand.get("side")) > _side_rank(old.get("side")):
                    old["side"] = cand.get("side")
                if len(_key(cand)) > len(_key(old)):
                    old["text"] = cand.get("text")
            self.last_dy = dy
            head = new[:shift]
            if head:
                self.acc[0:0] = head
            self.prev = new
            return len(head), k

        # 一条都对不上：滚过头了（或列表刚被重建），这一屏整体前插，中间那段已经丢了
        self.gaps += 1
        self.acc[0:0] = new
        self.prev = new
        return len(new), 0




def harvest(target: str, max_rounds: int, settle: float = 0.34, step: int = STEP_NOTCHES) -> dict:
    chat, _sessions, err = chat_read._locate_lists()
    if err:
        return {"ok": False, "error": err}

    from chat_radar import capture as cap

    frame = cap.capture_frame()
    if frame is None:
        return {"ok": False, "error": "抓不到微信窗口画面，无法判左右"}
    roi = frame.roi
    cx = (roi.left + roi.right) // 2
    cy = (roi.top + roi.bottom) // 2

    col = Collector()
    rounds = 0
    stall = 0
    log: list[dict] = []

    # 先滚回最新：脚本可能被重跑，上次停在历史中间 —— 不回底会从半截接着往上翻，
    # 中间那段直接漏掉（看起来还一切正常，是最难发现的一种错）。
    print("回到最新消息…", flush=True)
    for _ in range(30):
        first_before = _snap(chat)
        _wheel_at(cx, cy, -100)
        time.sleep(1.0)
        first_after = _snap(chat)
        # 到底的判据是「首条的消息内容和像素位置都不动」，不能看 RuntimeId ——
        # 那是控件 id，11 个复用控件从头到尾都是这几个，永远相等。
        if (
            first_before
            and first_after
            and first_before[0]["text"] == first_after[0]["text"]
            and first_before[0]["top"] == first_after[0]["top"]
        ):
            break
    time.sleep(0.8)

    first = _snap(chat)
    _label(first)
    col.add(first)
    log.append({"r": 0, "added": len(col.acc), "total": len(col.acc)})
    prev_top = first[0]["top"] if first else None

    # 到顶判据：连续 40 轮「既没有新增、首条位置也不动」。滚 2 格只有 161px，
    # 一张图能占 432px，所以「滚了但没新内容」是正常的（列表在动，只是还在同一
    # 条高消息里）—— 只有「位置都不动」才说明真滚不动了。
    while rounds < max_rounds and stall < 40:
        rounds += 1
        _wheel_at(cx, cy, step)
        time.sleep(settle)

        now = _snap(chat)
        if not now:
            time.sleep(0.4)
            now = _snap(chat)
            if not now:
                stall += 1
                continue

        _label(now)
        added, ov = col.add(now)
        moved = prev_top is None or abs(now[0]["top"] - prev_top) > 1
        prev_top = now[0]["top"]
        if added > 0:
            stall = 0
        elif not moved:
            stall += 1

        if rounds % 20 == 0:
            print(
                f"  …{rounds} 轮 / {len(col.acc)} 条 / 顶={now[0]['text'][:16]!r}",
                flush=True,
            )
        if rounds % 200 == 0:
            log.append({"r": rounds, "total": len(col.acc), "gaps": col.gaps})

    kept = [
        m for m in col.acc
        if m.get("text")
        and m.get("side") != "center"
        and not any(h in m["text"] for h in _SYS_HINTS)
    ]
    for i, m in enumerate(kept):
        m["i"] = i
    return {
        "ok": True,
        "target": target,
        "total": len(kept),
        "raw_total": len(col.acc),
        "rounds": rounds,
        "step": step,
        "last_dy": col.last_dy,
        "gaps": col.gaps,
        "stall": stall,
        "reached_top": stall >= 40,
        "messages": kept,
        "log": log,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="", help="期望的会话标题（只做校验/记录）")
    ap.add_argument("--max-rounds", type=int, default=6000, help="最多滚多少轮（每轮 2 格 ≈161px）")
    ap.add_argument("--out", default="", help="输出 json 路径")
    ap.add_argument("--settle", type=float, default=0.34, help="每轮后等待秒数")
    ap.add_argument("--step", type=int, default=STEP_NOTCHES, help="每轮滚几格（1格≈55px, 2格≈161px, 3格≈301px, 4格≈823px）")
    args = ap.parse_args()

    _msgs, _sess, cur, err = chat_read.collect()
    if err:
        print(f"读不到微信：{err}")
        return 2
    print(f"当前会话：{cur}")
    if args.target and args.target not in cur:
        print(f"⚠️ 目标「{args.target}」与当前会话不一致，先停止（请在微信里打开目标群）")
        return 3

    if not _foreground():
        print("⚠️ 没能把微信顶到前台，滚轮可能滚错窗口 —— 先手动点一下微信再跑")
        return 4

    t0 = time.time()
    res = harvest(cur, args.max_rounds, args.settle, args.step)
    dt = time.time() - t0
    if not res.get("ok"):
        print(f"收割失败：{res.get('error')}")
        return 5

    sides: dict[str, int] = {}
    for m in res["messages"]:
        sides[m["side"]] = sides.get(m["side"], 0) + 1
    print(
        f"滚 {res['rounds']} 轮（{res['rounds'] * res.get('step', STEP_NOTCHES)} 格），用时 {dt:.0f}s，"
        f"原始 {res['raw_total']} 条 → 有效 {res['total']} 条"
    )
    print(f"到顶：{'是' if res['reached_top'] else '否'}（stall={res['stall']}），断档 {res['gaps']} 次")
    print("side 分布：", sides)

    out = Path(args.out) if args.out else ROOT / "outputs" / "harvest" / f"harvest_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

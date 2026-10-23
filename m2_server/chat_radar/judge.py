"""L1 判断层：把四个几何信号翻译成「现在该不该回」。

设计原则（踩过坑才定下来的）
--------------------------
**判断层拿不到文字。** 这不是省成本，是防止污染：
    一旦把聊天原文喂进判断，模型就会去"理解对方心情"，
    而实测表明它连"最后一条是谁发的"都会判错（0.80s 给出自信的错误答案）。
    四个几何量是 100% 确定的，用它们判断，结论才可信。

分级触发（钱和延迟都花在刀刃上）
-----------------------------
    gap < 30s              → 不打扰（对方可能还在打字）
    gap > 10min 且对方最后  → 直接「该回了」（纯规则，0 成本）
    30s ~ 10min 边界区      → 才调千问（可选，未配 key 则仍走规则）

实测千问文本调用约 200-400ms，一次判断 ~100 in / ~5 out token，
百万次约 ¥0.02 —— 所以别为了省钱在这里抠，该调就调。

**问法必须有界。** 只问「0-5 的整数」，不要让它解释。
让它解释会生成上百 token，慢 10 倍还容易编。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .state import Signals

#: 动作枚举
NONE = "none"  # 不用回
WAIT = "wait"  # 再等等
SOON = "soon"  # 可以回了
NOW = "now"  # 该回了
UNKNOWN = "unknown"  # 信息不足

ACTION_LABEL = {
    NONE: "不用回",
    WAIT: "再等等",
    SOON: "可以回了",
    NOW: "该回了",
    UNKNOWN: "观察中",
}

#: 时间档位（秒）
GAP_TOO_EARLY = 30.0  # 小于这个，对方大概率还在连着打字
GAP_OVERDUE = 600.0  # 大于这个，纯规则直接判「该回了」

#: 连发修正：对方连发越多，越该回
STREAK_BUMP_AT = 3
STREAK_BUMP2_AT = 5


@dataclass
class Verdict:
    action: str
    score: int  # 0-5，越大越急
    reason: str
    source: str = "rule"  # rule / llm

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "label": ACTION_LABEL.get(self.action, self.action),
            "score": self.score,
            "reason": self.reason,
            "source": self.source,
        }


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "未知"
    if sec < 60:
        return f"{sec:.0f}秒"
    if sec < 3600:
        return f"{sec/60:.0f}分钟"
    return f"{sec/3600:.1f}小时"


def judge_by_rule(sig: Signals) -> Verdict:
    """纯规则判断。零延迟、零成本、结果确定，是默认路径。"""
    if sig.last_side is None:
        return Verdict(UNKNOWN, 0, "没有检测到聊天消息")

    if sig.last_side == "right":
        return Verdict(NONE, 0, "最后一条是我发的")

    if sig.tail_meta == "sys":
        return Verdict(NONE, 0, "最后是系统提示（如领取红包），没人等你回")

    # 到这里 = 对方最后说话
    gap = sig.gap_seconds
    if gap is None:
        return Verdict(UNKNOWN, 2, "对方最后说话，间隔未知（刚启动观察中）")

    prefix = "至少 " if sig.gap_lower_bound else ""
    if gap >= GAP_OVERDUE:
        return Verdict(
            NOW,
            min(5, 4 + (1 if sig.streak >= STREAK_BUMP_AT else 0)),
            f"对方最后说话，已过 {prefix}{_fmt_age(gap)}",
        )
    if gap < GAP_TOO_EARLY:
        if sig.gap_lower_bound:
            # 刚启动时屏幕上的消息可能是 3 秒前发的，也可能 3 小时前发的，
            # 这时候说「对方刚发」是在编造——宁可承认不知道。
            return Verdict(
                UNKNOWN, 2, f"对方最后说话，启动观察中（至少 {_fmt_age(gap)}，时长未知）"
            )
        return Verdict(WAIT, 1, f"对方刚发 {prefix}{_fmt_age(gap)}，可能还在打字")

    score = 3
    if sig.streak >= STREAK_BUMP_AT:
        score += 1
    if sig.streak >= STREAK_BUMP2_AT:
        score += 1
    extra = f"，对方连发 {sig.streak} 条" if sig.streak >= STREAK_BUMP_AT else ""
    return Verdict(SOON, min(5, score), f"对方最后说话 {prefix}{_fmt_age(gap)}{extra}")


# ------------------------------------------------------------------ 千问（可选）


def _llm_config() -> tuple[str, str, str] | None:
    key = os.environ.get("VM_RADAR_LLM_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        return None
    model = os.environ.get("VM_RADAR_LLM_MODEL", "qwen-flash")
    base = os.environ.get(
        "VM_RADAR_LLM_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    return key, model, base


_PROMPT = (
    "当前状态：对方最后发言，已过 {gap} 秒；对方连发 {streak} 条；"
    "我上次回复在 {my_age} 秒前。\n"
    "请给出「现在插话的合适程度」，只输出 0-5 的整数，不要任何解释。"
)


def judge_by_llm(sig: Signals, timeout: float = 3.0) -> Verdict | None:
    """边界档的细化判断。未配置 key / 请求失败都返回 None，由调用方降级。"""
    cfg = _llm_config()
    if cfg is None or sig.gap_seconds is None:
        return None
    key, model, base = cfg

    prompt = _PROMPT.format(
        gap=int(sig.gap_seconds),
        streak=sig.streak,
        my_age="未知" if sig.my_last_reply_age is None else int(sig.my_last_reply_age),
    )
    try:
        import requests

        resp = requests.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4,
                "temperature": 0.0,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        text = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None

    m = re.search(r"[0-5]", text or "")
    if not m:
        return None
    score = int(m.group())
    if score >= 4:
        action = NOW
    elif score >= 2:
        action = SOON
    else:
        action = WAIT
    return Verdict(action, score, f"千问评分 {score}/5", source="llm")


def judge_signals(sig: Signals, use_llm: bool = False) -> Verdict:
    """默认入口：规则优先，边界档可选交给千问。"""
    v = judge_by_rule(sig)
    if not use_llm:
        return v
    # 只有「30s ~ 10min 的对方最后发言」这一窄档值得花一次调用
    if v.action != SOON:
        return v
    refined = judge_by_llm(sig)
    return refined or v


def dumps(verdict: Verdict, sig: Signals) -> str:
    """单行输出，给 CLI / 桌面角标用。"""
    return json.dumps(
        {"verdict": verdict.as_dict(), "signals": sig.as_dict()}, ensure_ascii=False
    )

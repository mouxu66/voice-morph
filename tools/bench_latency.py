"""端到端延迟基准：把「实时变声有多快」量成一个可复现的数字。

为什么要有这个文件
------------------
申报书里写着「端到端 p50 ≈ 50 ms，✅ 本机实测」，但项目里**没有对应的测量
脚本、没有原始日志、没有测量方法**。而 `rvc_live.infer_latency()` 的
docstring 自己写得很清楚：

    ⚠️ 口径：这是「单块推理耗时」，**不是端到端延迟**。

这两句话放在一起就是风险：评审问「50ms 怎么测的」，答不上来。

本工具把口径拆开量，让每个数字都能追溯到它的定义：

    端到端延迟 = 分块缓冲 + 单块推理 + 驱动缓冲
                 ─────────   ────────   ────────
                 配置可算     日志实测    需实测/标称

★ 关键立场：**这是三段时间，不是一段时间。**
  把它们加起来当一个数报出去，就会得到一个谁也复现不了的"延迟"。
  所以本工具分开报，并显式标注哪一段是实测、哪一段是配置推算、哪一段是标称。

用法
----
    # 只算「配置可推算」那段（零依赖，随时能跑，CI 里也能跑）
    python tools/bench_latency.py --profile balanced

    # 加上日志实测的推理耗时（需要跑过一次实时变声，日志里有「推理耗时」）
    python tools/bench_latency.py --profile balanced --log <path/to/realtime_gui.log>

    # 把结果落盘成可引用的报告
    python tools/bench_latency.py --profile balanced --out docs/延迟基准报告.md

设计约束
--------
1. **绝不编造**：没有日志就明确说「推理段未实测」，不填一个估计值。
   宁可报告是"不完整"的，也不要一个看起来很完整但没人能复现的数。
2. **口径写进产物**：落盘的报告里每一段都带它的测量方法，方便评审复核。
3. **零新依赖**：只用标准库 + 项目已有的 `rvc_live`。
4. **纯函数可单测**：`summarize()` / `render_md()` 不碰文件系统。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------- 常量 ----------------

#: 音频驱动缓冲的**标称**值（毫秒）。★ 这不是实测值，是纸面参数。
#:
#: 为什么单独列出来并标注：MME/WASAPI 的驱动缓冲各家不一样（MME 常见 2×10ms，
#: WASAPI 共享模式常见 10ms，独占模式可到 3ms），而本工具**不探测真实驱动**
#: （那要独占音频设备，CI 里跑不了，且会打断用户正在听的音乐）。
#: 所以要如实标"标称"，让读者知道这一段最不可靠。
DRIVER_BUFFER_MS_NOMINAL = 20.0

#: 延迟声称的对照值（README / 申报书里那个数）。用来做"对账"，
#: 不是为了凑到它 —— 对不上就如实报警。
CLAIMED_E2E_P50_MS = 50.0

#: 推理耗时的分位口径。p50 用于代表"典型"，p95 用于代表"卡顿风险"。
#: ★ 实时变声里 p95 比 p50 更该关心：偶尔卡一帧，通话体验立刻掉。
PERCENTILES = (0.50, 0.95, 0.99)


# ---------------- 纯函数 ----------------

def percentile(vals: list[float], q: float) -> float | None:
    """就近取秩的分位数（与 `rvc_live.infer_latency` 同口径，避免两处不一致）。

    为什么不用插值：本项目里这些分布是**离散的耗时样本**，插值出来的
    "第 95 百分位"可能是一个从未真实出现过的值。就近取秩至少保证
    报出来的数**是真实测到过的**。
    """
    if not vals:
        return None
    s = sorted(vals)
    if q <= 0:
        return s[0]
    if q >= 1:
        return s[-1]
    idx = min(len(s) - 1, int(len(s) * q))
    return s[idx]


def summarize(infer_ms: list[float], block_ms: float,
              driver_ms: float = DRIVER_BUFFER_MS_NOMINAL) -> dict:
    """把三段耗时汇总成一个结构化结果（纯函数，不碰文件系统）。

    ★ 返回的 `e2e_ms` 只有在 `infer_ms` 非空时才是实测值；否则是 `None`
    —— **不给一个"看起来能用"的默认值**。缺实测就如实缺。
    """
    res: dict = {
        "block_ms": round(float(block_ms), 1),
        "driver_ms": round(float(driver_ms), 1),
        "infer_ms": None,
        "infer_p95_ms": None,
        "infer_p99_ms": None,
        "infer_samples": len(infer_ms),
        "e2e_ms": None,
        "e2e_p95_ms": None,
        "measured": bool(infer_ms),
    }
    if infer_ms:
        med = statistics.median(infer_ms)
        res["infer_ms"] = round(med, 1)
        res["infer_p95_ms"] = round(percentile(infer_ms, 0.95) or 0.0, 1)
        res["infer_p99_ms"] = round(percentile(infer_ms, 0.99) or 0.0, 1)
        # 端到端 = 等一块凑满 + 推理这一块 + 驱动缓冲
        res["e2e_ms"] = round(float(block_ms) + med + float(driver_ms), 1)
        res["e2e_p95_ms"] = round(
            float(block_ms) + (percentile(infer_ms, 0.95) or 0.0) + float(driver_ms), 1)
    return res


def compare_claim(e2e_ms: float | None, claimed: float = CLAIMED_E2E_P50_MS) -> dict:
    """把实测端到端与申报书里的声称对账。

    ★ 三种结论分开说，不许含糊：
      - `ok`       —— 实测与声称同量级（声称值落在实测的 ±30% 内）
      - `inflated` —— 实测**明显大于**声称（声称被高估，有诚信风险）
      - `unknown`  —— 没有实测值，无法对账（如实说"没测"）
    """
    if e2e_ms is None:
        return {"status": "unknown",
                "detail": f"没有实测值，无法与声称的 {claimed:.0f}ms 对账"}
    ratio = e2e_ms / claimed if claimed else 0.0
    if ratio >= 1.3:
        return {"status": "inflated", "ratio": round(ratio, 2),
                "detail": (f"实测端到端 {e2e_ms:.0f}ms 是声称 {claimed:.0f}ms 的 "
                           f"{ratio:.1f}× —— 声称的口径几乎肯定是「单块推理耗时」"
                           f"而非端到端，建议在材料里改写口径")}
    if ratio <= 0.7:
        return {"status": "better", "ratio": round(ratio, 2),
                "detail": f"实测 {e2e_ms:.0f}ms 优于声称 {claimed:.0f}ms，可更新材料"}
    return {"status": "ok", "ratio": round(ratio, 2),
            "detail": f"实测 {e2e_ms:.0f}ms 与声称 {claimed:.0f}ms 同量级"}


def render_md(res: dict) -> str:
    """渲染人类可读报告。★ 每段都带测量方法，方便评审复核。"""
    L: list[str] = []
    L.append("# 实时变声链路延迟基准")
    L.append("")
    L.append(f"- 档位：`{res.get('profile')}`"
             f"（block_time = {res.get('block_ms')}ms）")
    L.append(f"- 测量时间：{res.get('measured_at', '未记录')}")
    L.append("")
    L.append("## ★ 口径声明（先读这一段）")
    L.append("")
    L.append("端到端延迟是**三段之和**，不是单段：")
    L.append("")
    L.append("```")
    L.append("端到端 = 分块缓冲 + 单块推理 + 驱动缓冲")
    L.append("```")
    L.append("")
    L.append("| 段 | 值 | 来源 | 可靠性 |")
    L.append("|---|---|---|---|")
    L.append(f"| 分块缓冲 | {res['block_ms']}ms | 档位配置 `block_time` 推算 | 高（配置值） |")
    if res["measured"]:
        L.append(f"| 单块推理 | {res['infer_ms']}ms | 实时日志实测"
                 f"（{res['infer_samples']} 个样本，取中位数） | 高（真实日志） |")
    else:
        L.append("| 单块推理 | **未实测** | — | ⚠️ 缺（未提供日志） |")
    L.append(f"| 驱动缓冲 | {res['driver_ms']}ms | **标称值**（未探测真实驱动） | 低（纸面参数） |")
    L.append("")
    if res["measured"]:
        L.append(f"**合计 p50 = {res['e2e_ms']}ms**，p95 = {res['e2e_p95_ms']}ms。")
        L.append("")
        L.append(f"单块推理本身：p50 {res['infer_ms']}ms / p95 {res['infer_p95_ms']}ms"
                 f" / p99 {res['infer_p99_ms']}ms。")
    else:
        L.append("**合计无法给出** —— 推理段没有实测值。")
        L.append("")
        L.append("> 这是刻意的：拿 `0` 或一个估计值填进去，会得到一个"
                 "看起来很完整但谁也复现不了的数字。")
    L.append("")
    cp = res.get("claim") or {}
    L.append("## 与材料里的声称对账")
    L.append("")
    L.append(f"- 声称值：`{CLAIMED_E2E_P50_MS:.0f}ms`（申报书 / README）")
    L.append(f"- 结论：**{cp.get('status', 'unknown')}** —— {cp.get('detail', '')}")
    L.append("")
    L.append("## 怎么复核")
    L.append("")
    L.append("```bash")
    L.append("# ① 只看配置可推算的部分（零依赖）")
    L.append(f"python tools/bench_latency.py --profile {res.get('profile', 'balanced')}")
    L.append("")
    L.append("# ② 带上你机器上的实时日志，得到推理段的实测值")
    L.append("python tools/bench_latency.py --profile balanced \\")
    L.append("    --log <logs/<exp>/realtime_gui.log>")
    L.append("```")
    L.append("")
    L.append("★ 换机器测出来的数**必然不同**（GPU、CPU、驱动、后台负载都会变）。")
    L.append("本报告的价值不在那个数字本身，而在**口径被拆开、每一段都能追溯**。")
    L.append("")
    return "\n".join(L)


# ---------------- IO 层 ----------------

def _profile_block_ms(profile: str) -> float:
    """从 `rvc_live.PROFILE_TUNING` 读该档位的分块时长（毫秒）。

    ★ 读真配置而不是抄一份数字：抄的那份会和代码漂移，
    而漂移的后果是"报告里的延迟"和"实际跑的延迟"对不上。
    """
    import rvc_live
    tuning = rvc_live.PROFILE_TUNING.get(profile)
    if not tuning:
        raise KeyError(f"未知档位 {profile!r}；可用：{sorted(rvc_live.PROFILE_TUNING)}")
    return float(tuning["block_time"]) * 1000.0


def read_infer_ms(log: Path | None) -> list[float]:
    """从实时日志里抽出所有「推理耗时：X.XX秒」→ 毫秒列表。

    复用 `rvc_live.parse_infer_ms`（同一份解析逻辑，避免两处口径分叉）。
    """
    if log is None:
        return []
    p = Path(log)
    if not p.exists():
        return []
    import rvc_live
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return rvc_live.parse_infer_ms(text)


def bench(profile: str = "balanced", *, log: Path | None = None,
          tail: int | None = None) -> dict:
    """跑一次基准。`log` 为 None 时只给配置段（推理段留空）。"""
    from datetime import datetime

    try:
        block = _profile_block_ms(profile)
    except KeyError as e:
        return {"error": str(e), "error_stage": "档位"}

    vals = read_infer_ms(log)
    if tail and tail > 0:
        vals = vals[-tail:]
    res = summarize(vals, block)
    res["profile"] = profile
    res["measured_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    res["log"] = str(log) if log else None
    res["claim"] = compare_claim(res["e2e_ms"])
    return res


def main() -> None:
    # 重定向到管道时 Python 按 cp936 写 stdout —— 中文会变乱码，这里统一钉成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    p = argparse.ArgumentParser(
        description="实时变声链路延迟基准（端到端 = 分块缓冲 + 推理 + 驱动缓冲）")
    p.add_argument("--profile", default="balanced",
                   help="性能档位（balanced / game）")
    p.add_argument("--log", help="实时日志路径（有「推理耗时」行）；不给则推理段留空")
    p.add_argument("--tail", type=int, default=None,
                   help="只统计最后 N 个样本（默认全部）")
    p.add_argument("--out", help="把 Markdown 报告写到这个路径")
    p.add_argument("--json", action="store_true", help="打印 JSON 而不是 Markdown")
    args = p.parse_args()

    res = bench(args.profile, log=Path(args.log) if args.log else None, tail=args.tail)
    if res.get("error"):
        print(f"[bench] 失败（{res.get('error_stage')}）: {res['error']}")
        sys.exit(1)

    md = render_md(res)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"[bench] 报告已写入: {out}")

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(md)
        print(f"[bench] 分块缓冲 {res['block_ms']}ms"
              + (f" + 推理 {res['infer_ms']}ms + 驱动 {res['driver_ms']}ms"
                 if res["measured"] else "（推理段未实测）")
              + f" ⇒ {res['claim']['status']}")


if __name__ == "__main__":
    main()

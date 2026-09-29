# -*- coding: utf-8 -*-
"""音色体检「三灯」合成：相似度 / 自然度 / 夹嗓子风险。

2026-09-29 新增。为什么有这个模块：
    用户最痛的一句话是「训练完才知道音色不行」。项目里其实已经攒了一堆
    别家没有的质检件 —— CAM++ 声纹、`tools/clip_qc.py` 切片打分、
    `tools/voice_qc.py` 变声验收、A-B 盲听（Audition 页）—— 但它们都是
    **内部工具，用户看不见**。本模块把原始测量值合成三个灯，让「这份音色的
    质量」第一次在 UI 上可见，并且**训练前就能给预期**。

三个灯的数据来源（每一个都有明确出处，不是拍脑袋）：
    相似度   CAM++ 声纹余弦（变声输出 vs 目标音色参考）—— 现役判据，
             阈值 ≥0.95（与 `tools/voice_qc.py` 既有口径一致）。
    自然度   HNR（谐噪比，浊音帧中位数）。伪影/电音/底噪都会压低它。
    夹嗓子   H1-H2（主判据）+ 谱倾斜（印证）。方向：**越大/越负 = 越挤压**，
             这个方向是合成信号实测标定的，见 `tools/voice_metrics.py` 的
             `_h1_h2` 文档字符串（⚠️ 与调研文档 4.2 节的通用表述相反，以实测为准）。

★★ 阈值来自实测分布，不是文献数字：
    2026-09-29 在**干净训练集全部 73 条自录切片**（`video_260828_110637_*` +
    `video_260828_105338_*`，即 AGENTS.md 袋鼠音色铁律里唯一认可的素材）上
    跑出真实指标。但**灯的输入是「20 条采样的中位数」，不是单条值** ——
    所以阈值必须锚在**中位数的分布**上，否则会犯一个很隐蔽的错：
    拿单条分布（h1_h2 P75=5.16）去判中位数（实测 4.8）会误亮红灯。

    ★ 因此用 Bootstrap（每次抽 20 条取中位数 ×2000 次）标定中位数分布：

        HNR      P5=11.09  P25=11.39  P50=11.68  P75=11.91  P95=12.11
        H1-H2    P5= 2.99  P25= 3.44  P50= 3.78  P75= 4.33  P95= 4.92
        tilt     P5=-7.66  P25=-7.39  P50=-7.25  P75=-7.11  P95=-6.88

    黄档 = 越过 P25（好的一侧），红档 = 越过 P5。即「这份素材与你自己
    其余素材比，是否明显偏差」—— 而不是与某个绝对标准比。

    ⚠️ 副作用要说清楚：这意味着**换一个人、换一支麦克风的素材，档位会偏**
    （这批素材是同一个人同一支麦）。当前阶段可接受（单用户、单音色主场景）；
    要跨用户推广，得改成「与该用户自己的历史基线比」，记在
    `docs/音色体检三灯.md` 的「已知偏差」一节，不在这里假装它是通用的。

与 `tools/voice_qc.py` 的分工：
    `voice_qc.py` 是**入库验收**（4 指标 ×25 分 = 100 分制 PASS/FAIL，落盘
    `outputs/qc/<exp>.json`，`rvc_live` 回调依赖它）。本模块是**用户可见的体检**，
    输出三个灯 + 可读结论，**不改动**既有的 score/pass 语义 —— 两个东西
    服务不同目的，硬合并会让验收口径被 UI 需求带偏。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------- 灯的三档 ----------------
GREEN, YELLOW, RED = "green", "yellow", "red"

# ★ 阈值锚点：**20 条采样中位数**的 Bootstrap 分布 P25/P5（见模块文档字符串）。
# 语义：绿 = 在你自己的素材基线内；黄 = 越过 P25；红 = 越过 P5（尾部）。
# 每个阈值都给了出处，改之前先回去看 distribution。
_THRESHOLDS = {
    # 相似度：沿用 voice_qc.py 既有口径（≥0.95 PASS），黄档留缓冲区。
    # 注：它测的是**单次变声的余弦**，不是中位数，所以锚在既有验收口径上。
    "emb_sim":  {"green": 0.95, "yellow": 0.90, "higher_better": True},
    # 自然度：中位数分布 P25=11.39 / P5=11.09
    "hnr":      {"green": 11.39, "yellow": 11.09, "higher_better": True},
    # 夹嗓子：H1-H2 中位数分布 P25=3.44 / P5=2.99（越大越危险）
    "h1_h2":    {"green": 3.44, "yellow": 4.92, "higher_better": False},
    # 谱倾斜：中位数分布 P25=-7.39 / P5=-7.66。**越负越危险**。
    # ⚠️ 注意这里的 green/yellow 语义与上面三个**不同向**：
    #    tilt 的「好」一侧是**数值更大**（更接近 0，谱滚降更缓），
    #    所以 higher_better=True，而 green/yellow 值是「不低于多少才算好」。
    #    2026-09-29 实测踩过一次：写成 higher_better=False + green=-7.39
    #    会把 -7.0（更健康）判成红、把 -8.0（更挤压）判成绿，灯整个反掉。
    "tilt":     {"green": -7.39, "yellow": -7.66, "higher_better": True},
}


@dataclass
class Lamp:
    """一个灯。value 为 None 表示「没测到」，不是「不合格」——两者必须分开，
    否则 parselmouth 缺失会被显示成红灯，用户会去修一个不存在的问题。"""

    key: str
    label: str
    value: float | None
    lamp: str | None          # green/yellow/red；None = 未测
    detail: str = ""
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "value": self.value,
                "lamp": self.lamp, "detail": self.detail, "sources": self.sources}


def _grade(key: str, value: float | None) -> str | None:
    """按 _THRESHOLDS 给单值分档。value 为 None → None（未测，不算红）。"""
    if value is None:
        return None
    cfg = _THRESHOLDS[key]
    g, y, higher = cfg["green"], cfg["yellow"], cfg["higher_better"]
    if higher:
        if value >= g:
            return GREEN
        return YELLOW if value >= y else RED
    else:
        if value <= g:
            return GREEN
        return YELLOW if value <= y else RED


def similarity_lamp(emb_sim: float | None) -> Lamp:
    """灯 1：相似度（CAM++ 声纹余弦）。数据来自变声验收，**训练后才有**。"""
    lamp = _grade("emb_sim", emb_sim)
    if emb_sim is None:
        det = "未测：需要先完成一次变声验收（依赖 worker 8001 与可推理权重）"
    else:
        det = f"声纹余弦 {emb_sim:.4f}（阈值 ≥0.95）"
    return Lamp("similarity", "相似度", emb_sim, lamp, det)


def naturalness_lamp(hnr: float | None) -> Lamp:
    """灯 2：自然度（HNR）。伪影/电音/底噪都会压低它。"""
    lamp = _grade("hnr", hnr)
    if hnr is None:
        det = "未测：音频里没有足够的浊音段，或 parselmouth 不可用"
    else:
        det = f"HNR {hnr:.1f} dB（素材基线中位 11.7 dB）"
    return Lamp("naturalness", "自然度", hnr, lamp, det, ["hnr"])


def strain_lamp(h1_h2: float | None, tilt: float | None) -> Lamp:
    """灯 3：夹嗓子风险。**H1-H2 主判，谱倾斜印证**。

    取两者中**更危险**的那个档位 —— 保守优先。任一缺失则只用另一个；
    两个都缺 = 未测。这样设计的理由：两个量测的是同一个现象的不同侧面
    （谱包络陡度），不该互相平均掉；只要有一个显示危险，就值得复查。
    """
    l1, l2 = _grade("h1_h2", h1_h2), _grade("tilt", tilt)
    order = {GREEN: 0, YELLOW: 1, RED: 2}
    present = [x for x in (l1, l2) if x is not None]
    lamp = max(present, key=lambda x: order[x]) if present else None

    parts = []
    if h1_h2 is not None:
        parts.append(f"H1-H2 {h1_h2:+.1f} dB")
    if tilt is not None:
        parts.append(f"谱倾斜 {tilt:+.1f} dB/oct")
    if not parts:
        det = "未测：音频谐波结构不足，或 parselmouth 不可用"
    else:
        det = "，".join(parts) + "（越大/越负 = 越挤压）"
    srcs = [k for k, v in (("h1_h2", h1_h2), ("spectral_tilt", tilt)) if v is not None]
    return Lamp("strain", "夹嗓子风险", h1_h2, lamp, det, srcs)


def verdict(lamps: list[Lamp]) -> str:
    """一句话结论。**没测到的灯不参与判词** —— 不能因为测不了就说「没问题」。"""
    tested = [l for l in lamps if l.lamp is not None]
    if not tested:
        return "未测到可判定的指标（先跑一次变声验收，或确认 parselmouth 可用）"
    reds = [l.label for l in tested if l.lamp == RED]
    yellows = [l.label for l in tested if l.lamp == YELLOW]
    untested = [l.label for l in lamps if l.lamp is None]
    if reds:
        base = "需要复查：" + "、".join(reds) + " 亮红"
    elif yellows:
        base = "可用，但「" + "、".join(yellows) + "」偏弱，建议多试几段素材再定"
    else:
        base = "三项体检都在素材基线内，可以直接用"
    if untested:
        base += f"（{ '、'.join(untested) } 未测）"
    return base


def three_lamps(emb_sim: float | None = None, hnr: float | None = None,
                h1_h2: float | None = None, spectral_tilt: float | None = None) -> dict:
    """合成三灯。返回可直接 JSON 化的 dict（给前端音色卡片用）。

    参数全部可选 —— 训练前只有 HNR/H1-H2（能算，只需音频），
    相似度要等变声验收。**这个「部分可用」是刻意支持的**，因为
    「训练前给预期质量」正是这个功能最值钱的用法。
    """
    lamps = [similarity_lamp(emb_sim),
             naturalness_lamp(hnr),
             strain_lamp(h1_h2, spectral_tilt)]
    return {
        "lamps": [l.to_dict() for l in lamps],
        "verdict": verdict(lamps),
        "tested_count": sum(1 for l in lamps if l.lamp is not None),
    }

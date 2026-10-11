# -*- coding: utf-8 -*-
"""双空间声纹门禁：**独立声纹（CAM++）主判 + 自空间（Qwen `/emb`）保底**。

为什么不能只用一把尺子（2026-10-11 实测，pilotC，48 同人 / 96 异人配对）
------------------------------------------------------------------------
现役那条相似度口径（`tools/voice_qc.py` 的 `emb_sim`、`voice_report` 的相似度灯）
量的是 **Qwen `/emb` 自空间余弦** —— 与克隆用的是**同一个编码器**，属于「自己出题
自己判卷」。实测它并非分不开人（3 说话人的同人/异人 AUC 也是 1.0），而是
**同人区间被压扁**：

    自空间   同人 0.9783–0.9906（mean 0.9846，30s 起 100% ≥0.98）
             异人 0.9235–0.9534（mean 0.9386）
    CAM++    同人 0.6486–0.8345（mean 0.7486）
             异人 -0.0131–0.2744（mean 0.1452）

自空间整把尺子只有约 **0.07 行程**，而常用的 0.95 阈值离**异人上界（0.9534）只差
0.005** —— 阈值型门禁几乎没有分辨空间（要么全过、要么全挂）。CAM++ 是独立模型，
分布铺得开，对参考音时长的敏感度约为自空间的 12 倍。

证据：`docs/大创申报-2026-10/pilotC-公开语料证据/dual_embed/`（report.md /
results.json / pairs.csv，含 144 对逐条分数）。

阈值从实测分布推导（不是文献数字、不是手填）
--------------------------------------------
规则：取「同人最低分」与「异人最高分」之间的间隔，按 **7:3** 分 ——
靠同人一侧的 70% 点 = `pass`（同人过），30% 点 = `warn`（低于它判 `reject`）；
统一向下取整到 0.01。**为什么偏向同人一侧**：假拒绝（同人被拦）代价最高
（用户白录一遍、白等一次训练），而 `warn` 点仍远高于异人上界，不会误放。

`derive()` 是可复算的纯函数；`DUAL` 是它的冻结结果；`check_thresholds()` 是不变式
自检；`audit()` 拿逐条实测分数**重放**门禁（同人 48/48 通过、异人 0/96 误收）。
改标定或改公式前先跑 `m2_server/tests/test_voice_dual_gate.py`。

⚠️ 边界：n = 3 说话人、公开语料（AISHELL-3，参考音为同一说话人同一次录制的多条
拼接，**非被试**）、16k 中文 —— 阈值只对本项目这条路负责，换语料/换模型要重标。
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path

#: 证据目录（`pairs.csv` 就是标定数据本身，门禁的回归测试直接读它）。
EVIDENCE_DIR = Path("docs/大创申报-2026-10/pilotC-公开语料证据/dual_embed")

#: 2026-10-11 pilotC 实测簇界（取值口径见上）。⚠️ 改这里必须同步改 `audit()` 的期望吗？
#: 不用 —— `audit()` 每次从 pairs.csv 现算，这里的数字由
#: `test_measured_boundaries_match_the_committed_evidence` 钉住（改了就红）。
MEASURED: dict[str, dict[str, float]] = {
    # 独立声纹（CAM++，modelscope damo/speech_campplus_sv_zh-cn_16k-common）
    "cam_cos": {"same_min": 0.6486, "same_max": 0.8345, "cross_max": 0.2744},
    # 自空间（Qwen3-TTS worker `/emb`，与克隆同一编码器）
    "qwen_cos": {"same_min": 0.9783, "same_max": 0.9906, "cross_max": 0.9534},
}

#: 间隔里归「同人一侧」的比例（见上文的 7:3 规则）。
SAME_SHARE = 0.7

#: 取整步长：统一向下取整到 0.01（保守一侧，理由见上文）。
STEP = 0.01

#: 判词（与前端/档案记录的字符串契约，别改名）。
PASS, REVIEW, REJECT, UNKNOWN = "pass", "review", "reject", "unknown"


def _floor_to(x: float, step: float = STEP) -> float:
    """向下取整到 step 的整数倍。

    ★ 不能直接 `x // step * step`：浮点误差会让 0.53 变成 0.5299999999999999
    （0.53 / 0.01 = 52.99999999999999 → floor = 52）。先按 step 归一化再取整。
    """
    return math.floor(round(x / step, 6)) * step


def derive(same_min: float, cross_max: float, share: float = SAME_SHARE) -> dict:
    """从两簇的边界推导三级阈值（**纯函数**：换素材只需重跑这一步 + audit）。

    返回 `{"warn": ..., "pass": ...}`。两簇重叠（同人最低 ≤ 异人最高）时抛
    ValueError —— 那种数据上**没有阈值可推**（任何阈值都会有错判），
    硬给一个数字比报错危险得多。
    """
    gap = float(same_min) - float(cross_max)
    if gap <= 0:
        raise ValueError(
            f"两簇重叠，无阈值可推：同人最低 {same_min} ≤ 异人最高 {cross_max}")
    return {
        "warn": round(_floor_to(cross_max + (1.0 - share) * gap), 4),
        "pass": round(_floor_to(cross_max + share * gap), 4),
    }


@dataclass(frozen=True)
class DualThresholds:
    """双门槛三件套：主判两档 + 保底下限。

    `cam_pass` 主判通过线（独立声纹 ≥ 它才算「是同一人」）；
    `cam_warn` 复核线（低于它判 `reject`，之间判 `review`）；
    `qwen_floor` 自空间**下限**（主判过了但它低于此值 → 两尺矛盾，判 `review`）。

    ⚠️ **自空间没有「通过线」，只有下限** —— 它的天花板压在 0.98 以上（见模块文档
    字符串），拿它当准入线等于没有线。「≥0.98 就更好」这种读法也是错的：
    同人分挤在 0.978–0.991 之间，分数高低与听感好坏无关。
    """

    cam_pass: float
    cam_warn: float
    qwen_floor: float

    def to_dict(self) -> dict:
        return asdict(self)


DUAL = DualThresholds(
    cam_pass=derive(MEASURED["cam_cos"]["same_min"], MEASURED["cam_cos"]["cross_max"])["pass"],
    cam_warn=derive(MEASURED["cam_cos"]["same_min"], MEASURED["cam_cos"]["cross_max"])["warn"],
    qwen_floor=derive(MEASURED["qwen_cos"]["same_min"], MEASURED["qwen_cos"]["cross_max"])["pass"],
)


def check_thresholds(t: DualThresholds = DUAL) -> list[str]:
    """不变式自检：返回「不成立」的项（空列表 = 通过）。

    守的是「阈值必须落在两簇之间、且与 MEASURED 推导一致」—— 手改常量、改公式
    忘了改另一处、或把两簇数字抄反，都会在这里现形。测试直接断言它是空的。
    """
    problems: list[str] = []
    cam = MEASURED["cam_cos"]
    qwen = MEASURED["qwen_cos"]

    if not cam["cross_max"] < t.cam_warn < t.cam_pass < cam["same_min"]:
        problems.append(
            f"CAM++ 阈值没落在两簇之间：cross_max={cam['cross_max']} "
            f"warn={t.cam_warn} pass={t.cam_pass} same_min={cam['same_min']}")
    if not qwen["cross_max"] < t.qwen_floor < qwen["same_min"]:
        problems.append(
            f"自空间下限没落在两簇之间：cross_max={qwen['cross_max']} "
            f"floor={t.qwen_floor} same_min={qwen['same_min']}")

    exp_cam = derive(cam["same_min"], cam["cross_max"])
    if (t.cam_pass, t.cam_warn) != (exp_cam["pass"], exp_cam["warn"]):
        problems.append(
            f"CAM++ 阈值与实测推导不一致：冻结 {t.cam_pass}/{t.cam_warn} "
            f"vs 推导 {exp_cam['pass']}/{exp_cam['warn']}")
    exp_qwen = derive(qwen["same_min"], qwen["cross_max"])
    if t.qwen_floor != exp_qwen["pass"]:
        problems.append(
            f"自空间下限与实测推导不一致：冻结 {t.qwen_floor} vs 推导 {exp_qwen['pass']}")
    return problems


def verdict(cam_sim: float | None = None, qwen_sim: float | None = None,
            thresholds: DualThresholds = DUAL) -> dict:
    """双门槛判定。`cam_sim` = 独立声纹余弦，`qwen_sim` = 自空间余弦。

    返回 `{"verdict", "decided_by", "reasons", "thresholds"}`：

      pass     主判过、保底也过（保底未测也放行 —— 未测≠不合格）
      review   主判落在待复核区；或**两把尺子矛盾**（主判过、保底不过）
      reject   主判明确低于复核线
      unknown  主判未测（没基线 / 权重没缓存 / 算失败）—— **不等于不合格**

    ★ 矛盾为什么判 `review` 而不是 `reject`：自空间是**弱尺子**（天花板被压扁），
    拿它去否决独立声纹的结论，会把「独立模型说是本人」的样本判死；但它俩不一致
    时确实该有人听一下 —— 所以给「待复核」，而不是替用户下结论。
    """
    t = thresholds
    base = {"thresholds": t.to_dict()}
    if cam_sim is None:
        return {**base, "verdict": UNKNOWN, "decided_by": None,
                "reasons": ["独立声纹未测：判定不了（未测≠不合格）"]}

    cam = float(cam_sim)
    if cam < t.cam_warn:
        return {**base, "verdict": REJECT, "decided_by": "cam_cos",
                "reasons": [f"独立声纹 {cam:.4f} < {t.cam_warn}：与基线不是同一说话人"]}
    if cam < t.cam_pass:
        return {**base, "verdict": REVIEW, "decided_by": "cam_cos",
                "reasons": [f"独立声纹 {cam:.4f} 落在待复核区 "
                            f"[{t.cam_warn}, {t.cam_pass})：建议再录一段或人工听"]}

    if qwen_sim is None:
        return {**base, "verdict": PASS, "decided_by": "cam_cos",
                "reasons": [f"独立声纹 {cam:.4f} ≥ {t.cam_pass}（自空间保底未测，按主判放行）"]}
    qwen = float(qwen_sim)
    if qwen < t.qwen_floor:
        return {**base, "verdict": REVIEW, "decided_by": "qwen_cos",
                "reasons": [f"两把尺子矛盾：独立声纹 {cam:.4f} 过线，"
                            f"但自空间 {qwen:.4f} < {t.qwen_floor}"]}
    return {**base, "verdict": PASS, "decided_by": "cam_cos+qwen_cos",
            "reasons": [f"独立声纹 {cam:.4f} ≥ {t.cam_pass}，"
                        f"自空间 {qwen:.4f} ≥ {t.qwen_floor}"]}


def load_pairs(path: Path | str) -> list[dict]:
    """读 `dual_embed/pairs.csv`（列：role,speaker,dur,cond,sample_id,ref_id,qwen_cos,cam_cos）。

    分数统一转 float —— 调用方（`audit`）不该再关心表里的字符串。
    """
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                r["cam_cos"] = float(r["cam_cos"])
                r["qwen_cos"] = float(r["qwen_cos"])
            except (KeyError, TypeError, ValueError):
                continue  # 坏行跳过：一行脏数据不该毁掉整次复核
            rows.append(r)
    return rows


def audit(pairs: list[dict], thresholds: DualThresholds = DUAL) -> dict:
    """拿逐条实测分数**重放**门禁，返回通过率/误收率/待复核数。

    `pairs` 里 `role == "same"` 视作同人、其余视作异人（与 pairs.csv 口径一致）。
    期望（本项目标定数据上的实测值，测试直接钉死）：
      同人 pass 率 = 1.0（48/48，不误杀）；异人 pass 率 = 0.0（0/96，不误收）。

    ★ 这是「防退化」的主判据：改阈值/改判词逻辑会让这两个数字之一失守，
    比任何断言都直接 —— 阈值若松到能放异人进门，或紧到拦自己人，这里立刻红。
    """
    same = [p for p in pairs if p.get("role") == "same"]
    cross = [p for p in pairs if p.get("role") != "same"]

    def _tally(rows: list[dict]) -> dict:
        out = {PASS: 0, REVIEW: 0, REJECT: 0, UNKNOWN: 0}
        for r in rows:
            v = verdict(r.get("cam_cos"), r.get("qwen_cos"), thresholds)["verdict"]
            out[v] = out.get(v, 0) + 1
        return out

    same_tally, cross_tally = _tally(same), _tally(cross)

    def _rate(tally: dict, kind: str, n: int) -> float:
        return round(tally[kind] / n, 4) if n else 0.0

    return {
        "n_same": len(same), "n_cross": len(cross),
        "same": same_tally, "cross": cross_tally,
        "same_pass_rate": _rate(same_tally, PASS, len(same)),
        "cross_pass_rate": _rate(cross_tally, PASS, len(cross)),
        "thresholds": thresholds.to_dict(),
    }

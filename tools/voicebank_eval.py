# -*- coding: utf-8 -*-
"""嗓音保存复现质量评测引擎（大创研究层，2026-10-02）。

为什么有这个模块
    大创题目「面向职业用嗓人群的本地声音保存与少样本复现质量研究」的三个研究问题：

        RQ1  参考音时长（3s/10s/30s/60s）增加时复现质量如何变化？**收益在哪一档饱和？**
        RQ2  不同文本条件（同文/跨文/跨情感/长句）下，同一参考音的质量差异有多大？
        RQ3  客观指标与人工主观评分（MOS/ABX）的相关性如何？**能否替代人工听测？**

    本模块是回答这三个问题的**唯一执行入口**：矩阵规划 → 客观测量 → 统计与相关性。

    ★ 为什么对照对象是「时长」而不是「模型」：传统声音银行要录 50–200 句
      （ALS 官方指南 youralsguide.com/voice-banking 原文；同一页还给出口径
      「1 分钟音频 → 可用的 instant clone，10+ 分钟 → 更接近本人」），
      而本项目走的是秒级小样本克隆。
      「把录音负担压到几秒，代价是什么」是**有明确对照、且尚无人系统回答**的问题 ——
      这正是申报书铁律要求的「创新点必须有对照对象 + 数据支撑」。

设计约束（沿用项目惯例，都不是随手选的）
    1. **零新依赖**：只用 numpy + 已有 `tools/voice_metrics.py`。Spearman 自己实现，
       不引入 scipy —— 避免又一条「本机装了、CI 没装」的依赖债
       （见 `docs/犯错指南.md` 速查表第 61 条：本机全绿、CI 在 `--cov` 上红）。
    2. **绝不裸崩**：任何单项算不出来 → None，绝不抛。与 `voice_metrics` / `voice_qc`
       同一约定（`rvc_live` 训练完成回调依赖「QC 永不裸崩」）。
    3. **不需要 GPU**：`plan` / `measure` / `analyze` 三步都能在没有模型、没有 worker
       的环境跑完（`emb_sim` / `asr_overlap` 走可选开关，缺 worker 时降级为 None
       而不是报错）—— 这样**评测矩阵本身能在 CI 里被验证**，不必等 GPU。
    4. **指标方向集中在一处**（`HIGHER_IS_BETTER`），与 `docs/音色体检三灯.md`
       §3.2/§3.3 同口径：tilt 的「好」一侧是**数值更大**（更接近 0）；h1_h2 **越大越挤压**
       （本机合成信号实测标定，与文献通用表述相反）。方向写反 = 结论整个反掉，
       所以有专门的单测钉住（`test_voicebank_eval.py::TestMetricDirection`）。
    5. **不编造数字**：样本不足（n < 3）时相关系数返回 None，不硬凑一个数出来。

目录约定（`<root>/<exp>/` 下）
    matrix.json                            实验设计矩阵（plan 产出）
    corpus.json                            录音脚本 + 四类复现文本（plan 产出）
    refs/<说话人>/<时长档>.wav              分档参考音（人工录制/切分后放入）
    truth/<说话人>/<条件>.wav               复现文本的真实录音（ABX 对照，人工录制）
    samples/<说话人>/<时长档>/<条件>.wav     待测合成样本
    results.json / results.csv             客观测量结果（measure 产出）
    listen/key.csv                         盲号 → sample_id（揭盲用，**不外发**）
    listen/sheet.csv                       盲号 + 空 mos/abx 列（**发给听者**）
    listen/playlist.txt                    随机化播放顺序（种子落盘）
    subjective.csv                         多人评分合并结果（merge-listen 产出）
    report.json / report.md                统计与相关性（analyze 产出）

用法
    python tools/voicebank_eval.py plan    --exp vb01 --speakers spk01,spk02
    python tools/voicebank_eval.py split   --exp vb01 --speakers spk01 --source rec.wav
    python tools/voicebank_eval.py measure --exp vb01 [--with-emb] [--with-asr]
    python tools/voicebank_eval.py listen  --exp vb01              # 生成盲听材料
    python tools/voicebank_eval.py merge-listen --exp vb01 --sheets a.csv,b.csv
    python tools/voicebank_eval.py analyze --exp vb01 [--subjective subjective.csv]
    python tools/voicebank_eval.py corpus                # 打印录音脚本与四类复现文本
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import voice_metrics  # noqa: E402  嗓音客观指标（HNR/H1-H2/tilt/jitter/shimmer）

WORKER = "http://127.0.0.1:8001"

# ---------------- 实验设计常量 ----------------

# 参考音时长档（秒）。★ 为什么到 60s 就停：
#   ALS 官方指南给的口径是「1 分钟音频 → 可用的 instant clone，10+ 分钟 → 更接近本人」，
#   而本项目走秒级小样本。3/10/30/60 覆盖了「秒级 → 与传统 instant clone 同档」这段，
#   是研究问题真正落在的区间。要跑 300s（传统声音银行档）需自备更长的录音脚本
#   —— 参考脚本的字数决定了档位上限，`plan` 会给出提示（见 `_script_capacity_s`）。
DURATION_LADDER = (3.0, 10.0, 30.0, 60.0)

# 复现条件（RQ2 的自变量）。四类的语义不同，别合并：
#   same    同文 —— 复现文本就是参考音里念过的句子（考察「背下来的那句话」）
#   cross   跨文 —— 参考音里没有的日常句（考察泛化，**这才是真实使用场景**）
#   emotion 跨情感 —— 带情感色彩的句子（考察韵律迁移）
#   long    长句 —— >30 字（考察长程稳定性，TTS 长句最容易崩）
CONDITIONS = ("same", "cross", "emotion", "long")

# 参与统计的指标键。前 5 个来自 `voice_metrics.extract`，
# emb_sim / asr_overlap 来自 worker（可选，缺 worker 时是 None）。
METRIC_KEYS = (
    "emb_sim",          # 声纹余弦（相似度）
    "hnr",              # 谐噪比 dB（自然度）
    "h1_h2",            # H1-H2 dB（发声挤压）
    "spectral_tilt",    # 谱倾斜 dB/oct（发声挤压的印证）
    "jitter_local",     # 局部基频微扰 %
    "shimmer_local",    # 局部振幅微扰 %
    "asr_overlap",      # ASR 回译字符重合度（可懂度）
)

# ★ 指标方向：True = 数值越大越好。
#   与 `docs/音色体检三灯.md` §3.2/§3.3 同口径，**不要凭直觉改**：
#     - spectral_tilt 的「好」一侧是**数值更大**（更接近 0，谱滚降更缓）
#       —— 与其余指标方向不同向，三灯文档里记着这个坑（写反会让灯整个反掉）
#     - h1_h2 **越大 = 滚降越陡 = 越挤压**（本机合成信号实测标定；
#       调研文档 §4.2 转述文献的表述与此相反，以实测为准）
HIGHER_IS_BETTER = {
    "emb_sim": True,          # 声纹余弦越高越像
    "hnr": True,              # 谐噪比越高越自然
    "h1_h2": False,           # 越大越挤压
    "spectral_tilt": True,    # 越接近 0 越健康
    "jitter_local": False,    # 越低越稳
    "shimmer_local": False,   # 越低越稳
    "asr_overlap": True,      # 重合度越高越可懂
}

# 收益饱和判据：相邻档增量 < 总增益的 10% 视为「再录更久已经不值」。
# 10% 是工程阈值不是统计阈值 —— 结论里要连同这个数字一起报，别当成客观真理。
SATURATION_GAIN_RATIO = 0.10

# 相关系数的最小样本数。低于它返回 None（不编造数字）。
MIN_CORR_N = 3

# `silencedetect` 的默认噪声门限（dB）。**别只看这一个值** ——
# 它是绝对门限，而录音的底噪水平差得很远（自录在静音房 vs 从视频里分离的人声轨，
# 后者底噪可能高 15dB）。实测：同一段采访人声轨在 -35dB 下探测到 **0** 段静音，
# 在 -20dB 下才探到 34 段 —— 用固定门限会在后一种源上静默退化成"全硬切"。
DEFAULT_NOISE_DB = -35.0

#: 自适应时，门限取「整段 RMS - 这个余量(dB)」。
#: 15dB 的含义：比背景高 15dB 就算"在说话"，是语音活动检测的常见经验间隔。
ADAPT_MARGIN_DB = 15.0

#: 自适应没探到足够静音时，门限逐级**放宽**（+2dB 一档）的序列。
#:
#: ★ 为什么要这个：单一自适应值会落在检出边缘上，脆得很 —— 实测同一个人声源
#:   在 `RMS-15dB = -34.8dB` 下探到 **0 段**静音，放宽到 `-20dB` 才探到 34 段。
#:   而"探不到静音"的后果不是报错，是**切点全部退化成硬切**（只在 warnings 里留一句），
#:   于是协议 §4.3 的"对齐句末"就悄悄失效了 —— 这正是最该防的那种失败。
#:   逐级放宽 = 宁可门限宽松一点（多切几刀在噪声上），也不要静默退化成硬切。
ADAPT_FALLBACK_DBS = (-30.0, -27.0, -24.0, -21.0, -18.0)

#: 放宽到序列末尾还没探到任何静音 → 判定这个源不适合"对齐句末"（如实报，不硬凑）。
MIN_SILENCES_FOR_ALIGN = 1

# ---------------- 盲听（listen 子命令） ----------------

#: 盲号前缀。听者看到的只有 `S-0007` 这种编号 —— **不含说话人/档位/条件任何线索**。
#:
#: ★ 为什么必须不带线索：RQ3 要问的是「客观指标能不能替代人耳」，而人耳一旦知道
#:   "这条是 60s 档"，评分就会被先验预期污染（长参考音应该更好 —— 这个预期本身
#:   就是要检验的对象）。带线索的评分表等于把结论写进实验条件里。
LISTEN_ID_PREFIX = "S-"

#: 盲听评分表的列。★ 必须与 `_read_subjective` 认的列名**逐字一致**
#: （`sample_id` / `mos` / `abx`），否则 analyze --subjective 会静默丢掉全部行
#: —— 那正是"跑通了但出不来数"的典型症状。
LISTEN_CSV_COLUMNS = ("blind_id", "mos", "abx", "note")

#: 揭盲表文件名。**与评分表分开存**是硬要求：评分表是要发给听者的，
#: 揭盲表是研究者自己留的。放同一个文件里 = 把答案随试卷一起发出去。
LISTEN_KEY_FILE = "listen/key.csv"
LISTEN_SHEET_FILE = "listen/sheet.csv"
LISTEN_DIR = "listen"

#: mos 的合法区间。超出就是填错了（5 档量表），**不夹紧、不忽略** —— 报错让听者重填。
#: 悄悄夹到 5 会让一条填错的数据混进相关性分析里。
MOS_MIN, MOS_MAX = 1.0, 5.0

#: ABX 的合法取值：0 = 选错（听起来不像），1 = 选对（听起来像）。
ABX_VALUES = (0.0, 1.0)

# ---------------- 语料（★ 唯一真相源在 m2_server/voicepreserve.py） ----------------
#
# 这段语料**同时**被产品（引导用户照着念）与研究（评测矩阵）使用，
# 所以它只能有一份定义 —— 两处各写一份会漂移，而漂移的后果是**实验失效**
# （研究测的是 A 脚本，产品却让用户念 B 脚本，结论对不上任何真实使用）。
#
# 因此这里 **import** 而不是复制。本模块是离线工具、`voicepreserve` 是运行时存储层，
# 依赖方向看着有点反常 —— 但语料的归属是「用户要念什么」这个**产品问题**，
# 产品侧才是它的家。`test_voicebank_eval.py` 有一条对账断言钉住这条依赖。
import voicepreserve as _vp  # noqa: E402

REFERENCE_SCRIPT: tuple[str, ...] = _vp.RECORD_SCRIPT
REPRO_TEXTS: dict[str, tuple[dict[str, str], ...]] = _vp.REPRO_TEXTS
SCRIPT_CHARS_PER_SEC: float = _vp.SCRIPT_CHARS_PER_SEC


# ---------------- 小工具 ----------------

def _dur_tag(sec: float) -> str:
    """时长档标签：< 120s 用秒（`60s`），≥ 120s 且整分用分（`5min`）。

    ★ 不要用「一律转成分钟」的写法：60s 会变成 `1min`，而 3s 会变成 `0.05min`，
    目录名既难读又难排序。
    """
    s = int(round(float(sec)))
    if s < 120 or s % 60:
        return f"{s}s"
    return f"{s // 60}min"


def _parse_dur_tag(tag: str) -> float | None:
    """`60s` / `5min` → 秒数。认不出来给 None（调用方记 error，不猜）。"""
    t = str(tag).strip().lower()
    try:
        if t.endswith("min"):
            return float(t[:-3]) * 60.0
        if t.endswith("s"):
            return float(t[:-1])
    except ValueError:
        return None
    return None


def _char_count(text: str) -> int:
    """去标点空白后的字数。**实现与唯一真相源都在 `m2_server/voicepreserve.py`**
    （口径与 `tools/voice_qc.py::_char_count` 一致）。这里只是转发，
    不保留第二份实现 —— 两处各写一份会漂移，而字数漂了会直接改变时长档的容量判断。"""
    return _vp.char_count(text)


def _script_capacity_s(script: list[str] | None = None) -> float:
    """参考脚本念完大约多少秒 —— 用来判断某个时长档能不能被脚本覆盖。"""
    if script is None:
        return _vp.script_capacity_s()
    return sum(_char_count(s) for s in script) / SCRIPT_CHARS_PER_SEC


def _median(vals: list[float]) -> float | None:
    s = sorted(float(v) for v in vals)
    n = len(s)
    if not n:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _default_root() -> Path:
    """默认落盘根 = `<OUTPUTS_DIR>/voicebank`。

    ⚠️ **延迟 import config**，不在模块顶层 —— `config` 会读环境变量定路径，
    而测试环境靠 `m2_server/conftest.py` 顶部的 `VM_OUTPUTS_DIR` 做隔离
    （导入时早绑定的坑，见 `docs/犯错档案-工程.md` §8.36）。延迟 import 让
    `plan/measure/analyze` 都能接受显式 root，测试就不必碰真实 outputs/。
    """
    import config as cfg

    return Path(cfg.OUTPUTS_DIR) / "voicebank"


# ---------------- 统计（零依赖） ----------------

def _ranks(vals: list[float]) -> list[float]:
    """平均秩（并列取平均），供 Spearman 用。

    ★ 并列必须取平均秩：直接给并列项编不同的序号会让相关系数虚高，
    而主观听测（MOS 五档）**天然有大量并列** —— 这正是本模块的主用场景。
    """
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关。任一侧无方差（全相等）→ None。"""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sx = sum(d * d for d in dx) ** 0.5
    sy = sum(d * d for d in dy) ** 0.5
    if sx <= 0 or sy <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (sx * sy)


def spearman(xs: list[float], ys: list[float], *, min_n: int = MIN_CORR_N) -> float | None:
    """Spearman 秩相关。成对剔除 None；有效对 < min_n 或任一侧无方差 → None。

    为什么用 Spearman 而不是 Pearson：主观听测是**序数**（MOS 五档、ABX 二选一），
    且与客观指标的关系未必线性。秩相关不假设线性，也不被极端值带跑。
    """
    pairs = [(x, y) for x, y in zip(xs, ys)
             if x is not None and y is not None]
    if len(pairs) < min_n:
        return None
    return _pearson(_ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs]))


def saturation_point(durations: list[float], values: list[float], *,
                     higher_is_better: bool = True,
                     gain_ratio: float = SATURATION_GAIN_RATIO) -> dict | None:
    """收益饱和档位 —— 回答 RQ1 的「收益在哪一档饱和」。

    durations 升序、values 同序。内部按 `higher_is_better` 归一，调用方不用管方向
    （方向搞反是这个模块最容易犯的错，所以收进函数内部一次搞定）。

    返回 **None = 数据不足以判定**（档位不足 2 档 / 总增益 ≤ 0）。
    否则返回 dict：

        status          "saturated" | "still_climbing" | "non_monotonic"
        duration_s      饱和档秒数（**仅 status == "saturated" 时有意义**）
        duration_tag    同上，标签形式
        gain_fraction   该档增量占总增益的比例（仅 saturated）
        note            非 saturated 时的一句话解释

    ★ 为什么必须区分 `non_monotonic`（2026-10-02 端到端演示实测踩到）：
      若判据只是「首个相邻增量 < 阈值就宣布饱和」，**指标非单调时会给出荒谬结论** ——
      实测某指标序列是 [-21.9, -22.4, -20.6, -20.5]，第一步是**负增量**（回退），
      却被报成「3s 就饱和了」，而它其实根本没饱和。
      正确做法：**出现负增量就不下饱和结论**，如实说「趋势非单调，饱和点不可判定」。
      （同一条纪律：报告宁可说"没测出来"，也不要给一个看起来很像结论的错数。）
    """
    if len(durations) < 2 or len(values) != len(durations):
        return None
    v = [float(x) for x in values]
    if not higher_is_better:
        v = [-x for x in v]
    total = v[-1] - v[0]
    if total <= 0:
        return None
    steps = [v[i + 1] - v[i] for i in range(len(v) - 1)]
    if any(s < 0 for s in steps):
        return {"status": "non_monotonic", "duration_s": None, "duration_tag": None,
                "gain_fraction": None,
                "note": "指标随档位非单调（出现回退），饱和点不可判定"}
    for i, step in enumerate(steps):
        if step < gain_ratio * total:
            return {"status": "saturated", "duration_s": float(durations[i]),
                    "duration_tag": _dur_tag(durations[i]),
                    "gain_fraction": round(step / total, 4), "note": None}
    return {"status": "still_climbing", "duration_s": None, "duration_tag": None,
            "gain_fraction": None, "note": "逐档都在涨，尚未饱和"}


# ---------------- plan：实验设计矩阵 ----------------

def build_matrix(speakers: list[str], durations: list[float],
                 conditions: list[str]) -> list[dict]:
    """生成实验设计矩阵。每个说话人 × 时长档 × 条件 = 一个待测样本。

    ★ 条件文本取该条件的**第一条**（不轮换）—— 同一说话人在不同时长档之间
    必须念同一句话，否则「时长」这个自变量被文本混淆了。
    """
    rows: list[dict] = []
    for sp in speakers:
        for dur in durations:
            for cond in conditions:
                texts = REPRO_TEXTS.get(cond) or []
                t = texts[0] if texts else {"text": "", "style": ""}
                tag = _dur_tag(dur)
                rows.append({
                    "sample_id": f"{sp}/{tag}/{cond}",
                    "speaker": sp,
                    "duration_s": float(dur),
                    "duration_tag": tag,
                    "condition": cond,
                    "text": t.get("text", ""),
                    "style": t.get("style", ""),
                    "ref": f"refs/{sp}/{tag}.wav",
                    "truth": f"truth/{sp}/{cond}.wav",
                    "sample": f"samples/{sp}/{tag}/{cond}.wav",
                })
    return rows


def plan(exp: str, speakers: list[str], *,
         durations: list[float] | None = None,
         conditions: list[str] | None = None,
         root: Path | None = None) -> dict:
    """落盘实验设计矩阵 + 语料 + 目录骨架。不生成任何音频（那要 GPU）。"""
    base = (Path(root) if root else _default_root()) / exp
    durs = [float(d) for d in (durations or DURATION_LADDER)]
    conds = list(conditions or CONDITIONS)
    sps = [str(s) for s in speakers if str(s).strip()]
    res: dict = {"exp": exp, "root": str(base), "error": None, "error_stage": None,
                 "warnings": []}
    if not sps:
        res["error"] = "没有说话人（--speakers 为空）"
        res["error_stage"] = "参数"
        return res
    if not durs:
        res["error"] = "没有时长档"
        res["error_stage"] = "参数"
        return res

    matrix = build_matrix(sps, durs, conds)
    cap = _script_capacity_s()
    over = [d for d in durs if d > cap]
    if over:
        res["warnings"].append(
            f"时长档 {[_dur_tag(d) for d in over]} 超出参考脚本容量（约 {cap:.0f}s）"
            f"—— 需要扩充 REFERENCE_SCRIPT 或另备长录音，否则这些档位录不满")

    for d in ("refs", "truth", "samples"):
        (base / d).mkdir(parents=True, exist_ok=True)
    for sp in sps:
        (base / "refs" / sp).mkdir(parents=True, exist_ok=True)
        (base / "truth" / sp).mkdir(parents=True, exist_ok=True)
        for d in durs:
            (base / "samples" / sp / _dur_tag(d)).mkdir(parents=True, exist_ok=True)

    payload = {
        "exp": exp,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "speakers": sps,
        "durations": durs,
        "conditions": conds,
        "script_capacity_s": round(cap, 1),
        "n_samples": len(matrix),
        "matrix": matrix,
    }
    _write_json(base / "matrix.json", payload)
    _write_json(base / "corpus.json", {
        "reference_script": REFERENCE_SCRIPT,
        "reference_chars": sum(_char_count(s) for s in REFERENCE_SCRIPT),
        "repro_texts": REPRO_TEXTS,
    })
    res.update(payload)
    return res


# ---------------- split：一条录音 → 前缀四档 ----------------

def _rms_db(path: Path) -> float | None:
    """整段音频的 RMS（dBFS）。读不出来给 None（不猜）。"""
    try:
        import numpy as np
        import soundfile as sf

        a, _sr = sf.read(str(path), dtype="float32")
        if a.ndim > 1:
            a = a.mean(axis=1)
        if a.size == 0:
            return None
        rms = float(np.sqrt((a.astype("float64") ** 2).mean()))
        return float(20.0 * np.log10(max(rms, 1e-9)))
    except Exception:
        return None


def detect_silences(path: Path, *, noise_db: float | None = None,
                    min_silence_s: float = 0.15,
                    allow_relax: bool = True) -> tuple[list[tuple[float, float]], float, list]:
    """用 ffmpeg `silencedetect` 找静音段。

    返回 `([(起, 止)], 实用门限dB, 尝试记录)`。

    `noise_db=None` 时走**自适应**：先用「整段 RMS - `ADAPT_MARGIN_DB`」，
    探不到静音就按 `ADAPT_FALLBACK_DBS` 逐级放宽 —— 详见那两个常量的注释
    （单一自适应值会落在检出边缘，探不到就静默退化成硬切）。

    显式给了 `noise_db` 就**只用它**（不再放宽）：调用方明确要那个门限时，
    替他偷偷改掉会让结果不可复现。

    找不到 ffmpeg / 探测失败 → 返回 `([], 门限, [])`（调用方退化为硬切，不是崩）。

    为什么用 ffmpeg 而不是自己算能量包络：ffmpeg 已是项目硬依赖
    （`common.find_ffmpeg`），再写一套就是**第二份会漂移的实现**。
    """
    from common import find_ffmpeg

    def _run(db: float) -> list[tuple[float, float]]:
        r = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
             "-af", f"silencedetect=noise={db:.1f}dB:d={min_silence_s}",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
        found: list[tuple[float, float]] = []
        start: float | None = None
        for line in (r.stderr or "").splitlines():
            if "silence_start:" in line:
                try:
                    start = float(line.rsplit("silence_start:", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    start = None
            elif "silence_end:" in line:
                try:
                    end = float(line.rsplit("silence_end:", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    continue
                found.append((start if start is not None else 0.0, end))
                start = None
        return found

    trials: list[dict] = []
    if noise_db is not None:
        db = float(noise_db)
        found = _run(db)
        trials.append({"db": round(db, 1), "n": len(found), "adaptive": False})
        return found, db, trials

    rms = _rms_db(path)
    first = DEFAULT_NOISE_DB
    if rms is not None:
        # 两侧都要夹，而且**别夹错方向**（这里写反过一次，实测暴露）：
        #   上界 -18dB：抬太高会让轻声说话也被判成静音；
        #   下界 -50dB：真静音房的门限不该低到 -55，否则呼吸声都算"在说话"。
        first = min(max(rms - ADAPT_MARGIN_DB, -50.0), -18.0)
    candidates = [first, *(d for d in ADAPT_FALLBACK_DBS if d > first)]

    best: list[tuple[float, float]] = []
    best_db = first
    for i, db in enumerate(candidates):
        found = _run(db)
        trials.append({"db": round(db, 1), "n": len(found), "adaptive": True})
        if found:
            best, best_db = found, db
            break
        if i == len(candidates) - 1:
            best_db = db
    if not allow_relax and trials:
        # 只要能跑一次的场景（测试）—— 用首个候选的结果，别做多轮
        best_db = trials[0]["db"]
    return best, best_db, trials


#: 对齐句末时，切点相对标称档的**最大允许偏差**（占比）。
#:
#: ★ 为什么必须有这条守卫（实测暴露的真问题）：
#:   协议 §4.3 要求"对齐句末"。但把它**无条件**应用会走向反面 ——
#:   实测一段连续采访人声，静音点只有 35.40s 和 64.62s 两处，
#:   于是 60s 档"对齐"到了 **35.40s**，比 30s 档只多 5 秒，标称与实际差 **-24.6s**。
#:   这不是"更严谨"，这是**把自变量破坏了**：`analyze` 会用 `duration_s`（标称值）
#:   分组，于是"60s 档"这一列装的是 35 秒的音频，结论整个错位。
#:
#:   两种错的取舍：切在半句上 → 可听出来、写进 `aligned=False` 就能追溯；
#:   标称与实际差一半 → 看不出来，且会安静污染所有按档位的统计。
#:   所以**宁可硬切，也不要大幅偏离标称**。25% 是工程阈值，结论里要一起报。
ALIGN_MAX_DEVIATION = 0.25


def cut_points(targets: list[float], silences: list[tuple[float, float]],
               total_s: float,
               max_deviation: float = ALIGN_MAX_DEVIATION) -> dict[float, dict]:
    """为每个目标时长挑切点。返回 `{目标秒: {"cut": 秒, "aligned": bool, "reason": str}}`。

    ★ 协议 §4.3 要求**对齐到句末**（别在句子中间硬切）—— 半句话会同时
      劣化自然度与可懂度，把「时长」这个自变量污染掉。
      句末 ≈ 静音的**起点**（人说完最后一个字才开始静），所以候选集是
      `silence_start`，不是静音中点。

    ★ 但**对齐有上限**：对齐后的切点若偏离标称超过 `max_deviation`，
      就退化为硬切 target。理由见 `ALIGN_MAX_DEVIATION` 的注释 ——
      无上限的对齐会把 60s 档切成 35s，比硬切危险得多。

    三档结果分开记，因为报结论时要能区分"这个量到底是多少"：
      aligned=True     对齐到句末，且偏差在容差内
      aligned=False    硬切在你要求的秒数上（没有合适的句末，或对齐会偏太远）
    """
    starts = sorted(s for s, _e in silences if 0 < s <= total_s)
    out: dict[float, dict] = {}
    for t in sorted(targets):
        if t >= total_s:
            out[t] = {"cut": total_s, "aligned": False,
                      "reason": "录音比该档还短，取整段"}
            continue
        ok = [s for s in starts if s <= t]
        if not ok:
            out[t] = {"cut": float(t), "aligned": False,
                      "reason": "该档之前没有句末，硬切"}
            continue
        cand = max(ok)
        dev = abs(t - cand) / t
        if dev > max_deviation:
            out[t] = {"cut": float(t), "aligned": False,
                      "reason": (f"最近句末 {cand:.2f}s 偏离标称 {dev:.0%}"
                                 f"（上限 {max_deviation:.0%}），改为硬切")}
            continue
        out[t] = {"cut": float(cand), "aligned": True, "reason": ""}
    return out


def split(exp: str, speakers: list[str], source: Path, *,
          durations: list[float] | None = None,
          root: Path | None = None, noise_db: float | None = None,
          min_silence_s: float = 0.15) -> dict:
    """**一条录音 → 前缀切四档**，直接落成 `refs/<说话人>/<档>.wav`。

    这一步补的是协议 §4.3 自己承认的缺口（「切分目前是手工/脚本完成，
    `voicebank_eval.py` 不含 `split` 子命令」）。补它的理由是**它必须与研究
    设计同源**：切点规则（对齐句末 / 前缀截取 / 记录实际秒数）是协议的一部分，
    放在临时脚本里就会漂 —— 而漂了的切点会让「时长」这个自变量不再是它声称的量。

    ⚠️ `speakers` 只接受**一个**说话人：一条录音只能属于一个人，
      写多个人就是复制同一份音频当成"多个被试"，那是数据造假。
    """
    base = (Path(root) if root else _default_root()) / exp
    durs = [float(d) for d in (durations or DURATION_LADDER)]
    sps = [str(s) for s in speakers if str(s).strip()]
    res: dict = {"exp": exp, "root": str(base), "error": None, "error_stage": None,
                 "tiers": [], "warnings": []}

    if len(sps) != 1:
        res["error"] = f"split 只接受 1 个说话人（一条录音属于一个人），实际 {len(sps)} 个"
        res["error_stage"] = "参数"
        return res
    speaker = sps[0]
    src = Path(source)
    if not src.exists() or src.stat().st_size == 0:
        res["error"] = f"录音不存在或为空: {src}"
        res["error_stage"] = "读取录音"
        return res

    info = _wav_info(src)
    if info is None:
        res["error"] = f"读不出录音时长（可能需要先转成 wav）: {src}"
        res["error_stage"] = "探测时长"
        return res
    total = info["seconds"]

    need = max(durs)
    if total + 1e-6 < need:
        res["error"] = (f"录音 {total:.2f}s，不足最高档 {_dur_tag(need)}"
                        f"（差 {need - total:.2f}s）")
        res["error_stage"] = "时长不足"
        res["total_s"] = total
        res["needed_s"] = need
        return res

    silences, used_db, trials = detect_silences(src, noise_db=noise_db,
                                                min_silence_s=min_silence_s)
    res["noise_db"] = round(float(used_db), 1)
    res["adaptive_noise"] = noise_db is None
    res["noise_trials"] = trials
    if not silences:
        res["warnings"].append(
            f"没探测到静音段（放宽到 {used_db:.1f}dB 仍无）—— 切点退化为硬切，"
            f"可能切在半句上。这段录音要么本身是连续朗读没有停顿，"
            f"要么底噪太高；显式指定 --noise-db 可再试")
    cuts = cut_points(durs, silences, total)

    refs_dir = base / "refs" / speaker
    refs_dir.mkdir(parents=True, exist_ok=True)
    from common import find_ffmpeg

    for d in sorted(durs):
        tag = _dur_tag(d)
        cp = cuts[d]
        sec = float(cp["cut"])
        if sec <= 0:
            res["error"] = f"{tag} 档切点算出 0 秒（录音开头就是静音？）"
            res["error_stage"] = "切点"
            return res
        dest = refs_dir / f"{tag}.wav"
        r = subprocess.run(
            [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
             "-t", f"{sec:.3f}", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
        if r.returncode != 0 or not dest.exists():
            tail = (r.stderr or "").strip().splitlines()[-2:]
            res["error"] = "切片失败: " + " | ".join(tail)[:300]
            res["error_stage"] = "切片"
            return res
        after = _wav_info(dest)
        actual = (after or {}).get("seconds")
        res["tiers"].append({
            "tag": tag,
            "duration_s": float(d),                 # 标称档
            "cut_s": round(sec, 3),                 # 切点（= 实际时长）
            "actual_s": actual,                     # 读回来的实际时长
            # ★ 标称 vs 实际必须分开记（协议 §4.3）：报结论要用实际值
            "aligned": bool(cp["aligned"]),
            "cut_reason": cp["reason"],
            # 偏差直接给出 —— 报结论时要能一眼看出哪一档"名不副实"
            "deviation_s": (None if actual is None else round(actual - float(d), 3)),
            "bytes": dest.stat().st_size,
        })
        if cp["reason"]:
            res["warnings"].append(f"{tag} 档：{cp['reason']}")

    res["total_s"] = total
    res["silences"] = len(silences)
    res["speaker"] = speaker
    res["source"] = str(src)
    # 静音段本身也留档：切点是派生的，但探测结果是一次性的观测量，
    # 换了门限就变了 —— 报结论时要能追溯"当时看到的静音在哪"。
    res["silence_spans"] = [[round(s, 3), round(e, 3)] for s, e in silences]
    # 切完把「实际用了什么切点」落盘 —— 复现结论时要能查到自变量到底是多少
    _write_json(base / "split.json", dict(res))
    # ★ 实验级 `split.json` 是**单说话人**的：第二个说话人 split 时就把第一个覆盖掉了。
    # 2026-10-08 实测（pilotC，3 人依次切档）：跑完只剩最后一个人的切点与静音观测，
    # 另两人的切点丢了 —— 而协议 §4.3 要求「切点留档」。所以逐说话人另存一份；
    # 实验级那个文件保持原样，不破既有消费方。
    _write_json(base / "split" / f"{speaker}.json", dict(res))
    return res


def _wav_info(path: Path) -> dict | None:
    """读 wav 的时长/采样率/声道。读不出来给 None（不猜）。"""
    try:
        import soundfile as sf

        i = sf.info(str(path))
        return {"seconds": round(float(i.frames) / float(i.samplerate), 4),
                "samplerate": int(i.samplerate), "channels": int(i.channels)}
    except Exception:
        return None


# ---------------- measure：客观测量 ----------------

def _post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _emb(path: Path) -> list:
    data = _post_json(WORKER + "/emb", {"path": str(path)}, timeout=300)
    if data.get("error") or not data.get("emb"):
        raise RuntimeError(f"声纹提取失败: {data.get('error')}")
    return data["emb"]


def _transcribe(path: Path) -> str:
    """worker /transcribe。`vad_filter=False` 与 `voice_qc` 同口径：
    切片已按静音切好，whisper 内部 VAD 会吞短片段（分不清「没说话」和「被吃掉」）。"""
    data = _post_json(WORKER + "/transcribe",
                      {"path": str(path), "vad_filter": False}, timeout=300)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return str(data.get("text") or "")


def char_overlap(text_a: str, text_b: str) -> float:
    """字符重合度（集合交/并，去逗号）。

    ★ 口径必须与 `tools/voice_qc.py::_char_overlap` 一致，否则同一个样本
    在两份报告里给出不同的可懂度。改这里就同步改那边（两处都有这条注释）。
    """
    a = set(str(text_a).replace("，", "").replace(",", ""))
    b = set(str(text_b).replace("，", "").replace(",", ""))
    return len(a & b) / max(len(a | b), 1)


def _emb_sim(path: Path, ref: Path) -> float:
    import numpy as np

    v1, v2 = np.asarray(_emb(path)), np.asarray(_emb(ref))
    return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


def measure(exp: str, *, root: Path | None = None, with_emb: bool = False,
            with_asr: bool = False) -> dict:
    """扫描 `samples/`，对每个样本跑客观指标，落盘 results.json + results.csv。

    可选的两项（emb_sim / asr_overlap）需要 worker 8001；缺它时**降级为 None 并记 note**，
    绝不中断整轮测量 —— 一轮测量要跑几十分钟，不该因为一个可选指标全废。
    """
    base = (Path(root) if root else _default_root()) / exp
    res: dict = {"exp": exp, "root": str(base), "created_at":
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "with_emb": bool(with_emb), "with_asr": bool(with_asr),
                 "count": 0, "measured": 0, "errors": 0,
                 "samples": [], "error": None, "error_stage": None, "notes": []}

    samples_dir = base / "samples"
    if not samples_dir.exists():
        res["error"] = f"样本目录不存在: {samples_dir}（先跑 plan，再把样本放进去）"
        res["error_stage"] = "样本目录"
        return res

    wavs = sorted(samples_dir.glob("*/*/*.wav"))
    res["count"] = len(wavs)
    if not wavs:
        res["error"] = (f"{samples_dir} 下没有样本（期望 "
                        f"samples/<说话人>/<时长档>/<条件>.wav）")
        res["error_stage"] = "样本扫描"
        return res

    worker_up = True
    if with_emb or with_asr:
        try:
            import urllib.request

            with urllib.request.urlopen(WORKER + "/health", timeout=10):
                pass
        except Exception as e:
            worker_up = False
            res["notes"].append(
                f"worker 8001 未启动（{type(e).__name__}），emb_sim / asr_overlap 降级为 None")

    for i, w in enumerate(wavs, 1):
        tag = w.parent.name
        speaker = w.parent.parent.name
        cond = w.stem
        dur = _parse_dur_tag(tag)
        rec: dict = {"sample_id": f"{speaker}/{tag}/{cond}", "speaker": speaker,
                     "duration_tag": tag, "duration_s": dur, "condition": cond,
                     "path": str(w), "metrics": {}, "error": None, "note": None}

        if dur is None:
            rec["error"] = f"无法解析时长档: {tag}"
            res["errors"] += 1
            res["samples"].append(rec)
            continue

        m = voice_metrics.extract(w)
        if m.get("error"):
            rec["error"] = m["error"]
            res["errors"] += 1
        else:
            res["measured"] += 1
        rec["metrics"] = {k: m.get(k) for k in
                          ("hnr", "h1_h2", "spectral_tilt",
                           "jitter_local", "shimmer_local", "f0_median",
                           "voiced_ratio", "duration_s")}

        if with_emb and worker_up:
            ref = base / "refs" / speaker / f"{tag}.wav"
            if not ref.exists():
                rec["note"] = f"缺参考音 {ref.name}，emb_sim 跳过"
            else:
                try:
                    rec["metrics"]["emb_sim"] = round(_emb_sim(w, ref), 4)
                except Exception as e:
                    rec["note"] = f"声纹失败: {type(e).__name__}: {e}"

        if with_asr and worker_up:
            truth = base / "truth" / speaker / f"{cond}.wav"
            if not truth.exists():
                rec["note"] = (rec["note"] or "") + f" 缺真实录音 {cond}.wav，asr_overlap 跳过"
            else:
                try:
                    ov = char_overlap(_transcribe(truth), _transcribe(w))
                    rec["metrics"]["asr_overlap"] = round(ov, 4)
                except Exception as e:
                    rec["note"] = (rec["note"] or "") + f" 转写失败: {type(e).__name__}"

        res["samples"].append(rec)
        if i % 20 == 0 or i == len(wavs):
            print(f"[measure] {i}/{len(wavs)}（失败 {res['errors']}）", flush=True)

    _write_json(base / "results.json", res)
    _write_results_csv(base / "results.csv", res["samples"])
    return res


def _write_results_csv(path: Path, samples: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["sample_id", "speaker", "duration_tag", "duration_s", "condition",
            *METRIC_KEYS, "error", "note"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in samples:
            m = s.get("metrics") or {}
            w.writerow([s.get("sample_id"), s.get("speaker"), s.get("duration_tag"),
                        s.get("duration_s"), s.get("condition"),
                        *[m.get(k) for k in METRIC_KEYS],
                        s.get("error") or "", s.get("note") or ""])
    return path


# ---------------- analyze：统计与相关性 ----------------

def _group_medians(samples: list[dict], key: str) -> dict:
    """按某维度分组，每组对每个指标取中位数。

    ★ 取**中位数**不是均值：合成失败/爆音的样本是长尾常客，均值会被它们拉走，
    而我们要的是「这个档位的典型水平」（与 `voice_qc._aggregate_vocal` 同一理由）。
    """
    groups: dict = {}
    for s in samples:
        k = s.get(key)
        if k is None:
            continue
        groups.setdefault(k, []).append(s)
    out: dict = {}
    for k, rows in groups.items():
        out[k] = {}
        for metric in METRIC_KEYS:
            vals = [(r.get("metrics") or {}).get(metric) for r in rows]
            vals = [v for v in vals if v is not None]
            out[k][metric] = round(_median(vals), 4) if vals else None
    return out


def _read_subjective(path: Path) -> list[dict]:
    """读主观听测 CSV：必须含 `sample_id` 列，`mos` / `abx` 至少一列。

    认不出来的行**跳过并计数**，不猜、不补零 —— 补零会把相关系数算成一个假数。
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("sample_id") or "").strip()
            if not sid:
                continue
            rec: dict = {"sample_id": sid}
            for k in ("mos", "abx"):
                v = (r.get(k) or "").strip()
                if v:
                    try:
                        rec[k] = float(v)
                    except ValueError:
                        pass
            if len(rec) > 1:
                rows.append(rec)
    return rows


def analyze(exp: str, *, root: Path | None = None,
            subjective: Path | None = None) -> dict:
    """读 results.json，产出 RQ1/RQ2/RQ3 的结论，落盘 report.json + report.md。"""
    base = (Path(root) if root else _default_root()) / exp
    res: dict = {"exp": exp, "root": str(base), "error": None, "error_stage": None,
                 "notes": []}
    rf = base / "results.json"
    if not rf.exists():
        res["error"] = f"没有测量结果: {rf}（先跑 measure）"
        res["error_stage"] = "测量结果"
        return res
    try:
        data = json.loads(rf.read_text(encoding="utf-8"))
    except Exception as e:
        res["error"] = f"测量结果读取失败: {type(e).__name__}: {e}"
        res["error_stage"] = "测量结果"
        return res

    samples = data.get("samples") or []
    if not samples:
        res["error"] = "测量结果里没有样本"
        res["error_stage"] = "测量结果"
        return res

    # ★ sample_id 必须唯一，否则 RQ3 的连接会退化成「取最后一条」：
    #   `by_id` 是 dict，重复 id 后面的覆盖前面的，于是同一组客观值被配到多个
    #   主观分上 —— 相关系数**照样算得出来**，只是错的。这种情况必须留痕。
    counts: dict = {}
    for s in samples:
        sid = s.get("sample_id")
        if sid is not None:
            counts[sid] = counts.get(sid, 0) + 1
    dup = sorted(k for k, v in counts.items() if v > 1)
    if dup:
        res["notes"].append(
            f"results.json 里有 {len(dup)} 个 sample_id 重复（如 {dup[:3]}）—— "
            f"RQ3 的主客观连接会取到最后一条，相关系数不可信；先查样本目录命名")

    # ---- RQ1：时长 → 质量，以及饱和点 ----
    by_dur = _group_medians(samples, "duration_s")
    rq1: dict = {}
    for metric in METRIC_KEYS:
        durs = sorted(d for d in by_dur if by_dur[d].get(metric) is not None)
        vals = [by_dur[d][metric] for d in durs]
        rq1[metric] = {
            "direction": "higher_better" if HIGHER_IS_BETTER[metric] else "lower_better",
            "by_duration": {_dur_tag(d): by_dur[d][metric] for d in durs},
            "saturation": saturation_point(
                durs, vals, higher_is_better=HIGHER_IS_BETTER[metric]),
        }

    # ---- RQ2：条件 → 质量 ----
    by_cond = _group_medians(samples, "condition")
    rq2 = {c: by_cond.get(c, {}) for c in sorted(by_cond)}

    # ---- RQ3：客观指标 ↔ 主观评分 ----
    rq3: dict = {"subjective_rows": 0, "matched": 0, "correlations": {}}
    if subjective:
        sp = Path(subjective)
        if not sp.exists():
            res["notes"].append(f"主观评分文件不存在，RQ3 跳过: {sp}")
        else:
            rows = _read_subjective(sp)
            rq3["subjective_rows"] = len(rows)
            by_id = {s.get("sample_id"): s for s in samples}
            matched = [r for r in rows if r.get("sample_id") in by_id]
            rq3["matched"] = len(matched)
            if len(matched) < MIN_CORR_N:
                res["notes"].append(
                    f"主观评分与样本匹配上 {len(matched)} 条（< {MIN_CORR_N}），"
                    f"相关系数不给（不编造数字）")
            else:
                for target in ("mos", "abx"):
                    ys = [r.get(target) for r in matched]
                    if sum(1 for y in ys if y is not None) < MIN_CORR_N:
                        continue
                    per_metric = {}
                    for metric in METRIC_KEYS:
                        xs = [(by_id[r["sample_id"]].get("metrics") or {}).get(metric)
                              for r in matched]
                        per_metric[metric] = spearman(xs, ys)
                    rq3["correlations"][target] = per_metric

    res.update(rq1=rq1, rq2=rq2, rq3=rq3,
               n_samples=len(samples),
               measured=data.get("measured"),
               errors=data.get("errors"))
    _write_json(base / "report.json", res)
    (base / "report.md").write_text(_render_md(res), encoding="utf-8")
    return res


def _sat_text(sat: dict | None) -> str:
    """把饱和点结论渲染成一句话。

    ★ 三种「没有结论」必须分开说 —— 一律写成"未饱和"会让读者以为
    「再录更久还能变好」，而真实情况可能是"根本测不出趋势"（数据不足或非单调）。
    """
    if not sat:
        return "数据不足以判定"
    st = sat.get("status")
    if st == "saturated":
        return f"{sat['duration_tag']}（该档增量占总增益 {sat['gain_fraction']:.1%}）"
    if st == "non_monotonic":
        return "非单调，不可判定"
    if st == "still_climbing":
        return "未饱和（逐档都在涨）"
    return "数据不足以判定"


# ---------------- 盲听材料（RQ3 的输入端） ----------------
#
# 为什么要有这一段：`analyze --subjective` 的 RQ3 分支**从来没在真数据上跑过**，
# 而卡住它的不是分析代码，是**没有任何东西能产出听测数据** —— 没有配对、
# 没有盲化、没有评分表。研究者只能手工拼文件名、手工遮住条件名，而手工遮
# 会留下线索（文件名、目录结构、播放顺序都是线索）。
#
# 本段只做「生成材料 + 汇总分数」，**不播放音频**：听测由人在播放器里做，
# 我们不假装能替人听。

def _blind_pairs(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """把样本摊成盲听条目：每条 = 一个待评音频 + 它的真值身份。

    返回 `(items, unpaired)`：`unpaired` 是**有合成样本但缺 truth** 的条目
    —— ABX 对照不了，只能做绝对 MOS。**如实上报，不悄悄丢**：丢了会让
    "24 名听者 × 全部样本" 的预期与实际悄悄对不上。

    ★ 顺序：先按 sample_id 排序**再**编号，保证同一批样本在任何机器上
    得到同一组盲号（盲号可复现，随机顺序也才是唯一的不确定来源）。
    """
    items: list[dict] = []
    unpaired: list[dict] = []
    for s in sorted(samples, key=lambda r: str(r.get("sample_id"))):
        sid = str(s.get("sample_id") or "")
        if not sid:
            continue
        sp, tag, cond = str(s.get("speaker")), str(s.get("duration_tag")), str(s.get("condition"))
        items.append({
            "sample_id": sid,
            "speaker": sp,
            "duration_tag": tag,
            "condition": cond,
            "sample": str(s.get("path") or s.get("sample") or ""),
            "truth": s.get("truth") or f"truth/{sp}/{cond}.wav",
        })
    return items, unpaired


def listen(exp: str, *, root: Path | None = None, seed: int = 20261005,
           base_dir: Path | None = None) -> dict:
    """生成盲听材料：盲号映射（揭盲用）+ 评分表（发给听者）+ 播放清单。

    落在 `<root>/<exp>/listen/` 下：
        key.csv      盲号 → sample_id（**研究者留存，不可发给听者**）
        sheet.csv    盲号 → 空的 mos / abx 列（**这张才是发出去的**）
        playlist.txt 按随机顺序排好的音频路径（照着放，别自己翻目录）

    ★ 三个设计约束，都是"踩了才会发现"的：
      1. **评分表不含身份**：sheet.csv 只有盲号与空列。谁要是为了"方便"
         把 sample_id 列塞进 sheet，整轮听测就废了 —— 所以有单测钉住。
      2. **随机顺序 + 种子落盘**：听者的疲劳会随时序累积（后听的打低分），
         固定顺序 = 把"位置效应"混进"条件效应"。种子落盘才能复现同一顺序。
      3. **缺 truth 的样本单独列**：ABX 需要对照，没对照就只能打 MOS。
         混合统计时要能一眼看出哪些样本只有 MOS（否则 n 对不上会以为丢数据）。
    """
    base = (Path(root) if root else _default_root()) / exp
    out_dir = (Path(base_dir) if base_dir else base / LISTEN_DIR)
    res: dict = {"exp": exp, "root": str(base), "out": str(out_dir),
                 "error": None, "error_stage": None, "notes": [], "warnings": []}

    rf = base / "results.json"
    if not rf.exists():
        res["error"] = f"没有测量结果: {rf}（先跑 measure）"
        res["error_stage"] = "测量结果"
        return res
    try:
        data = json.loads(rf.read_text(encoding="utf-8"))
    except Exception as e:
        res["error"] = f"results.json 读不动: {type(e).__name__}: {e}"
        res["error_stage"] = "测量结果"
        return res

    samples = data.get("samples") or []
    if not samples:
        res["error"] = "results.json 里没有样本（先跑 measure）"
        res["error_stage"] = "样本"
        return res

    items, _ = _blind_pairs(samples)

    # 缺 truth 的样本：ABX 做不了，只能打 MOS —— 明写出来
    missing_truth = [it for it in items
                     if not (base / str(it["truth"])).exists()]
    paired = [it for it in items if it not in missing_truth]

    # 缺合成音频的样本：列出来了但播不出来，剔除并警告
    # （不生成一张放不出来的清单 —— 听者按下播放键却什么都没有，
    #  他会以为是自己操作错了，而不是来报"文件不存在"）
    #
    # ★ 空路径必须算"缺"：`Path("")` 会静默变成 `"."` 而 `"."` 是**存在的**，
    #   于是"没有路径"会被判成"文件在" —— 这正是本轮实测踩到的坑
    #   （`_row` 里 path 为空串，`Path("").exists()` 返回 True）。
    def _audio_ok(p) -> bool:
        s = str(p or "").strip()
        if not s:
            return False
        return (base / s).exists() or Path(s).exists()

    missing_audio = [it for it in items if not _audio_ok(it["sample"])]
    if missing_audio:
        res["warnings"].append(
            f"{len(missing_audio)} 个样本的音频文件不存在（如 "
            f"{missing_audio[0]['sample_id']}）—— 已从评分表里剔除，"
            f"请先确认合成是否跑完")

    playable = [it for it in items if it not in missing_audio]

    # 随机顺序（种子可复现）
    order = list(range(len(playable)))
    rnd = random.Random(seed)
    rnd.shuffle(order)

    rows: list[dict] = []
    for pos, idx in enumerate(order, 1):
        it = playable[idx]
        bid = f"{LISTEN_ID_PREFIX}{pos:04d}"
        rows.append({"blind_id": bid, "pos": pos, **{k: it[k] for k in
                    ("sample_id", "speaker", "duration_tag", "condition",
                     "sample", "truth")}})

    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- key.csv：盲号 → 真身份（研究者留存） ----
    with open(out_dir / "key.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["blind_id", "pos", "sample_id",
                                          "speaker", "duration_tag", "condition",
                                          "sample", "truth"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ---- sheet.csv：★ 只有盲号 + 空列，不含任何身份 ----
    with open(out_dir / "sheet.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(list(LISTEN_CSV_COLUMNS))
        for r in rows:
            w.writerow([r["blind_id"], "", "", ""])

    # ---- playlist.txt：照着放 ----
    lines = [f"# 盲听播放清单 · 实验 {exp}",
             f"# 盲号 {LISTEN_ID_PREFIX}0001..{LISTEN_ID_PREFIX}{len(rows):04d}，"
             f"共 {len(rows)} 条",
             "# ★ 按下面的顺序播放，别自己翻目录（顺序已随机化，种子可复现）",
             f"# 评分表：{out_dir / 'sheet.csv'}（只有盲号，填 mos / abx）",
             ""]
    for r in rows:
        p = base / str(r["sample"])
        lines.append(f"{r['blind_id']}\t{p}")
    (out_dir / "playlist.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    res.update(
        seed=seed,
        n_items=len(rows),
        n_paired=len(paired),
        n_missing_truth=len(missing_truth),
        missing_truth=[{"sample_id": it["sample_id"], "truth": it["truth"]}
                       for it in missing_truth],
        n_dropped=len(missing_audio),
        dropped=[it["sample_id"] for it in missing_audio],
        key=str(out_dir / "key.csv"),
        sheet=str(out_dir / "sheet.csv"),
        playlist=str(out_dir / "playlist.txt"),
    )
    if missing_truth:
        res["warnings"].append(
            f"{len(missing_truth)} 个样本缺 truth 对照 → 只能评 MOS，ABX 无法评；"
            f"汇总时这些样本的 abx 会是空值")
    _write_json(out_dir / "listen.json", res)
    return res


def merge_listen(exp: str, sheets: list[Path], *, root: Path | None = None) -> dict:
    """把多名听者的评分表合并成 `analyze --subjective` 能直接吃的 CSV。

    ★ 合并口径（做了三个决定，都不是随意的）：
      1. **先算每个人的分，再对人数平均**（等权听者）。直接对所有行求平均会让
         "评得多的听者"权重更大 —— 而评得多往往是因为他没跳题，与评分松紧无关，
         那属于**噪音权重**，不是信息权重。
      2. **n 一并输出**：`analyze` 只认 sample_id/mos/abx 三列，但研究者需要看到
         每个样本被几个人评过。多出来的 `n` 列 analyze 会忽略（`_read_subjective`
         只挑它认识的键），所以**多给一列是安全的、且必要的**。
      3. **对不上的行单独报**：盲号在 key.csv 里查不到 → 记进 `unmapped`，
         不静默丢。丢了会让"24 人 × 64 样本"的预期悄悄少一块。
    """
    base = (Path(root) if root else _default_root()) / exp
    res: dict = {"exp": exp, "root": str(base), "error": None, "error_stage": None,
                 "notes": [], "warnings": [], "sheets_read": 0, "rows_read": 0,
                 "unmapped": [], "invalid": []}

    key_path = base / LISTEN_DIR / "key.csv"
    if not key_path.exists():
        res["error"] = f"没有揭盲表: {key_path}（先跑 listen）"
        res["error_stage"] = "揭盲表"
        return res
    blind2sid: dict[str, str] = {}
    with open(key_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            bid = (r.get("blind_id") or "").strip()
            sid = (r.get("sample_id") or "").strip()
            if bid and sid:
                blind2sid[bid] = sid
    if not blind2sid:
        res["error"] = f"揭盲表为空: {key_path}"
        res["error_stage"] = "揭盲表"
        return res

    per_listener: list[dict[str, dict]] = []
    for sp in sheets:
        sp = Path(sp)
        if not sp.exists():
            res["notes"].append(f"评分表不存在，跳过: {sp}")
            continue
        res["sheets_read"] += 1
        lmap: dict[str, dict] = {}
        with open(sp, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                bid = (r.get("blind_id") or r.get("sample_id") or "").strip()
                if not bid:
                    continue
                res["rows_read"] += 1
                sid = blind2sid.get(bid)
                if not sid:
                    res["unmapped"].append({"sheet": sp.name, "blind_id": bid})
                    continue
                rec: dict = {}
                for k in ("mos", "abx"):
                    v = (r.get(k) or "").strip()
                    if not v:
                        continue
                    try:
                        fv = float(v)
                    except ValueError:
                        res["invalid"].append(
                            {"sheet": sp.name, "blind_id": bid, "field": k, "value": v})
                        continue
                    if k == "mos" and not (MOS_MIN <= fv <= MOS_MAX):
                        res["invalid"].append(
                            {"sheet": sp.name, "blind_id": bid, "field": "mos",
                             "value": v, "why": f"超出 {MOS_MIN:.0f}–{MOS_MAX:.0f}"})
                        continue
                    if k == "abx" and fv not in ABX_VALUES:
                        res["invalid"].append(
                            {"sheet": sp.name, "blind_id": bid, "field": "abx",
                             "value": v, "why": "只能是 0 或 1"})
                        continue
                    rec[k] = fv
                if rec:
                    lmap[sid] = rec
        per_listener.append(lmap)

    if not per_listener:
        res["error"] = "没有读到任何评分表"
        res["error_stage"] = "评分表"
        return res

    ids = sorted({sid for m in per_listener for sid in m})
    out_rows: list[dict] = []
    for sid in ids:
        mos_v = [m[sid]["mos"] for m in per_listener if sid in m and "mos" in m[sid]]
        abx_v = [m[sid]["abx"] for m in per_listener if sid in m and "abx" in m[sid]]
        if not mos_v and not abx_v:
            continue
        row = {"sample_id": sid, "mos": "", "abx": "",
               "n_mos": len(mos_v), "n_abx": len(abx_v)}
        if mos_v:
            row["mos"] = round(sum(mos_v) / len(mos_v), 4)
        if abx_v:
            row["abx"] = round(sum(abx_v) / len(abx_v), 4)
        out_rows.append(row)

    # ★ 排序后写：同一批输入在任何机器上产出同一个文件（便于 diff 与复现）
    out_rows.sort(key=lambda r: r["sample_id"])
    dest = base / "subjective.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["sample_id", "mos", "abx", "n_mos", "n_abx"])
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    res.update(
        n_samples=len(out_rows),
        n_listeners=len(per_listener),
        out=str(dest),
        analyze_hint=(f"python tools/voicebank_eval.py analyze --exp {exp} "
                      f"--subjective {dest}"),
    )
    if res["unmapped"]:
        res["warnings"].append(
            f"{len(res['unmapped'])} 行盲号在揭盲表里查不到（如 "
            f"{res['unmapped'][0]['blind_id']}）—— 可能拿错了实验的评分表")
    if res["invalid"]:
        res["warnings"].append(
            f"{len(res['invalid'])} 行取值非法（如 mos={res['invalid'][0].get('value')}）"
            f"—— 已剔除，请让听者重填该行")
    if len(per_listener) < 2:
        res["warnings"].append(
            "只有 1 名听者 —— RQ3 的相关系数在单人评分上意义有限（听者间一致性无法估计）")
    return res



def _render_md(res: dict) -> str:
    """把报告渲染成人类可读的 Markdown。数字原样带出，不做二次加工。"""
    L = [f"# 嗓音保存复现质量评测 · {res.get('exp')}", "",
         f"- 样本数 {res.get('n_samples')}（测量成功 {res.get('measured')}，"
         f"失败 {res.get('errors')}）", ""]

    rq1 = res.get("rq1", {})
    tags = sorted({t for v in rq1.values() for t in (v.get("by_duration") or {})},
                  key=lambda x: _parse_dur_tag(x) or 0)
    L += ["## RQ1 参考音时长 → 质量，以及收益饱和点", "",
          "| 指标 | 方向 | " + " | ".join(tags) + " | 饱和档 |",
          "|---|---|" + "---|" * (len(tags) + 1)]
    for metric, v in rq1.items():
        bd = v.get("by_duration") or {}
        if not bd:
            continue
        cols = [str(bd.get(t)) for t in tags]
        L.append(f"| {metric} | {v.get('direction')} | " + " | ".join(cols)
                 + f" | {_sat_text(v.get('saturation'))} |")
    L.append("")

    L += ["## RQ2 文本条件 → 质量", "",
          "| 条件 | " + " | ".join(METRIC_KEYS) + " |",
          "|---|" + "---|" * len(METRIC_KEYS)]
    for cond, v in res.get("rq2", {}).items():
        L.append(f"| {cond} | " + " | ".join(str(v.get(m)) for m in METRIC_KEYS) + " |")
    L.append("")

    rq3 = res.get("rq3") or {}
    L += ["## RQ3 客观指标 ↔ 主观评分（Spearman 秩相关）", "",
          f"- 主观评分行数 {rq3.get('subjective_rows')}，匹配上 {rq3.get('matched')}",
          ""]
    if rq3.get("correlations"):
        L += ["| 指标 | vs MOS | vs ABX |", "|---|---|---|"]
        for metric in METRIC_KEYS:
            row = [metric]
            for target in ("mos", "abx"):
                v = (rq3["correlations"].get(target) or {}).get(metric)
                row.append("—" if v is None else f"{v:+.3f}")
            L.append("| " + " | ".join(row) + " |")
    else:
        L.append("（未提供主观评分，或匹配样本不足 —— 不给系数，不编造）")
    L.append("")

    if res.get("notes"):
        L += ["## 备注", ""] + [f"- {n}" for n in res["notes"]] + [""]
    return "\n".join(L)


# ---------------- CLI ----------------

def _print_corpus():
    print(f"=== 参考脚本（按顺序念，共 {len(REFERENCE_SCRIPT)} 句 / "
          f"{sum(_char_count(s) for s in REFERENCE_SCRIPT)} 字 / "
          f"约 {_script_capacity_s():.0f}s）===")
    for i, s in enumerate(REFERENCE_SCRIPT, 1):
        print(f"{i:3d}. {s}")
    print("\n=== 四类复现文本（每个样本念一条）===")
    for cond in CONDITIONS:
        print(f"\n[{cond}]")
        for t in REPRO_TEXTS.get(cond, []):
            print(f"  （{t['style']}）{t['text']}")
    print(f"\n可用时长档: {[_dur_tag(d) for d in DURATION_LADDER]}"
          f"（脚本容量约 {_script_capacity_s():.0f}s，超出需扩充脚本）")


def main():
    # 中文控制台（本机默认 GBK/CP936）下，报告正文里的 `↔` 之类的字符会让 print 抛
    # UnicodeEncodeError —— 而它出现在**最后一步**：report.json / report.md 都写好了，
    # 命令却以非零退出收场，看起来像"分析失败"（2026-10-08 实测）。
    # 与 tools/check.py 的 `_console()` 同一处理。
    import contextlib

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(
        description="嗓音保存复现质量评测引擎（矩阵 / 测量 / 统计）")
    sub = p.add_subparsers(dest="cmd")

    pp = sub.add_parser("plan", help="生成实验设计矩阵 + 目录骨架")
    pp.add_argument("--exp", required=True, help="实验名（落盘到 outputs/voicebank/<exp>/）")
    pp.add_argument("--speakers", required=True, help="说话人 id，逗号分隔")
    pp.add_argument("--durations", help="时长档秒数，逗号分隔（默认 3,10,30,60）")
    pp.add_argument("--conditions", help="条件，逗号分隔（默认 same,cross,emotion,long）")
    pp.add_argument("--root", help="落盘根（默认 outputs/voicebank）")

    ps = sub.add_parser("split", help="一条录音 → 前缀切四档（对齐句末）")
    ps.add_argument("--exp", required=True)
    ps.add_argument("--speakers", required=True, help="**只接受 1 个**说话人 id")
    ps.add_argument("--source", required=True, help="一条连续录音（wav；其他格式先转）")
    ps.add_argument("--durations", help="时长档秒数，逗号分隔（默认 3,10,30,60）")
    ps.add_argument("--root")
    ps.add_argument("--noise-db", type=float, default=None,
                    help="silencedetect 噪声门限 dB（默认自适应：整段 RMS - 15dB）")
    ps.add_argument("--min-silence", type=float, default=0.15,
                    help="最短静音时长秒（默认 0.15）")

    pm = sub.add_parser("measure", help="对 samples/ 跑客观指标")
    pm.add_argument("--exp", required=True)
    pm.add_argument("--root")
    pm.add_argument("--with-emb", action="store_true", help="加声纹相似度（需 worker 8001）")
    pm.add_argument("--with-asr", action="store_true", help="加 ASR 可懂度（需 worker 8001）")

    pa = sub.add_parser("analyze", help="出 RQ1/RQ2/RQ3 报告")
    pa.add_argument("--exp", required=True)
    pa.add_argument("--root")
    pa.add_argument("--subjective", help="主观听测 CSV（列：sample_id, mos, abx）")

    pl = sub.add_parser("listen", help="生成盲听材料（盲号 + 评分表 + 播放清单）")
    pl.add_argument("--exp", required=True)
    pl.add_argument("--root")
    pl.add_argument("--seed", type=int, default=20261005,
                    help="随机顺序种子（默认 20261005；同一实验复现同一顺序）")

    pmg = sub.add_parser("merge-listen", help="合并多名听者的评分表 → subjective.csv")
    pmg.add_argument("--exp", required=True)
    pmg.add_argument("--root")
    pmg.add_argument("--sheets", required=True,
                     help="评分表路径，逗号分隔（每人一份）")

    sub.add_parser("corpus", help="打印录音脚本与四类复现文本")

    args = p.parse_args()
    if args.cmd == "corpus":
        _print_corpus()
        return
    if not args.cmd:
        p.error("请指定子命令：plan / split / measure / analyze / listen / "
                "merge-listen / corpus")

    root = Path(args.root) if getattr(args, "root", None) else None

    if args.cmd == "plan":
        durs = ([float(x) for x in args.durations.split(",") if x.strip()]
                if args.durations else None)
        conds = ([x.strip() for x in args.conditions.split(",") if x.strip()]
                 if args.conditions else None)
        sps = [x.strip() for x in args.speakers.split(",") if x.strip()]
        res = plan(args.exp, sps, durations=durs, conditions=conds, root=root)
        if res.get("error"):
            print(f"[plan] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for w in res.get("warnings", []):
            print(f"[plan] 警告: {w}")
        print(f"[plan] {len(res['speakers'])} 说话人 × {len(res['durations'])} 时长档 "
              f"× {len(res['conditions'])} 条件 = {res['n_samples']} 个样本")
        print(f"[plan] 落盘: {res['root']}")
        print("[plan] 下一步：按 corpus 录制 refs/<说话人>/<档>.wav 与 truth/<说话人>/<条件>.wav，"
              "合成样本放进 samples/<说话人>/<档>/<条件>.wav")

    elif args.cmd == "split":
        durs = ([float(x) for x in args.durations.split(",") if x.strip()]
                if args.durations else None)
        sps = [x.strip() for x in args.speakers.split(",") if x.strip()]
        res = split(args.exp, sps, Path(args.source), durations=durs, root=root,
                    noise_db=args.noise_db, min_silence_s=args.min_silence)
        if res.get("error"):
            print(f"[split] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for w in res.get("warnings", []):
            print(f"[split] 警告: {w}")
        print(f"[split] 源 {res['total_s']:.2f}s，探测到 {res['silences']} 段静音"
              f"（门限 {res['noise_db']}dB{' 自适应' if res.get('adaptive_noise') else ''}）")
        for t in res["tiers"]:
            flag = "对齐句末" if t["aligned"] else "★硬切(未对齐)"
            print(f"[split] {t['tag']:>4s} 档 → 实际 {t['actual_s']:6.2f}s  "
                  f"（切点 {t['cut_s']:6.2f}s，{flag}）  {t['bytes']//1024}KB")
        print(f"[split] 落盘: {res['root']}/refs/{res['speaker']}/  "
              f"＋ split.json 与 split/{res['speaker']}.json（切点留档）")

    elif args.cmd == "measure":
        res = measure(args.exp, root=root, with_emb=args.with_emb, with_asr=args.with_asr)
        if res.get("error"):
            print(f"[measure] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for n in res.get("notes", []):
            print(f"[measure] 注意: {n}")
        print(f"[measure] {res['count']} 个样本，成功 {res['measured']}，失败 {res['errors']}")
        print(f"[measure] 落盘: {res['root']}/results.json 与 results.csv")

    elif args.cmd == "analyze":
        subj = Path(args.subjective) if args.subjective else None
        res = analyze(args.exp, root=root, subjective=subj)
        if res.get("error"):
            print(f"[analyze] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for n in res.get("notes", []):
            print(f"[analyze] 注意: {n}")
        print(_render_md(res))
        print(f"[analyze] 落盘: {res['root']}/report.json 与 report.md")

    elif args.cmd == "listen":
        res = listen(args.exp, root=root, seed=args.seed)
        if res.get("error"):
            print(f"[listen] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for n in res.get("notes", []):
            print(f"[listen] 注意: {n}")
        for w in res.get("warnings", []):
            print(f"[listen] 警告: {w}")
        print(f"[listen] {res['n_items']} 条（其中 {res['n_paired']} 条有 truth 对照，"
              f"{res['n_missing_truth']} 条只能评 MOS）；顺序种子 {res['seed']}")
        print(f"[listen] 揭盲表（★ 自己留着，别发）: {res['key']}")
        print(f"[listen] 评分表（发给听者）      : {res['sheet']}")
        print(f"[listen] 播放清单                 : {res['playlist']}")
        print(f"[listen] 收齐后: python tools/voicebank_eval.py merge-listen "
              f"--exp {args.exp} --sheets 甲.csv,乙.csv")

    elif args.cmd == "merge-listen":
        sheets = [Path(x.strip()) for x in args.sheets.split(",") if x.strip()]
        res = merge_listen(args.exp, sheets, root=root)
        if res.get("error"):
            print(f"[merge-listen] 失败（{res.get('error_stage')}）: {res['error']}")
            sys.exit(1)
        for n in res.get("notes", []):
            print(f"[merge-listen] 注意: {n}")
        for w in res.get("warnings", []):
            print(f"[merge-listen] 警告: {w}")
        print(f"[merge-listen] 读入 {res['sheets_read']} 份评分表 / {res['rows_read']} 行；"
              f"{res['n_listeners']} 名听者 → {res['n_samples']} 个样本")
        print(f"[merge-listen] 落盘: {res['out']}")
        print(f"[merge-listen] 下一步: {res['analyze_hint']}")


if __name__ == "__main__":
    main()

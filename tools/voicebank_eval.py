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
    report.json / report.md                统计与相关性（analyze 产出）

用法
    python tools/voicebank_eval.py plan    --exp vb01 --speakers spk01,spk02
    python tools/voicebank_eval.py measure --exp vb01 [--with-emb] [--with-asr]
    python tools/voicebank_eval.py analyze --exp vb01 [--subjective scores.csv]
    python tools/voicebank_eval.py corpus                # 打印录音脚本与四类复现文本
"""

from __future__ import annotations

import argparse
import csv
import json
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

# ---------------- 语料 ----------------
#
# 参考脚本：**按顺序念**，时长档 = 取前 N 秒（前缀截断）。
# 为什么用前缀截断而不是随机抽句：档位之间**只差"录了多久"**这一个变量，
# 内容完全被控制住 —— 否则"10s 档比 3s 档好"可能只是抽到了更好念的句子。
#
# 选句原则：日常自然（声音保存要念的就是以后真会说的话）+ 尽量覆盖普通话声韵母。
REFERENCE_SCRIPT = [
    "你好，很高兴见到你。",
    "今天天气很好，我们一起出去走走吧。",
    "我想喝一杯温水，谢谢。",
    "请帮我打开窗户，房间里有点闷。",
    "妈妈做的饭菜永远是最好吃的。",
    "明天上午九点我们在地铁站见面。",
    "这本书写得很有意思，我看了两遍。",
    "孩子，你要记得按时吃饭。",
    "我有点累了，想早点休息。",
    "春天来了，路边的花都开了。",
    "麻烦你帮我拿一下那个包。",
    "他说话的声音很温柔，让人很安心。",
    "我们家的猫喜欢趴在阳台上晒太阳。",
    "这个周末我打算去看望爷爷奶奶。",
    "请你慢一点说，我听得不太清楚。",
    "学校门口新开了一家面馆，味道不错。",
    "谢谢你一直陪在我身边。",
    "下雨了，出门记得带伞。",
    "我特别喜欢清晨的安静。",
    "他现在在外地工作，过年才回来。",
    "你把钥匙放在桌子上就好。",
    "这首老歌让我想起了小时候。",
    "医生说我需要多喝水，多休息。",
    "我们拍一张合影留作纪念吧。",
    "不管发生什么，我都会支持你。",
    "山上的空气很新鲜，风也凉快。",
    "请你把这段话再念一遍给我听。",
    "晚上八点有一场很好看的电影。",
    "我从来没有去过那么远的地方。",
    "希望你以后每天都开开心心的。",
]

# 四类复现文本。style 是给合成器的语气提示（不是要念出来的字）。
REPRO_TEXTS: dict[str, list[dict[str, str]]] = {
    "same": [
        {"text": "你好，很高兴见到你。", "style": "平静"},
    ],
    "cross": [
        {"text": "麻烦你把空调的温度调低一点。", "style": "平静"},
        {"text": "我下周要去一趟广州出差。", "style": "平静"},
    ],
    "emotion": [
        {"text": "太好了，我终于等到这一天了！", "style": "高兴"},
        {"text": "你怎么能这样说话呢！", "style": "生气"},
        {"text": "我真的不知道该怎么办了。", "style": "难过"},
    ],
    "long": [
        {"text": "如果你以后有机会去到很远的地方，一定要记得给家里打个电话，"
                 "别让我们担心。", "style": "平静"},
    ],
}

# 参考脚本容量的估算语速（字/秒）。用来提示「你的档位要录多久才够」。
# 4 字/秒 ≈ 240 字/分钟，是普通话朗读的常见速率。
SCRIPT_CHARS_PER_SEC = 4.0


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
    """去标点空白后的字数。口径与 `tools/voice_qc.py::_char_count` 一致。"""
    punct = set("，。！？；：、·…—“”‘’（）《》〈〉【】,.!?;:()[]{}<>\"'`~@#$%^&*_+=/\\|- \t\r\n")
    return sum(1 for ch in text if ch not in punct)


def _script_capacity_s(script: list[str] | None = None) -> float:
    """参考脚本念完大约多少秒 —— 用来判断某个时长档能不能被脚本覆盖。"""
    chars = sum(_char_count(s) for s in (script or REFERENCE_SCRIPT))
    return chars / SCRIPT_CHARS_PER_SEC


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
    p = argparse.ArgumentParser(
        description="嗓音保存复现质量评测引擎（矩阵 / 测量 / 统计）")
    sub = p.add_subparsers(dest="cmd")

    pp = sub.add_parser("plan", help="生成实验设计矩阵 + 目录骨架")
    pp.add_argument("--exp", required=True, help="实验名（落盘到 outputs/voicebank/<exp>/）")
    pp.add_argument("--speakers", required=True, help="说话人 id，逗号分隔")
    pp.add_argument("--durations", help="时长档秒数，逗号分隔（默认 3,10,30,60）")
    pp.add_argument("--conditions", help="条件，逗号分隔（默认 same,cross,emotion,long）")
    pp.add_argument("--root", help="落盘根（默认 outputs/voicebank）")

    pm = sub.add_parser("measure", help="对 samples/ 跑客观指标")
    pm.add_argument("--exp", required=True)
    pm.add_argument("--root")
    pm.add_argument("--with-emb", action="store_true", help="加声纹相似度（需 worker 8001）")
    pm.add_argument("--with-asr", action="store_true", help="加 ASR 可懂度（需 worker 8001）")

    pa = sub.add_parser("analyze", help="出 RQ1/RQ2/RQ3 报告")
    pa.add_argument("--exp", required=True)
    pa.add_argument("--root")
    pa.add_argument("--subjective", help="主观听测 CSV（列：sample_id, mos, abx）")

    sub.add_parser("corpus", help="打印录音脚本与四类复现文本")

    args = p.parse_args()
    if args.cmd == "corpus":
        _print_corpus()
        return
    if not args.cmd:
        p.error("请指定子命令：plan / measure / analyze / corpus")

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


if __name__ == "__main__":
    main()

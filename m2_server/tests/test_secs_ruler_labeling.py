# -*- coding: utf-8 -*-
r"""「SECS 是哪把尺子」的措辞守卫（2026-10-11，犯错档案 §8.71）。

### 为什么需要这条测试

2026-10-11 与导师对话时被问穿的那类错误：**材料把值来自 A 把尺子的数，
标成 B 把尺子**。项目里有**两个**都叫 `secs` 的指标：

| 出处 | 实现 | 真实尺子 |
|---|---|---|
| `m2_server/audition_score.py::_secs` | `speaker_sep._sv_embed`（16k / 192 维） | **CAM++ 独立声纹** |
| `tools/clone_eval.py` 的 SECS、`tools/voice_qc.py::_emb`、
  `voicepreserve._emb_sim_vs_baseline` | worker 8001 `/emb` →
  `qwen3_tts_service._speaker_embedding` →
  `create_voice_clone_prompt(x_vector_only_mode=True).ref_spk_embedding` | **Qwen 自空间**
  （与克隆条件同源 = 出题人自己判卷，实测会饱和） |

`pilotC` 双声纹实验（同批 48 条）证明两把尺子不是一个量级：自空间同人 **87.5% ≥0.98**、
CAM++ 同人 0.6486–0.8345、对参考音时长的敏感度约为自空间的 12 倍。
所以「0.9905 / 0.9888」这些数**只能在自己的刻度下引用**。

### 为什么写成「读源码找措辞」而不是「比对数字」

这里守的是**声明**，不是数值：数值会随素材漂（守它只会逼人改文档），
但「把自空间说成 CAM++」是**原则性错误**，与数值漂不漂无关，值得用字符串钉子钉死。
负例同样重要 —— 所以断言写成两半：① 必须出现正确尺子名，② 禁止出现那句错标原文。

边界：只扫这次出问题的那批文件（工具源码 + 相关材料），不做全库扫描 ——
`docs/音色体检三灯.md` / `docs/调研-SVC路线与嗓音客观指标-2026-09-28.md` 等文件里的
"CAM++" 指的是**真** `_sv_embed` 那条路，把它们一并禁掉就是假红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

#: 那句错标的原文（禁止再出现在这些文件里）。
BAD_CLAIM = "底层即 CAM++"


def _text(rel: str) -> str:
    p = _ROOT / rel
    assert p.exists(), f"文件不存在，守卫的前提已经变了：{rel}"
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 一、尺子的归属（实现层）—— 先确认事实，再谈措辞
# --------------------------------------------------------------------------
def test_worker_emb_is_qwen_self_space_not_campplus():
    """`/emb` 的实现必须是 Qwen 自己的声纹接口，不是 CAM++。

    这条是整个措辞守卫的前提。哪天 `/emb` 改成调 `speaker_sep._sv_embed`
    （即两把尺子真的合并了），上面那些措辞测试的依据就作废 —— 那时应当**同时**改测试，
    而不是让它安静地变成一条恒真的守卫。
    """
    src = _text("m2_server/qwen3_tts_service.py")
    body = src[src.index("def _speaker_embedding"):]
    assert "create_voice_clone_prompt" in body, "/emb 已不走 Qwen 自己的编码器了"
    assert "x_vector_only_mode=True" in body, "/emb 不再只取说话人向量了"
    assert "_sv_embed" not in body and "campplus" not in body.lower(), (
        "/emb 已经改用 CAM++ —— 尺子合并了，"
        "本文件的措辞断言与 clone_eval 的反向声明必须一起重写"
    )


def test_audition_score_secs_is_the_real_campplus():
    """另一头也得钉住：`audition_score._secs` 用的**才是** CAM++（两把尺子不许混为一谈）。"""
    src = _text("m2_server/audition_score.py")
    assert "_sv_embed" in src, "audition_score 不再走 CAM++ 了？那 §8.71 的归属表就反了"
    assert "speaker_sep" in src


# --------------------------------------------------------------------------
# 二、clone_eval：错标必须已修，且反向声明必须在
# --------------------------------------------------------------------------
def test_clone_eval_no_longer_claims_campplus_and_declares_self_space():
    doc = _text("tools/clone_eval.py")
    assert BAD_CLAIM not in doc, (
        f"clone_eval 又写回「{BAD_CLAIM}」了 —— 那是 2026-10-11 被导师问穿的错标（§8.71）"
    )
    # 反向声明：必须**显式**说"不是 CAM++"，光写对尺子名不够（下一个人照样会猜错）
    assert "不是 CAM++" in doc, "clone_eval 缺『不是 CAM++』的反向声明"
    assert "自空间" in doc, "clone_eval 没说明这把尺子是 Qwen 自空间"
    assert "speaker_sep" in doc, "clone_eval 没指出真 CAM++ 在哪（两条路必须能互相导航）"


def test_clone_eval_runtime_notice_names_the_ruler():
    """运行时那行提示也得带尺子名 —— 打印出来的话会被抄进报告。"""
    src = _text("tools/clone_eval.py")
    hits = [ln for ln in src.splitlines() if "已取原声嵌入" in ln]
    assert hits, "找不到取嵌入的提示行（改动前请先确认这行还在）"
    assert any("自空间" in ln for ln in hits), "提示行没标尺子，抄进材料就是第二次错标"


# --------------------------------------------------------------------------
# 三、这次被写坏的那几份材料：错标不得残留
# --------------------------------------------------------------------------
STALE_DOCS = [
    "docs/端到端实测-音色保存与录课视频.md",
    "docs/大创申报-2026-10/减负pilot-s01-记录.md",
    "docs/大创申报-2026-10/大创方案-v2.md",
]

#: 档案**故意**引用错标原文（那是它的职责：留下"曾经怎么写"的证据），
#: 所以它不进"禁止残留"这一组，只进"更正必须可读"那一组。
#: 把它误加进 STALE_DOCS 会让守卫永远红 —— 假红比不守更糟。
QUOTE_OK_DOCS = ["docs/犯错档案-工程.md"]


@pytest.mark.parametrize("rel", STALE_DOCS)
def test_mislabeled_campplus_claim_removed(rel: str):
    text = _text(rel)
    assert "CAM++ /emb" not in text, f"{rel} 还把 /emb 称作 CAM++"
    assert "CAM++ 声纹，SECS" not in text, f"{rel} 标题仍写「CAM++ 声纹，SECS 余弦」"
    assert "（`/emb` CAM++" not in text, f"{rel} 证据行仍写「/emb CAM++」"
    assert "CAM++ 声纹余弦 | 现役判据 ≥0.95" not in text, f"{rel} 又把 ≥0.95 阈值挂到 CAM++ 名下"


@pytest.mark.parametrize("rel", STALE_DOCS + QUOTE_OK_DOCS)
def test_correction_is_visible_not_silent(rel: str):
    """改过要留痕：光把错句删掉，读者无从知道历史上错在哪、为什么数字对不上。"""
    text = _text(rel)
    assert ("自空间" in text) or ("双门槛" in text) or ("voice_dual_gate" in text), (
        f"{rel} 只删了错标、没写正确口径 —— 更正必须可读"
    )


# --------------------------------------------------------------------------
# 四、门禁的正解：阈值来自实测推导，不是手填
# --------------------------------------------------------------------------
def test_dual_gate_is_the_referenced_answer():
    """材料指向门禁时，必须指向**双门槛**那一份（自空间单尺不能当准入门槛）。"""
    gate = _text("tools/voice_dual_gate.py")
    assert "derive" in gate, "双门槛阈值推导函数不在了 —— 阈值退化成手填就必须重标"
    text = _text("docs/大创申报-2026-10/大创方案-v2.md")
    assert "voice_dual_gate" in text, "方案文档没引用双门槛门禁"


#: SECS 行必须出现的"标尺声明"关键词。
#: ★ 刻意**不含** `CAM++` —— 那正是错标 token：
#:   「SECS（CAM++ 声纹）」这种写法只要含 CAM++ 就会被上一版放行（2026-10-11 变异实测：
#:   把标题里的正确尺子名换成任意其它词，守卫仍绿 —— 因为它只要求"含某个关键词"）。
#:   所以这里要求的是"说清是自己的尺子"，不是"提到 CAM++ 就算标了"。
RULER_WORDS = ("自空间", "自标尺", "尺子", "口径", "更正")

#: 与 SECS 同句出现 CAM++ 时，必须处于**否定/更正**语境。
NEGATION_WORDS = ("不是", "非", "原写", "更正", "误标", "标错")

_SECS_RE = re.compile(r"SECS")
_CAM_RE = re.compile(r"CAM\+\+")


def test_no_naked_secs_in_new_docs():
    """更正过的材料里，裸 `SECS` 必须带尺子名。"""
    rel = "docs/端到端实测-音色保存与录课视频.md"
    for ln in _text(rel).splitlines():
        if "SECS" not in ln:
            continue
        ok = any(k in ln for k in RULER_WORDS)
        assert ok, f"{rel} 有裸 SECS 未标尺子：{ln[:80]}"


def test_secs_next_to_campplus_must_be_a_denial():
    """★ 反向守卫（真洞）：`SECS … CAM++` 同句出现时，必须在否定/更正语境里。

    不写这条，把标题改回 2026-10-11 之前那句「（CAM++ 声纹，SECS 余弦）」**守卫不会红** ——
    因为 CAM++ 在场。而那句恰恰是本次要禁的原文。见速查表第 134 条（反向守卫要测真实作用）。
    """
    rel = "docs/端到端实测-音色保存与录课视频.md"
    for ln in _text(rel).splitlines():
        if not (_SECS_RE.search(ln) and _CAM_RE.search(ln)):
            continue
        assert any(w in ln for w in NEGATION_WORDS), (
            f"{rel} 把 SECS 与 CAM++ 写在同一句却没有否定/更正语境（就是那次错标的形状）：{ln[:90]}"
        )


def test_denial_guard_actually_fires_on_the_original_mislabel():
    """自我核验：把错标原文喂给上一条的规则，**必须**判定为违规。

    这条防的是"守卫恒真" —— 上一行那个正则若写得松到什么都放行，
    只有拿历史原文回灌才能发现。
    """
    old_title = "## 音色相似度客观验证（CAM++ 声纹，SECS 余弦）"
    assert _SECS_RE.search(old_title) and _CAM_RE.search(old_title)
    assert not any(w in old_title for w in NEGATION_WORDS), (
        "错标原文被判合规 —— 反向守卫失效，这条测试要修的是规则本身"
    )

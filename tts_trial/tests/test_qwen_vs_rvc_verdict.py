# -*- coding: utf-8 -*-
"""从**归档的原始分**重放「千问微调 vs RVC」裁决 —— 拿证据当回归测试。

为什么要有这个文件（2026-10-14）
--------------------------------
`docs/交接-AI必读.md` §5（「用千问微调做声音克隆」一行）对外说了三件事：

  ① RVC 的音色像度**逐句 3/3** 高于千问微调，且跨 seed 区间**不重叠**；
  ② 千问微调相对未微调的增益 **+0.0024**，落在它自己的波动内（等于没学到音色）；
  ③ 自然度（NatScore）这一轴**不可用** —— 真人参考音排在所有合成之下。

这三条都写在**文档**里，而文档不会自己报错。所以这里把裁决**从
`m2_server/tests/data/dachuang-2026-10/千问微调vsRVC-裁决/scores.json` 重算一遍**：
数字被重新生成/覆盖时，测试立刻红，而不是让文档悄悄过期。

⚠️ 这是「证据重放」，不是「重新测量」—— 它不跑模型、不 import torch，
   只在已经落盘的那批分数上重算判据（与 `m2_server/tests/test_voice_qc_emb_judgement.py` 同一路数）。
"""
import json
import os
import sys

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: 2026-10-27：`docs/大创申报-2026-10/` 里能跑的东西搬成测试夹具 `m2_server/tests/data/dachuang-2026-10/`
#:（申报材料本体另存 `_trash_大创-2026-10/`）—— 本文件是 4 个消费方之一，别改回 `docs/`。
_EVIDENCE = os.path.join(
    _ROOT, "m2_server", "tests", "data", "dachuang-2026-10", "千问微调vsRVC-裁决", "scores.json"
)


@pytest.fixture(scope="module")
def scores() -> dict:
    if not os.path.exists(_EVIDENCE):
        pytest.fail(f"裁决证据缺失：{_EVIDENCE}（它应随仓库一起提交）")
    with open(_EVIDENCE, encoding="utf-8") as f:
        return json.load(f)


def _secs(scores, key) -> float:
    v = scores[key]["secs"]
    assert v is not None, f"{key} 的 secs 未测：{scores[key].get('score_error')}"
    return float(v)


def _nats(scores, key) -> float:
    v = scores[key]["nats"]
    assert v is not None, f"{key} 的 nats 未测：{scores[key].get('score_error')}"
    return float(v)


class TestTimbreAxisIsUsable:
    """① 音色轴：RVC 赢，且三重稳健。"""

    def test_rvc_beats_qwen_ft_on_every_sentence(self, scores):
        """逐句 3/3 —— 均值赢可能被某一句拉出来，逐句赢才算。"""
        for seed in ("rvc_s1234", "rvc_s4321"):
            for i in (1, 2, 3):
                rvc = _secs(scores, f"{seed}_{i}")
                ft = _secs(scores, f"qwen_ft_{i}")
                assert rvc > ft, f"seed={seed} 第{i}句：RVC {rvc} 未高于微调 {ft}"

    def test_cross_seed_interval_does_not_overlap(self, scores):
        """跨 seed 的 RVC 区间与微调区间不重叠 ⇒ 才算「确定优势」（犯错指南 #167）。"""
        rvc = [
            np.mean([_secs(scores, f"rvc_s{s}_{i}") for i in (1, 2, 3)])
            for s in (1234, 4321)
        ]
        ft = np.mean([_secs(scores, f"qwen_ft_{i}") for i in (1, 2, 3)])
        assert min(rvc) > ft, f"区间重叠：RVC {min(rvc):.4f}~{max(rvc):.4f} vs 微调 {ft:.4f}"

    def test_qwen_ft_gain_over_base_is_within_noise(self, scores):
        """② 千问微调相对未微调的增益小于自身逐条波动 ⇒ 判「无可测增益」。"""
        ft = np.array([_secs(scores, f"qwen_ft_{i}") for i in (1, 2, 3)])
        base = np.array([_secs(scores, f"base_{i}") for i in (1, 2, 3)])
        gain = float(ft.mean() - base.mean())
        spread = float(np.std(ft, ddof=1))
        assert abs(gain) < 0.01, f"微调增益已超出「不可测」量级：{gain:+.4f}"
        assert abs(gain) < spread, "增益应小于自身波动（否则本轮结论要重写）"

    def test_rvc_output_is_more_stable_than_ft(self, scores):
        """RVC 逐条标准差应明显小于微调（3 倍量级）⇒ 输出更稳。"""
        rvc = np.array([_secs(scores, f"rvc_s1234_{i}") for i in (1, 2, 3)])
        ft = np.array([_secs(scores, f"qwen_ft_{i}") for i in (1, 2, 3)])
        assert np.std(rvc, ddof=1) * 2 < np.std(ft, ddof=1)

    def test_ceiling_is_above_every_arm(self, scores):
        """音色上限必须最高，否则口径错了（比如参考音指错）。"""
        ceiling = min(_secs(scores, "ceiling_1234"), _secs(scores, "ceiling_4321"))
        for k in list(scores):
            if k.startswith(("base_", "qwen_ft_", "rvc_")) and k != "ref":
                assert ceiling > _secs(scores, k), f"{k} 高于音色上限，口径可疑"

    def test_reference_scores_against_itself_is_one(self, scores):
        """参考音与自己的相似度必须是 1.0 —— 打分链路自检。"""
        assert abs(_secs(scores, "ref") - 1.0) < 1e-6


class TestNaturalnessAxisIsUnusable:
    """③ 自然度轴：尺子把真人排在合成之下 ⇒ 本批不允许用它下结论。"""

    def test_real_reference_scores_below_every_synthetic_arm(self, scores):
        """真人参考音的 nats 低于**所有**合成样本 —— 正是它不可用的直接证据。"""
        ref = _nats(scores, "ref")
        synth = [
            _nats(scores, k)
            for k in scores
            if k.startswith(("base_", "qwen_ft_", "rvc_"))
        ]
        assert synth, "没有合成臂可比"
        assert ref < min(synth), f"真人 {ref} 未低于合成最低 {min(synth)} —— 结论需重核"

    def test_ceiling_also_below_synthetic(self, scores):
        """连「真人原音 → RVC」的天花板也被排在合成之下 ⇒ 不是单点噪声。"""
        ceiling = min(_nats(scores, "ceiling_1234"), _nats(scores, "ceiling_4321"))
        base = max(_nats(scores, f"base_{i}") for i in (1, 2, 3))
        assert ceiling < base

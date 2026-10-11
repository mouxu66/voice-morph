"""双空间声纹实验脚本单测（2026-10-11）。

被测：`tools/voicebank_dual_embed.py` 的纯函数部分。守的是：

1. 配对口径：同人 = 自己那一档参考音；异人 = **同档**的其他说话人。
   异人配对若用了别的档位，时长这个自变量就混进"异人"分布里了。
2. AUC 方向：完美分离 = 1.0、纯噪声 = 0.5、反向 = 0.0 —— 方向写反 = 整份结论反掉。
3. EER：完美分离 ≈ 0、同一分布 ≈ 0.5。
4. d′：分母为 0（两组都没方差）不许编数字 → None。
5. 天花板统计（top_frac）：只数 >= 阈值的同人分数，异人分布不掺进来。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
import voicebank_dual_embed as vde  # noqa: E402


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF")
    return p


def _layout(tmp_path: Path, speakers=("A", "B", "C"), durs=("3s", "10s"), conds=("same", "cross")):
    for sp in speakers:
        for dur in durs:
            _touch(tmp_path / "refs" / sp / f"{dur}.wav")
            for cond in conds:
                _touch(tmp_path / "samples" / sp / dur / f"{cond}.wav")
    return vde.collect_layout(tmp_path)


def test_pairs_same_is_own_ref_and_cross_is_same_duration(tmp_path):
    layout = _layout(tmp_path)  # 3 人 × 2 档 × 2 条件
    same, cross = vde.build_pairs(layout, tmp_path)
    assert len(same) == 12
    assert len(cross) == 24  # 每格 2 个"别的说话人"
    for p in same:
        assert p["ref_id"].split("/")[0] == p["speaker"]
        assert p["ref_rel"].startswith(f"refs/{p['speaker']}/")
    for p in cross:
        assert p["ref_id"].split("/")[1] == p["dur"]      # 异人必须同档
        assert p["ref_id"].split("/")[0] != p["speaker"]  # 异人必须是别人


def test_auc_direction_and_ties():
    assert vde.auc([0.9, 0.95], [0.1, 0.2]) == 1.0
    assert vde.auc([0.1, 0.2], [0.9, 0.95]) == 0.0
    assert vde.auc([0.5, 0.5], [0.5, 0.5]) == pytest.approx(0.5)
    assert vde.auc([], [0.1]) is None


def test_eer_perfect_and_chance():
    assert vde.eer([0.9, 0.95], [0.1, 0.2]) == 0.0
    assert vde.eer([0.3, 0.6], [0.3, 0.6]) == pytest.approx(0.5, abs=0.06)
    assert vde.eer([], [0.1]) is None


def test_dprime_zero_variance_is_none():
    assert vde.dprime([1.0, 1.0], [0.0, 0.0]) is None
    assert vde.dprime([1.0], [0.0, 0.1]) is None  # 单样本没有方差
    assert vde.dprime([1.0, 1.2], [0.0, 0.2]) > 0


def test_summarize_top_frac_counts_same_only():
    s = vde.summarize([0.99, 0.985, 0.5], [0.2, 0.1])
    assert s["same"]["n"] == 3 and s["cross"]["n"] == 2
    assert s["same"]["top_frac"] == pytest.approx(0.6667)  # 汇总按 4 位小数落盘
    assert s["cross"].get("top_frac") is None  # 异人分布不算天花板占比
    assert s["auc"] == 1.0 and s["eer"] == 0.0


def test_cos_rejects_zero_vector():
    assert vde.cos([1, 0], [2, 0]) == pytest.approx(1.0)
    assert vde.cos([1, 0], [0, 3]) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        vde.cos([0, 0], [1, 0])

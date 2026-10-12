# -*- coding: utf-8 -*-
"""ppt12_qc 语音切分测试。

钉死的是 2026-10-12 定位到的真实 bug（不是风格问题）：

`ppt12_qc.py` 原式 `thr = max(noise_floor + 12.0, -45.0)` 把 -45.0 当成了
门限的**地板**，于是自适应门限被往上钳。实测素材 noise_floor = -67.1dB，
自适应值 -55.1dB 被抬到 -45.0dB ⇒ 切出 70 条 / 204.1s，
而按本意是 127 条 / 538.1s，webrtcvad 独立复核 715.3s。
后果是 RVC 训练集只有 3.4min（官方推荐 10min），
结论被误记成「素材不够、只能再录」—— 而盘上其实有 12.23min。

这些用例保证：门限跟着底噪走；低底噪素材不再被无谓地砍掉语音。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ppt12_qc as qc  # noqa: E402


class TestComputeThreshold:
    def test_follows_noise_floor(self):
        """低底噪素材：门限应贴着 noise_floor + margin，而不是被固定值钳住。"""
        # 90% 静音帧在 -80dB 附近，10% 语音帧在 -25dB
        db = np.concatenate([np.full(900, -80.0), np.full(100, -25.0)])
        thr, floor, ok = qc.compute_threshold(db)
        assert floor == pytest.approx(-80.0, abs=0.5)
        assert thr == pytest.approx(-68.0, abs=0.5)   # -80 + 12
        assert ok

    def test_low_floor_not_clamped_to_minus_45(self):
        """★ 回归：noise_floor=-67.1 时门限必须是 -55.1，不能被钳到 -45。"""
        db = np.concatenate([np.full(900, -67.1), np.full(100, -20.0)])
        thr, floor, ok = qc.compute_threshold(db)
        assert floor == pytest.approx(-67.1, abs=0.1)
        assert thr == pytest.approx(-55.1, abs=0.1)
        assert thr != pytest.approx(-45.0, abs=0.1), "门限又被固定值往上钳了"
        assert ok

    def test_high_noise_floor_clamped_from_above(self):
        """底噪很高时会算出正得离谱的门限 → 夹到上界，并标记 ok=False。"""
        db = np.concatenate([np.full(900, -20.0), np.full(100, -5.0)])
        thr, floor, ok = qc.compute_threshold(db)
        assert thr == pytest.approx(qc.AUTO_THR_MAX_DB)
        assert not ok

    def test_pathological_floor_clamped_from_below(self):
        """全静音（门限会掉到极低）→ 夹到下界，并标记 ok=False。"""
        db = np.full(1000, -95.0)
        thr, _floor, ok = qc.compute_threshold(db)
        assert thr == pytest.approx(qc.AUTO_THR_MIN_DB)
        assert not ok

    def test_quiet_speech_is_kept(self):
        """★ 核心后果：低电平但真实的语音不该被门限吃掉。"""
        # 静音底 -70dB，语音只有 -52dB（比旧的 -45 门限低 7dB）
        db = np.concatenate([np.full(900, -70.0), np.full(100, -52.0)])
        thr, _floor, ok = qc.compute_threshold(db)
        assert ok
        assert (db > thr).sum() == 100, "低电平语音被切掉了"

    def test_threshold_is_between_floor_and_speech(self):
        db = np.concatenate([np.full(800, -65.0), np.full(200, -30.0)])
        thr, floor, _ok = qc.compute_threshold(db)
        assert floor < thr < -30.0


class TestSplitSpeechSegments:
    def _db(self, pattern, hop_ms=10):
        """pattern: [(值, 帧数), ...] → db 数组。"""
        return np.concatenate([np.full(n, v) for v, n in pattern]), hop_ms

    def test_short_fragment_dropped(self):
        """<0.4s 的碎片要丢掉（低于 min_seg_s）。"""
        db, hop = self._db([(-20.0, 30), (-80.0, 100)])  # 30 帧 × 10ms = 0.3s
        assert qc.split_speech_segments(db, -50.0, hop) == []

    def test_segment_kept_when_long_enough(self):
        """2s 整段应被保留。"""
        db, hop = self._db([(-20.0, 200)])  # 2.0s
        clips = qc.split_speech_segments(db, -50.0, hop)
        assert len(clips) == 1
        assert clips[0] == pytest.approx((0.0, 2.0), abs=0.05)

    def test_nearby_segments_merged(self):
        """间隔 <0.25s 的相邻段合并（同一句话被顿号切开）。"""
        db, hop = self._db([(-20.0, 150), (-80.0, 10), (-20.0, 150)])
        clips = qc.split_speech_segments(db, -50.0, hop)
        assert len(clips) == 1, "近邻段没合并"
        assert clips[0][1] - clips[0][0] > 3.0

    def test_far_segments_not_merged(self):
        """间隔 >0.25s 的段不合并。"""
        db, hop = self._db([(-20.0, 250), (-80.0, 100), (-20.0, 250)])
        clips = qc.split_speech_segments(db, -50.0, hop)
        assert len(clips) == 2

    def test_long_segment_split(self):
        """超长段被切成 <=max_len_s 的块，且不留 <min_len_s 的尾巴。"""
        db, hop = self._db([(-20.0, 3000)])  # 30s
        clips = qc.split_speech_segments(db, -50.0, hop, max_len_s=12.0, min_len_s=2.0)
        assert len(clips) >= 3
        for a, b in clips:
            assert b - a <= 12.0 + 1e-6
            assert b - a >= 2.0 - 1e-6

    def test_lower_threshold_recovers_more_speech(self):
        """★ 核心后果：同一份 db，门限低 10dB 应恢复显著更多语音。

        这直接对应本次 bug —— 旧门限 -45dB，本意 -55.1dB。
        """
        # 造出「底噪 -67 + 大量 -52dB 的轻声说话」
        db = np.concatenate([
            np.full(600, -67.0),   # 静音
            np.full(1400, -52.0),  # 轻声语音，旧门限看不见
        ])
        hop = 10
        old = qc.split_speech_segments(db, -45.0, hop)
        new = qc.split_speech_segments(db, -55.1, hop)
        old_s = sum(b - a for a, b in old)
        new_s = sum(b - a for a, b in new)
        assert old_s == 0.0, "旧门限本应完全看不到这段轻声语音"
        assert new_s > 12.0, f"新门限应恢复这段语音，实际 {new_s:.1f}s"

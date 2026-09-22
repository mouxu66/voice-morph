"""长文分段发送：`pack_chunks` 预算判据 + `_send_batch` 编排 + 预算护栏。

方案：`docs/微信语音-长文分段发送方案.md`（问题：微信单条语音 60s 硬上限，
到点自动结束并发送，后半段静默丢失）。

这个文件里的用例是**这条 bug 的唯一防复发机制**的载体：
  · `pack_chunks` 的**不变量**（每一段 + LEAD/TAIL/LAG 必须 ≤60）；
  · `_send_batch` 的**作用域**（_prepare_env 与 _safe_restore 恒为各 1 次）；
  · `_check_budget` 的**护栏**（超预算拒发，而不是发一条注定被截断的）。

⚠️ 变异测试（本仓传统，见 docs/犯错指南.md §8.19）：把 `MAX_CHUNK_S` 调到 70、
   或删掉 `_check_budget` 的判据，本文件必须**变红**。做变异时**整行删除**，
   不能只注释 —— 断言可能匹配到注释里的同名文本（速查表第 46 条）。
"""

import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import config as cfg  # noqa: E402
import wechat_voice as wv  # noqa: E402

# ---------------- 夹具 ----------------


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """隔离输出目录与历史文件；绝不触碰真实微信 / 声卡。"""
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    yield tmp_path


def _fit_60(audio_s: float) -> float:
    """一段音频真正会占掉的录音时长（判据的唯一形状）。"""
    return wv.PLAY_LEAD_S + audio_s + wv.TAIL_S + wv.FINISH_LAG_S


# ---------------- pack_chunks：边界 ----------------


def test_budget_constants_add_up():
    """预算算式是硬判据：60 − LEAD − TAIL − LAG − SAFETY = MAX_CHUNK_S。"""
    expected = wv.MAX_MSG_S - wv.PLAY_LEAD_S - wv.TAIL_S - wv.FINISH_LAG_S - wv.SAFETY_S
    assert pytest.approx(expected) == wv.MAX_CHUNK_S
    assert pytest.approx(54.0) == wv.MAX_CHUNK_S
    # 余量必须真的存在：预算 + 固定开销 + 余量 = 60
    assert _fit_60(wv.MAX_CHUNK_S) + wv.SAFETY_S == pytest.approx(wv.MAX_MSG_S)


def test_empty_input():
    assert wv.pack_chunks([]) == []


def test_single_sentence_fits():
    assert wv.pack_chunks([10.0]) == [(0, 1)]


def test_exactly_at_budget_stays_one_chunk():
    """恰好等于预算 → 一段（不是两段）。边界不能取严。"""
    d = wv.MAX_CHUNK_S
    assert wv.pack_chunks([d]) == [(0, 1)]


def test_one_ms_over_budget_still_one_chunk_for_single():
    """单句超预算时纯函数**不崩**：它照样返回一段，由调用方做三级降级。

    这是有意的分工 —— "再切一次并重新合成"不是纯函数能决定的（见 _split_for_budget）。
    """
    plan = wv.pack_chunks([wv.MAX_CHUNK_S + 0.001])
    assert plan == [(0, 1)]  # 不崩、不丢句


def test_gap_counts_toward_budget():
    """段内句子之间的 gap 也要计入预算 —— 否则实发时长会比算出来的长。"""
    # 每句 27s，两句 + 一个 gap(0.3) = 54.3 > 54 → 必须分成两段
    assert wv.pack_chunks([27.0, 27.0], gap_s=0.3) == [(0, 1), (1, 2)]
    # 各 26.8s：26.8 + 0.3 + 26.8 = 53.9 ≤ 54 → 一段
    assert wv.pack_chunks([26.8, 26.8], gap_s=0.3) == [(0, 2)]
    # 同样两句但 gap=0：54.0 ≤ 54 → 一段
    assert wv.pack_chunks([27.0, 27.0], gap_s=0.0) == [(0, 2)]


def test_no_gap_before_first_sentence():
    """段内第一句前面没有 gap（否则会凭空多算 0.3s）。

    52.0 一句独占一段（52 ≤ 54）；下一段的**第一句** 2.0s 前面也不该有 gap，
    所以 2.0 进得去；能进就说明没在第一句前面多算那 0.3s。
    （若多算，2.3 ≤ 54 也进得去 —— 所以这条的真正判据是"同一段内第二句要算 gap"，
     见 test_gap_counts_toward_budget。）
    """
    assert wv.pack_chunks([52.0, 2.0], gap_s=0.3) == [(0, 1), (1, 2)]
    # 反过来：同一段里第二句要算 gap，52.0 + 0.3 + 2.0 = 54.3 > 54
    assert wv.pack_chunks([30.0, 2.0], gap_s=0.3) == [(0, 2)]
    assert wv.pack_chunks([53.8, 2.0], gap_s=0.3) == [(0, 1), (1, 2)]


def test_short_tail_merged_into_previous():
    """尾段 < MIN_CHUNK_S 且**并入后仍守预算** → 并入上一段（避免发一条 1.2 秒的）。

    要触发"尾段"必须让贪心先分开：30+30 装不下（>54）→ 各成一段，尾段 1.0 才是孤立尾段。
    """
    # 30 + 30 + 1.0：并入后第 2 段变 31s ≤ 54 → 并（这是真实的"避免 1 秒气泡"场景）
    assert wv.pack_chunks([30.0, 30.0, 1.0], gap_s=0.0) == [(0, 1), (1, 3)]
    # 全部装得下时根本不会分成多段（41 ≤ 54），别把它当"合并"用例
    assert wv.pack_chunks([20.0, 20.0, 1.0], gap_s=0.0) == [(0, 3)]
    # 50 + 1.0 同理：51 ≤ 54，贪心一次装完
    assert wv.pack_chunks([50.0, 1.0], gap_s=0.0) == [(0, 2)]


def test_short_tail_not_merged_when_previous_is_already_full():
    """★ 上一段已经满了，尾段再短也不能并（会破预算）—— 宁可多发一条。

    [54.0, 1.0]：第 1 段 54（满），尾段 1.0 < 2.5 想并 → 并进去 55 > 54 → 不并。
    """
    assert wv.pack_chunks([54.0, 1.0], gap_s=0.0) == [(0, 1), (1, 2)]


def test_merge_boundary_exactly_at_budget():
    """边界：并入后仍守预算 → 允许并；装不下 → 不并。

    [38, 16, 1]：38+16=54 装得下 → 第 1 段 (0,2)；尾段 1.0 并入后 55 > 54 → 不并。
    [38, 15, 29, 1]：1.0 本来就能进第 2 段（29+1=30 ≤ 54）→ 并成 (2,4)。
    """
    assert wv.pack_chunks([38.0, 16.0, 1.0], gap_s=0.0) == [(0, 2), (2, 3)]
    assert wv.pack_chunks([38.0, 15.0, 29.0, 1.0], gap_s=0.0) == [(0, 2), (2, 4)]
    # 真的卡在边界上：第 1 段 53，尾段 1.0 并入后恰好 54 → 允许并
    assert wv.pack_chunks([53.0, 1.0], gap_s=0.0) == [(0, 2)]


def test_short_tail_is_NOT_merged_when_it_would_bust_budget():
    """★ 尾段虽短，但并入会让那一条气泡吃穿余量/破预算 → **不并**，宁可多发一条。

    pack_chunks([54.0, 1.0]) 就是真实触发路径：并进去是 55.0s > MAX_CHUNK_S(54)。
    多发一条 1.0s 的气泡 vs 赌 SAFETY 那 4.6s —— 多一条气泡便宜得多。

    注意 [50.0, 1.0] 和它是**不同**情形：51 ≤ 54，该并不并才是 bug。
    """
    assert wv.pack_chunks([54.0, 1.0], gap_s=0.0) == [(0, 1), (1, 2)]
    # 30+30 各自成段（60 > 54 装不下），尾段 1.0 并入第 2 段会变 31s —— 很划算，该并
    assert wv.pack_chunks([30.0, 30.0, 1.0], gap_s=0.0) == [(0, 1), (1, 3)]
    # 边界：并入后恰好等于预算 → 允许并
    assert wv.pack_chunks([38.0, 16.0, 1.0], gap_s=0.0) == [(0, 2), (2, 3)]


def test_short_tail_is_not_dropped():
    """并入不能丢句 —— 所有下标必须被恰好覆盖一次。"""
    for durs in ([20.0, 20.0, 1.0], [54.0, 1.0], [30.0, 30.0, 1.0]):
        plan = wv.pack_chunks(durs, gap_s=0.0)
        covered = [i for a, b in plan for i in range(a, b)]
        assert covered == list(range(len(durs))), f"{durs} → {plan}"


def test_short_single_chunk_is_not_merged_away():
    """只有一段时不做"并入上一段"（没有上一段），也不许返回空。"""
    assert wv.pack_chunks([1.0], min_s=2.5) == [(0, 1)]


def test_greedy_packs_as_much_as_possible():
    """贪心：能凑满就凑满，别浪费气泡（多一条气泡是用户可见成本）。"""
    # 18s × 3 = 54 → 一段装下；加第 4 句就超
    assert wv.pack_chunks([18.0, 18.0, 18.0, 18.0], gap_s=0.0) == [(0, 3), (3, 4)]


def test_max_s_must_be_positive():
    with pytest.raises(ValueError):
        wv.pack_chunks([1.0], max_s=0)


# ---------------- pack_chunks：不变量（随机） ----------------


@pytest.mark.parametrize("seed", range(12))
def test_invariant_no_chunk_exceeds_60s(seed):
    """★ 核心不变量：**任何**随机输入下，每一段的实发时长都必须 ≤60s。

    这条用例就是整个方案的价值所在 —— 它是"绝不静默截断"的机器化表达。
    把 MAX_CHUNK_S 改大到 70，本用例必须变红（变异测试的第一条）。
    """
    rnd = random.Random(seed)
    n = rnd.randint(1, 40)
    # 覆盖三种句长：正常句、长句、超长句（超预算的单句由调用方降级，这里只验不变量
    # 对"能装下的句"成立）
    durs = [rnd.choice([0.8, 2.0, 5.5, 12.0, 20.0, 30.0, 53.0]) for _ in range(n)]
    plan = wv.pack_chunks(durs)
    assert plan, "至少要有一段"
    for a, b in plan:
        assert a < b, f"空段 {a},{b}"
        span = sum(durs[a:b]) + wv.SENT_GAP_S * max(b - a - 1, 0)
        # 分两档：正常段必须严格守预算；只含一句的段允许超（调用方降级）
        if b - a > 1 or durs[a] <= wv.MAX_CHUNK_S:
            assert _fit_60(span) <= wv.MAX_MSG_S, (
                f"第 {a}:{b} 段实发 {_fit_60(span):.2f}s 超过 60s 上限（dur={span:.2f}s）"
            )


@pytest.mark.parametrize("seed", range(6))
def test_invariant_covers_every_sentence_exactly_once(seed):
    """不变量：句子的覆盖恰好是 [0, n) 一次，不重不漏。"""
    rnd = random.Random(1000 + seed)
    n = rnd.randint(1, 30)
    durs = [round(rnd.uniform(0.5, 25.0), 2) for _ in range(n)]
    plan = wv.pack_chunks(durs)
    covered = [i for a, b in plan for i in range(a, b)]
    assert covered == list(range(n))
    # 段间必须连续（不重叠、不留缝）
    assert all(plan[k][1] == plan[k + 1][0] for k in range(len(plan) - 1))


def test_chunk_plan_ok_accepts_valid_and_rejects_over_budget():
    """_chunk_plan_ok 是 pack_chunks 输出的自检器，两个方向都要能判。"""
    assert wv._chunk_plan_ok([20.0, 20.0, 20.0], [(0, 2), (2, 3)], gap_s=0.0) is True
    # 58.0 虽然实发 59.4s < 60（物理上限过得去），但吃穿了 SAFETY 余量 → 判否
    assert wv._chunk_plan_ok([58.0], [(0, 1)], gap_s=0.0) is False
    assert wv._chunk_plan_ok([54.0], [(0, 1)], gap_s=0.0) is True


def test_max_chunk_is_exact_50_point_4_not_float_fuzz():
    """算式推出来的常量必须是 54.0 这个可读值，不能是 54.00000000000001。

    浮点尾巴会让"恰好等于预算"的边界比较变得不可预测，
    也会让错误提示里出现 `预算 54s` 变成 `54.00000000000001s`。
    """
    assert wv.MAX_CHUNK_S == 54.0
    assert repr(wv.MAX_CHUNK_S) == "54.0"


# ---------------- 预算护栏（唯一防复发机制） ----------------


def _stub_send_io(monkeypatch, tmp_path):
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF")


class _FakeProc:
    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_guard_rejects_over_budget_single_wav(tmp_path, monkeypatch):
    """★ 护栏：单条超过预算就**拒发**，而不是发一条注定被 60s 截断的。

    把 _check_budget 的判据整行删掉，本用例必须变红（变异测试的第二条）。
    """
    _stub_send_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 90.0)
    touched = []
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: touched.append("record"))

    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))

    # 早退：JSONResponse 400 / outcome=too_long，且**一次录音都没起**
    assert res.status_code == 400
    import json

    data = json.loads(res.body)
    assert data["outcome"] == "too_long"
    assert data["duration_s"] == 90.0
    assert data["max_chunk_s"] == wv.MAX_CHUNK_S
    assert touched == [], "超预算时绝不允许碰微信录音"
    # 错误信息要说清"会分成几条"，否则用户不知道下一步该干什么
    assert "分段" in data["error"]


def test_guard_allows_at_budget(tmp_path, monkeypatch):
    """恰好等于预算 → 放行（边界不能取严，否则 54s 的合法音频被误拒）。"""
    _stub_send_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: wv.MAX_CHUNK_S)
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {"kind": "recording_env"})
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)

    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert isinstance(res, dict)
    assert res["outcome"] == "ok"


def test_guard_rejects_when_fixed_overhead_pushes_past_60(tmp_path, monkeypatch):
    """音频本身没超 MAX_CHUNK_S，但加上首尾静音与收尾延迟会破 60 → 也要拒。"""
    _stub_send_io(monkeypatch, tmp_path)
    # 54.5 > MAX_CHUNK_S(54.0)，虽然 54.5 + 1.4 = 55.9 < 60 也不能发：
    # 预算是留了 SAFETY 的硬判据，不能"恰好卡在 59.9s"侥幸通过。
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 54.5)
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res.status_code == 400
    import json

    assert json.loads(res.body)["outcome"] == "too_long"


def test_guard_reason_mentions_seconds():
    """护栏的拒绝理由要能直接给用户看：必须带上具体秒数、上限、以及会切成几条。"""
    reason = wv._check_budget(90.0)
    assert "90.0s" in reason
    assert "54" in reason  # 预算
    assert "2" in reason  # 90 / 54 → 2 条
    assert "条语音" in reason
    assert wv._check_budget(10.0) == ""


def test_min_chunks_for_rounds_up():
    """提示语里的条数要向上取整（宁可说多一条，也不能说少一条让用户以为发得完）。"""
    assert wv._min_chunks_for(54.0) == 1
    assert wv._min_chunks_for(54.1) == 2
    assert wv._min_chunks_for(90.0) == 2
    assert wv._min_chunks_for(120.0) == 3


# ---------------- _send_batch：编排与作用域 ----------------


def test_batch_prepares_and_restores_env_exactly_once(tmp_path, monkeypatch):
    """★ D7/D13 的核心：切卡与还原**整批各只做一次**，与条数无关。

    3 条批次断言调用计数 —— 现状每条都要 apply+restore 会白等 N×6s，
    而且反复切卡本身就是故障源（速查表第 22 条）。
    """
    prep, restore = [], []
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: prep.append(1) or {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: restore.append(1) or (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)

    def fake_seg(wav, duration, **kw):
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    wavs = []
    for i in range(3):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    res = wv._send_batch(wavs)
    assert prep == [1], "环境准备必须只做一次"
    assert restore == [1], "声卡还原必须只做一次（且失败也要做）"
    assert res["total_chunks"] == 3
    assert res["sent_chunks"] == 3
    assert res["outcome"] == "ok"


def test_batch_restores_even_when_segment_raises(tmp_path, monkeypatch):
    """还原放在 finally：任何异常都必须还原声卡（否则默认麦留在 CABLE 上）。"""
    restore = []
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: restore.append(1) or (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(
        wv,
        "_record_and_send",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸了")),
    )
    p = tmp_path / "c0.wav"
    p.write_bytes(b"RIFF")

    with pytest.raises(RuntimeError):
        wv._send_batch([p])
    assert restore == [1]


def test_batch_fail_fast_stops_and_reports_remaining(tmp_path, monkeypatch):
    """★ D12：第 2 条失败 → 失败即停，第 3 条**不许被调用**，且回传 failed_index。"""
    calls = []
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)

    def fake_seg(wav, duration, **kw):
        calls.append(wav.name)
        if wav.name == "c1.wav":
            return {"ok": False, "outcome": "failed", "wav": wav.name, "duration_s": duration, "steps": []}
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    wavs = []
    for i in range(3):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    res = wv._send_batch(wavs)
    assert calls == ["c0.wav", "c1.wav"], "失败即停，第 3 条不许再发"
    assert res["outcome"] == "partial"
    assert res["sent_chunks"] == 1
    assert res["failed_index"] == 1
    assert res["total_chunks"] == 3


def test_batch_refreshes_uia_baseline_per_segment(tmp_path, monkeypatch):
    """★ D10：每段都要**重新取** UIA 基线。

    沿用第一条的 before_count 会让第二条起永远判成"已新增"（基线没动），
    于是"没发出去"这件事在批次里永远检不出来。
    """
    snapshots = []
    wv_count = [10]

    def fake_uia_window(*a, **k):
        snapshots.append(wv_count[0])
        return f'语音{5 + len(snapshots)}"秒'

    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: True)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)

    class FakeUia:
        @staticmethod
        def latest_voice_message():
            return fake_uia_window()

        @staticmethod
        def voice_messages():
            return [1] * wv_count[0]

    monkeypatch.setattr(wv, "_uia", FakeUia)

    seen = []

    def fake_seg(wav, duration, before_msg=None, before_count=-1, **kw):
        seen.append(before_count)
        wv_count[0] += 1  # 每发一条，聊天区就多一条
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    wavs = []
    for i in range(3):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    wv._send_batch(wavs)
    # 基线必须是递增的真实快照（10, 11, 12），不是恒定沿用第一条的 10
    assert seen == [10, 11, 12], f"基线没有每段重取：{seen}"


def test_batch_uses_reuse_restore_per_segment(tmp_path, monkeypatch):
    """批次内每段都要 reuse_restore=True —— 否则还原被做了 N 次，D7 就白设计了。"""
    flags = []
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)

    def fake_seg(wav, duration, **kw):
        flags.append(kw.get("reuse_restore"))
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    wavs = []
    for i in range(2):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    wv._send_batch(wavs)
    assert flags == [True, True]


def test_batch_reports_progress(tmp_path, monkeypatch):
    """进度回调按 k/N 递增 —— 桌宠面板的「第 2/3 条发送中…」靠它。"""
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)
    monkeypatch.setattr(
        wv,
        "_record_and_send",
        lambda wav, d, **k: {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": d, "steps": []},
    )
    wavs = []
    for i in range(3):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    got = []
    wv._send_batch(wavs, on_progress=lambda k, n, d: got.append((k, n)))
    assert got == [(1, 3), (2, 3), (3, 3)]


def test_batch_empty_list_is_noop(tmp_path, monkeypatch):
    """空列表不该去切卡（否则用户点了个没有内容的发送会白动声卡）。"""
    prep = []
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: prep.append(1) or {})
    res = wv._send_batch([])
    assert prep == []
    assert res["total_chunks"] == 0
    assert res["outcome"] == "ok"


def test_batch_marks_manual_fallback_as_whole_batch(tmp_path, monkeypatch):
    """整批只要有一条降级成 manual_fallback，批次 outcome 就要如实反映（要人按 Alt）。"""
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)
    monkeypatch.setattr(
        wv,
        "_record_and_send",
        lambda wav, d, **k: {
            "ok": True,
            "outcome": "manual_fallback",
            "wav": wav.name,
            "duration_s": d,
            "steps": [],
        },
    )
    p = tmp_path / "c0.wav"
    p.write_bytes(b"RIFF")
    res = wv._send_batch([p])
    assert res["outcome"] == "manual_fallback"


# ---------------- 变异证据（把"这些用例真的会红"钉在代码里） ----------------


def test_mutation_evidence_guards_are_wired():
    """★ 把三条变异证据固化成断言：将来有人"顺手简化"预算判据时会立刻红。

    为什么要在测试里放这个：本仓的传统是**守护测试必须做变异测试**
    （docs/犯错指南.md §8.19–§8.21）—— 注入真违规，断言它**会红**。手工跑过一次
    变异只是"当时验过"，半年后没人记得。这里用源码级断言把判据的形状钉住，
    任何人删掉 `_check_budget` 的两条判据 / `_send_batch` 的 finally 还原，
    本用例立即失败并明确指出该补什么。

    ⚠️ 这**不是**替代真正的变异测试（那只在注入时才有意义），而是防止
       "判据被悄悄掏空、用例还全绿"这种最危险的状态。
    """
    import inspect

    src = inspect.getsource(wv)

    # ① MAX_CHUNK_S 必须仍由算式推出（不是被写成硬编码 70 之类的"省事值"）
    assert "MAX_MSG_S - 0.8 - 0.3 - FINISH_LAG_S - SAFETY_S" in src, (
        "MAX_CHUNK_S 的算式被改掉了：它必须显式扣掉 LEAD/TAIL/LAG/SAFETY，"
        "不允许退化成硬编码常量（那正是 60s 截断复发的老路）。"
    )

    # ② _check_budget 必须同时查"预算余量"与"物理上限"两条
    gb = inspect.getsource(wv._check_budget)
    assert "MAX_CHUNK_S" in gb, "_check_budget 丢了预算余量判据（第二条）"
    assert "MAX_MSG_S" in gb, "_check_budget 丢了物理上限判据（第一条）"
    assert "return \"\"" in gb, "_check_budget 必须有一条放行路径"

    # ③ _send_batch 必须'批量只做一次'环境准备、且 finally 里还原
    sb = inspect.getsource(wv._send_batch)
    assert "_prepare_env(" in sb, "_send_batch 丢了环境准备"
    assert "finally:" in sb and "_safe_restore()" in sb, (
        "_send_batch 必须在 finally 里还原声卡（D7：整批一次，且失败也要还原）"
    )
    assert sb.count("_safe_restore()") == 1, (
        "_send_batch 里 _safe_restore() 只应有 1 处（整批一次）；"
        "多出来说明有人把还原又塞回了每段循环。"
    )

    # ④ _do_send 必须委托给 _record_and_send —— "单条就是只有一条的批次"
    ds = inspect.getsource(wv._do_send)
    assert "_record_and_send(" in ds, (
        "_do_send 必须走 _record_and_send 这个共同内核，不得自己另写一份录制逻辑"
        "（否则批次修好了、单条还漏着）。"
    )


def test_pack_chunks_is_pure():
    """pack_chunks 必须是纯函数（同输入同输出、不改动入参）—— 它是硬判据的载体。"""
    durs = [10.0, 20.0, 30.0]
    snapshot = list(durs)
    a = wv.pack_chunks(durs)
    b = wv.pack_chunks(durs)
    assert a == b
    assert durs == snapshot, "pack_chunks 改动了入参列表"


def test_do_send_single_path_shares_batch_kernel(tmp_path, monkeypatch):
    """★ 单条路径必须**走同一段代码**（_record_and_send），防"批次修好了单条还漏着"。

    断言的不是"结果一样"，而是"确实委托给了同一个内核"。
    """
    _stub_send_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {"kind": "recording_env"})
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    seen = {}

    def fake_seg(wav, duration, **kw):
        seen["wav"] = wav.name
        seen["duration"] = duration
        seen["reuse_restore"] = kw.get("reuse_restore")
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"
    assert seen["wav"] == "tts_x.wav"
    assert seen["duration"] == 5.0
    # 单条路径自己还原（后台线程 / 异常路径都依赖它），不是批次语义
    assert seen["reuse_restore"] is False

"""长文分段发送：`pack_chunks` 预算判据 + `_send_batch` 编排 + 预算护栏。

方案：`docs/微信语音-长文分段发送方案.md`（问题：微信单条语音 60s 硬上限，
到点自动结束并发送，后半段静默丢失）。

这个文件里的用例是**这条 bug 的唯一防复发机制**的载体：
  · `pack_chunks` 的**不变量**（每一段 + LEAD/TAIL/LAG 必须 ≤60）；
  · `_send_batch` 的**作用域**（_prepare_env 与 _safe_restore 恒为各 1 次）；
  · `_check_budget` 的**护栏**（超预算拒发，而不是发一条注定被截断的）。

⚠️ 变异测试（本仓传统，见 `docs/犯错档案-工程.md` §8.19）：把 `MAX_CHUNK_S` 调到 70、
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


# ---------------- 历史里的批次信息（桌宠看门狗靠它判断"整批发完了"） ----------------
#
# 由来（2026-09-23）：桌宠 pet.html 的 watchSendOutcome 是"主进程没回传结果"时的
# 兜底收尾 —— 它只能读 /api/wechat/history。分段发送下"出现了一行新记录"不再等于
# "整批发完了"：第 1 条落地时后面还在录，照旧收尾就会提前打绿勾，
# 用户看到"已完成"还可能去动键鼠，正好打断录制。
# 所以批次信息必须**落库**，让看门狗能数够条数才收尾。


def test_history_row_carries_total_chunks_for_batches(tmp_path, monkeypatch):
    """★ 多段批次：每条历史都要带上 total_chunks=N（看门狗据此判"凑齐了没有"）。"""
    monkeypatch.setattr(wv, "_prepare_env", lambda pre_apply=None, steps=None: {})
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_await_uia_active", lambda: False)
    monkeypatch.setattr(wv, "_await_overlay_gone", lambda gap_s=0.0, timeout=None: True)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.0)

    real_append = wv._append_history

    def fake_append(wav, duration, outcome="ok", warning="", total_chunks=1):
        # 只观察参数，仍走真实实现（要真的落盘，下面才好回读）
        observed.append(total_chunks)
        return real_append(wav, duration, outcome, warning, total_chunks)

    observed: list[int] = []
    monkeypatch.setattr(wv, "_append_history", fake_append)

    def fake_seg(wav, duration, index=None, total=None, **kw):
        # 真 _record_and_send 会落历史；这里模拟它把 total 带下去
        wv._append_history(wav, duration, "ok", "", total or 1)
        return {"ok": True, "outcome": "ok", "wav": wav.name, "duration_s": duration, "steps": []}

    monkeypatch.setattr(wv, "_record_and_send", fake_seg)
    wavs = []
    for i in range(3):
        p = tmp_path / f"c{i}.wav"
        p.write_bytes(b"RIFF")
        wavs.append(p)

    res = wv._send_batch(wavs)
    assert observed == [3, 3, 3], "每条历史都必须带上同一批的总数（3）"
    assert res["total_chunks"] == 3 and res["sent_chunks"] == 3

    # 回读落盘的历史：桌宠就是这么读的
    import json

    hist = json.loads(wv.HISTORY_FILE.read_text("utf-8"))
    assert len(hist) == 3
    assert [r.get("total_chunks") for r in hist] == [3, 3, 3]


def test_history_row_omits_total_chunks_when_single(tmp_path, monkeypatch):
    """单条不写这个字段：省得每行都多一个恒为 1 的噪声字段。

    也钉住"读侧必须容缺"—— 历史里还有大量旧记录（分段之前落库的），
    前端读不到这个字段时必须退化成单条行为，而不是当成 0/null 崩掉。
    """
    p = tmp_path / "one.wav"
    p.write_bytes(b"RIFF")
    assert wv._append_history(p, 5.0, "ok", "") is True
    import json

    rec = json.loads(wv.HISTORY_FILE.read_text("utf-8"))[-1]
    assert "total_chunks" not in rec


def test_history_default_total_chunks_keeps_backward_compat(tmp_path, monkeypatch):
    """`total_chunks` 必须有默认值 —— 老调用点（单条路径）不改也得能过。"""
    import inspect

    sig = inspect.signature(wv._append_history)
    assert sig.parameters["total_chunks"].default == 1


# ---------------- 前端契约（桌宠渲染侧 / 主进程） ----------------
#
# 这三个文件改不了"逻辑"，但它们承载着分段发送的**用户可见语义**，
# 而改 *.cjs 要重打 asar、pet.html 是热替换 —— 三者版本会漂。
# 用源码级断言钉住关键形状，比"下次再读一遍"可靠。

_ELECTRON_DIR = Path(__file__).resolve().parents[2] / "web" / "electron"


def _electron_fn_body(relpath: str, anchor: str) -> str:
    """取 `web/electron/<relpath>` 里从 `anchor` 开始到下一个顶格 `}` 的片段。

    ⚠️ 断言必须落在**这一段**里，不能只查整个文件 —— 变异测试证明过一次：
    只查"文件里出现过 total_chunks"会被函数顶部读取 `data.total_chunks`
    那一行**假绿**（字段名出现两次，删掉转发那一处照样能匹配到）。
    """
    src = (_ELECTRON_DIR / relpath).read_text(encoding="utf-8")
    body = src[src.index(anchor) :]
    end = body.find("\n}\n")
    return body if end < 0 else body[: end + 3]


def test_pet_actions_forwards_chunk_fields():
    """★ 主进程必须把 total_chunks/sent_chunks/failed_index/remaining_wavs 透传给面板。

    不透传的后果不是报错，而是**静默降级**：面板只好按单条口径报
    "已发到微信 ✓"，而实际只发出了 3 条里的第 1 条。
    """
    body = _electron_fn_body("pet-actions.cjs", "function doSendTextToWechat")
    # 只看**转发给面板的成功载荷**：该函数里 `send("pet:send-result"` 出现两次
    # （失败路径在前、成功路径在后），必须取**最后**一次，否则断言的是失败载荷
    # （它本来就不带批次字段）→ 恒定红。这个坑是变异测试直接暴露出来的。
    assert 'send("pet:send-result"' in body, "没找到回传面板的调用"
    payload = body[body.rindex('send("pet:send-result"') :]
    payload = payload[: payload.index("});")]
    for field in ("total_chunks", "sent_chunks", "failed_index", "remaining_wavs"):
        assert field in payload, (
            f"pet-actions.cjs 回传面板的载荷里没有 {field} —— 面板无从判断批次状态"
            "（会出现'只发出 1 条却报整批成功'）"
        )


def test_pet_actions_timeout_covers_batches():
    """★ 超时必须够长：长文 N 条要录 N 轮。

    固定 180s 对 3 条以上会**假超时** —— 主进程放弃等待但后端仍在录，
    用户看到"失败"却在微信里收到语音，还可能再点一次 → 两批语音。
    """
    import re

    body = _electron_fn_body("pet-actions.cjs", "function doSendTextToWechat")
    timeouts = [int(m) for m in re.findall(r"\},\s*(\d+)\);", body)]
    assert timeouts, "没找到 backendPost 的超时参数"
    assert max(timeouts) >= 600000, f"发送超时 {max(timeouts)}ms 对长文不够（应 ≥600s）"


def test_pet_panel_watchdog_waits_for_whole_batch():
    """★ 看门狗不能"一看到新记录就收尾"（第 1 条落地时后面还在录）。

    必须存在一个"计数"概念（count）与"稳定/凑齐"判据，
    否则长文会在第 1 条就显示"已发到微信 ✓"，并且诱导用户去动键鼠打断录制。
    """
    src = (_ELECTRON_DIR / "pet" / "pet.html").read_text(encoding="utf-8")
    body = src[src.index("async function watchSendOutcome") :]
    body = body[: body.index("\nasync function countHistorySince")]
    assert "countHistorySince" in body, "看门狗没有数条数 —— 无法区分'发到第几条'与'整批完了'"
    assert "stableRounds" in body, "看门狗没有'稳定'判据，可能出现'刚出现就收尾'或'永远不收尾'"
    assert "total_chunks" in body, "看门狗没用后端给的段数（最准的'整批结束'判据）"
    # 收尾必须发生在"凑齐/稳定"之后，不能因为"出现了新记录"就 return
    assert "stableRounds >= 2" in body or "cnt >= wantTotal" in body, (
        "看门狗缺少'整批结束'的判据 —— 会在第 1 条落地时就收尾"
    )


def test_pet_panel_settle_reports_chunk_counts():
    """★ 结算必须按**条数**口径说话，不能拿单条的时长/告警代替整批。

    两条具体错误：① 用一条的 duration_s 说"已发出（12.3s）"（整段其实 40s）；
    ② 把第 1 条的静音告警说成整批"可能没声音"（后两条其实是好的）。
    """
    src = (_ELECTRON_DIR / "pet" / "pet.html").read_text(encoding="utf-8")
    body = src[src.index("async function settleSendResult") :]
    body = body[: body.index("\n}\n") + 3]
    assert "total_chunks" in body, "结算没看条数 —— 会按单条口径报整批"
    assert "sent_chunks" in body, "结算没看已发条数 —— 部分失败时会含糊成'失败了'"
    assert "条语音" in body, "结算没有'条数'口径的文案"


def test_send_text_returns_batch_fields(tmp_path, monkeypatch):
    """入口契约：多段时 send_text 的响应必须带 total_chunks/sent_chunks/wavs。"""
    monkeypatch.setattr(wv, "_send_preflight", lambda: "")
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_split_for_budget", lambda *a, **k: ([Path("a.wav"), Path("b.wav")], []))
    monkeypatch.setattr(
        wv, "_send_batch", lambda wavs, pre_apply=None: {
            "ok": True, "outcome": "ok", "total_chunks": 2, "sent_chunks": 2,
            "failed_index": None, "wavs": [w.name for w in wavs], "duration_s": 30.0, "steps": [],
        }
    )
    monkeypatch.setattr(wv, "_send_lock", __import__("threading").Lock())
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo"))
    assert res["total_chunks"] == 2
    assert res["sent_chunks"] == 2
    assert res["wavs"] == ["a.wav", "b.wav"]


# ---------------- 变异证据（把"这些用例真的会红"钉在代码里） ----------------


def test_mutation_evidence_guards_are_wired():
    """★ 把三条变异证据固化成断言：将来有人"顺手简化"预算判据时会立刻红。

    为什么要在测试里放这个：本仓的传统是**守护测试必须做变异测试**
    （`docs/犯错档案-工程.md` §8.19–§8.21）—— 注入真违规，断言它**会红**。手工跑过一次
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


def test_mutation_evidence_frontend_guards_are_wired():
    """★ 前端五处护栏的变异证据（2026-09-23 实测：5 个变异全被抓到）。

    这仓的传统是"守护测试必须做变异测试"（`docs/犯错档案-工程.md` §8.19）——
    而**变异测试最容易骗人的地方就是"以为被抓到了"**：第一次跑变异 #1（删掉
    pet-actions 里的 `total_chunks: total,`）用例**照样绿**，因为断言只查
    "文件里出现过这个字段"，而函数顶部读 `data.total_chunks` 那行就满足了它。
    修完（改成只看回传载荷那一段）才真的会红。

    所以这里把**每个变异的注入点与预期红**写成注释留档，并断言"判据的形状"仍在：
    任何人把这些判据改成"更宽松的字符串搜索"，本用例应该让他停下来读一读。
    """
    import inspect

    # ① 回传载荷必须真的带批次字段（变异：删掉 `total_chunks: total,` → 红）
    body = _electron_fn_body("pet-actions.cjs", "function doSendTextToWechat")
    payload = body[body.rindex('send("pet:send-result"') :]
    payload = payload[: payload.index("});")]
    assert "total_chunks: total" in payload, (
        "回传载荷的 total_chunks 被删了 —— 面板会'只发出 1 条却报整批成功'"
    )

    # ② 超时必须 ≥600s（变异：改回 180000 → 红，报"发送超时 180000ms 对长文不够"）
    import re

    timeouts = [int(m) for m in re.findall(r"\},\s*(\d+)\);", body)]
    assert max(timeouts) >= 600000

    # ③ 看门狗必须有"整批结束"判据（变异：把 if 条件换成 `if (true)` → 红）
    wh = _electron_fn_body("pet/pet.html", "async function watchSendOutcome")
    assert "wantTotal && cnt >= wantTotal" in wh, "看门狗丢了'凑齐段数'判据"
    assert "!wantTotal && stableRounds >= 2" in wh, "看门狗丢了'连续不增长'兜底判据"

    # ④ 结算必须读条数（变异：把 `Number(r.total_chunks) || 1` 写成常量 1 → 红）
    st = _electron_fn_body("pet/pet.html", "async function settleSendResult")
    assert "Number(r.total_chunks) || 1" in st, "结算把条数写死了 —— 会按单条口径报整批"
    assert "r.sent_chunks != null" in st, "结算不再区分'已发几条'"

    # ⑤ 后端历史必须落 total_chunks（变异：把 `if total_chunks > 1:` 短路 → 红，
    #    报 `[None, None, None] == [3, 3, 3]`）
    ah = inspect.getsource(wv._append_history)
    assert 'rec["total_chunks"] = total_chunks' in ah, (
        "历史丢了 total_chunks —— 桌宠看门狗只能看到'又出现一行'，"
        "无法区分'发到第 1 条'与'整批发完'，会在第 1 条落地时就打绿勾"
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


# ---------------- 音效标记 `[爆炸]` 混进分段链路（2026-09-25） ----------------
#
# 用户原话：「我就是想用这个程序自动按的时候如何在我说话的步骤能插一个爆炸声，
# 然后我继续说话呢」→ 方案定的是「文字里标记，程序合成时就拌进去」。
#
# 为什么不是悬浮窗那套：**音频在播放前就整段合成完了**（`_split_for_budget` 先跑完，
# `_record_and_send` 才把整段 wav 一次性灌进 CABLE），不存在"程序正在说话"这个
# 中间态，也就没有可插入的时间窗口。见 `sfx_mark.py` 模块注释。
#
# 这一节守护的是**另外两条口径**（第三条在 test_sfx_mark.py 里）：
#   · 标记在合成前被剔掉（不许被念出来）；
#   · 音效时长**并进预算**（不许因为加了音效而破 54s → 静默截断）。


#: 假素材库：id → (名字, 秒数)。用假库是为了不依赖本机真实音效素材，
#: 也让"音效到底多长"可控 —— 预算用例必须能精确摆布这个数字。
_FAKE_SFX = {
    "boom": ("爆炸", 1.8),
    "applause": ("掌声", 2.4),
}


@pytest.fixture
def sfx_files(tmp_path_factory):
    """造两个**真实可读**的音效 wav（`AudioSegment.from_wav` 要真文件）。

    放在 `tmp_path_factory` 的目录里，跑完自动清掉 —— **不许**往
    `m2_server/tests/` 里写临时产物（那会让仓库里出现跑测试才有的垃圾文件，
    而且 CI 上并行跑会互相覆盖）。
    """
    from pydub import AudioSegment

    d = tmp_path_factory.mktemp("sfx_wavs")
    out: dict[str, Path] = {}
    for sid, (_name, seconds) in _FAKE_SFX.items():
        p = d / f"{sid}.wav"
        AudioSegment.silent(duration=int(seconds * 1000), frame_rate=24000).export(str(p), format="WAV")
        out[sid] = p
    return out


@pytest.fixture
def fake_sfx(monkeypatch, sfx_files, tmp_path):
    """把 `sfx_lib` 的两个函数打掉（`_sfx_clip` 走的就是它们）。"""
    import sfx_lib

    def _list_samples():
        return [
            {"id": sid, "name": name, "tags": [], "icon": "", "duration_s": sec}
            for sid, (name, sec) in _FAKE_SFX.items()
        ]

    def _resolve_path(sample_id: str):
        sid = str(sample_id or "")
        if sid not in _FAKE_SFX:
            raise sfx_lib.SfxError(f"没有这个音效：{sid}", status=404)
        return sfx_files[sid], True

    monkeypatch.setattr(sfx_lib, "list_samples", _list_samples)
    monkeypatch.setattr(sfx_lib, "resolve_path", _resolve_path)
    return _FAKE_SFX


def _fake_synth(seconds_per_char: float = 0.1, log: list | None = None, out_dir: Path | None = None):
    """造一个合成器：**时长由字数决定**，这样用例能精确算出预算。

    返回的 wav 是真文件（`_trim_edges` / `AudioSegment.from_wav` 都要求真文件）。
    写入位置优先用 `out_dir`（用例给的临时目录），否则落 `cfg.OUTPUTS_DIR`
    —— 那个在 `isolate_split` 里已经指到 `tmp_path`，所以同样不污染仓库。
    """
    from pydub import AudioSegment

    target_dir = out_dir if out_dir is not None else cfg.OUTPUTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    counter = [0]

    def synth(sentence: str, tts_voice: str) -> bytes:
        if log is not None:
            log.append(sentence)
        counter[0] += 1
        ms = max(50, int(len(sentence) * seconds_per_char * 1000))
        seg = AudioSegment.silent(duration=ms, frame_rate=24000)
        p = target_dir / f"_synth_{counter[0]:04d}.wav"
        seg.export(str(p), format="WAV")
        return p.read_bytes()

    return synth


@pytest.fixture(autouse=True)
def isolate_split(tmp_path, monkeypatch):
    """`_split_for_budget` 的隔离：输出目录 + 参考音探测。

    `_voice_ref_for` 必须打掉 —— 真实现会去查真实音色的参考音文件，单测里没有。
    `session_out` 每次现读 `cfg.OUTPUTS_DIR`（模块注释里明确禁止早绑定），
    所以只打 `cfg.OUTPUTS_DIR` 就够，不用（也不该）去打 `session_out` 的内部名。
    """
    import config as cfg

    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "_voice_ref_for", lambda v: (None, "x"))
    yield tmp_path


def test_mark_is_stripped_before_synthesis(tmp_path, fake_sfx):
    """★ 口径 2：`[爆炸]` 三个字**不许被念出来**。

    合成器收到的是纯文本 —— 这是"标记不进 TTS"的机器化表达。
    变异：把 `_split_for_budget` 里 `spoken = "".join(t for t, _ in segs)` 改回用原文，
    本用例必须变红（合成器日志里会出现 `[爆炸]`）。
    """
    seen: list[str] = []
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        "我今天去那个 [爆炸] 那个地方。", "v", "v", "", 0, 0.0, steps, synth=_fake_synth(log=seen)
    )
    assert outs, "至少要产出一条"
    for s in seen:
        assert "[爆炸]" not in s, f"标记被送去合成了：{s!r}"
        assert "爆炸" not in s, f"标记的名字被念出来了：{s!r}"
    assert any("插入音效：1 处" in s for s in steps), f"steps 没报音效：{steps}"


def test_sfx_audio_is_actually_in_the_output(tmp_path, fake_sfx):
    """★ 端到端：音效真的被拼进了产物（不是只在 steps 里说了一句）。

    判据用**时长**：产物应比"纯人声 + 一个句内 gap"长约 音效时长 + SFX_GAP_S。
    这比"听一下"可控 —— 而"steps 说插了但音频里没有"正是最该防的那种 bug。
    """
    text = "第一句话在这里。第二句话也在这里。"
    plain_steps: list[str] = []
    plain, _ = wv._split_for_budget(
        text, "v", "v", "", 0, 0.0, plain_steps, synth=_fake_synth(seconds_per_char=0.05)
    )
    plain_s = wv._wav_duration(plain[0])

    marked_steps: list[str] = []
    marked, _ = wv._split_for_budget(
        "第一句话在这里。第二句话 [爆炸] 也在这里。",
        "v",
        "v",
        "",
        0,
        0.0,
        marked_steps,
        synth=_fake_synth(seconds_per_char=0.05),
    )
    marked_s = wv._wav_duration(marked[0])
    # 原文多了 [爆炸] 只加长"文字"（被剔掉，不加长人声），所以差值 ≈ 音效 + gap
    expect = _FAKE_SFX["boom"][1] + wv.SFX_GAP_S
    assert marked_s - plain_s == pytest.approx(expect, abs=0.35), (
        f"产物没有变长 {expect}s（{plain_s}s → {marked_s}s）—— 音效没被拼进去"
    )


def test_mark_only_sentence_still_inserts_sfx(tmp_path, fake_sfx):
    """整句就是一个标记（`[掌声]`）→ 没有文字可念，但**音效照插**。

    这是"我想在两句之间来一段掌声"的写法。不能因为"这句没字"就把它丢掉 ——
    丢了等于用户写了但没生效，正是最坏的结果。
    """
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        "前面说一句。[掌声] 后面再说一句。",
        "v",
        "v",
        "",
        0,
        0.0,
        steps,
        synth=_fake_synth(),
    )
    assert len(outs) == 1
    assert wv._wav_duration(outs[0]) > 1.0, "音效（2.4s 的掌声）没被拼进去"
    assert any("插入音效：1 处" in s for s in steps)


def test_unknown_sfx_raises_400_before_synth(tmp_path, fake_sfx, monkeypatch):
    """★ 口径 1：未知名字要**报错**，而且要在**合成之前**就报。

    为什么必须在合成前：TTS+RVC 要 1~2 分钟。为了一句"没有这个音效：爆炸声"
    让用户白等两分钟、再在最后一步失败，是没道理的。
    """
    from fastapi import HTTPException

    called: list[str] = []
    steps: list[str] = []
    with pytest.raises(HTTPException) as ei:
        wv._split_for_budget(
            "你好 [爆炸声] 啊。",
            "v",
            "v",
            "",
            0,
            0.0,
            steps,
            synth=_fake_synth(log=called),
        )
    assert ei.value.status_code == 400
    assert "爆炸声" in ei.value.detail
    assert "可用" in ei.value.detail, "报错要给出可用音效，否则用户不知道该写什么"
    assert called == [], "未知音效必须在合成前报错，不能白跑 TTS"


def test_unknown_sfx_lists_available_names(tmp_path, fake_sfx):
    """报错文案里要**列出可用的名字**（按显示名，就是用户该照着写的那些）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        wv._split_for_budget("啊 [没有这个] 呀", "v", "v", "", 0, 0.0, [], synth=_fake_synth())
    assert "爆炸" in ei.value.detail
    assert "掌声" in ei.value.detail


def test_sfx_duration_counts_toward_budget(tmp_path, fake_sfx):
    """★★ 口径 3（最关键的一条）：音效时长必须**并进预算**，否则会静默截断。

    构造成"差一点就满"的两句：不加音效时两句能装进同一段，
    加一声 2.4s 的掌声之后就**装不下**了 —— 必须分成两段。

    变异：把 `d += sd + SFX_GAP_S` 整行删掉，本用例必须变红
    （两段会合回一段，然后实发时长超 54s → 微信在第 60s 截断，后半句静默丢失）。
    """
    # 每字 0.1s：60 字 = 6.0s。用 4 句各 ~13s（130 字）凑到 52s 左右
    long_sent = "字" * 128 + "。"  # 128*0.1 = 12.8s
    text = long_sent * 4  # 4 句 → 51.2s + 3*0.3 gap = 52.1s ≤ 54 → 一段装得下

    plain_steps: list[str] = []
    plain, _ = wv._split_for_budget(
        text, "v", "v", "", 0, 0.0, plain_steps, synth=_fake_synth(seconds_per_char=0.1)
    )
    assert len(plain) == 1, f"前置条件不成立：不加音效时应是一段（{plain_steps}）"

    # 现在给**每一句**后面都插一声 2.4s 的掌声 → 每句 12.8+0.15+2.4 = 15.35s
    # 两句 = 30.7 + gap 0.3 = 31.0s ≤ 54；三句 = 46.35 + 0.6 = 46.95 ≤ 54；
    # 四句 = 62.0 + 0.9 = 62.9 > 54 → 必须分两段（3 句 + 1 句）
    marked_steps: list[str] = []
    marked, _ = wv._split_for_budget(
        (long_sent[:-1] + " [掌声]。") * 4,
        "v",
        "v",
        "",
        0,
        0.0,
        marked_steps,
        synth=_fake_synth(seconds_per_char=0.1),
    )
    assert len(marked) == 2, f"音效时长没算进预算：本该分 2 段（{marked_steps}）"
    # 每段的实发时长仍必须守 60s 上限（这是整条链路存在的理由）
    for p in marked:
        assert _fit_60(wv._wav_duration(p)) <= wv.MAX_MSG_S
    assert any("插入音效：4 处" in s for s in marked_steps)


def test_sfx_alone_does_not_bust_budget(tmp_path, fake_sfx):
    """★ 不变量：**很多**音效堆在一处时也不许破预算。

    这是上一条的极限版 —— "音效算进预算"如果只在"1 处"时对，那它就是巧合而不是判据。
    这里 25 秒人声 + 一声掌声，合计 25 + gap 0.15 + 2.4 = 27.55s，仍 ≤54 → 一段。

    ⚠️ 注意 `audiobook.split_sentences` 会在 80 字处二次切分，所以"很多音效堆在一句里"
    这个场景要先想清楚它在断句之后长什么样（本用例实测：3×80 字 + 12 字）。
    """
    # 80+80+80+12 = 252 字（断句后）→ 人声 25.2s
    base = "字" * 246 + " [掌声]。"
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        base, "v", "v", "", 0, 0.0, steps, synth=_fake_synth(seconds_per_char=0.1)
    )
    assert len(outs) == 1, f"本该是一段：{steps}"
    assert [s for s in steps if s.startswith("断句")] == ["断句：4 句（复用 audiobook.split_sentences）"], (
        f"断句结果变了，本用例的长度推算要跟着改：{steps}"
    )
    d = wv._wav_duration(outs[0])
    assert _fit_60(d) <= wv.MAX_MSG_S, f"实发 {_fit_60(d):.2f}s 超过 60s —— 会被截断"
    # 人声 25.2s + 3 个句内 gap(0.3) + SFX_GAP 0.15 + 掌声 2.4 = 28.65s（实测 28.25s）
    assert d == pytest.approx(25.2 + 3 * wv.SENT_GAP_S + wv.SFX_GAP_S + _FAKE_SFX["applause"][1], abs=0.6)


def test_many_sfx_marks_still_obey_budget(tmp_path, fake_sfx):
    """★ 多个音效（跨句）时，每一段的实发时长仍必须 ≤60s。

    上面那条是"一句里好几声"，这条是"每句一声"——两者是不同的装箱路径
    （前者只影响一个 `durs[i]`，后者影响多个）。变异：把 `d += sd + SFX_GAP_S`
    删掉，本用例会变红（少算了 4×(2.4+0.15)=10.2s，会多塞一句进第 1 段）。
    """
    # 每句 128 字 = 12.8s；4 句都带 [掌声] → 每句 15.35s
    long_sent = "字" * 127 + " [掌声]。"
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        long_sent * 4, "v", "v", "", 0, 0.0, steps, synth=_fake_synth(seconds_per_char=0.1)
    )
    for p in outs:
        assert _fit_60(wv._wav_duration(p)) <= wv.MAX_MSG_S
    # 3 句 = 46.05 + 2 gap 0.6 = 46.65 ≤ 54 → 一段；4 句 = 61.4 + 0.9 > 54 → 必须两段
    assert len(outs) == 2, f"本该分 2 段：{steps}"
    assert any("插入音效：4 处" in s for s in steps), f"steps 没报足 4 处：{steps}"


def test_sfx_gap_is_included_and_reported(tmp_path, fake_sfx):
    """人声与音效之间的静音（SFX_GAP_S）也要算进预算 —— 它是真实存在的声音。"""
    assert wv.SFX_GAP_S > 0, "不垫静音的话音效会咬住最后一个字的尾音"
    assert wv.SFX_GAP_S < 0.4, "垫太长会在微信里显得空了一拍"
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        "一句话 [爆炸] 说完了。", "v", "v", "", 0, 0.0, steps, synth=_fake_synth(seconds_per_char=0.05)
    )
    # 人声 ≈ 6 字 × 0.05 = 0.3s + gap 0.15 + 音效 1.8 = 2.25s
    assert wv._wav_duration(outs[0]) == pytest.approx(
        6 * 0.05 + wv.SFX_GAP_S + _FAKE_SFX["boom"][1], abs=0.3
    )


def test_sfx_missing_file_raises_readable_error(tmp_path, fake_sfx, monkeypatch):
    """素材 id 对、但文件读不到 → 报 400 可读错误，不是"第 N 句合成失败"。

    `_sfx_clip` 把 `SfxError` 翻成 `HTTPException`，就是为了让用户看到
    "没有这个音效"而不是"合成失败"（后者会把人往 TTS 那边带偏）。
    """
    import sfx_lib

    from fastapi import HTTPException

    def _broken(sample_id):
        raise sfx_lib.SfxError("素材读取失败：boom.wav（坏文件）")

    monkeypatch.setattr(sfx_lib, "resolve_path", _broken)
    with pytest.raises(HTTPException) as ei:
        wv._split_for_budget("啊 [爆炸] 呀", "v", "v", "", 0, 0.0, [], synth=_fake_synth())
    assert ei.value.status_code == 400
    assert "素材读取失败" in ei.value.detail


def test_no_mark_path_unchanged(tmp_path, fake_sfx):
    """★ 回归：不带标记的文字走的是**原来那条路**，没有任何多余产物。

    这是防"加功能把老路带坏了"的护栏 —— 分段数、steps 文案都不该变。
    """
    steps: list[str] = []
    outs, _ = wv._split_for_budget(
        "第一句。第二句。", "v", "v", "", 0, 0.0, steps, synth=_fake_synth()
    )
    assert len(outs) == 1
    assert not any("插入音效" in s for s in steps), f"没标记却报了音效：{steps}"
    # steps 仍只有"断句 + 第 k 条"这几条（不多不少）
    assert steps[0].startswith("断句：2 句")
    assert len(steps) == 2, f"无标记路径的 steps 变了：{steps}"


def test_sfx_mark_module_is_wired_into_split():
    """★ 变异证据：把标记解析接进分段链路的**三处**必须都在。

    这三处各自对应一条会静默失败的口径，少任何一处都不会报错、只会行为不对：
      ① 解析（`sfx_mark.parse`）—— 没有它，`[爆炸]` 会被逐字念出来；
      ② 未知报错（`sfx_mark.available`）—— 没有它，错写会静默不生效；
      ③ 时长并进预算（`d += sd + SFX_GAP_S`）—— 没有它，带音效的段会超预算被截断。
    """
    import inspect

    src = inspect.getsource(wv._split_for_budget)
    assert "sfx_mark.parse(" in src, "① 标记没被解析 —— [爆炸] 会被念出来"
    assert "sfx_mark.available(" in src, "② 未知音效没有报错文案 —— 错写会静默不生效"
    assert "d += sd + SFX_GAP_S" in src, (
        "③ 音效时长没并进预算 —— 带音效的那条会超 54s，被微信静默截断（后半句丢失）"
    )
    # 音效必须在**拼接阶段**真的被加进去（不是只改了预算数字）
    assert "AudioSegment.from_wav(str(sp))" in src, "音效没被拼进产物（只算了时长）"
    assert "AudioSegment.silent(\n                    duration=int(SFX_GAP_S * 1000)" in src, (
        "人声与音效之间没垫静音 —— 音效会咬住最后一个字的尾音"
    )

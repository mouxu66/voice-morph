"""chat_radar 纯逻辑测试：全部用合成图与假信号，不抓微信、不开窗口、不联网。

覆盖三个最容易改坏的地方：
    1. 行投影/列投影的边界（它们决定「谁发的」，一错全错）
    2. gap 的冷启动语义（下界 vs 精确值——写错会静默漏报，最难发现）
    3. 规则引擎的分支（尤其「最后是系统提示」这一支，实测里 VLM 就栽在这里）
"""

import numpy as np
import pytest
from chat_radar import judge as jd
from chat_radar import layout as ly
from chat_radar import state as st
from chat_radar.capture import Roi

# ------------------------------------------------------------------ 合成图工具

BG = 250  # 浅色主题聊天区背景，实测值

#: 真实微信文字的行距：字高 18、行高 22（间隙 4 < ROW_GAP=5，所以多行会被合并成一块）。
#: 合成图必须照这个节奏画，否则实心大块会被 _classify_kind 判成 media，
#: 测出来的就不是「文字消息」而是「图片消息」了。
_LINE_H = 18
_LINE_STEP = 22


def _fill_text(img: np.ndarray, x0: int, x1: int, y0: int, y1: int, val: int) -> None:
    """按真实行距在 [y0, y1) 内画若干文字行。

    隔列填充模拟笔画之间的空隙——真实微信文字块的 fill 只有 10-14%，
    画成实心的话会被 _classify_kind 判成 media，测出来的就不是文字消息了。
    """
    y = y0
    while y < y1 - 2:
        h = min(_LINE_H, y1 - y)
        img[y : y + h, x0:x1:2] = val
        y += _LINE_STEP


def make_chat_image(
    msgs: list[tuple[str, int, int]],
    w: int = 800,
    h: int = 400,
) -> np.ndarray:
    """画一张假的聊天区截图。

    msgs: [(side, y0, y1), ...]  side 为 left / right / center
    左消息画在 x=20..200，右消息 x=600..780，居中块 x=380..420（窄，模拟时间戳）。
    """
    img = np.full((h, w, 3), BG, dtype=np.uint8)
    for side, y0, y1 in msgs:
        if side == "left":
            _fill_text(img, 20, 200, y0, y1, 60)
        elif side == "right":
            _fill_text(img, 600, 780, y0, y1, 60)
        else:
            img[y0:y1, 380:420] = 172  # 浅灰，实测时间戳亮度 171-172
    return img


ROI = Roi(left=0, top=0, right=800, bottom=400)


# ------------------------------------------------------------------ 基础算子


def test_longest_run_returns_widest_segment():
    """最长连续列段：不能被左侧孤立噪声点带偏（这是时间戳被误判成「左」的根因）。"""
    mask = np.zeros(100, dtype=bool)
    mask[2] = True  # 孤立噪声
    mask[30:70] = True  # 真正的内容
    assert ly._longest_run(mask, max_gap=6) == (30, 69)


def test_longest_run_bridges_small_gaps():
    """允许 max_gap 内的空隙，否则头像与文字会被拆成两块。"""
    mask = np.zeros(100, dtype=bool)
    mask[10:20] = True
    mask[30:40] = True  # 空隙 10，小于 max_gap=20 → 应合并
    assert ly._longest_run(mask, max_gap=20) == (10, 39)


def test_longest_run_empty():
    assert ly._longest_run(np.zeros(10, dtype=bool), max_gap=5) is None


@pytest.mark.parametrize(
    "x0,x1,expected",
    [
        (20, 200, ly.LEFT),  # 贴左
        (600, 780, ly.RIGHT),  # 贴右
        (380, 420, ly.CENTER),  # 居中的窄块（时间戳）
        (330, 470, ly.CENTER),  # 居中的宽块（系统提示）
    ],
)
def test_classify_side(x0, x1, expected):
    assert ly._classify_side(x0, x1, 800) == expected


def test_row_runs_splits_on_big_gap_but_not_small():
    rowcnt = np.zeros(200)
    rowcnt[10:30] = 50  # 段 1
    rowcnt[33:50] = 50  # 空隙 3 < ROW_GAP=5 → 合并进段 1
    rowcnt[80:100] = 50  # 空隙 30 → 新段
    runs = ly._row_runs(rowcnt)
    assert runs == [(10, 49), (80, 99)]


def test_row_runs_ignores_faint_rows():
    """每行前景像素 < 3 的行不算数——否则抗锯齿边缘会造出假消息。"""
    rowcnt = np.zeros(50)
    rowcnt[5:10] = 2
    assert ly._row_runs(rowcnt) == []


# ------------------------------------------------------------------ 端到端布局


def test_analyze_three_sides():
    """一张图里同时有左消息、右消息、居中时间戳，三者的方向必须各归各位。"""
    img = make_chat_image(
        [
            ("left", 40, 90),
            ("right", 150, 200),
            ("center", 260, 274),
            ("left", 320, 370),
        ]
    )
    lay = ly.analyze(img, ROI)
    got = [(b.side, b.kind) for b in lay.blocks]
    assert got == [
        (ly.LEFT, "text"),
        (ly.RIGHT, "text"),
        (ly.CENTER, "ts"),
        (ly.LEFT, "text"),
    ]


def test_analyze_merges_adjacent_same_side():
    """同向且挨得近的两段（头像 + 气泡）要合并成一个块，否则 streak 会翻倍。"""
    img = make_chat_image([("left", 40, 70), ("left", 78, 110)])
    lay = ly.analyze(img, ROI)
    assert len(lay.blocks) == 1
    assert lay.blocks[0].lines == 2


def test_analyze_last_chat_skips_centered():
    """「最后一条聊天消息」不能被末尾的时间戳顶掉。"""
    img = make_chat_image([("left", 40, 90), ("center", 200, 214)])
    lay = ly.analyze(img, ROI)
    assert lay.last_chat is not None
    assert lay.last_chat.side == ly.LEFT
    assert lay.tail_centered[0].kind == "ts"


def test_analyze_wide_centered_is_system_message():
    """居中且宽 = 系统提示（"XXX领取了你的红包"），窄 = 时间戳。"""
    img = np.full((300, 800, 3), BG, dtype=np.uint8)
    img[100:114, 300:500] = 160  # 宽 200 的浅灰块
    lay = ly.analyze(img, Roi(0, 0, 800, 300))
    assert lay.blocks[0].side == ly.CENTER
    assert lay.blocks[0].kind == "sys"


def test_dark_theme_uses_bright_foreground():
    """深色主题下前景是「比背景亮」，判据反转但结论不变。"""
    img = np.full((300, 800, 3), 30, dtype=np.uint8)
    img[100:150, 20:200] = 220  # 浅色文字块
    lay = ly.analyze(img, Roi(0, 0, 800, 300))
    assert len(lay.blocks) == 1
    assert lay.blocks[0].side == ly.LEFT


# ------------------------------------------------------------------ ROI 定位


def _fake_window(chat_left: int = 300, w: int = 1200, h: int = 600) -> np.ndarray:
    """造一张假窗口图：左侧会话列表背景 238，右侧聊天区背景 250。"""
    img = np.full((h, w, 3), 250, dtype=np.uint8)
    img[:, :chat_left] = 238
    return img


def test_roi_finds_list_boundary():
    from chat_radar.capture import locate_chat_roi

    roi = locate_chat_roi(_fake_window())
    assert roi is not None
    assert abs(roi.left - 300) <= 2


def test_roi_ignores_large_image_in_chat_area():
    """聊天区里出现深色大图时，左边界不能被图片左缘带偏。

    这是真机踩到的坑：一次抓图里聊天区有张 452px 高的照片，
    列中位数被图片拉低，左边界从 343 漂到 568，整个 ROII 切掉一半。
    """
    from chat_radar.capture import locate_chat_roi

    img = _fake_window()
    img[100:500, 500:700] = 80  # 深色大图，占 400/600 行
    roi = locate_chat_roi(img)
    assert roi is not None
    assert abs(roi.left - 300) <= 2, f"左边界被图片带偏到 {roi.left}"


def test_roi_rejects_blank_image():
    from chat_radar.capture import locate_chat_roi

    assert locate_chat_roi(np.zeros((0, 0, 3), dtype=np.uint8)) is None


# ------------------------------------------------------------------ 状态机


def _layout(*sides):
    blocks = [
        ly.Block(y0=i * 60, y1=i * 60 + 40, x0=20 if s == "left" else 600,
                 x1=200 if s == "left" else 780, side=s, kind="text")
        for i, s in enumerate(sides)
    ]
    return ly.Layout(blocks=blocks, roi_w=800, roi_h=400)


def test_gap_on_cold_start_is_lower_bound():
    """冷启动时屏幕上已有的消息，gap 只能是「至少」，不能当成精确值。

    这条是回归测试：早先版本把「首次观察时刻」当成「消息出现时刻」，
    于是已经放了 10 分钟的消息被报成「刚发 0 秒」，永远不提醒（静默漏报）。
    """
    s = st.RadarState()
    img = make_chat_image([("left", 300, 350)])
    sig = s.feed(img, ROI, _layout("left"), now=1000.0)
    assert sig.gap_lower_bound is True
    assert sig.gap_seconds == pytest.approx(0.0, abs=0.1)

    # 5 秒后画面没变 → gap 涨到 5，但仍标注为下界
    sig = s.feed(img, ROI, _layout("left"), now=1005.0)
    assert sig.gap_lower_bound is True
    assert sig.gap_seconds == pytest.approx(5.0, abs=0.1)


def test_gap_becomes_exact_after_observed_change():
    """观察到一次真实变化后，gap 才是精确值（这正是这个模块存在的意义）。"""
    s = st.RadarState()
    img_a = make_chat_image([("left", 300, 350)])
    s.feed(img_a, ROI, _layout("left"), now=1000.0)
    s.feed(img_a, ROI, _layout("left"), now=1001.0)  # 稳定

    img_b = make_chat_image([("left", 300, 350), ("left", 360, 395)])
    s.feed(img_b, ROI, _layout("left", "left"), now=1002.0)  # 新消息出现

    sig = s.feed(img_b, ROI, _layout("left", "left"), now=1032.0)
    assert sig.gap_lower_bound is False
    assert sig.gap_seconds == pytest.approx(30.0, abs=0.5)
    assert sig.streak == 2


def test_change_detection_catches_same_size_message():
    """同宽同高的两条消息（「哈哈」→「呵呵」）也必须被判定为变化。

    块签名只看宽高，会漏掉这种情况，所以这里用的是底部条带的像素差分。
    """
    s = st.RadarState()
    img_a = make_chat_image([("left", 300, 350)])
    s.feed(img_a, ROI, _layout("left"), now=1000.0)
    assert s.feed(img_a, ROI, _layout("left"), now=1001.0).changed is False

    img_b = img_a.copy()
    img_b[300:350, 20:200] = 120  # 同尺寸，内容亮度不同
    assert s.feed(img_b, ROI, _layout("left"), now=1002.0).changed is True


def test_streak_counts_trailing_left_only():
    s = st.RadarState()
    lay = _layout("left", "right", "left", "left", "left")
    img = make_chat_image([("left", 300, 350)])
    sig = s.feed(img, ROI, lay, now=1000.0)
    assert sig.streak == 3


# ------------------------------------------------------------------ 规则引擎


def _sig(**kw) -> st.Signals:
    base: dict = {"last_side": "left", "streak": 1, "gap_seconds": 0.0}
    base.update(kw)
    return st.Signals(**base)


def test_rule_none_when_i_spoke_last():
    v = jd.judge_by_rule(_sig(last_side="right"))
    assert v.action == jd.NONE and v.score == 0


def test_rule_none_when_tail_is_system_message():
    """最后是系统提示（领取红包）→ 没人等你回。

    实测 Qwen3-VL 在这里答「对方在等你」且只用 0.8 秒，
    快而错比慢而对危险得多，所以这条规则必须硬编码。
    """
    v = jd.judge_by_rule(_sig(tail_meta="sys", gap_seconds=900))
    assert v.action == jd.NONE
    assert "系统提示" in v.reason


def test_rule_wait_when_just_arrived():
    assert jd.judge_by_rule(_sig(gap_seconds=5)).action == jd.WAIT


def test_rule_cold_start_is_unknown_not_wait():
    """刚启动时不能断言「对方刚发」——那是编造，只能说时长未知。

    真机第一帧就是这种情况：屏幕上的消息不知道躺了多久，
    报「再等等」会让用户以为对方刚发，而它可能已经等了半小时。
    """
    v = jd.judge_by_rule(_sig(gap_seconds=3, gap_lower_bound=True))
    assert v.action == jd.UNKNOWN
    assert "未知" in v.reason


def test_rule_soon_in_middle_band():
    v = jd.judge_by_rule(_sig(gap_seconds=180))
    assert v.action == jd.SOON
    assert 2 <= v.score <= 4


def test_rule_now_when_overdue():
    v = jd.judge_by_rule(_sig(gap_seconds=1200))
    assert v.action == jd.NOW
    assert v.score >= 4


def test_rule_streak_raises_urgency():
    low = jd.judge_by_rule(_sig(gap_seconds=180, streak=1))
    high = jd.judge_by_rule(_sig(gap_seconds=180, streak=5))
    assert high.score > low.score


def test_rule_unknown_without_chat():
    assert jd.judge_by_rule(st.Signals()).action == jd.UNKNOWN


def test_rule_gap_lower_bound_is_surfaced():
    """下界值必须在文案里露出「至少」，否则用户会以为是真的刚发。"""
    v = jd.judge_by_rule(_sig(gap_seconds=70, gap_lower_bound=True))
    assert "至少" in v.reason


def test_llm_disabled_without_key(monkeypatch):
    monkeypatch.delenv("VM_RADAR_LLM_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert jd.judge_by_llm(_sig(gap_seconds=180)) is None


def test_judge_falls_back_to_rule(monkeypatch):
    monkeypatch.delenv("VM_RADAR_LLM_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    v = jd.judge_signals(_sig(gap_seconds=180), use_llm=True)
    assert v.source == "rule"


def test_verdict_dict_has_label():
    d = jd.judge_by_rule(_sig(gap_seconds=1200)).as_dict()
    assert d["label"] == "该回了"
    assert set(d) == {"action", "label", "score", "reason", "source"}


# ------------------------------------------------------------------ 编排层


def test_peek_does_not_advance_state(monkeypatch):
    """peek（诊断）不能推进状态机 —— 否则「看一眼」等于「观察一次」。

    走 tick 的实害：frames_seen +1、上一帧指纹被覆盖、_stable 可能归零、
    _observed_since 可能重置（冷启动的 gap_lower_bound 就靠它）。
    表现是「一调 /probe，gap 就从 0 重算」，排查问题时把现场搅乱。
    """
    from chat_radar import radar as rd
    from chat_radar.capture import Frame, WindowInfo

    img = make_chat_image([("left", 300, 350)])
    win = WindowInfo(hwnd=1, title="微信", cls="Qt51514QWindowIcon", left=0, top=0, width=800, height=400)
    frame = Frame(window=win, image=img, roi=ROI)

    r = rd.Radar()
    monkeypatch.setattr(r, "_grab_with_roi", lambda: (frame, ""))

    r.tick()
    after_tick = r._state._frames
    assert after_tick == 1

    got_frame, lay, err = r.peek()
    assert err == ""
    assert got_frame is frame
    assert lay is not None and lay.blocks
    assert r._state._frames == after_tick, "peek 不该推进观察帧数"


def test_peek_reports_error_without_state_change(monkeypatch):
    """观察失败时 peek 只回原因，同样不碰状态机。"""
    from chat_radar import radar as rd

    r = rd.Radar()
    monkeypatch.setattr(r, "_grab_with_roi", lambda: (None, "没找到微信窗口（微信是否在运行？）"))
    frame, lay, err = r.peek()
    assert frame is None and lay is None
    assert "没找到微信窗口" in err
    assert r._state._frames == 0


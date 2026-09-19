"""`rvc_live._device_matches` 的**行为级**守卫：多候选 + 16 通道变体否决。

**为什么单独立一个文件**：`test_cable_keyword_consistency.py` 是**文本级**守卫
（只读源码抓默认值、不 import、毫秒级）；这里是**行为级**守卫（真的 import
`rvc_live` 调函数）。两者职责不同，别混在一起 —— 那边 import 会拉起 sounddevice。

**要守住的坑**（2026-09-18 用户问"为什么会有两个虚拟声卡"时发现的潜伏风险）：
VB-Audio 的虚拟声卡在系统里挂了**两个播放端** —— 立体声的
`扬声器 (VB-Audio Virtual Cable)` 和 16 通道的
`CABLE In 16 Ch (VB-Audio Virtual Cable)`（Windows「输出」列表里并排出现，
用户就是这么看到"两个虚拟声卡"的）。变声链路只走前者；一旦选中后者，
声音会灌进**没人听的**端点 → 微信侧就是静音。

危险点是 `_device_matches` 里的 `b in a`（候选被设备名包含）这一支：

| 设备名来源 | 16Ch 的名字 | 候选 `VB-Audio Virtual Cable` |
|---|---|---|
| MME（`sd.query_devices()`） | `'CABLE In 16 Ch (VB-Audio Virtua'`（截断到 31 字符） | 匹配不上 ✅ |
| MMDevice（`audio_config.ps1 -action list`） | `'CABLE In 16 Ch (VB-Audio Virtual Cable)'`（完整） | **误命中** ❌ |

当前三个调用点（`rvc_live.py` 的 `_live_devices` 与 `rvc_live_audio_devices_set`）
都走 MME，所以**不会触发**；本文件把这个"将来会踩"的坑提前钉死。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import rvc_live  # noqa: E402

# 生产默认值：驱动名优先、端点词兜底（见 cascade_stream.OUTPUT_KEYWORD 处注释）
CANDIDATES = rvc_live.OUTPUT_DEVICE

# 16 通道变体，两种名字形态
SIXTEEN_CH_FULL = "CABLE In 16 Ch (VB-Audio Virtual Cable)"  # MMDevice 视角（完整）
SIXTEEN_CH_TRUNC = "CABLE In 16 Ch (VB-Audio Virtua"  # MME 视角（截断到 31 字符）
# 立体声播放端，两种名字形态
STEREO_LOCALIZED = "扬声器 (VB-Audio Virtual Cable)"  # 中文 Windows（完整）
STEREO_ENGLISH = "CABLE Input (VB-Audio Virtual C"  # 英文 Windows（截断）


# ---------------------------------------------------------------- 16 通道变体否决


def test_sixteen_ch_full_name_is_rejected():
    """**这条就是本次加固的目标**：完整名下的 16Ch 不许被候选命中。

    去掉 `_mentions_16ch` 那段否决，本用例会因 `b in a` 而转红。
    """
    assert not rvc_live._device_matches(SIXTEEN_CH_FULL, CANDIDATES)


def test_sixteen_ch_truncated_name_is_rejected():
    """MME 截断名下的 16Ch 也不许命中（加固前就成立，回归守卫）。"""
    assert not rvc_live._device_matches(SIXTEEN_CH_TRUNC, CANDIDATES)


def test_sixteen_ch_single_endpoint_candidate_is_rejected():
    """只用端点词候选时也不许命中 16Ch（`CABLE Input` 不是 `CABLE In 16 Ch` 的子串）。"""
    assert not rvc_live._device_matches(SIXTEEN_CH_FULL, "CABLE Input")
    assert not rvc_live._device_matches(SIXTEEN_CH_TRUNC, "CABLE Input")


@pytest.mark.parametrize("want", ["16 ch", "CABLE In 16 Ch", "cable in 16 ch"])
def test_sixteen_ch_matches_when_candidate_asks_for_it(want):
    """真要选 16 通道版时，关键词里带上 "16 ch" 就该匹配上（否决不能过头）。

    注意候选必须**字面出现**在设备名里（`16 ch` 带空格），写成 "16ch" 匹配不上 ——
    这是匹配函数一贯的"包含匹配"语义，不是本次否决逻辑造成的。
    """
    assert rvc_live._device_matches(SIXTEEN_CH_FULL, want)
    assert rvc_live._device_matches(SIXTEEN_CH_TRUNC, want)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("CABLE In 16 Ch (VB-Audio Virtual Cable)", True),
        ("cable in 16ch (vb-audio)", True),
        ("扬声器 (VB-Audio Virtual Cable)", False),
        ("CABLE Output (VB-Audio Virtual Cable)", False),
        ("麦克风阵列 (Senary Audio)", False),
        ("", False),
        (None, False),
    ],
)
def test_mentions_16ch_helper(name, expected):
    """`_mentions_16ch` 只认英文的 "16 ch"/"16ch"（实测枚举名里这一段是英文）。"""
    assert rvc_live._mentions_16ch(name) is expected


# ------------------------------------------------- 立体声端点仍要正常命中（别修过头）


def test_stereo_playback_endpoint_still_matches():
    """中文 Windows 的立体声播放端（名字完整）必须仍然命中。"""
    assert rvc_live._device_matches(STEREO_LOCALIZED, CANDIDATES)


def test_stereo_playback_endpoint_english_truncated_still_matches():
    """英文 Windows 的立体声播放端（MME 截断名）必须靠端点词候选命中。"""
    assert rvc_live._device_matches(STEREO_ENGLISH, CANDIDATES)


def test_capture_endpoint_still_matches():
    """采集端 `CABLE Output` 仍要命中（它的 in 通道数也是 16，但名字里没有 "16 ch"）。

    这条同时钉住一件事：否决只看**名字**，不看通道数 —— 否则采集端会被误杀，
    变声链路直接断（微信录不到）。
    """
    assert rvc_live._device_matches("CABLE Output (VB-Audio Virtual Cable)", "CABLE Output")
    assert rvc_live._device_matches("CABLE Output (VB-Audio Virtual C", "CABLE Output")


def test_physical_devices_do_not_match():
    """物理设备不许被候选命中（否则输出会落到真实扬声器）。"""
    assert not rvc_live._device_matches("麦克风阵列 (Senary Audio)", CANDIDATES)
    assert not rvc_live._device_matches("扬声器 (Senary Audio)", CANDIDATES)
    assert not rvc_live._device_matches("", CANDIDATES)
    assert not rvc_live._device_matches(None, CANDIDATES)


# ------------------------------------------------------- 选择路径：别挑到 16Ch


def test_pick_skips_16ch_even_when_listed_first():
    """模拟"16Ch 排在前面"的枚举顺序，选中项必须是立体声播放端。

    真实 MME 顺序里立体声（idx 7）在 16Ch（idx 9）之前，`next()` 恰好先撞对；
    但那是**运气**，不是保证 —— 本用例故意把顺序倒过来。
    """
    devices = [
        {"name": SIXTEEN_CH_FULL, "out": 16},
        {"name": STEREO_LOCALIZED, "out": 2},
    ]
    picked = next(
        (
            d["name"]
            for d in devices
            if d["out"] > 0 and rvc_live._device_matches(d["name"], CANDIDATES)
        ),
        None,
    )
    assert picked == STEREO_LOCALIZED


def test_pick_returns_none_when_only_16ch_present():
    """只有 16Ch 时宁可**不选**（返回 None → 走"沿用旧配置"分支），也不许选错端点。"""
    devices = [{"name": SIXTEEN_CH_FULL, "out": 16}]
    picked = next(
        (
            d["name"]
            for d in devices
            if d["out"] > 0 and rvc_live._device_matches(d["name"], CANDIDATES)
        ),
        None,
    )
    assert picked is None


def test_real_local_devices_pick_stereo_if_available():
    """本机有 sounddevice 时，拿真实 MME 设备表跑一遍选择逻辑（无设备则跳过）。

    这条把"真机上确实挑的是立体声播放端"变成可执行断言，而不是靠肉眼看日志。
    """
    sd = pytest.importorskip("sounddevice")
    try:
        devices = list(sd.query_devices())
    except Exception as e:  # 裸 runner / 无声卡
        pytest.skip(f"枚举音频设备失败: {e}")
    apis = sd.query_hostapis()
    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    if mme is None:
        pytest.skip("本机没有 MME 主机 API")
    outs = [d["name"] for d in devices if d["hostapi"] == mme and d["max_output_channels"] > 0]
    if not outs:
        pytest.skip("本机 MME 下没有任何播放端设备")
    picked = next((n for n in outs if rvc_live._device_matches(n, CANDIDATES)), None)
    if picked is None:
        pytest.skip(f"本机没有 VB-Audio 播放端（可用：{outs}）")
    assert not rvc_live._mentions_16ch(picked), f"挑到了 16 通道变体：{picked!r}；可用：{outs}"

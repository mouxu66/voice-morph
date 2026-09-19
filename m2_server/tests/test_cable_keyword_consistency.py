"""守住「播放端设备关键词」在五处源码里的一致（2026-09-19 级联启动即退出事故）。

**事故**：`cascade_stream.py` / `cascade.py` / `rvc_live.py` / `play_worker.py` 的默认值
还是端点词 `"CABLE Input"`，而 `wechat_voice.py` 早在 2026-09-18（§2.16）就改成了驱动名
`"VB-Audio Virtual Cable"` —— **同一个环境变量 `VM_LIVE_OUTPUT_DEVICE`，同一个坑，只修了一处**。
用户看到的是桌宠一句"启动失败：级联子进程启动即退出（日志见 outputs/cascade_run.log）"，
日志里真正的错是 `RuntimeError: 找不到输出设备（关键词: CABLE Input）`。

**为什么单个关键词都不够**（这是本次最终改成"双候选"的原因）：

| 系统 | MME 枚举出的播放端名字 | 谁能命中 |
|---|---|---|
| 中文 Windows | `扬声器 (VB-Audio Virtual Cable)`（名字完整） | 只有**驱动名**（端点词根本不出现） |
| 英文 Windows | `CABLE Input (VB-Audio Virtual C`（MME **截断到 31 字符**） | 只有**端点词**（完整驱动名匹配不上截断名） |

所以默认值是 `"VB-Audio Virtual Cable|CABLE Input"`：驱动名优先、端点词兜底，
消费方（`find_device` / `_resolve_device` / `_device_matches`）按 `|` 拆开逐个试。

**为什么用文本级守卫而不是 import**：这几个模块 import 时会拉起 sounddevice / RVC 依赖
（`cascade_stream` 顶部就 `import sounddevice`），单测里既慢又可能因缺设备报错。
这里只读源码抓默认值，零依赖、毫秒级。
"""

import re
from pathlib import Path

import pytest

M2 = Path(__file__).resolve().parents[1]

# 驱动名（中文系统名字完整时命中）与端点词（英文系统被截断时命中）
DRIVER = "VB-Audio Virtual Cable"
ENDPOINT = "CABLE Input"
EXPECTED = f"{DRIVER}|{ENDPOINT}"

# `VM_LIVE_OUTPUT_DEVICE` 的四个消费点：(文件, 变量名)
_ENV_CONSUMERS = [
    ("cascade_stream.py", "OUTPUT_KEYWORD"),
    ("cascade.py", "OUTPUT_DEVICE"),
    ("rvc_live.py", "OUTPUT_DEVICE"),
    ("wechat_voice.py", "OUTPUT_DEVICE_KEYWORD"),
]

_ENV_TMPL = r'{var}\s*=\s*os\.environ\.get\(\s*"VM_LIVE_OUTPUT_DEVICE"\s*,\s*"([^"]*)"'
_PLAY_WORKER_RE = re.compile(r'len\(sys\.argv\)\s*>\s*1\s*else\s*"([^"]*)"')


def _read(name: str) -> str:
    return (M2 / name).read_text("utf-8")


def _env_default(fname: str, var: str) -> str | None:
    m = re.search(_ENV_TMPL.format(var=re.escape(var)), _read(fname))
    return m.group(1) if m else None


@pytest.mark.parametrize("fname,var", _ENV_CONSUMERS)
def test_env_default_is_identical_everywhere(fname, var):
    """四处 `VM_LIVE_OUTPUT_DEVICE` 的默认值必须**完全一致**。

    这次事故的根因就是"只改了一处" —— 单点断言挡不住漂移，必须逐处比。
    """
    got = _env_default(fname, var)
    assert got is not None, (
        f'{fname} 里找不到 `{var} = os.environ.get("VM_LIVE_OUTPUT_DEVICE", ...)` —— '
        "变量被改名/删除的话这条守卫就失效了，请同步更新本测试"
    )
    assert got == EXPECTED, (
        f"{fname} 的 `{var}` 默认值是 {got!r}，应为 {EXPECTED!r}。"
        "单个关键词覆盖不了「中文名字完整 / 英文名字被 MME 截断」两种情形（见模块 docstring）"
    )


def test_play_worker_fallback_is_identical():
    """`play_worker.py` 没收到 argv[1] 时的兜底关键词也必须一致。

    正常路径下 `wechat_voice._start_play()` 会把关键词当 argv[1] 传进来，
    所以这个兜底只在手动/调试直接跑 play_worker 时才生效 —— 正因如此更容易被漏改。
    """
    m = _PLAY_WORKER_RE.search(_read("play_worker.py"))
    assert m, "play_worker.py 里找不到 KEYWORD 的兜底默认值（表达式被改写？）"
    assert m.group(1) == EXPECTED, f"play_worker.py 兜底是 {m.group(1)!r}，应为 {EXPECTED!r}"


def test_driver_name_is_first_candidate():
    """驱动名必须是**第一个**候选。

    消费方按序尝试，所以顺序即优先级：中文系统下名字完整，
    若把端点词放前面就可能先撞上 16 通道版（见下一条）。
    """
    for fname, var in _ENV_CONSUMERS:
        got = _env_default(fname, var) or ""
        first = got.split("|")[0].strip()
        assert first == DRIVER, f"{fname} 的首候选是 {first!r}，应为 {DRIVER!r}"


def test_candidates_cover_both_localized_and_english_names():
    """候选必须同时覆盖中文（名字完整）与英文（被 MME 截断）两种叫法。

    这条把"为什么要双候选"变成可执行断言，不依赖真机设备。
    """
    localized = "扬声器 (VB-Audio Virtual Cable)"  # 中文 Windows：名字完整
    english_truncated = "CABLE Input (VB-Audio Virtual C"  # 英文 Windows：MME 截断到 31 字符
    cands = [c.strip().lower() for c in EXPECTED.split("|") if c.strip()]
    assert any(c in localized.lower() for c in cands), "没有候选能命中中文叫法"
    assert any(c in english_truncated.lower() for c in cands), "没有候选能命中英文截断叫法"


def test_candidates_do_not_match_16ch_variant():
    """反面守卫：候选不许误命中 `CABLE In 16 Ch` 那个变体。

    它也是 VB-Audio 的播放端（16 通道版），一旦被选中，音频会灌进没人听的端点。
    两个候选都刚好躲开它：驱动名匹配不上（名字被截断成 "…VB-Audio Virtua"），
    端点词 "CABLE Input" 也不匹配（它只有 "CABLE In"）。
    """
    sixteen = "CABLE In 16 Ch (VB-Audio Virtua"
    cands = [c.strip().lower() for c in EXPECTED.split("|") if c.strip()]
    for c in cands:
        assert c not in sixteen.lower(), f"候选 {c!r} 会误命中 16 通道版"


def test_real_local_machine_names_if_available():
    """本机有 sounddevice 时，直接拿真设备名验一遍候选（无设备则跳过）。"""
    sd = pytest.importorskip("sounddevice")
    try:
        devices = list(sd.query_devices())
    except Exception as e:  # 没有音频设备（CI）时跳过
        pytest.skip(f"枚举音频设备失败: {e}")
    apis = sd.query_hostapis()
    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    if mme is None:
        pytest.skip("本机没有 MME 主机 API")
    cands = [c.strip().lower() for c in EXPECTED.split("|") if c.strip()]
    outs = [d["name"] for d in devices if d["hostapi"] == mme and d["max_output_channels"] > 0]
    if not outs:
        pytest.skip("本机 MME 下没有任何播放端设备（裸 runner / 无声卡）")
    hit = [n for n in outs if any(c in n.lower() for c in cands)]
    assert hit, f"候选 {cands} 在本机 MME 播放端里一个都没命中；可用：{outs}"

# 让 pytest 能直接 import m2_server 内的模块（config / rvc_live / server）
import os
import shutil
import sys
from pathlib import Path
from typing import NoReturn

import pytest

# 测试环境禁止启动时预热：server.py 在导入期就会 start_background() 预热
# TTS worker + 常驻 RVC 模型（加载 1.7B 模型占 GPU），测试里绝不能真起这个
# 子进程。warmup.ENABLED 在导入时读 VM_WARMUP，故必须在 import server 之前置 0。
os.environ.setdefault("VM_WARMUP", "0")

# 测试环境禁止真重启微信：wechat_voice._prepare_recording_env 在 auto 模式下
# 会杀进程 + 拉起 Weixin.exe（真机副作用、还可能把用户的微信弄掉线）。
# 强制置 0；需要覆盖的用例自己 monkeypatch.setenv。
os.environ["VM_WECHAT_RESTART"] = "0"

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True, scope="session")
def _isolated_plugin_state(tmp_path_factory):
    """把插件开关的状态文件挪到临时目录 —— **测试绝不能读写用户的真配置**。

    第 6 步起 `outputs/plugins.json` 决定「哪些能力被挂载」：测试若读它，
    某台开发机上残留的一份配置就能让全量测试集体变红（且红得莫名其妙）；
    测试若写它，更是直接改掉用户设置。两样都不能发生，所以整个会话统一隔离。
    需要验证写文件行为的用例，自己 `monkeypatch.setattr(plugin_manifest, "STATE_FILE", ...)`。
    """
    import plugin_manifest

    real = plugin_manifest.STATE_FILE
    plugin_manifest.STATE_FILE = tmp_path_factory.mktemp("plugin_state") / "plugins.json"
    yield
    plugin_manifest.STATE_FILE = real


# ==================== 本机资源探测（"本机绿 ≠ CI 绿"的第二根轴）====================
# 为什么需要（2026-09-13 CI 首次运行）：
#     CI 首跑红了 9 failed + 4 errors，**没有一条是代码缺陷**，全部是
#     "开发机有、runner 没有"的外部资源：
#       · ffmpeg            → pet_skin_build / pet_market / pet_scan 共 9 条
#       · D:\RVC\.venv      → rvc_worker 的 2 条回退用例
#       · kangaroo 微调产物  → seedvc 的 1 条
#       · 屏幕分辨率 1938×1609 → wechat_record 的绿钮定位 1 条
#     报错形态还特别难认：subprocess 抛 `[WinError 2]`、`RvcError: 找不到 RVC 运行环境`，
#     看起来像生产代码坏了，其实是环境。
#
#     `tools/check.py --ci-fidelity` 当时只复刻了 **Python 依赖集** 这一根轴
#     （Pillow/comtypes 那次事故的产物），对"资源缺失"这根轴完全看不见。
#
# 本段就是第二根轴的机器对策：资源探测集中到一处，缺资源时
#     · 本机  → skip，并用人话说明怎么装（而不是抛 WinError 2）
#     · CI 上 → **fail**。刻意的：CI 缺一个已声明的资源说明 workflow 的环境准备
#               步骤坏了，不能让 skip 把它掩盖成绿色。
#
# `VM_BARE_RUNNER=1` 把所有本机资源一律判为"不存在"，让本机也能复现裸 runner
# 的判定（`tools/check.py --ci-fidelity` 会设它）。这才是"改完测试先自问一句
# CI 上有没有这东西"的机器版本。


def _on_ci() -> bool:
    """是否跑在 CI 上（GitHub Actions 会设 CI=true）。"""
    return os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")


def bare_runner() -> bool:
    """是否按「裸 runner」判定：所有本机资源一律视为不存在。

    由 `VM_BARE_RUNNER=1` 打开；`tools/check.py --ci-fidelity` 会设。
    """
    return os.environ.get("VM_BARE_RUNNER", "").strip() not in ("", "0")


def missing_local(what: str, how: str) -> NoReturn:
    """缺本机资源：本机 skip（附安装提示），CI 上 fail（环境坏了不能装绿）。"""
    msg = f"缺少本机资源 {what}（{how}）"
    if _on_ci():
        pytest.fail(msg + " —— CI 上出现说明环境准备步骤没生效，不许用 skip 掩盖")
    pytest.skip(msg)


def ffmpeg_path() -> str:
    """ffmpeg 可执行路径。缺则 skip（CI 上 fail），绝不返回裸字符串。

    为什么不能直接用 `common.find_ffmpeg()`：它找不到时会回落到字面量 `"ffmpeg"`
    （生产代码需要这个行为，好让 PATH 上的 ffmpeg 兜底）。但测试里这个回落会把
    "没有 ffmpeg"变成 subprocess 的 `[WinError 2] The system cannot find the file
    specified` —— 2026-09-13 CI 上那 9 条就是这样，看不出真因。所以这里必须再确认
    一次它真能跑起来。
    """
    if not bare_runner():
        from common import find_ffmpeg

        exe = find_ffmpeg()
        if Path(exe).exists() or shutil.which(exe):
            return exe
    missing_local("ffmpeg", "winget install Gyan.FFmpeg，或设 FFMPEG_PATH 指向完整版")


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    """ffmpeg 可执行路径（session 级）。需要真 ffmpeg 的用例依赖它即可自动跳过。"""
    return ffmpeg_path()


@pytest.fixture(scope="session")
def on_bare_runner() -> bool:
    """是否处于「裸 runner」模拟模式（见 `bare_runner()`）。

    给"只在本机才有意义"的用例用：它们在本机跑、在 CI 跳过，
    模拟模式下也要跟着跳过，否则 --ci-fidelity 的跳过集跟 CI 对不上。
    """
    return bare_runner()


@pytest.fixture(autouse=True)
def _offline_market_license(monkeypatch):
    """市场许可探测一律离线（G4）。

    为什么放在 conftest 而不是各个测试文件里：`market_install` 会在**安装收尾**调
    `market_license.probe()`，也就是说任何一条"跑一遍安装"的用例都会顺手发一个
    真实 HTTP 请求。那种依赖不会以失败的形式暴露 —— 它表现为 CI 变慢、偶发超时、
    以及在没网的 runner 上莫名其妙地变红。所以在这里**一次性掐掉**默认取数层。

    打桩的是 `_default_get`（最底下那层）而不是 `probe` 本身，这样
    `probe` 的三态判定、两个 parser 都还在被测；需要真实响应的用例自己传
    `probe(entry, get=...)` 注入，不受影响。
    """
    import market_license

    def _no_network(url: str, timeout: float):  # noqa: ARG001
        raise RuntimeError(
            "测试环境不做真实网络请求：market_license._default_get 已被 conftest 打桩。"
            "需要构造响应请传 probe(..., get=...) 或直接测 parse_*_license()。"
        )

    monkeypatch.setattr(market_license, "_default_get", _no_network)

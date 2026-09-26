# 让 pytest 能直接 import m2_server 内的模块（config / rvc_live / server）
import os
import shutil
import subprocess
import sys
import tempfile
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

# 测试环境禁止「首次运行播种插件状态」（2026-09-23）。
#
# `plugin_manifest.ensure_state_file()` 在 `server.py::_mount_all()` 开头把默认套餐
# （「标准」= 变声本体，3 项）落成一份 `outputs/plugins.json`。它必须在 `mount_plan()`
# **之前**跑，而 `_mount_all()` 是 `import server` 时执行的 —— 也就是说：
#
#   · `tests/test_plugin_switch.py` 等模块在**顶层**就 `import server`，那是 pytest 的
#     **收集阶段**，比任何夹具都早。于是播种会真的发生，`server._ROUTER_ORDER` 只剩下
#     本体的 router（其余 9 项被跳过），而期望「20 项全挂」的那批用例会集体变红：
#     `test_manifest_covers_exactly_the_registered_routers`（清单 26 个 router 对不上）、
#     `test_all_plugins_are_ok_on_a_healthy_start`（期望 counts = 19/19/0/0）…
#   · 更要命的是**那时 `STATE_FILE` 还没被下面的会话级夹具挪走** —— 播种会写在
#     夹具接管之前的那个路径上，于是"到底写了哪份"取决于哪个测试模块先被收集。
#
# 一行环境变量把"取决于收集顺序"变成"一定不发生"。需要验证播种行为的用例
# `monkeypatch.delenv("VM_PLUGIN_SEED")` + 自己的 `STATE_FILE`，见 `test_plugin_switch.py`。
os.environ["VM_PLUGIN_SEED"] = "0"

# 测试环境禁止真向声卡播放：`soundboard._spawn_worker()` 会起 RVC venv 里的
# 常驻播放器把素材写进 CABLE（`VM_SOUNDBOARD=0` 时直接拒播）。测试要跑通"播放成功"
# 路径时 monkeypatch `soundboard._spawn_worker`，而不是把这个开关打开 ——
# 免得用例把声音放到用户的虚拟声卡上（那会被正在录音的微信录进去）。
os.environ.setdefault("VM_SOUNDBOARD", "0")

# ★ 数据目录（outputs/ 与 media/）必须在**任何 m2_server 模块被导入之前**切到临时目录
# （2026-09-22）。
#
# 为什么不能靠 `monkeypatch.setattr(config, "OUTPUTS_DIR", ...)`：
# 一批模块把它**早绑定**成模块级常量 ——
#     market_preview.MARKET_DIR = cfg.OUTPUTS_DIR / "market"
#     market_images.CACHE_DIR  = OUTPUTS_DIR / "market" / "imgs_cache"  # from config import OUTPUTS_DIR
#     finetune.FT_DIR          = cfg.MEDIA_DIR / "ft"
#     cascade.STATE_FILE / history.HISTORY_FILE / runtime.OUT / …（全仓 40+ 处）
# 它们是**导入时求值**的，导入之后再改 `cfg.*` 对它们**完全无效、且不报错**
# （见 `docs/犯错档案-工程.md` §8.36）。
#
# 后果实测：跑全量时真实 `outputs/market/` 里累积了 20+ 个**夹具名**的 sidecar
# （auto_rb / busy_rb / circular / mutex / no_idx / test_voice …，最早可追到 2026-09-06）、
# `outputs/market/imgs_cache/.revision` 被覆写成远端版本号、
# `media/ft/dstkoi/status.json` 被改写成测试数据（`dstkoi` 同样是夹具名；
# 要害是它落在**用户数据根 `media/`** 里，与文件名无关）——
# 也就是**动了用户的真实数据**。
#
# 更阴的是**时序**：这些写入常常来自**活过用例 teardown 的后台线程** ——
#   · `market_preview.generate()` 起 daemon 线程，`_maybe_backoff()` 还会
#     `sleep(_BACKOFF_S=20)` 后再写一次；
#   · `finetune._train_job()` 起的训练线程在用例结束后才落 status.json。
# monkeypatch 在 teardown 就还原了 ⇒ 后段的写入落回**真实**目录。
# 所以「单跑一个文件」根本看不出来（进程退出把线程杀了），
# 必须"这个文件跑完还有别的文件在跑"才复现（实测：`test_market_search_install.py`
# 加任意一个文件一起跑 → 立刻泄漏 3 个）。见 §8.37。
#
# 放在**本文件**而不是 `tests/conftest.py`：pytest 先加载 `m2_server/conftest.py`，
# 而 `tests/conftest.py` 在导入期就会 `import config` —— 晚一步就来不及了。
# 用**环境变量**而不是直接改 `config` 模块属性：`config` 此刻还没被导入，
# 让它自己在导入时算出正确的值，是最不容易漏的写法（同上面的 `VM_WARMUP`）。
# 一行替掉 40+ 处早绑定常量各自的补丁，且以后新增模块**自动被覆盖**。
_VM_TEST_OUTPUTS = tempfile.mkdtemp(prefix="vm-test-outputs-")
os.environ["VM_OUTPUTS_DIR"] = _VM_TEST_OUTPUTS
_VM_TEST_MEDIA = tempfile.mkdtemp(prefix="vm-test-media-")
os.environ["VM_MEDIA_DIR"] = _VM_TEST_MEDIA

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


# Windows 上「系统资源不足」的错误码 —— 必须与「ffmpeg 不存在」区分开。
# 这三个都是"这次跑不动"，不是"这环境没有"。
_RESOURCE_EXHAUSTED_WINERRORS = {
    8: "ERROR_NOT_ENOUGH_MEMORY（内存不足）",
    1450: "ERROR_NO_SYSTEM_RESOURCES（系统资源不足）",
    1455: "ERROR_COMMITMENT_LIMIT（页面文件/提交内存不足）",
}


@pytest.fixture(scope="session")
def ffmpeg_run(ffmpeg_bin):
    """跑 ffmpeg 造测试素材；**系统资源不足** → skip（不是产品问题）。

    为什么要包一层（2026-09-22 实测，junit 取证）：全量跑到后段时本机提交内存/
    句柄接近上限，`subprocess.run` 在 CreateProcess 阶段抛

        OSError: [WinError 1450] 系统资源不足，无法完成请求的服务。

    报出来是「测试失败」，而且失败用例在不同次全量里会漂移 —— 看着像产品缺陷。
    同一次全量里 `test_offline_vc_infer_pth_guard.py` 也因内存不足假红
    （`DefaultCPUAllocator: not enough memory`），两条**同源**：环境资源耗尽。
    这类"测试自己造素材"的调用是资源敏感点（要创建子进程），所以统一走这里。

    ⚠️ 为什么这里不像 `missing_local` 那样"CI 上 fail"：
    `missing_local` 处理的是**确定性缺失**（ffmpeg 没装 ⇒ 环境准备一定坏了），
    而这里是**瞬时资源竞争** —— 同一份环境上一次绿、这一次红，没有确定性判据。
    在 CI 上 fail 只会把偶发噪声变成必现阻塞，反而更掩盖真问题。

    只豁免上面那三个 Windows 资源耗尽错误码；其余 `OSError` **照常抛** ——
    尤其 `FileNotFoundError`（WinError 2）是"ffmpeg 真的没了"，那是真问题。

    用法：`ffmpeg_run(["-y", "-f", "lavfi", "-i", "...", str(out)])`
    （可执行路径由夹具自己带上，调用处只传参数）。
    """

    def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [ffmpeg_bin, *args], capture_output=True, text=True, timeout=timeout
            )
        except OSError as exc:
            kind = _RESOURCE_EXHAUSTED_WINERRORS.get(getattr(exc, "winerror", None))
            if kind is None:
                raise
            pytest.skip(
                f"系统资源不足，无法创建 ffmpeg 子进程：{kind}（{exc}）—— 这是**环境问题**"
                f"（全量跑后段提交内存/句柄接近上限），不是产品问题；单独跑该文件即为绿。"
            )

    return _run


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


#: 仓库外**绝不允许被测试写到**的路径：测试只该动 tmp_path，
#: 真实运行环境（RVC venv / 用户配置）不在其中。
#: 可用 `VM_OFFLIMITS_EXTRA` 追加（`;` 分隔）—— 守卫自己的测试靠它指向受控目录，
#: 这样测"拦没拦住"时不会真去碰真机文件。
OFF_LIMITS: list[Path] = [
    Path("D:/RVC"),
    Path(os.path.expanduser("~/.workbuddy")),
]


def _offlimits_roots() -> list[Path]:
    """禁区根目录（含 `VM_OFFLIMITS_EXTRA` 追加项），每次现读、不缓存。"""
    roots = list(OFF_LIMITS)
    for chunk in (os.environ.get("VM_OFFLIMITS_EXTRA") or "").split(";"):
        chunk = chunk.strip()
        if chunk:
            roots.append(Path(chunk))
    return roots


def _offlimits_hit(path) -> Path | None:
    """路径落在禁区里就返回解析后的绝对路径，否则 None。

    只拦**写**不拦读：测试读真实环境（探版本、看权重在不在）是正当的。

    `VM_OFFLIMITS_BYPASS=1` 时整体旁路 —— 只给"守卫自己的测试准备犯规现场"用
    （受控目录按定义就在禁区里，不旁路就建不出目标）。
    """
    if os.environ.get("VM_OFFLIMITS_BYPASS") == "1":
        return None
    try:
        target = Path(os.fspath(path)).resolve()
    except (TypeError, ValueError, OSError):
        return None
    for root in _offlimits_roots():
        try:
            root_r = root.resolve()
        except OSError:
            continue
        if target == root_r or root_r in target.parents:
            return target
    return None


def _guard_offlimits_writes() -> None:
    """禁止测试写仓库外的真实环境（RVC venv / 用户配置）。

    2026-09-26 事故：`test_cover_fetch.py::_run_env` 里两行顺序写反 ——
    先 `Path(cover.cfg.RVC_ROOT)` 取了真机 `D:\\RVC`、再 monkeypatch 成 tmp_path，
    于是 `venv.write_bytes(b"")` 把**真解释器**截成 0 字节，RVC 链路整条瘫掉
    （退出码 -1073741515，`0xC0000135`）。这类错误**不会以断言失败暴露** ——
    形态是"测试全绿 + 机器坏掉"，所以必须在测试期拦一道。

    拦哪几层（2026-09-26 逐层实测，结论与直觉相反，务必照抄）：
      · `io.open`  ← **关键层**。`Path.write_bytes` / `write_text` / `Path.open`
        最终都走它。当年元凶正是 `Path.write_bytes`，只有补这里才拦得住。
      · `builtins.open` ← 脚本式 `open(...)` 走它（**注意它不经过 `io.open`**，
        两层互不覆盖，必须都补）。
      · `os.open` ← 第三方库绕过前面两层的底层 fd 操作。
      · `os.remove` / `os.unlink` / `os.mkdir` / `os.rmdir` ← 删除与建目录。

    曾经的错误结论（已作废，别再照做）：以为"`Path.write_bytes` 绕过 `os.open`、
    所以无法拦截"。真相是它绕过 `os.open` 但要过 `io.open` ——
    当时只补了 `os.open` 和 `builtins.open`，恰好唯一没补那个必经之路。

    为什么用环境变量开关而不是 `monkeypatch`：本模块在 `import` 期就要生效，
    且 fixture teardown 在失败场景下顺序不可靠；一个进程级标志最简单。
    """
    if os.environ.get("_VM_OFFLIMITS_GUARD") == "on":
        return
    os.environ["_VM_OFFLIMITS_GUARD"] = "on"

    import builtins
    import io as _io

    def _refuse(target: Path, action: str) -> None:
        raise AssertionError(
            f"测试试图{action}真实环境: {target}\n"
            "测试只能写 tmp_path。若代码里先取了路径、后 monkeypatch 根目录，"
            "把 monkeypatch 挪到取路径之前 —— 否则会打到真机上。"
            "参见 2026-09-26 RVC venv 被清空事故。"
        )

    def _is_write_mode(mode) -> bool:
        return any(c in str(mode) for c in "wax+")

    # ---- ① io.open：Path.write_bytes / write_text / Path.open 的必经之路 ----
    _io_open = _io.open

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(mode):
            hit = _offlimits_hit(file)
            if hit is not None:
                _refuse(hit, "写入")
        return _io_open(file, mode, *args, **kwargs)

    _io.open = guarded_io_open

    # ---- ② builtins.open：脚本式 open(...)。它**不**经过 io.open，单独补 ----
    _builtin_open = builtins.open

    def guarded_builtin_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(mode):
            hit = _offlimits_hit(file)
            if hit is not None:
                _refuse(hit, "写入")
        return _builtin_open(file, mode, *args, **kwargs)

    builtins.open = guarded_builtin_open

    # ---- ③ os.open：第三方库的底层 fd 操作 ----
    _os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            hit = _offlimits_hit(path)
            if hit is not None:
                _refuse(hit, "写入")
        return _os_open(path, flags, *args, **kwargs)

    os.open = guarded_os_open

    # ---- ④ 删除 / 建目录 ----
    _os_remove, _os_mkdir, _os_rmdir = os.remove, os.mkdir, os.rmdir

    def guarded_remove(path, *a, **k):
        hit = _offlimits_hit(path)
        if hit is not None:
            _refuse(hit, "删除")
        return _os_remove(path, *a, **k)

    def guarded_mkdir(path, *a, **k):
        hit = _offlimits_hit(path)
        if hit is not None:
            _refuse(hit, "创建目录于")
        return _os_mkdir(path, *a, **k)

    def guarded_rmdir(path, *a, **k):
        hit = _offlimits_hit(path)
        if hit is not None:
            _refuse(hit, "删除目录")
        return _os_rmdir(path, *a, **k)

    os.remove = guarded_remove
    os.unlink = guarded_remove
    os.mkdir = guarded_mkdir
    os.rmdir = guarded_rmdir


_guard_offlimits_writes()

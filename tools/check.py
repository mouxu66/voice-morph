"""一键自检 —— 交付前 / CI / git 钩子的唯一入口。

背景：本项目铁律 1/2 是「改动即提交、补测且全绿」，但此前**没有任何机器可执行的
检查入口**：跑不跑、跑哪些、环境变量设没设，全靠人记得。2026-09-11 的代码审查
就撞上两次「文档写着全绿、实际已过期」（GBK 编码失败的用例、随机挂的队列用例）。

本脚本把六个检查串起来，顺序按「快 → 慢」，失败即停并返回非零：

    1. licenses    —— 第三方许可登记门禁：`THIRD_PARTY_NOTICES.md` 的机器块必须与
                      当前依赖集严格相等（漏登记 / 残留条目 / 缺义务行都判红）。
                      许可漏了是**事后补不回来**的（包已发出去就违规），而症状是零，
                      只有"加依赖那一次提交"能拦。纯本地文本解析，约 0.05s。
    2. requires    —— 静态检查 electron/*.cjs 里「用了 Node 内建模块标识符但没 require」。
                      2026-09-12 事故：alt-hint.cjs 拆文件时漏 require("fs")，打包后
                      主进程 require 阶段即崩，用户装了打不开。零依赖、约 0.1s。
    3. electron    —— 用 electron 桩 require 全部 electron/*.cjs，require 阶段崩即 FAIL。
                      requires 的上位替代：还能抓 require 了不存在的路径、
                      顶层求值期访问 undefined 等。约 0.15s。
    4. ruff        —— 静态扫描，专抓真 bug 类规则（F/E9：未定义名、未用变量、
                      f-string 缺占位符、语法错误）。实测抓到过 rvc_common 的
                      未定义 logger（生产代码 NameError）。
    5. pytest      —— m2_server 全量（默认）或快速子集（--fast）。
    6. tsc -b      —— web 前端类型检查（不产出 dist）。

用法：

    python tools/check.py                 # 全量（= 交付前 / CI 跑的那条）
    python tools/check.py --fast          # 提交前（pre-commit）：licenses + requires
                                          #          + electron + ruff + 快速子集
    python tools/check.py --no-web        # 没有 Node 环境时
    python tools/check.py --only pytest   # 只跑某一项（逗号分隔）
    python tools/check.py --list          # 只看会跑什么，不执行
    python tools/check.py --ci-fidelity   # 复刻 CI：在只装 requirements-dev.txt 的干净
                                          # venv 里跑 CI 那条命令（改依赖后必跑）

为什么需要 --ci-fidelity（2026-09-13 事故）：
    本机 `.venv` 全绿不代表 CI 绿。那天 CI 首次运行就会红 2 failed + 2 errors，
    因为 `Pillow` / `comtypes` 从未写进任何 requirements —— 本机有只是因为它们是
    **别的包的传递依赖**（Pillow←gradio/matplotlib，comtypes←pycaw/uiautomation）。
    更阴的是 `from PIL import ...` 写在函数体里，`import server` 干净环境照样成功，
    "能启动"这个检查完全没看见缺口。细节见 docs/犯错指南.md §3.9。

    本模式就是这个事故的机器对策：拿**干净 venv** 跑 CI 那条命令。

    · venv 固定为项目根 `.venv-ci/`（已 gitignore），只装 requirements-dev.txt
    · Python 版本从 `.github/workflows/ci.yml` **读**，不在这里写死
      （写死的话"复刻"自己会跟 CI 漂移，那就白复刻了）
    · 依赖变更靠 stamp（requirements-dev.txt 的 sha256 + 版本号）判定，
      没变就跳过 pip install，省 1~2 分钟
    · `--recreate` 用 `venv --clear` 重建，保证零污染（手工装过东西的 venv 不可信）

"本机绿 ≠ CI 绿"其实有**两根轴**，第一根是依赖集，第二根是本机资源：

    轴① 依赖集 —— 干净 venv 解决（上面那套）。
    轴② 本机资源 —— ffmpeg / `D:\\RVC` / 微调产物 / 开发机屏幕分辨率这类"开发机有、
         runner 没有"的东西。**2026-09-13 CI 首跑红的 13 条全部来自这根轴**，
         而当时只复刻了轴①，绿灯照样放行（细节见 docs/犯错指南.md §3.10）。
         对策分两层，缺一层都拦不住：
             (a) `VM_BARE_RUNNER=1` —— m2_server/conftest.py 的资源探测把一切都判为
                 "不存在"，于是做了守卫的用例干净 skip、跳过集与 CI 对齐；
             (b) **从 PATH 摘掉含 ffmpeg 的目录** —— 这一层才是关键：(a) 只约束
                 "走了探测"的用例，而 2026-09-13 红掉的 9 条是
                 `subprocess.run(["ffmpeg", ...])` 这种**绕过探测**的写法，
                 本机 PATH 上有它就照样绿。(b) 让这类用例在本机也红。
         注意 CI 会**额外安装** workflow 里声明的东西（如 ffmpeg），所以依赖它们的
         用例在本机模拟下跳过、在 CI 上真跑 —— 两个绿灯合起来才是真绿。
         另：屏幕分辨率这类"环境本身"的差异没法用环境变量模拟，只能靠用例自己
         桩掉（见 test_wechat_record 的 `_fake_user32_screen`）。

设计约定（都是踩过的坑，别改）：

- **必须设 `CODEBUDDY_SAFE_DELETE_ENABLED=0`**：本机 safe-delete shim 会把
  `shutil.rmtree` 包装成「移回收站」，触发 bulk guard → `raise SystemExit(1)`，
  表现为 pytest 随机挂几个、vite build 直接失败（见 docs/犯错指南.md §3.5）。
- **必须设 `VM_WARMUP=0`**：server.py 导入期会预热 TTS/RVC，测试里绝不能真起
  那个吃显存的子进程（conftest.py 用 setdefault 兜了一层，这里再钉死）。
- **ruff 只用 `F,E9`**：默认规则集会掺进大量行宽/风格建议，噪音会让人开始无视
  这个入口；只留「能抓真 bug」的那几条。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ruff 规则集：F=Pyflakes（未定义名/未用变量等真 bug），E9=语法级错误
RUFF_SELECT = "F,E9"

# --fast 用的快速子集：只挑纯内存/纯文件、无模型无网络无 sleep 的用例。
# 预算 <5s（2026-09-11 实测 94 例 3.4s）——超过 10s 的钩子人就会开始用
# --no-verify 绕过，那还不如不要钩子。故意不放 test_pet_market（它用真实线程
# +sleep，单独就 21s）：那类用例交给 pre-push 与 CI 的全量跑。
FAST_TESTS = [
    "m2_server/tests/test_config.py",          # 路径/环境变量解析
    "m2_server/tests/test_common.py",          # 公共校验（voice_id 白名单等）
    "m2_server/tests/test_storage.py",         # 存储读写
    "m2_server/tests/test_history.py",         # 历史落盘/清理
    "m2_server/tests/test_rvc_common.py",      # 进程枚举/强杀的失败路径
    "m2_server/tests/test_offline_vc_infer.py",
    "m2_server/tests/test_tagging.py",
    "m2_server/tests/test_live_settings.py",
    "m2_server/tests/test_play_worker.py",
    "m2_server/tests/test_wechat_uia.py",
    # 硬编码凭据门禁（tools/check_secrets.py 的仓库自检，约 0.05s）：
    # 2026-09-13 那个明文证书密码就是从".githooks 只按文件名拦密钥"的缝里进的仓库，
    # 放进 --fast 才能在提交那一刻拦住。
    "m2_server/tests/test_check_secrets.py",
]


def _console() -> None:
    """把自家 stdout 也切成 UTF-8（中文输出在 GBK 控制台会 UnicodeEncodeError）。

    这类坑在本项目反复出现（审查时就撞过一次 GBK 解码失败的用例），
    子进程靠 PYTHONIOENCODING 解决，本进程在打印前必须先 reconfigure。
    真切不了（重定向到奇怪管道等）也无所谓：汇总里的标记已全部用 ASCII。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _env() -> dict:
    """子进程环境：钉死本项目的两个必需开关。"""
    env = dict(os.environ)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env["VM_WARMUP"] = "0"
    env.setdefault("PYTHONIOENCODING", "utf-8")   # 中文日志在 Windows 控制台不炸
    return env


def _ruff_cmd() -> list[str] | None:
    """优先用当前解释器里的 ruff 模块（版本与 .venv 一致），否则 PATH 上的 ruff。"""
    try:
        import ruff  # noqa: F401
        return [sys.executable, "-m", "ruff"]
    except ImportError:
        pass
    exe = shutil.which("ruff")
    return [exe] if exe else None


def _npx_cmd() -> list[str] | None:
    """Windows 上 npx 是 npx.CMD；shutil.which 会按 PATHEXT 找到它。"""
    exe = shutil.which("npx")
    return [exe] if exe else None


def _say(text: str = "") -> None:
    """打印并立刻 flush。

    子进程是直接写 fd 的，而父进程的 print 在**管道**里是块缓冲的：不 flush 就会看到
    "子进程输出在前、标题在后"的错乱日志（2026-09-13 实测 `| tail` 时就是这样）。
    CI 日志全是管道，所以跟子进程相邻的标题一律走这里。
    """
    print(text, flush=True)


def _run(name: str, cmd: list[str], cwd: Path,
         extra_env: dict | None = None) -> tuple[bool, str]:
    """跑一条检查，输出直接透传给终端（CI 日志要能一眼看懂）。返回 (是否通过, 备注)。

    `extra_env` 里值为 None 表示**从子进程环境里删掉**这个变量（不是设成空串）。
    """
    _say(f"\n=== [{name}] {' '.join(cmd)}")
    env = _env()
    for key, val in (extra_env or {}).items():
        if val is None:
            env.pop(key, None)
        else:
            env[key] = val
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env)
    except FileNotFoundError as exc:
        return False, f"命令不存在：{exc}"
    elapsed = time.perf_counter() - started
    ok = proc.returncode == 0
    _say(f"--- [{name}] {'通过' if ok else '失败'}（{elapsed:.1f}s）")
    return ok, f"{elapsed:.1f}s"


def _bare_runner_env() -> tuple[dict, list[str]]:
    """构造"裸 runner"子进程环境；返回 (extra_env, 从 PATH 里摘掉的目录)。

    为什么要动 PATH，而不只是让探测说"没有"：
        `VM_BARE_RUNNER=1` 只约束**走了探测**的用例（`m2_server/conftest.py` 的
        `ffmpeg_bin` 夹具）。若某个用例绕过探测直接写
        `subprocess.run(["ffmpeg", ...])`，本机 PATH 上有它就照样绿 ——
        而 2026-09-13 CI 上红掉的 9 条，正是这种写法（`test_pet_scan._mk_gif`
        原来就硬编码 `"ffmpeg"`）。把含 ffmpeg 的 PATH 目录摘掉，才能让
        "没做守卫"的用例在本机也红出来。

    RVC 运行环境 / 微调产物是文件系统路径、不在 PATH 上，摘不掉；
    那类依赖已由用例自己桩掉（见 test_rvc_worker 的 `_fake_rvc_env`）。
    """
    dropped: list[str] = []
    kept: list[str] = []
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        if any((Path(part) / n).exists() for n in ("ffmpeg.exe", "ffmpeg")):
            dropped.append(part)
        else:
            kept.append(part)
    extra: dict = {"VM_BARE_RUNNER": "1", "FFMPEG_PATH": None,
                   "PATH": os.pathsep.join(kept)}
    return extra, dropped


def _check_ruff() -> tuple[bool, str]:
    cmd = _ruff_cmd()
    if cmd is None:
        return False, "未安装（pip install ruff；见 requirements-dev.txt）"
    return _run("ruff", cmd + ["check", "m2_server", "tools", "--select", RUFF_SELECT,
                               "--output-format", "concise"], ROOT)


def _check_pytest(fast: bool) -> tuple[bool, str]:
    targets = FAST_TESTS if fast else ["m2_server"]
    return _run("pytest" + ("(fast)" if fast else ""),
                [sys.executable, "-m", "pytest", *targets, "-q", "--no-header"], ROOT)


def _check_web() -> tuple[bool, str]:
    web = ROOT / "web"
    if not (web / "package.json").exists():
        return False, "未找到 web/package.json"
    if not (web / "node_modules").is_dir():
        return False, "web/node_modules 缺失（先 cd web && npm ci）"
    cmd = _npx_cmd()
    if cmd is None:
        return False, "未找到 npx（需要 Node.js）"
    return _run("tsc", cmd + ["tsc", "-b", "--pretty", "false"], web)


#: `tools/test-*.cjs` 里**已被专属步骤跑过**的两个，别在 nodetest 里重复一遍
NODE_TESTS_OWNED_BY_OTHER_STEPS = {
    "test-check-require.cjs",   # → requires 步
    "test-electron-load.cjs",   # → electron 步
}

#: 不属于 tools/test-*.cjs、但同样该由本步守护的独立冒烟脚本
NODE_SMOKES = ["web/electron/smoke-loadpath.cjs"]

#: 入口脚本里"直接解析 electron"的写法（含 `require.resolve("electron", …)`）
_STRAY_ELECTRON_RE = re.compile(r"""require(?:\.resolve)?\s*\(\s*['"]electron['"]""")


def _stray_electron_requires(path: Path) -> list[int]:
    """返回入口脚本里"直接解析 electron"的行号（忽略注释行）。

    为什么是硬错误（2026-09-14 CI 事故）：本步在 CI 的 backend job 里跑，而那个 job
    **不装 npm 依赖**。于是任何 `require("electron")` / `require.resolve("electron")`
    都会 `MODULE_NOT_FOUND` —— 脚本只能在装了 `web/node_modules` 的本机跑，又回到
    "测试存在却从不在门禁里跑"的老坑（犯错指南 §3.15）。
    正确写法：先 `installElectronStub(...)`，再 require 受测模块。

    本机有 node_modules，所以**本地跑是绿的**，光看结果发现不了 —— 只能静态拦。
    """
    bad: list[int] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if _STRAY_ELECTRON_RE.search(line):
            bad.append(lineno)
    return bad


def _check_node_tests() -> tuple[bool, str]:
    """跑 `tools/test-*.cjs` 全部主进程/工具层单元测试（外加 smoke-loadpath）。

    为什么必须补这一步：这些脚本 2026-09-14 之前**没有任何入口跑它们** ——
    它们看起来像存在着，其实一直是死的。首次接入时当场发现
    `tools/test-setup-ipc.cjs` 已经红了 3 条（上一轮改首启引导时改坏了契约，
    没人知道）。**没人跑的门禁比没有门禁更糟**：它会让人以为有保护。

    用 glob 而不是写死列表：以后新加一个 `tools/test-xxx.cjs` 自动进网，
    不需要有人记得回来改这里。

    约 3.5s（8 个 node 进程的启动开销占大头），所以**不进 --fast** ——
    pre-commit 要保持 8s 量级，全量/pre-push/CI 才跑它。

    没有 node 时跳过而非失败：跳过是诚实的，假装通过不是。

    **裸 runner 复刻（第三根轴）**：CI 的 backend job **从不 `npm install`**，
    所以入口脚本必须能在没有 `web/node_modules` 时跑。本机装了 node_modules，
    动态跑永远绿 —— 2026-09-14 就是这样让 CI 当场红了 2/7。做法是
    `--ci-fidelity`（`VM_BARE_RUNNER=1`）期间把 `web/node_modules` 临时改名挪开，
    跑完在 finally 里挪回来（同一分区 rename，O(1)，不动数据）。
    静态守卫（`_stray_electron_requires`）仍然保留：它拦的是**写法**，
    挪目录只能拦 electron 这一种 npm 依赖。
    """
    node = shutil.which("node")
    if node is None:
        return True, "跳过（未找到 node）"
    scripts = sorted(
        p for p in (ROOT / "tools").glob("test-*.cjs")
        if p.name not in NODE_TESTS_OWNED_BY_OTHER_STEPS
    )
    scripts += [ROOT / rel for rel in NODE_SMOKES]
    if not scripts:
        return False, "一个 node 测试都没找到（glob 写错了？）"

    hidden = _hide_node_modules()
    if hidden is False:
        return False, "无法临时移开 web/node_modules（裸 runner 复刻失败，未跑任何脚本）"
    try:
        failed: list[str] = []
        for path in scripts:
            if not path.exists():
                failed.append(f"{path.name}（文件不存在）")
                continue
            stray = _stray_electron_requires(path)
            if stray:
                where = ", ".join(str(n) for n in stray)
                failed.append(f"{path.name}（第 {where} 行直接解析 electron：CI 不装 npm 依赖，"
                              f"须改用 electron-stub）")
                continue
            ok, _note = _run(path.stem, [node, str(path)], ROOT)
            if not ok:
                failed.append(path.name)
    finally:
        _unhide_node_modules(hidden)
    if failed:
        return False, f"{len(failed)}/{len(scripts)} 个失败：{', '.join(failed)}"
    suffix = "（裸 runner：node_modules 已临时移开）" if hidden else ""
    return True, f"{len(scripts)} 个脚本全绿{suffix}"


#: `web/node_modules` 在裸 runner 复刻期间被挪到的名字（与源目录同分区，rename 即可）
NODE_MODULES_HIDDEN = "web/__node_modules_hidden_for_bare_runner__"


def _hide_node_modules():
    """裸 runner 复刻：临时移开 `web/node_modules`。

    返回 True（已移开）/ False（就是本次调用留下的残留，不该发生）/ None（未启用或没有目录）。
    只在 `VM_BARE_RUNNER=1`（即 `--ci-fidelity`）时启用：日常全量跑要保持本机原样，
    否则每次跑都动一次 node_modules 太容易出意外。
    """
    if os.environ.get("VM_BARE_RUNNER") != "1":
        return None
    nm = ROOT / "web" / "node_modules"
    dst = ROOT / NODE_MODULES_HIDDEN
    if dst.exists():
        # 上一次被强杀留下的残留：这次先还原，绝不删
        _say(f"[nodetest] 发现上次残留 {NODE_MODULES_HIDDEN}，先还原")
        _unhide_node_modules(True)
    if not nm.is_dir():
        return None                      # 本机本来就没装，等价于裸 runner
    try:
        nm.rename(dst)
    except OSError as exc:
        _say(f"[nodetest] 移开 web/node_modules 失败：{exc}")
        return False
    _say(f"[nodetest] 裸 runner 复刻：web/node_modules → {NODE_MODULES_HIDDEN}")
    return True


def _unhide_node_modules(hidden) -> None:
    """把 `web/node_modules` 挪回来。`hidden` 为真时无条件尝试（失败也不抛）。"""
    if not hidden:
        return
    nm = ROOT / "web" / "node_modules"
    dst = ROOT / NODE_MODULES_HIDDEN
    if dst.is_dir() and not nm.exists():
        try:
            dst.rename(nm)
            _say("[nodetest] 已还原 web/node_modules")
        except OSError as exc:                      # 还原失败必须吼出来，别静默
            _say(f"[nodetest] ✗ 还原 web/node_modules 失败：{exc}")
            _say(f"[nodetest] ✗ 手工执行：mv {NODE_MODULES_HIDDEN} web/node_modules")


def _check_licenses() -> tuple[bool, str]:
    """第三方许可登记门禁：`THIRD_PARTY_NOTICES.md` 的覆盖性必须对得上当前依赖集。

    为什么进这个入口：许可漏登记是**事后补不回来**的 —— 安装包一旦发出去就已经违规，
    而症状是零（没人会报错）。唯一能拦的地方就是"加依赖的那一次提交"。

    纯本地文本解析、零依赖、约 0.05s，所以也进 --fast（pre-commit）。
    只判"有没有漏"，不判"许可填得对不对"：后者要人回溯一手来源，机器判断不了。
    """
    script = ROOT / "tools" / "audit_licenses.py"
    if not script.exists():
        return False, "未找到 tools/audit_licenses.py"
    return _run("licenses", [sys.executable, str(script)], ROOT)


def _check_requires() -> tuple[bool, str]:
    """静态检查 electron/*.cjs 里「用了内建模块标识符但没 require」。

    2026-09-12 事故：alt-hint.cjs 从 main.cjs 拆出时漏了 require("fs") 却用 fs.existsSync，
    开发模式没暴露、打包后主进程 require 阶段即崩（ReferenceError: fs is not defined），
    用户装了 0.2.1/0.2.2 打不开。这类 bug 编译器不报，只在启动路径上炸 —— 必须机器拦。

    零依赖、只读 13 个文件、约 0.1s，所以进 --fast（pre-commit）名单是划算的。
    """
    script = ROOT / "tools" / "check-require.cjs"
    if not script.exists():
        return False, "未找到 tools/check-require.cjs"
    node = shutil.which("node")
    if node is None:
        return False, "未找到 node（需要 Node.js）"
    return _run("requires", [node, str(script)], ROOT)


def _check_electron_load() -> tuple[bool, str]:
    """用 electron 桩 require 全部 electron/*.cjs，require 阶段崩即 FAIL。

    与 requires 互补：静态扫描只能抓「用了内建模块标识符却没 require」，抓不到
      · require 了不存在的路径（文件名拼错）
      · 顶层求值期访问 undefined
      · 模块顶层副作用崩溃
    桩加载是**直接验证**（真跑一遍 require），比静态扫描更硬。实测约 0.15s。
    """
    script = ROOT / "tools" / "test-electron-load.cjs"
    if not script.exists():
        return False, "未找到 tools/test-electron-load.cjs"
    node = shutil.which("node")
    if node is None:
        return False, "未找到 node（需要 Node.js）"
    return _run("electron-load", [node, str(script)], ROOT)


STEPS = {
    "licenses": lambda fast: _check_licenses(),
    "requires": lambda fast: _check_requires(),
    "electron": lambda fast: _check_electron_load(),
    "nodetest": lambda fast: _check_node_tests(),
    "ruff": lambda fast: _check_ruff(),
    "pytest": lambda fast: _check_pytest(fast),
    "web": lambda fast: _check_web(),
}


# ============ CI 保真复刻（--ci-fidelity）============
# 为什么需要：见模块 docstring。一句话 —— 本机 .venv 全绿不代表 CI 绿（2026-09-13
# 就是靠这个手段发现 Pillow/comtypes 从未被声明）。
#
# 这里只做三件事：建/更新干净 venv → 用它跑 CI 那条命令 → 报告哪些差异没被复刻。
# 刻意**不写死版本号**，而是从 ci.yml 读：写死的话"复刻"自己会跟 CI 漂移。

CI_VENV = ROOT / ".venv-ci"                    # 已 gitignore（见 .gitignore 的"虚拟环境"段）
CI_REQ = ROOT / "requirements-dev.txt"
CI_STAMP = CI_VENV / ".ci-deps-stamp"          # 内容 = requirements 的 sha256 + Python 版本
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# 不引 PyYAML（check.py 保持零依赖），只抠当前 ci.yml 的写法。
# 抠不到就回落默认值，**并在报告里写明**——宁可说清"本次没按 CI 的版本跑"，
# 也不要装作复刻成功了。
_CI_PY_VER_RE = re.compile(r"python-version:\s*[\"']?(\d+\.\d+)")
_CI_NODE_VER_RE = re.compile(r"node-version:\s*[\"']?(\d+(?:\.\d+)*)")
DEFAULT_CI_PY = "3.11"
DEFAULT_CI_NODE = "22"


def _ci_workflow_text() -> str:
    try:
        return CI_WORKFLOW.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ci_python_version(ci_text: str) -> str | None:
    m = _CI_PY_VER_RE.search(ci_text)
    return m.group(1) if m else None


def _ci_node_version(ci_text: str) -> str | None:
    m = _CI_NODE_VER_RE.search(ci_text)
    return m.group(1) if m else None


def _deps_stamp(req_bytes: bytes, py_version: str) -> str:
    """依赖指纹：requirements-dev.txt 的内容 + Python 版本。任一变化就重装。"""
    return f"{hashlib.sha256(req_bytes).hexdigest()[:12]}|{py_version}"


def _venv_python(venv: Path) -> Path:
    """venv 里的解释器路径（Windows 与 POSIX 不同）。"""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe_python(cmd: list[str]) -> str | None:
    """问一个启动器要 `X.Y` 形式的版本号；不存在/不可执行返回 None。"""
    try:
        proc = subprocess.run(
            [*cmd, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _find_python(want: str) -> tuple[list[str], str | None]:
    """找与 CI 钉的版本一致的解释器；找不到就用当前解释器（报告里会标注）。"""
    candidates: list[list[str]] = []
    for name in (f"python{want}", "python3", "python"):
        exe = shutil.which(name)
        if exe:
            candidates.append([exe])
    if os.name == "nt":
        candidates.append(["py", f"-{want}"])       # Windows 官方启动器
    candidates.append([sys.executable])
    fallback: tuple[list[str], str | None] | None = None
    for cmd in candidates:
        version = _probe_python(cmd)
        if version == want:
            return cmd, version
        if version and fallback is None:
            fallback = (cmd, version)
    return fallback or ([sys.executable], None)


def _probe_node() -> str | None:
    exe = shutil.which("node")
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "-v"], capture_output=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return None
    out = proc.stdout.strip()
    return out.lstrip("v") or None


def _ensure_ci_venv(recreate: bool) -> tuple[Path | None, str, str]:
    """建/更新瘦 venv。返回（venv 里的解释器, 想要的版本, 实际用的版本）。

    用 `venv --clear` 实现重建：它删目录内容是 stdlib 行为，**不走 shutil.rmtree**
    —— 本机的 safe-delete shim 会拦 rmtree（见 docs/犯错指南.md §3.5）。
    """
    ci_text = _ci_workflow_text()
    want = _ci_python_version(ci_text)
    if not want:
        want = DEFAULT_CI_PY
        _say(f"[ci-fidelity] 警告：读不到 ci.yml 的 python-version，回落 {want}")
    launcher, real = _find_python(want)
    if real != want:
        _say(f"[ci-fidelity] 警告：找不到 Python {want}，用 {real or '未知'} 代替（保真度下降）")

    py = _venv_python(CI_VENV)
    if recreate or not py.exists():
        cmd = [*launcher, "-m", "venv", *(["--clear"] if recreate else []), str(CI_VENV)]
        _say(f"[ci-fidelity] {' '.join(cmd)}")
        if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
            return None, want, real or ""

    stamp_now = _deps_stamp(CI_REQ.read_bytes(), real or want)
    try:
        stamp_old = CI_STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        stamp_old = ""
    if stamp_old == stamp_now and not recreate:
        _say(f"[ci-fidelity] 依赖指纹未变（{stamp_now}），跳过 pip install")
    else:
        # 先说 --recreate：它自己会清掉 stamp，否则会误报成"首次建立"（日志要能解释自己）
        if recreate:
            why = "--recreate（--clear 重建）"
        elif not stamp_old:
            why = "首次建立"
        else:
            why = "依赖或版本变了"
        _say(f"[ci-fidelity] 装瘦环境依赖（{why}）：{CI_REQ.name}")
        for cmd in ([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                    [str(py), "-m", "pip", "install", "-q", "-r", str(CI_REQ)]):
            if subprocess.run(cmd, cwd=str(ROOT), env=_env()).returncode != 0:
                _say(f"[ci-fidelity] 依赖安装失败，可手跑看详情：{' '.join(cmd)}")
                return None, want, real or ""
        CI_STAMP.write_text(stamp_now + "\n", encoding="utf-8")
    return py, want, real or want


def _check_ci_fidelity(recreate: bool = False) -> int:
    """在干净 venv 里跑 CI 的那条命令，并诚实报告"哪些差异没被复刻"。"""
    _say(f"\n=== [ci-fidelity] 复刻 CI（干净 venv：{CI_VENV.name}/）")
    py, want, real = _ensure_ci_venv(recreate)
    if py is None:
        _say("\n[ci-fidelity] 瘦环境准备失败，本项未完成。")
        return 2

    # 与 ci.yml 的 backend job 逐字相同（同一脚本、同一开关），只多一份"裸 runner"环境：
    # 让"本机有、runner 没有"的资源（ffmpeg / D:\RVC / 微调产物）真的缺席。
    # 这不是多余的谨慎 —— 2026-09-13 CI 首跑红的 13 条**全部**来自这根轴，
    # 而当时的 --ci-fidelity 只看依赖集，照样给了绿灯。
    # 判定规则见 m2_server/conftest.py 顶部「本机资源探测」。
    bare_env, dropped = _bare_runner_env()
    _say("[ci-fidelity] 本机资源按裸 runner 模拟（VM_BARE_RUNNER=1）")
    for d in dropped:
        _say(f"[ci-fidelity]   · 已从 PATH 摘掉（含 ffmpeg）：{d}")
    if not dropped:
        _say("[ci-fidelity]   · 注意：PATH 上没找到 ffmpeg，本机本来就等价于裸 runner")
    backend_ok, _ = _run("ci-backend",
                         [str(py), str(ROOT / "tools" / "check.py"), "--no-web"], ROOT,
                         extra_env=bare_env)

    # 与 web job 对应，但只能本机近似：CI 跑在 Linux + npm ci 全新安装
    web_ok: bool | None = None
    if shutil.which("npx"):
        web_ok, _ = _check_web()
    else:
        _say("\n=== [ci-web] 跳过：本机没有 npx")

    ci_node = _ci_node_version(_ci_workflow_text()) or DEFAULT_CI_NODE
    ok = backend_ok and web_ok is not False
    print("\n================ ci-fidelity 汇总 ================")
    print(f"  [{'PASS' if backend_ok else 'FAIL'}] 后端 job（requires/electron/ruff/pytest）")
    print(f"  [{'SKIP' if web_ok is None else ('PASS' if web_ok else 'FAIL')}] 前端 job（tsc -b，本机近似）")
    print(f"  Python：CI 钉 {want} / 本次实际 {real}")
    print(f"  node  ：CI 钉 {ci_node} / 本机 {_probe_node() or '未知'}")
    print("  本机资源：已按裸 runner 模拟（VM_BARE_RUNNER=1），跳过集应与 CI 对齐")
    print("\n  结论：" + ("CI 应会绿（后端+前端都过）" if ok else "CI 仍会红，先修上面 FAIL 的那步"))
    print("""
  未被复刻的差异（别把这里的绿当成 CI 一定绿）：
    · runner 是全新的 windows-latest / ubuntu-latest 镜像，本机不是
    · 前端 job 跑在 Linux：大小写敏感 + npm ci 全新安装，本机只能近似
    · 偶发并发/时序问题（历史上撞过一次随机挂的队列用例），本机不复现不代表没有
    · 本机资源已按"缺失"模拟；但 CI 会**额外安装** workflow 里声明的东西
      （如 ffmpeg）—— 依赖它们的用例在本机模拟下跳过、在 CI 上真跑。
      所以两边都绿才是真绿：这里绿 = 没有用例会在裸机器上崩，
      全量自检（钩子紧接着跑的那条）绿 = 那些用例本身也过。""")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变声项目一键自检")
    ap.add_argument("--fast", action="store_true",
                    help="快速模式：ruff + 快速测试子集，跳过前端类型检查")
    ap.add_argument("--no-web", action="store_true", help="跳过前端 tsc 检查")
    ap.add_argument("--only", default="",
                    help="只跑指定项（逗号分隔：requires,electron,ruff,pytest,web）")
    ap.add_argument("--list", action="store_true", help="只列出将要执行的命令")
    ap.add_argument("--ci-fidelity", action="store_true",
                    help="复刻 CI：在只装 requirements-dev.txt 的干净 venv（.venv-ci/）里"
                         "跑 CI 那条命令，并把本机资源按裸 runner 模拟（VM_BARE_RUNNER=1）。"
                         "改了依赖/加了测试/改了 CI 配置后跑它（约 3 分钟）")
    ap.add_argument("--recreate", action="store_true",
                    help="配合 --ci-fidelity：先 --clear 重建瘦 venv（手工装过东西的 venv 不可信）")
    args = ap.parse_args(argv)
    _console()

    # CI 保真复刻是独立模式：它不跑"当前环境"的检查，而是先造一个干净环境再回来跑，
    # 所以 --only/--fast 对它无效（与 ci.yml 保持一致才是它的全部意义）。
    if args.ci_fidelity:
        return _check_ci_fidelity(recreate=args.recreate)

    # 顺序按「快 → 慢」：licenses/requires/electron/ruff 都是毫秒级静态或直接加载检查，
    # 放前面先拦低级错误；nodetest（约 3.5s）与 pytest 居中；web（tsc）最慢，只在非 --fast 时跑。
    # requires + electron 都进 fast：合计约 0.25s，专治「拆文件漏 require」
    # 这类启动即崩、编译器又不报的 bug（2026-09-12 事故）。
    # licenses 进 fast：许可漏登记只有"加依赖那一次提交"能拦，且只要 0.05s。
    # nodetest 不进 fast：8 个 node 进程的启动开销就 3.5s，而 pre-commit 只有 8s 预算 ——
    # 让钩子变慢，人就该开始绕过它了。全量 / pre-push / CI 都会跑。
    names = [n.strip() for n in args.only.split(",") if n.strip()] or [
        "licenses", "requires", "electron", "ruff", "nodetest", "pytest", "web"
    ]
    if args.fast:
        names = [n for n in names if n not in ("web", "nodetest")]
    if args.no_web:
        names = [n for n in names if n != "web"]
    unknown = [n for n in names if n not in STEPS]
    if unknown:
        print(f"未知检查项：{unknown}（可选：{', '.join(STEPS)}）")
        return 2

    if args.list:
        print(f"项目根：{ROOT}")
        print(f"将执行：{', '.join(names)}" + ("（fast）" if args.fast else ""))
        print(f"pytest 目标：{FAST_TESTS if args.fast else ['m2_server']}")
        return 0

    print(f"变声项目自检 · 根目录 {ROOT}{'（fast）' if args.fast else ''}")
    results: list[tuple[str, bool, str]] = []
    for name in names:
        ok, note = STEPS[name](args.fast)
        results.append((name, ok, note))
        if not ok:
            break        # 失败即停：先修最前面那个，别让后面的噪音淹没真问题

    print("\n================ 自检汇总 ================")
    for name, ok, note in results:
        # 标记一律用 ASCII：本机控制台是 GBK，emoji 会直接把脚本自己搞崩
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<14} {note}")
    skipped = [n for n in names if n not in [r[0] for r in results]]
    if skipped:
        print(f"  [SKIP] 未执行（前一项失败）：{', '.join(skipped)}")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"\n结果：未通过（{', '.join(r[0] for r in failed)}）")
        return 1
    print("\n结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

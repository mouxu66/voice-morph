"""一键自检 —— 交付前 / CI / git 钩子的唯一入口。

背景：本项目铁律 1/2 是「改动即提交、补测且全绿」，但此前**没有任何机器可执行的
检查入口**：跑不跑、跑哪些、环境变量设没设，全靠人记得。2026-09-11 的代码审查
就撞上两次「文档写着全绿、实际已过期」（GBK 编码失败的用例、随机挂的队列用例）。

本脚本把三个检查串起来，顺序按「快 → 慢」，失败即停并返回非零：

    1. ruff        —— 静态扫描，专抓真 bug 类规则（F/E9：未定义名、未用变量、
                      f-string 缺占位符、语法错误）。实测抓到过 rvc_common 的
                      未定义 logger（生产代码 NameError）。
    2. pytest      —— m2_server 全量（默认）或快速子集（--fast）。
    3. tsc -b      —— web 前端类型检查（不产出 dist）。

用法：

    python tools/check.py                 # 全量（= 交付前 / CI 跑的那条）
    python tools/check.py --fast          # 提交前（pre-commit）：ruff + 快速子集
    python tools/check.py --no-web        # 没有 Node 环境时
    python tools/check.py --only pytest   # 只跑某一项（逗号分隔）
    python tools/check.py --list          # 只看会跑什么，不执行

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
import os
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


def _run(name: str, cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """跑一条检查，输出直接透传给终端（CI 日志要能一眼看懂）。返回 (是否通过, 备注)。"""
    print(f"\n=== [{name}] {' '.join(cmd)}")
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=_env())
    except FileNotFoundError as exc:
        return False, f"命令不存在：{exc}"
    elapsed = time.perf_counter() - started
    ok = proc.returncode == 0
    print(f"--- [{name}] {'通过' if ok else '失败'}（{elapsed:.1f}s）")
    return ok, f"{elapsed:.1f}s"


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


STEPS = {
    "ruff": lambda fast: _check_ruff(),
    "pytest": lambda fast: _check_pytest(fast),
    "web": lambda fast: _check_web(),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变声项目一键自检")
    ap.add_argument("--fast", action="store_true",
                    help="快速模式：ruff + 快速测试子集，跳过前端类型检查")
    ap.add_argument("--no-web", action="store_true", help="跳过前端 tsc 检查")
    ap.add_argument("--only", default="", help="只跑指定项（逗号分隔：ruff,pytest,web）")
    ap.add_argument("--list", action="store_true", help="只列出将要执行的命令")
    args = ap.parse_args(argv)
    _console()

    names = [n.strip() for n in args.only.split(",") if n.strip()] or ["ruff", "pytest", "web"]
    if args.fast:
        names = [n for n in names if n != "web"]
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

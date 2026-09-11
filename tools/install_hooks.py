"""把仓库自带的 git 钩子挂上（幂等，可反复跑）。

    python tools/install_hooks.py            # 安装（= git config --local core.hooksPath .githooks）
    python tools/install_hooks.py --check    # 只看当前状态，不改动
    python tools/install_hooks.py --uninstall

为什么用 `core.hooksPath` 而不是往 `.git/hooks/` 里拷脚本：
`.git/hooks/` 不入版本库，换台机器/重新 clone 就没了；`.githooks/` 跟着仓库走，
新克隆只需再跑一次本脚本。**这是 --local 配置，只影响本仓库，不动全局 git 配置。**

挂上之后：

    commit → .githooks/pre-commit  拦密钥文件 + tools/check.py --fast（<5s）
    push   → .githooks/pre-push    tools/check.py 全量（ruff + pytest + tsc，~90s）

对应 AGENTS.md 铁律 1/2 与「不跳过 git hooks」那条红线。真的需要临时绕过时用
`git commit --no-verify`，但请先看一眼钩子报了什么——它通常是对的。
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ".githooks"
REQUIRED = ("pre-commit", "pre-push")


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout.strip()


def current() -> str:
    """当前 core.hooksPath（未设置返回空串）。"""
    try:
        return _git("config", "--local", "--get", "core.hooksPath").strip()
    except SystemExit:
        return ""


def verify() -> int:
    """检查脚本文件是否齐、hooksPath 是否指对。返回 0/1。"""
    ok = True
    for name in REQUIRED:
        p = ROOT / HOOKS_DIR / name
        if not p.is_file():
            print(f"  [缺失] {HOOKS_DIR}/{name}")
            ok = False
    path = current()
    if path == HOOKS_DIR:
        print(f"  [已挂载] core.hooksPath = {path}")
    else:
        print(f"  [未挂载] core.hooksPath = {path or '(空)'}；运行：python tools/install_hooks.py")
        ok = False
    # 顺手提醒：Git for Windows 需要能执行 sh 脚本，钩子的 shebang 是 /bin/sh
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安装/检查本仓库自带的 git 钩子")
    ap.add_argument("--check", action="store_true", help="只检查当前状态")
    ap.add_argument("--uninstall", action="store_true", help="取消挂载")
    args = ap.parse_args(argv)

    print(f"仓库根：{ROOT}")
    if args.check:
        return verify()

    if args.uninstall:
        if current():
            _git("config", "--local", "--unset", "core.hooksPath")
            print(f"已取消 core.hooksPath（原先为 {HOOKS_DIR}）")
        else:
            print("本来就没挂载，无需处理")
        return 0

    missing = [n for n in REQUIRED if not (ROOT / HOOKS_DIR / n).is_file()]
    if missing:
        raise SystemExit(f"缺少钩子脚本：{', '.join(missing)}（仓库不完整？）")

    _git("config", "--local", "core.hooksPath", HOOKS_DIR)
    print(f"已设置 core.hooksPath = {HOOKS_DIR}（--local，只影响本仓库）")

    # 把钩子脚本的可执行位补上：Windows 上 clone 出来的可能是 644，
    # Git for Windows 会忽略执行位，但别的系统（或 WSL）会因此报权限错误。
    for name in REQUIRED:
        p = ROOT / HOOKS_DIR / name
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    print("钩子清单：")
    print("  commit → 拦密钥入库 + tools/check.py --fast")
    print("  push   → tools/check.py 全量（ruff + pytest + tsc）")
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())

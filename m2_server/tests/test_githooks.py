"""git 钩子自身的回归测试。

为什么需要（2026-09-13）：
    推送 tag 时 `git push origin v0.2.0-tag` 被 pre-push 钩子拦下，报
    `fatal: ambiguous argument '@{u}..HEAD'`。根因是钩子这样取上游：

        upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)

    `rev-parse` 解析不出上游时**会把 `@{u}` 原样打到 stdout** 再报错；`2>/dev/null`
    只吞掉了错误，`|| true` 让赋值成功 —— 于是 `upstream` 成了字面量 `@{u}`，
    后面 `git diff "$upstream"..HEAD` 必炸，`set -e` 直接中止推送。

    触发条件并不罕见：`git filter-repo` 会移除 remote，重新加回并 `push -u` 之后，
    若 `refs/remotes/origin/<branch>` 不存在（本机环境甚至会静默丢弃对该目录的写入），
    `git branch -vv` 就显示 `[origin/master: gone]`，`@{u}` 解析失败。

    钩子崩掉的后果比"少跑一次检查"严重得多：**它会让所有推送失败**，而
    `--no-verify` 是项目红线。所以这里用真实 `git push` 兜住这条路径。

手法：造一个最小仓库 + 本地裸远端，把**真实钩子**装进去，并把 `tools/check.py`
换成"立刻成功"的桩 —— 这样能只测钩子的控制流，不必真跑 6 分钟的全量门禁。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / ".githooks"


def _run(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd)


def test_all_hooks_are_valid_posix_sh():
    """语法错误的钩子 = 所有提交/推送全挂，且报错信息通常很难懂。"""
    hooks = sorted(p for p in HOOKS_DIR.iterdir() if p.is_file())
    assert hooks, f"未找到任何钩子：{HOOKS_DIR}"
    for hook in hooks:
        r = _run(["sh", "-n", str(hook)], cwd=ROOT)
        assert r.returncode == 0, f"{hook.name} 语法错误：{r.stderr.strip()}"


@pytest.fixture()
def repo_with_hook(tmp_path: Path) -> Path:
    """最小仓库：真实 pre-push 钩子 + 桩掉的 tools/check.py + 本地裸远端。"""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "-q", "--bare"], bare)

    repo = tmp_path / "work"
    repo.mkdir()
    _git(["init", "-q", "-b", "master"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)

    # 真实钩子（连同 hooksPath 配置，与项目一致）
    shutil.copytree(HOOKS_DIR, repo / ".githooks")
    _git(["config", "core.hooksPath", ".githooks"], repo)

    # 桩：立刻成功，避免真跑全量门禁（那是 push 时的正常行为，不是本测试的对象）
    (repo / "tools").mkdir()
    (repo / "tools" / "check.py").write_text(
        "import sys\nprint('[stub] check.py')\nsys.exit(0)\n", encoding="utf-8"
    )

    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    return repo


def _hook_env() -> dict:
    """把当前解释器所在目录塞进 PATH，让钩子的 `command -v python` 能命中。"""
    env = dict(os.environ)
    py_dir = str(Path(sys.executable).parent)
    env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
    return env


def test_pre_push_survives_unresolvable_upstream(repo_with_hook: Path):
    """上游配置存在但跟踪引用缺失（`[origin/master: gone]`）时，推送必须照常成功。

    这正是 2026-09-13 推 tag 失败的场景。修复前钩子会以非 0 退出并中止推送。
    """
    repo = repo_with_hook
    # 造出 "配置里有上游、但 refs/remotes/origin/master 不存在" 的状态
    _git(["config", "branch.master.remote", "origin"], repo)
    _git(["config", "branch.master.merge", "refs/heads/master"], repo)
    assert _git(["for-each-ref", "refs/remotes"], repo).stdout.strip() == "", \
        "前置条件：不应存在远程跟踪引用"

    r = _run(["git", "push", "origin", "master"], repo, env=_hook_env())
    assert r.returncode == 0, (
        "上游不可解析时钩子不应中止推送。\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
    # 确认确实推上去了，而不是"钩子早退跳过了一切"
    assert "master" in _git(["ls-remote", str(repo.parent / "remote.git")], repo).stdout


def test_pre_push_runs_with_no_upstream_configured(repo_with_hook: Path):
    """首次 push（完全没有上游配置）同样要能通过 —— 这条是原有行为，防回归。"""
    r = _run(["git", "push", "origin", "master"], repo_with_hook, env=_hook_env())
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

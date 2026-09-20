#!/usr/bin/env python
"""核验「桌面端后端副本」与源码是否零漂移（逐字节）。

用法::

    # 默认目标：%LOCALAPPDATA%\\Programs\\voice-morph-desktop\\resources\\backend
    python tools/verify_backend_sync.py

    # 显式指定目标（比如源码根下的 staging 目录）
    python tools/verify_backend_sync.py --target D:\\变声\\voice-morph-desktop\\resources\\backend

    # 连"可忽略项"的明细也打出来
    python tools/verify_backend_sync.py --all

退出码：``0`` = 生产代码零漂移；``1`` = 有漂移；``2`` = 目标目录不存在。

为什么要有这个脚本（而不是一行 shell）？
----------------------------------------
**不要用 `md5sum` 逐文件对比 Windows 路径。** Git Bash / MSYS2 的 coreutils
`md5sum` 在文件名含反斜杠时会在哈希前加一个 ``\\`` 转义前缀：

    $ md5sum m2_server/common.py
    e6f32025c093741f618c21b6a304e2de *m2_server/common.py      ← 相对路径，无前缀
    $ md5sum "C:\\Users\\<用户名>\\...\\common.py"
    \\e6f32025c093741f618c21b6a304e2de *C:\\\\Users\\\\...        ← 绝对路径，有前缀

哈希其实一样，但 ``cut -d' ' -f1`` 拿到的字符串字面不等 →
**一侧相对路径一侧绝对路径时全部文件假红**（本机实测 68 个 .py 全报不一致）；
更危险的是**两侧都用绝对路径时全部假绿**，把「半新半旧混装」放过去
（见 ``docs/犯错指南.md`` §2.28 / §2.29）。

本脚本用 Python 按字节比对，不受 shell 路径形态影响，且一次遍历同时报出
「内容不一致 / 目标缺失 / 目标多余」三类差异。

生产代码 vs 可忽略项
--------------------
副本里**本来就不该有**的东西（测试目录、pytest 缓存、运行产物、备份）单独归类，
不计入失败 —— 否则几十条噪音会把真正的漂移埋掉。但**忽略项一律显式列出**
（``--all`` 看明细），不做静默过滤：静默过滤正是 §2.30 那个坑的成因。

覆盖范围与 ``m2_server/backend_autosync.py`` 的 ``_SYNC_PAIRS`` 保持一致
（``m2_server`` / ``tools`` / ``web/dist`` → ``web_dist``），另额外核对
``web/electron/pet``（**不在** autosync 镜像范围内，需要手动拷，最容易漏）。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

# 让 `import backend_autosync` 生效（本脚本在 tools/ 下，被测模块在 m2_server/ 下）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "m2_server"))

import backend_autosync

#: 额外核对项：(源码相对路径, 副本内目标名, 说明)
#: `web/electron/pet` 是**渲染侧**文件，不在 autosync 镜像范围里，
#: 安装版从磁盘读，所以每次改完都得手动拷 —— 单独列出来提醒。
_EXTRA_PAIRS: list[tuple[str, str, str]] = [
    ("web/electron/pet", "web/electron/pet", "桌宠渲染侧（需手动拷）"),
]

#: 可忽略项的判定规则：`(相对路径片段/后缀, 理由)`，按顺序第一个命中即生效。
#: ⚠️ 这些是「副本里本来就不该有」的东西，不算漂移；但**必须显式报出来**。
#: 判定依据都经过引用核查（`grep` 全仓无生产代码引用），不是拍脑袋加的。
_IGNORE_RULES: list[tuple[str, str]] = [
    (".pytest_cache/", "pytest 缓存"),
    (".pytest_cache", "pytest 缓存"),
    ("/tests/", "测试文件（安装版不需要）"),
    ("tests/", "测试文件（安装版不需要）"),
    ("desktop-control/out/", "运行产物（截图）"),
    ("/outputs/", "运行产物"),
    ("outputs/", "运行产物"),
    (".bak-", "备份文件"),
    (".orig", "备份文件"),
    # 开发期独立工具：安装版运行时不调用（2026-09-18 全仓 grep 确认无引用）
    ("desktop-control/", "开发期工具（桌面控制）"),
    ("wx_green_judge_check.py", "开发期工具（离线测量）"),
    ("verify_backend_sync.py", "开发期工具（本核验脚本自身）"),
]


def _ignore_reason(rel: str) -> str | None:
    """相对路径属于「可忽略项」时返回理由，否则 None。"""
    low = rel.replace("\\", "/")
    for token, reason in _IGNORE_RULES:
        if token in low:
            return reason
    return None


def _default_target() -> Path | None:
    """默认目标：已安装的桌面端后端目录。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / "Programs" / "voice-morph-desktop" / "resources" / "backend"


def _diff_pair(src_root: Path, src_rel: str, dst_root: Path, dst_rel: str) -> dict:
    """对比一对目录，把差异分成「生产代码」与「可忽略项」两类。"""
    src = src_root / src_rel
    dst = dst_root / dst_rel
    res: dict = {
        "src_rel": src_rel,
        "dst_rel": dst_rel,
        "src_absent": False,
        "dst_absent": False,
        "changed": [], "missing": [], "extra": [],
        "ign_changed": [], "ign_missing": [], "ign_extra": [],
        "prod_total": 0, "ign_total": 0,
    }
    if not src.is_dir():
        res["src_absent"] = True
        return res

    src_files = backend_autosync._walk(src)
    if not dst.is_dir():
        res["dst_absent"] = True
        dst_files: dict = {}
    else:
        dst_files = backend_autosync._walk(dst)

    def _bucket(rel: str) -> str:
        return "ign" if _ignore_reason(rel) else "prod"

    for rel, meta in sorted(src_files.items()):
        b = _bucket(rel)
        res[f"{b}_total"] += 1
        if rel not in dst_files:
            res["ign_missing" if b == "ign" else "missing"].append(rel)
        elif dst_files[rel] != meta:
            res["ign_changed" if b == "ign" else "changed"].append(rel)
    for rel in sorted(dst_files):
        if rel not in src_files:
            b = _bucket(rel)
            res["ign_extra" if b == "ign" else "extra"].append(rel)
    return res


def _prod_bad(res: dict) -> int:
    return len(res["changed"]) + len(res["missing"]) + len(res["extra"])


def _ign_bad(res: dict) -> int:
    return len(res["ign_changed"]) + len(res["ign_missing"]) + len(res["ign_extra"])


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认可能是 GBK，中文输出会炸。改不了就算了（比如输出被重定向到
    # 不支持 reconfigure 的对象），**不该因为编码设置失败就让整个核验挂掉**。
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="核验桌面端后端副本与源码是否零漂移")
    parser.add_argument("--target", type=Path, default=None, help="副本根目录（默认取已安装的桌面端）")
    parser.add_argument("--all", action="store_true", help="连「可忽略项」的明细也打出来")
    args = parser.parse_args(argv)

    target = args.target or _default_target()
    if target is None:
        print("找不到默认目标：环境变量 LOCALAPPDATA 未设置。请用 --target 显式指定。")
        return 2
    target = target.expanduser().resolve()

    print(f"源码根: {_PROJECT_ROOT}")
    print(f"目标:   {target}")
    if not target.is_dir():
        print(f"\n❌ 目标目录不存在：{target}")
        print("   （若是首次安装，先装一次桌面端；或改用 --target 指向 staging 目录）")
        return 2
    print()

    results: list[tuple[dict, str]] = []
    for src_rel, dst_rel in backend_autosync._SYNC_PAIRS:
        results.append((_diff_pair(_PROJECT_ROOT, src_rel, target, dst_rel), ""))
    for src_rel, dst_rel, note in _EXTRA_PAIRS:
        results.append((_diff_pair(_PROJECT_ROOT, src_rel, target, dst_rel), note))

    total_bad = 0
    ign_lines: list[str] = []
    print("=== 生产代码 ===")
    for res, note in results:
        label = f"{res['src_rel']} -> {res['dst_rel']}" + (f"  [{note}]" if note else "")
        if res["src_absent"]:
            print(f"  跳过  {label}（源码侧不存在）")
            continue
        if res["dst_absent"]:
            print(f"  ❌    {label}  副本目录不存在（{res['prod_total']} 个文件全缺）")
            total_bad += res["prod_total"]
            continue
        bad = _prod_bad(res)
        total_bad += bad
        if bad == 0:
            print(f"  OK    {label}  ({res['prod_total']} 个文件)")
        else:
            print(
                f"  ❌    {label}  不一致={len(res['changed'])} "
                f"缺失={len(res['missing'])} 多余={len(res['extra'])}"
            )
            for rel in res["changed"]:
                print(f"          内容不一致: {rel}")
            for rel in res["missing"]:
                print(f"          目标缺失:   {rel}")
            for rel in res["extra"]:
                print(f"          目标多余:   {rel}")
        # 可忽略项汇总（按理由归类）
        if _ign_bad(res):
            reasons: dict[str, int] = {}
            for rel in res["ign_changed"] + res["ign_missing"] + res["ign_extra"]:
                r = _ignore_reason(rel) or "其他"
                reasons[r] = reasons.get(r, 0) + 1
            parts = "、".join(f"{k} {v} 个" for k, v in sorted(reasons.items()))
            ign_lines.append(f"  {res['src_rel']}: {parts}")
            if args.all:
                for rel in res["ign_changed"]:
                    ign_lines.append(f"      [忽略·内容不同] {rel}")
                for rel in res["ign_missing"]:
                    ign_lines.append(f"      [忽略·副本没有] {rel}")
                for rel in res["ign_extra"]:
                    ign_lines.append(f"      [忽略·副本多出] {rel}")

    if ign_lines:
        print()
        print("=== 可忽略项（副本里本来就不该有，不计入失败）===")
        print("\n".join(ign_lines))
        print("  看明细加 --all")

    print()
    if total_bad == 0:
        print("✅ 生产代码零漂移：副本与源码逐字节一致。")
        return 0
    print(f"❌ 生产代码发现 {total_bad} 处漂移。修法：")
    print('   $dst = "$env:LOCALAPPDATA\\Programs\\voice-morph-desktop\\resources\\backend"')
    print("   & D:\\变声\\tools\\sync_backend.ps1 -WhatIfSync -TargetRoot $dst   # 先看会动什么")
    print("   & D:\\变声\\tools\\sync_backend.ps1           -TargetRoot $dst   # 再真同步")
    print("   ⚠️ 桌宠文件（web/electron/pet）不在 sync 范围内，需单独拷（见 docs/犯错指南.md §2.29）")
    print("   ⚠️ sync_backend.ps1 不含 web/dist → web_dist，前端要用 backend_autosync 或手动拷")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

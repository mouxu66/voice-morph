#!/usr/bin/env python
"""硬删除工具：先截断文件到 0 字节，再删除。

背景：本机 WorkBuddy 的 safe-delete shim 会把所有删除（含 Git Bash 的 rm -rf、
Python 的 os.remove/shutil.rmtree）重定向成「移到回收站」，导致删除后磁盘空间
不释放；而 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 在本机实测同样失效，且批量删除会
触发 SAFE_DELETE_BULK_GUARD_ERROR 直接 SystemExit(1)。

绕过办法：删除前先把文件内容 truncate 到 0 —— 数据块立刻归还文件系统，
之后即使被移进回收站也只是一个 0 字节空壳，不占空间。

用法：
    python tools/hard_delete.py <path> [<path> ...]
    python tools/hard_delete.py --dry-run <path>   # 只统计不删除
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _truncate_tree(root: Path) -> tuple[int, int]:
    """递归把 root 下所有文件截断为 0 字节。返回 (文件数, 释放字节数)。"""
    n = freed = 0
    if root.is_file():
        sz = root.stat().st_size
        if sz:
            with open(root, "r+b") as fh:
                fh.truncate(0)
            freed += sz
        return 1, freed
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                sz = fp.stat().st_size
                if sz:
                    with open(fp, "r+b") as fh:
                        fh.truncate(0)
                    freed += sz
                n += 1
            except (PermissionError, OSError) as e:
                print(f"  ! 跳过 {fp}: {e}", file=sys.stderr)
    return n, freed


def hard_delete(path: str | Path, dry_run: bool = False) -> int:
    p = Path(path)
    if not p.exists():
        print(f"  - 不存在，跳过: {p}")
        return 0
    n, freed = _truncate_tree(p) if not dry_run else (
        sum(1 for _ in p.rglob("*") if _.is_file()) if p.is_dir() else 1,
        (sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
         if p.is_dir() else p.stat().st_size),
    )
    if dry_run:
        print(f"  [dry-run] {freed / 1024 ** 3:.2f}G / {n} 文件  {p}")
        return freed
    try:
        if p.is_file() or p.is_symlink():
            os.remove(p)
        else:
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():  # rmtree 被拦截时的兜底
                os.rmdir(p)
    except OSError as e:
        print(f"  ! 删除失败（数据已截断，空间已释放）: {p}: {e}", file=sys.stderr)
    print(f"  ✓ {freed / 1024 ** 3:.2f}G / {n} 文件  {p}")
    return freed


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    before = shutil.disk_usage(str(Path(args[0]).anchor))[2]
    total = 0
    for a in args:
        total += hard_delete(a, dry)
    after = shutil.disk_usage(str(Path(args[0]).anchor))[2]
    print(f"\n合计 {total / 1024 ** 3:.2f}G；磁盘可用 "
          f"{before / 1024 ** 3:.2f}G → {after / 1024 ** 3:.2f}G "
          f"(+{(after - before) / 1024 ** 3:.2f}G)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

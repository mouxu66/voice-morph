# -*- coding: utf-8 -*-
"""核验试听页的 mp3 确实互不相同（上轮踩过"字节数一样=假对照"）。

判据：按**内容 md5** 分组 —— 同组 >1 个就是"几条其实是同一个文件"，那整页 A/B 就是假的
（字节数相同不代表内容相同，内容相同才是事故）。核心是纯函数 `duplicate_groups`，
守卫用一对真相同 / 真不同的假文件钉住它。
"""
import hashlib
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

BASE = pathlib.Path(__file__).resolve().parent.parent / "media/covers/song2/mypitch"


def _md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:10]


def duplicate_groups(directory: pathlib.Path, pattern: str = "*.mp3") -> dict[str, list[str]]:
    """目录下按内容 md5 分组，返回 {md5: [文件名,...]}（只含实际找到的文件）。"""
    seen: dict[str, list[str]] = {}
    for f in sorted(directory.glob(pattern)):
        seen.setdefault(_md5(f), []).append(f.name)
    return seen


def main() -> int:
    d = BASE
    if not d.exists():
        print(f"（缺目录）{d}")
        return 2
    seen = duplicate_groups(d)
    dup = {h: n for h, n in seen.items() if len(n) > 1}
    print(f"不同内容 {len(seen)} 组 / 共 {sum(len(n) for n in seen.values())} 个 mp3")
    for h, names in seen.items():
        mark = "  ← 同一内容！" if len(names) > 1 else ""
        print(f"  {h}  {names}{mark}")

    page = d / "listen.html"
    if page.exists():
        html = page.read_text(encoding="utf-8")
        print(f"\nlisten.html: {page.stat().st_size/1048576:.1f} MB, data-src 出现 "
              f"{html.count('data-src=')} 次")
    return 1 if dup else 0


if __name__ == "__main__":
    raise SystemExit(main())

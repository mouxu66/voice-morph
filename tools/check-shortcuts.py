"""校验 Windows 快捷方式（.lnk）的目标路径。

## 为什么需要这个脚本

本机安全策略拦截 COM 对象实例化（`New-Object -ComObject WScript.Shell` 会报
"COM object instantiation can run arbitrary code"），因此无法用官方姿势
`(New-Object -ComObject WScript.Shell).CreateShortcut($p).TargetPath` 读快捷方式。
符号链接需要管理员、硬链接需要同盘，也都不适用。

于是按 MS-SHLLINK 结构直接解析字节——只读、无副作用、不需要任何权限。

## 用法

    python tools/check-shortcuts.py                # 默认查「变声工坊」桌面 + 开始菜单
    python tools/check-shortcuts.py a.lnk b.lnk    # 查指定文件

退出码：0 = 全部解析成功；1 = 有文件缺失或解析失败。

## 实现要点（踩过的坑）

1. `.lnk` 里目标路径**同时**以 ANSI（LocalBasePath）和 UTF-16（IDList）两份存在。
   只 grep ASCII 会漏掉中文文件名——`变声工坊.exe` 的 GBK 字节里只有 `.exe`
   是可见 ASCII，所以 grep 出来的路径看起来像"目录 + .exe"，是假象。
2. 所以解析时**优先读 Unicode 版路径**（`LinkInfoHeaderSize >= 0x24` 时才有，
   见 MS-SHLLINK 2.3 的 `LocalBasePathOffsetUnicode`），读不到才退 ANSI + `mbcs`。
3. 路径里既有 ANSI 又有 UTF-16 片段，**不能**把整个文件当单一编码解码。
"""

from __future__ import annotations

import os
import struct
import sys

# ShellLinkHeader 的 LinkFlags 位（MS-SHLLINK 2.1.1）
HAS_LINK_TARGET_ID_LIST = 0x00000001
HAS_LINK_INFO = 0x00000002
HAS_NAME = 0x00000004
HAS_RELATIVE_PATH = 0x00000008
HAS_WORKING_DIR = 0x00000010
HAS_ARGUMENTS = 0x00000020
HAS_ICON_LOCATION = 0x00000040
IS_UNICODE = 0x00000080

HEADER_SIZE = 76

DEFAULT_LNKS = [
    os.path.expanduser("~/Desktop/变声工坊.lnk"),
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\变声工坊.lnk"),
]


class LnkError(Exception):
    """解析失败。"""


def _ansi(raw: bytes) -> str:
    """按系统 ANSI 代码页解码（简中 = GBK）。"""
    try:
        return raw.decode("mbcs")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("gbk", errors="replace")


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _cstring(buf: bytes, off: int, end: int) -> bytes:
    """读 null 结尾字节串，越界则截断。"""
    if off <= 0 or off >= len(buf):
        return b""
    stop = buf.find(b"\x00", off)
    if stop < 0 or stop > end:
        stop = min(end, len(buf))
    return buf[off:stop]


def _utf16_cstring(buf: bytes, off: int, end: int) -> str:
    """读 UTF-16LE null 结尾字符串。

    比 `_cstring` + `mbcs` 可靠：不受系统 ANSI 代码页影响，
    非 ASCII 路径（`变声工坊.exe`）也能原样读出。
    """
    if off <= 0 or off >= len(buf):
        return ""
    limit = min(end, len(buf))
    chunks = []
    i = off
    while i + 1 < limit:
        pair = buf[i : i + 2]
        if pair == b"\x00\x00":
            break
        chunks.append(pair)
        i += 2
    return b"".join(chunks).decode("utf-16-le", errors="replace")


def parse_lnk(path: str) -> dict:
    """解析 .lnk，返回 {target, working_dir, arguments, icon}。

    解析不出来时抛 LnkError。
    """
    with open(path, "rb") as fh:
        buf = fh.read()

    if len(buf) < HEADER_SIZE:
        raise LnkError("文件过小，不是合法 .lnk")
    if _u32(buf, 0) != 0x0000004C:
        raise LnkError("HeaderSize 不是 0x4C，不是合法 .lnk")

    flags = _u32(buf, 20)
    unicode_strings = bool(flags & IS_UNICODE)
    pos = HEADER_SIZE

    # --- LinkTargetIDList：跳过（文件名在里面是 UTF-16，但我们优先用 LinkInfo）
    if flags & HAS_LINK_TARGET_ID_LIST:
        pos += 2 + _u16(buf, pos)

    result: dict = {"target": "", "working_dir": "", "arguments": "", "icon": ""}

    # --- LinkInfo：LocalBasePath 是完整目标路径
    if flags & HAS_LINK_INFO:
        info_size = _u32(buf, pos)
        info_end = pos + info_size
        header_size = _u32(buf, pos + 4)
        info_flags = _u32(buf, pos + 8)
        # LinkInfoHeaderSize >= 0x24 才带 Unicode 偏移字段（MS-SHLLINK 2.3）。
        # 有就优先走 Unicode —— 免掉 ANSI 代码页这一层不确定性。
        has_unicode_offsets = header_size >= 0x24
        if info_flags & 0x1:  # VolumeIDAndLocalBasePath
            if has_unicode_offsets:
                base = _utf16_cstring(buf, pos + _u32(buf, pos + 28), info_end)
                suffix = _utf16_cstring(buf, pos + _u32(buf, pos + 32), info_end)
            else:
                base = _ansi(_cstring(buf, pos + _u32(buf, pos + 16), info_end))
                suffix = _ansi(_cstring(buf, pos + _u32(buf, pos + 24), info_end))
            result["target"] = base + suffix
        pos = info_end

    # --- StringData（长度前缀是字符数，不是字节数）
    def read_string() -> str:
        nonlocal pos
        if pos + 2 > len(buf):
            return ""
        count = _u16(buf, pos)
        pos += 2
        if unicode_strings:
            raw = buf[pos : pos + count * 2]
            pos += count * 2
            return raw.decode("utf-16-le", errors="replace")
        raw = buf[pos : pos + count]
        pos += count
        return _ansi(raw)

    if flags & HAS_NAME:
        read_string()
    if flags & HAS_RELATIVE_PATH:
        rel = read_string()
        if not result["target"]:
            result["target"] = rel
    if flags & HAS_WORKING_DIR:
        result["working_dir"] = read_string()
    if flags & HAS_ARGUMENTS:
        result["arguments"] = read_string()
    if flags & HAS_ICON_LOCATION:
        result["icon"] = read_string()

    if not result["target"]:
        raise LnkError("未解析出目标路径")
    return result


def probe(path: str) -> bool:
    """打印单个 .lnk 的解析结果，返回是否成功。"""
    print("===", path)
    if not os.path.exists(path):
        print("    [缺失] 快捷方式不存在")
        return False
    try:
        info = parse_lnk(path)
    except (LnkError, OSError) as exc:
        print(f"    [失败] {exc}")
        return False

    target = info["target"]
    state = "存在" if os.path.exists(target) else "不存在"
    print(f"    目标   : {target}")
    print(f"    状态   : [{state}]")
    if info["working_dir"]:
        print(f"    起始位置: {info['working_dir']}")
    if info["arguments"]:
        print(f"    参数   : {info['arguments']}")
    return True


def main(argv: list[str]) -> int:
    paths = argv[1:] or DEFAULT_LNKS
    ok = all(probe(p) for p in paths)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python
"""生成随发行物分发的许可原文载荷（`web/public/licenses/`）。

为什么需要它
------------
OFL-1.1 字体（`@fontsource/inter` 等）的第 1 条要求：**再分发字体时必须附带版权
声明与许可原文**。而我们只把 woff2 打进了 `web/dist`，许可原文没跟着走 ——
这是 `THIRD_PARTY_NOTICES.md` §5 的缺口 G1。

做法与为什么这么做
------------------
`web/public/` 是 vite 的静态目录：内容会被原样复制到 `web/dist/`，而 `web/dist`
**两条路都在发行物里**：

  1. `package.json` 的 `build.files: ["dist/**/*"]` → 进 `app.asar`
  2. `build.extraResources: dist → backend/web_dist` → 安装目录下的明文件

所以把许可原文放进 `public/licenses/` 就等于「随安装包分发」，不需要改打包配置。
同时后端有一条 SPA catch-all（`m2_server/server.py`）会把 `web_dist` 下的任意文件
发出去，前端因此能用 `backendPrefix() + "/licenses/…"` 在 dev / file:// / 局域网
三种模式下都读到同一份 —— UI 里那个「开源许可」入口就是靠它。

**单一来源**：原文从 `node_modules` 复制，版权行从原文首行**正则提取**（不手写），
避免出现"NOTICES 里写的版权行和实际分发的许可原文对不上"这种最难查的漂移。

用法
----
    python tools/sync_license_payload.py            # 重新生成载荷
    python tools/sync_license_payload.py --check    # 只校验是否已同步（**本机**用）

退出码：0 = 一致 / 同步成功；1 = `--check` 下发现漂移，或源文件缺失。

`--check` 需要 `web/node_modules`，因此**不进 CI**；CI 那边的守卫在
`tools/audit_licenses.py` 里 —— 它拿入库的 `web/package-lock.json` 核对载荷声明的
版本，所以"升级了字体却没重跑本脚本"同样会被 CI 抓住，不需要 node_modules。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 需要附带 OFL 原文的字体包 → 载荷内的文件名与说明。
#: 这里**必须与 `web/package.json` 的 dependencies 里的 `@fontsource/*` 对齐** ——
#: `audit_licenses.py` 会双向核对，加字体却不加条目会直接判红。
FONT_PACKAGES: dict[str, dict[str, str]] = {
    "@fontsource/inter": {
        "payload": "inter-OFL-1.1.txt",
        "usage": "界面正文字体 Inter（woff2 打进 web/dist）",
    },
    "@fontsource/outfit": {
        "payload": "outfit-OFL-1.1.txt",
        "usage": "展示标题字体 Outfit（font-display 用）",
    },
    "@fontsource/jetbrains-mono": {
        "payload": "jetbrains-mono-OFL-1.1.txt",
        "usage": "等宽字体 JetBrains Mono（日志 / 码值展示用）",
    },
}

#: 载荷目录（相对仓库根）。为什么在 public/ 而不是 assets/：见模块 docstring。
PAYLOAD_DIR = Path("web") / "public" / "licenses"
MANIFEST_NAME = "index.json"

#: 版权行的提取式。OFL 原文首行形如
#:   `Copyright 2016 The Inter Project Authors (https://…) Inter-Italic[…]: Copyright 2016 …`
#: 同一行里可能塞了两条（主字体 + 变体）。这里只取**第一条**，作为该包的版权行。
COPYRIGHT_RE = re.compile(r"^(Copyright \d{4} The .+? Project Authors)")


def _extract_copyright(text: str) -> str:
    """从 OFL 原文里提取版权行；提不到就报错而不是编一个。"""
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = COPYRIGHT_RE.match(first)
    if not m:
        raise ValueError(f"OFL 原文首行提不到版权行，首行是：{first[:120]!r}")
    return m.group(1)


def _read_version(pkg_json: Path) -> str:
    return str(json.loads(pkg_json.read_text(encoding="utf-8"))["version"])


def _targets(root: Path) -> dict[str, bytes]:
    """算出一份「载荷文件 → 期望内容」的映射（不落盘）。"""
    nm = root / "web" / "node_modules"
    out: dict[str, bytes] = {}
    total = 0

    components: list[dict] = []
    for pkg, meta in FONT_PACKAGES.items():
        src = nm / pkg / "LICENSE"
        if not src.exists():
            raise FileNotFoundError(
                f"缺少 {src}。这个工具需要 web/node_modules —— 先 `cd web && npm ci`。"
                "（注意：只是**生成**时需要；已提交的载荷本身就是权威副本，CI 不依赖 node_modules）"
            )
        text = src.read_text(encoding="utf-8")
        copyright_line = _extract_copyright(text)
        out[meta["payload"]] = text.encode("utf-8")
        total += len(out[meta["payload"]])
        components.append(
            {
                "id": f"font-{pkg.removeprefix('@fontsource/')}",
                "name": pkg,
                "version": _read_version(nm / pkg / "package.json"),
                "license": "OFL-1.1",
                "copyright": copyright_line,
                "file": meta["payload"],
                "distributed": "随安装包（web/dist → app.asar 与 backend/web_dist 各一份）",
                "scope": meta["usage"],
            }
        )

    components.append(
        {
            "id": "project-license",
            "name": "变声工坊（本项目自身）",
            "version": "",
            "license": "MIT",
            "copyright": "Copyright (c) 2026 mouxu",
            "file": "PROJECT-LICENSE.txt",
            "distributed": "随安装包",
            "scope": "本仓库源码与发行物",
        }
    )
    out["PROJECT-LICENSE.txt"] = (root / "LICENSE").read_bytes()
    total += len(out["PROJECT-LICENSE.txt"])

    manifest = {
        "_comment": (
            "本目录由 tools/sync_license_payload.py 生成，请勿手改；"
            "改动许可事实请改 THIRD_PARTY_NOTICES.md 与生成器。"
            "web/public/ → web/dist/ → 随安装包分发（app.asar + backend/web_dist 两份）。"
        ),
        "components": components,
    }
    out[MANIFEST_NAME] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return out


def _report(name: str, ok: bool, detail: str) -> None:
    print(f"  {'OK  ' if ok else 'DRIFT'} {name}  {detail}")


def run(root: Path, check: bool) -> int:
    try:
        targets = _targets(root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"载荷生成失败：{exc}")
        return 1

    dest = root / PAYLOAD_DIR
    drift = 0
    if not check:
        dest.mkdir(parents=True, exist_ok=True)

    for name, want in sorted(targets.items()):
        path = dest / name
        cur = path.read_bytes() if path.exists() else None
        if cur == want:
            _report(name, True, f"{len(want)} 字节")
        else:
            drift += 1
            note = "缺失" if cur is None else f"不一致（现有 {len(cur)} 字节，应为 {len(want)}）"
            _report(name, False, note)
            if not check:
                path.write_bytes(want)

    # 载荷里多余的文件也要报：否则删掉一个字体后旧许可会一直躺在发行物里
    if dest.exists():
        for stray in sorted(p.name for p in dest.iterdir() if p.is_file() and p.name not in targets):
            drift += 1
            _report(stray, False, "载荷里的多余文件（对应包已不是依赖？）")
            if not check:
                (dest / stray).unlink()

    if check:
        print(f"{'一致' if not drift else f'{drift} 处漂移'} → "
              f"{'OK' if not drift else 'FAIL'}（跑 python tools/sync_license_payload.py 可修）")
        return 0 if not drift else 1

    print(f"载荷已写入 {PAYLOAD_DIR}（{len(targets)} 个文件）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成/校验随发行物分发的许可原文载荷")
    ap.add_argument("--root", default=None, help="仓库根（默认脚本上一级）")
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    return run(root, args.check)


if __name__ == "__main__":
    sys.exit(main())

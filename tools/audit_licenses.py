#!/usr/bin/env python
"""开源合规门禁：让 `THIRD_PARTY_NOTICES.md` 不能悄悄过期。

为什么需要它
------------
`THIRD_PARTY_NOTICES.md` 是一份**必须随分发物一起走**的义务清单（字体 OFL、图标
CC BY-SA、第三方素材等）。文档类交付物的通病是"写一次就烂"：有人给
`requirements.txt` 加了个包、给 `web/package.json` 加了依赖，notices 不会自己更新，
等到发版才发现少了一份许可 —— 而许可是**事后补不回来的**（已分发的版本已经违规）。

所以把"覆盖性"变成机器判据：**声明的依赖集** 与 **notices 里登记的名字集合**
必须严格相等（双向），另外几条**已知义务**（字体许可原文、CC BY-SA 署名、
VB-CABLE 禁止再分发、禁止把 NC 权重打进发行物…）必须各自有对应条目。

判据的非对称性是刻意的
----------------------
- **声明了但 notices 没登记** → 失败（这是真的漏项）。
- **notices 登记了但已经不是依赖** → 也失败（残留条目会让人误判"这份文档是活的"）。
双向严格 = 不会腐烂。若确实要留"历史条目"，请写进正文而非机器块。

设计约束
--------
- **纯离线**：只读本仓库的文本文件，不发任何网络请求（CI 上跑得起、不依赖 PyPI/HF 可达）。
  许可**事实**（哪个包是什么许可）写在 notices 正文里，由人逐条回溯一手来源；
  本工具只负责"**有没有漏**"，不负责"许可填得对不对"。
- **无第三方依赖**：stdlib only（CI 的瘦环境装得起）。
- 路径可注入（`root=`），便于测试用 tmp 目录构造假依赖集。

用法
----
    python tools/audit_licenses.py            # 人读报告
    python tools/audit_licenses.py --json     # 机器读
退出码：0 = 全绿；1 = 有漏项/残留/缺义务条目。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 机器块格式
#
# THIRD_PARTY_NOTICES.md 里两段 HTML 注释围起来的块。用注释而不是表格：
# markdown 渲染时不显示，却对 `re` 稳定（表格一改列宽就解析不了）。
#
#   <!-- audit:deps:begin -->
#   python:comtypes
#   npm:react
#   <!-- audit:deps:end -->
#
#   <!-- audit:obligations:begin -->
#   ofl-font-notice = 随发行物附带 @fontsource/*/LICENSE 原文（OFL-1.1 §1）
#   <!-- audit:obligations:end -->
#
DEPS_BEGIN = "<!-- audit:deps:begin -->"
DEPS_END = "<!-- audit:deps:end -->"
OBLIG_BEGIN = "<!-- audit:obligations:begin -->"
OBLIG_END = "<!-- audit:obligations:end -->"

#: 与"声明依赖集"无关、但必须永远存在的义务条目。
#: 这几条不是从依赖推导出来的，而是本项目**已经踩过/已知会踩**的坑：
#:   - vb-cable-terms         ：VB-CABLE 内置/分发有条款（不是"禁止"，也不是"随便用"）
#:   - ffmpeg-external        ：ffmpeg 只以子进程方式调用，不随包分发
#:   - no-nc-weights          ：项目已开源，NC 权重（F5-TTS/MaskGCT…）不得进发行物
#:   - third-party-asset-names：第三方素材名/本体不入库（与 issue #3 同一件事）
#:   - market-voice-disclaimer：市场下载的社区自训音色必须带"仅供学习研究"提示
ALWAYS_OBLIGATIONS = {
    "vb-cable-terms",
    "ffmpeg-external",
    "no-nc-weights",
    "third-party-asset-names",
    "market-voice-disclaimer",
}

#: 依赖 → 触发义务的规则。加了对应依赖却不登记义务 = 失败。
DEP_TRIGGERED_OBLIGATIONS = {
    "@fontsource/": "ofl-font-notice",
}


def normalize(name: str) -> str:
    """包名归一：PEP 503 风格 + npm 保留原大小写。

    Python 侧 `Pillow` / `pillow`、`python_multipart` / `python-multipart` 必须等价，
    否则 notices 里换个写法就被判成漏项。
    """
    n = name.strip()
    if n.startswith("@"):  # npm scoped 包：@scope/name 保持原样（npm 区分大小写）
        return n
    return n.lower().replace("_", "-")


def parse_requirements(path: Path) -> list[str]:
    """requirements.txt → 顶层包名列表。

    只取每行第一个字段，丢掉版本约束/环境标记/行尾注释；跳过 `-r`/`--index-url` 这类指令。
    别用 `packaging` 解析 —— 瘦环境里没有它，且这里只需要包名。
    """
    out: list[str] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[\s;(]", line, maxsplit=1)[0].strip()
        if name:
            out.append(normalize(name))
    return out


def parse_frontend_deps(package_json: Path) -> list[str]:
    """web/package.json 的 **dependencies**（devDependencies 不进发行物，不登记）。"""
    if not package_json.exists():
        return []
    data = json.loads(package_json.read_text(encoding="utf-8"))
    return sorted(normalize(k) for k in data.get("dependencies", {}))


def _block(text: str, begin: str, end: str) -> str | None:
    """取两个标记之间的内容；缺任一标记返回 None（分开报"块缺失"与"块为空"）。"""
    i = text.find(begin)
    j = text.find(end)
    if i < 0 or j < 0 or j < i:
        return None
    return text[i + len(begin): j]


def parse_notices(path: Path) -> tuple[list[str], dict[str, str], list[str]]:
    """解析 notices → (依赖条目, 义务条目, 结构性问题)。

    依赖条目形如 `python:requests` / `npm:react`；义务条目形如 `key = 说明`。
    """
    problems: list[str] = []
    if not path.exists():
        return [], {}, [f"文件不存在：{path}"]

    text = path.read_text(encoding="utf-8", errors="replace")

    deps_raw = _block(text, DEPS_BEGIN, DEPS_END)
    if deps_raw is None:
        problems.append(f"缺少依赖机器块（{DEPS_BEGIN} … {DEPS_END}）")
        deps: list[str] = []
    else:
        deps = []
        for line in deps_raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                problems.append(f"依赖条目格式应为 `python:名字` / `npm:名字`，实际：{line}")
                continue
            eco, name = line.split(":", 1)
            eco = eco.strip().lower()
            if eco not in ("python", "npm"):
                problems.append(f"未知生态 `{eco}`（只支持 python / npm）：{line}")
                continue
            deps.append(normalize(name))

    oblig_raw = _block(text, OBLIG_BEGIN, OBLIG_END)
    if oblig_raw is None:
        problems.append(f"缺少义务机器块（{OBLIG_BEGIN} … {OBLIG_END}）")
        obligations: dict[str, str] = {}
    else:
        obligations = {}
        for line in oblig_raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                problems.append(f"义务条目格式应为 `key = 说明`，实际：{line}")
                continue
            key, desc = line.split("=", 1)
            obligations[key.strip()] = desc.strip()

    return deps, obligations, problems


def audit(root: Path) -> dict:
    """跑一遍全部判据，返回可 JSON 化的结果（不打印、不退出）。"""
    declared: dict[str, list[str]] = {}
    for rel in ("requirements.txt", "requirements-dev.txt"):
        for name in parse_requirements(root / rel):
            declared.setdefault(name, []).append(rel)
    for name in parse_frontend_deps(root / "web" / "package.json"):
        declared.setdefault(name, []).append("web/package.json")

    notices_path = root / "THIRD_PARTY_NOTICES.md"
    listed, obligations, problems = parse_notices(notices_path)

    listed_set = set(listed)
    declared_set = set(declared)

    missing = sorted(declared_set - listed_set)          # 声明了但没登记 → 真漏项
    stale = sorted(listed_set - declared_set)            # 登记了但已不是依赖 → 残留

    dupes = sorted({n for n in listed if listed.count(n) > 1})

    required = set(ALWAYS_OBLIGATIONS)
    for name in declared_set:
        for prefix, key in DEP_TRIGGERED_OBLIGATIONS.items():
            if name.startswith(prefix):
                required.add(key)
    missing_obligations = sorted(required - set(obligations))

    errors: list[str] = []
    errors += problems
    if missing:
        errors.append(f"{len(missing)} 个声明依赖未在 notices 登记：{', '.join(missing)}")
    if stale:
        errors.append(f"{len(stale)} 个 notices 条目已不是声明依赖（残留）：{', '.join(stale)}")
    if dupes:
        errors.append(f"notices 依赖条目重复：{', '.join(dupes)}")
    if missing_obligations:
        errors.append(f"缺少必需义务条目：{', '.join(missing_obligations)}")

    return {
        "ok": not errors,
        "declared_count": len(declared_set),
        "listed_count": len(listed_set),
        "missing": missing,
        "stale": stale,
        "duplicates": dupes,
        "missing_obligations": missing_obligations,
        "obligations": obligations,
        "errors": errors,
    }


def render(result: dict) -> str:
    lines = [
        "第三方许可登记审计（tools/audit_licenses.py）",
        f"  声明依赖 {result['declared_count']} 个 / notices 登记 {result['listed_count']} 个"
        f" / 义务条目 {len(result['obligations'])} 条",
    ]
    if result["ok"]:
        lines.append("  RESULT: OK")
        return "\n".join(lines)
    lines.append("  RESULT: FAIL")
    for e in result["errors"]:
        lines.append(f"  - {e}")
    lines.append("")
    lines.append("  修法：改 `THIRD_PARTY_NOTICES.md` 的机器块（正文同步补许可事实），")
    lines.append("        再去回溯一手来源确认许可 —— 别只把名字补上。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="第三方许可登记审计（离线）")
    ap.add_argument("--root", default=None, help="仓库根（默认脚本上一级）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    result = audit(root)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

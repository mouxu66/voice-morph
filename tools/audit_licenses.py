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
VB-CABLE 分发条款、禁止把 NC 权重打进发行物…）必须各自有对应条目。

三层判据，一层比一层硬
----------------------
1. **登记**：依赖集 ⇄ 机器块双向严格相等。漏登记、残留条目都红。
2. **履行**：`ofl-font-notice` 这类义务，光"写了"不算 —— `web/public/licenses/`
   里必须真有许可原文、版权行对得上、且与字体依赖双向对齐（`parse_payload`）。
   把义务从"有人知道"升级成"机器能证明做了"。
3. **不许凭空要求**：资产触发的义务（`ASSET_TRIGGERS`）只在**扫到资产**时才要求。
   2026-09-14 实证：NOTICES 曾挂一条 "OpenMoji 署名缺口"，而 `git log --all
   --diff-filter=A -- '*openmoji*'` 为空、资产全是自制插画 —— 义务源自一句描述
   *意图*的代码注释，没有产物。**手写断言会撒谎，文件系统不会。**

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

# ---------------------------------------------------------------- 许可原文载荷
#
# "登记了义务"和"履行了义务"是两件事。`ofl-font-notice = 随发行物附带 OFL 原文`
# 写进机器块只证明**有人知道**这件事，不证明**做了**。
# 所以这里再核一层：`web/public/licenses/`（→ web/dist → 随安装包分发，见
# tools/sync_license_payload.py 的 docstring）里必须真有那份原文，且版权行对得上。
#
PAYLOAD_DIR = Path("web") / "public" / "licenses"
PAYLOAD_MANIFEST = "index.json"

# ---------------------------------------------------------------- 资产触发的义务
#
# 有些义务不是靠"依赖"触发的，而是靠"发行物里出现了某类资产"。
# 设计意图：让 **phantom obligation（凭空捏造的义务）自己暴露出来**。
# 2026-09-14 实例：NOTICES 曾把 "市场占位图 = OpenMoji 图标" 列为分发组件并挂了个
# 署名缺口，但 `git log --all --diff-filter=A -- '*openmoji*'` 为空、`market_imgs/`
# 全是自制插画 —— 那条义务源自一句**描述意图的代码注释**，从未有对应产物。
# 手写断言会撒谎，文件系统不会：所以改成"扫到资产才要求署名"，没有资产就不要求。
ASSET_ROOTS = ("m2_server/assets", "web/public")
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ttf", ".otf", ".woff", ".woff2"}
ASSET_TRIGGERS: dict[str, tuple[str, str]] = {
    "openmoji": (
        "openmoji-attribution",
        "CC BY-SA 4.0 © hfg-gmuend/openmoji：署名 + 许可链接必须随分发可见",
    ),
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


def parse_payload(root: Path) -> tuple[list[dict], list[str]]:
    """读许可原文载荷 `web/public/licenses/index.json` → (组件列表, 结构性问题)。

    只判"在不在、对不对得上"，不判"许可填得对不对"（那是正文的职责，见模块 docstring）。
    """
    path = root / PAYLOAD_DIR / PAYLOAD_MANIFEST
    if not path.exists():
        return [], [
            f"缺少许可原文载荷：{PAYLOAD_DIR / PAYLOAD_MANIFEST}"
            "（跑 `python tools/sync_license_payload.py` 生成）"
        ]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"载荷清单不是合法 JSON：{exc}"]

    components = data.get("components")
    if not isinstance(components, list) or not components:
        return [], [f"载荷清单里 `components` 不是非空列表：{PAYLOAD_DIR / PAYLOAD_MANIFEST}"]

    problems: list[str] = []
    out: list[dict] = []
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            problems.append(f"载荷清单第 {i + 1} 项不是对象")
            continue
        for field in ("id", "name", "license", "file"):
            if not str(comp.get(field) or "").strip():
                problems.append(f"载荷清单第 {i + 1} 项缺少 `{field}`")
        out.append(comp)
    return out, problems


def scan_assets(root: Path, key: str) -> list[str]:
    """在发行物资产目录里找文件名含 `key` 的**素材**（返回相对路径，已排序）。

    只看素材后缀 —— 否则许可原文载荷文件名（`openmoji-CC-BY-SA-4.0.txt`）会命中，
    而那份文件是**补救措施**不是违规证据，自命中会让这条规则废掉。
    """
    hits: list[str] = []
    payload = (root / PAYLOAD_DIR).resolve()
    for rel in ASSET_ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in ASSET_SUFFIXES:
                continue
            if payload in p.resolve().parents:  # 载荷目录是补救措施，不算资产
                continue
            if key in p.name.lower():
                hits.append(str(p.relative_to(root)).replace("\\", "/"))
    return sorted(hits)


def locked_versions(root: Path) -> dict[str, str]:
    """从 `web/package-lock.json`（**入库**，lockfileVersion 3）读各包的锁定版本。

    为什么是 lock 而不是 node_modules：CI 上不装前端依赖，但 lock 是入库的 ——
    这样"改了版本却没重跑 sync_license_payload.py"也能在 CI 被抓到，
    而不必依赖本机 node_modules。
    """
    path = root / "web" / "package-lock.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for key, info in (data.get("packages") or {}).items():
        if key.startswith("node_modules/") and isinstance(info, dict) and info.get("version"):
            out[key[len("node_modules/"):]] = str(info["version"])
    return out


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

    # ---- 许可原文载荷：把"登记了义务"升级成"履行了义务" ----
    payload, payload_problems = parse_payload(root)
    payload_names = {str(c.get("name", "")).strip() for c in payload}
    payload_by_file: dict[str, dict] = {}
    for comp in payload:
        fname = str(comp.get("file", "")).strip()
        if not fname:
            continue
        target = root / PAYLOAD_DIR / fname
        if not target.is_file():
            payload_problems.append(f"载荷声明的原文不存在：{PAYLOAD_DIR / fname}")
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            payload_problems.append(f"载荷原文是空文件：{PAYLOAD_DIR / fname}")
            continue
        want_copyright = str(comp.get("copyright") or "").strip()
        if want_copyright and want_copyright not in text:
            payload_problems.append(
                f"{fname} 里找不到声明的版权行 `{want_copyright}`"
                "（载荷与许可事实漂移了，别手改文件，跑 sync_license_payload.py）"
            )
        payload_by_file[fname] = comp

    # 双向核对：字体依赖 ⇄ 载荷条目。加了字体却不附原文 = 真漏项（OFL §1 硬要求）。
    font_deps = {n for n in declared_set if n.startswith("@fontsource/")}
    payload_fonts = {n for n in payload_names if n.startswith("@fontsource/")}
    if font_deps - payload_fonts:
        payload_problems.append(
            "字体依赖没有对应的许可原文载荷："
            + ", ".join(sorted(font_deps - payload_fonts))
            + "（加字体必须同步 tools/sync_license_payload.py 的 FONT_PACKAGES）"
        )
    if payload_fonts - font_deps:
        payload_problems.append(
            "载荷里的字体已不是声明依赖（残留）："
            + ", ".join(sorted(payload_fonts - font_deps))
        )

    # 版本新鲜度：载荷声明的版本 vs lockfile。改了依赖没重跑 sync → 这里红。
    locked = locked_versions(root)
    for comp in payload:
        name = str(comp.get("name", "")).strip()
        ver = str(comp.get("version") or "").strip()
        locked_ver = locked.get(name)
        if ver and locked_ver and ver != locked_ver:
            payload_problems.append(
                f"载荷里 {name} 写的是 {ver}，lockfile 是 {locked_ver}"
                "（升级依赖后忘了跑 sync_license_payload.py？）"
            )

    # ---- 资产触发的义务：有资产就必须有署名，没资产就不许凭空要求 ----
    triggered: list[str] = []
    for key, (oblig_key, hint) in sorted(ASSET_TRIGGERS.items()):
        hits = scan_assets(root, key)
        if hits:
            triggered.append(key)
            if oblig_key not in obligations:
                payload_problems.append(
                    f"发行物里出现了 {len(hits)} 个 `{key}` 资产（如 {hits[0]}），"
                    f"但机器块里没有 `{oblig_key}` 义务条目 —— {hint}"
                )

    errors: list[str] = []
    errors += problems
    errors += payload_problems
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
        "payload_components": len(payload_by_file),
        "payload_errors": payload_problems,
        "asset_triggers": triggered,
        "errors": errors,
    }


def render(result: dict) -> str:
    lines = [
        "第三方许可登记审计（tools/audit_licenses.py）",
        f"  声明依赖 {result['declared_count']} 个 / notices 登记 {result['listed_count']} 个"
        f" / 义务条目 {len(result['obligations'])} 条"
        f" / 许可原文载荷 {result.get('payload_components', 0)} 份",
    ]
    if result.get("asset_triggers"):
        lines.append(f"  资产触发义务：{', '.join(result['asset_triggers'])}")
    if result["ok"]:
        lines.append("  RESULT: OK")
        return "\n".join(lines)
    lines.append("  RESULT: FAIL")
    for e in result["errors"]:
        lines.append(f"  - {e}")
    lines.append("")
    lines.append("  修法：改 `THIRD_PARTY_NOTICES.md` 的机器块（正文同步补许可事实），")
    lines.append("        许可原文载荷跑 `python tools/sync_license_payload.py`。")
    lines.append("        两条都是先回溯一手来源确认许可，**别只把名字补上**。")
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

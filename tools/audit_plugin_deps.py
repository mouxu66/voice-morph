"""插件可选依赖审计：清单里的 `extras.python` 与代码**真实 import** 对账。

为什么需要
----------
插件化第 5 步把 `torch` / `demucs` 这类重包从 `requirements.txt`（核心）挪进了
各插件的 `extras.python`。挪完立刻多出一个风险：**漏声明**。

漏声明的症状特别难认 —— 装完核心环境、应用能启动、核心页面都能用，只有点到
某个功能时才抛 `ModuleNotFoundError`（更糟的是被 try/except 兜成"静默降级"，
用户看到的是"这个按钮没反应"）。而 `extras.python` 是**手写**的，靠人肉维护必然漂。

本工具用**静态 import 图**把它变成可计算的：从每个插件声明的 router / hook /
health 模块出发，沿 first-party 模块做传递闭包，收集第三方 import，再与
「核心依赖 ∪ 所有插件的 extras ∪ 豁免表」比对。

判定口径（三档）
----------------
* **required**：非标准库、非 first-party、**没有被 ImportError 兜住**的导入。
  缺了就是硬失败 → 必须声明。
* **optional**：被 `try/except ImportError`（或 `except Exception` / 裸 `except`）
  兜住的导入。缺了会降级 → **声明与否都合法**（声明 = 让这个可选增强默认可用）。
  只报为信息，不算缺口。
* **豁免**（`EXEMPT`）：不该写进任何 `extras.python` 的模块。**只放真正抑制告警的
  条目** —— 加一条必须能用变异验证证明它有用，否则就是死条目。

哪些包**刻意不声明**（信息性说明，不是机器判定；一旦它们变成"必需导入"，门禁会直接报出来）
------------------------------------------------------------------------------------
* `sounddevice` / `webrtcvad` / `train` —— 真正的宿主是 **RVC 整合包 `.venv`**
  （`cascade_stream.py` / `play_worker.py` 在那边跑，`train` 是 RVC 自带的包）。
  主进程里要么只是子进程命令行字符串（`rvc_live._system_default_input`），
  要么是被 `try/except` 兜住的惰性导入（`rvc_live._list_devices`）。
* `comtypes` / `onnxruntime` / `soxr` / `platformdirs` / `tokenizers` /
  `safetensors` / `starlette` / `anyio` / `jinja2` —— 已声明包的**传递依赖**，
  项目代码不直接 import 它们，故不会出现在闭包里。

已知局限（刻意的，不假装能覆盖）
--------------------------------
* **子进程依赖看不见**。`seed_vc`（主 .venv 跑 `inference_v2.py`）、`demucs -m`、
  打分器（`sys.executable`）这些是**命令行字符串**里的依赖，静态 import 图抓不到，
  必须靠人读代码声明 —— `tests/test_plugin_deps.py` 的 `subprocess_only` 表
  把这些逐条记下来了。
* **动态 import**（`importlib`）看不见。仓库里 router 挂载、`m1_workshop/pipeline.py`
  都是这种，故不追。

用法
----
    python tools/audit_plugin_deps.py            # 人读报告；有缺口时退出码 1
    python tools/audit_plugin_deps.py --json     # 机器读（守护测试用）
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_M2 = ROOT / "m2_server"

# first-party 模块的搜索根（重名时按此优先级）
_SOURCE_ROOTS = (_M2, ROOT / "m1_workshop", ROOT / "tools")

# 不该出现在任何 `extras.python` 里的模块。**只放真正抑制告警的条目** ——
# 每加一条都要能用变异验证证明它有用（把条目删掉、门禁必须变红），否则就是死条目。
#
# ⚠️ 曾经这里列了 14 条，实测只有 1 条在干活（其余要么不在任何闭包里、要么是
#    optional、要么已被核心覆盖）—— 一张 13/14 是摆设的豁免表会让人误判风险。
EXEMPT: dict[str, str] = {
    "huggingface_hub": "transformers 的传递依赖（tools/natscore_local 直接用 hf_hub_download）",
}

# 安装名 → import 名的少数不一致（多数包两者只差 `-`/`_`，normalize 兜住）
_IMPORT_ALIASES = {
    "Pillow": "PIL",
    "python-dotenv": "dotenv",
    "python-multipart": "multipart",
    "qwen-tts": "qwen_tts",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
}

# 兜住导入的异常类型：这些 handler 之后的导入算 optional
_GUARD_EXC = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def normalize(name: str) -> str:
    """把包名/模块名归一到同一口径：小写、去 extras 与版本号、`-` → `_`。"""
    name = name.split("[")[0].split(";")[0].strip()
    name = re.split(r"[<>=!~ ]", name)[0]
    return name.strip().lower().replace("-", "_")


def import_name_of(pkg: str) -> str:
    """安装名 → 顶层 import 名。"""
    return normalize(_IMPORT_ALIASES.get(pkg, pkg))


def _requirements(path: Path) -> set[str]:
    """读 requirements*.txt 里的**直接**依赖（忽略注释、`-r`、空行）。"""
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        out.add(import_name_of(line))
    return out


def core_packages() -> set[str]:
    """核心依赖 = **只算 `requirements.txt`**（运行时核心）。

    为什么不带上 `requirements-dev.txt`：那份是**测试环境**（含 `comtypes` /
    `sounddevice` / `Pillow` 这类"测试路径要用、但运行时由别处提供"的包）。
    把它算进核心，会让门禁看不见"新机器只装核心时这个功能缺依赖"——
    而后者正是本工具要回答的问题。实测两套口径在本仓库都是绿的，那就选**更严**的。
    """
    return _requirements(ROOT / "requirements.txt")


def _resolve(module: str) -> Path | None:
    """first-party 模块 → 文件路径；不是 first-party 返回 None。"""
    top = module.split(".")[0]
    for root in _SOURCE_ROOTS:
        for cand in (root / f"{top}.py", root / top / "__init__.py"):
            if cand.exists():
                return cand
    return None


def _guard_handlers(node: ast.Try) -> bool:
    """这个 try 是否在兜导入失败。"""
    for h in node.handlers:
        if h.type is None:  # 裸 except
            return True
        names = [n.id for n in ast.walk(h.type) if isinstance(n, ast.Name)]
        names += [n.attr for n in ast.walk(h.type) if isinstance(n, ast.Attribute)]
        if _GUARD_EXC & set(names):
            return True
    return False


def _imports_of(path: Path) -> dict[str, bool]:
    """一个 .py 文件里出现的顶层 import 名 → 是否被 ImportError 兜住。

    含函数内的惰性导入是刻意的：`librosa` / `transformers` 在本仓库几乎全是惰性
    导入，但它们仍然是"这个功能要能用就得装"的依赖 —— 只统计模块级会系统性漏掉。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return {}

    guarded_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _guard_handlers(node):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded_ids.add(id(sub))

    out: dict[str, bool] = {}
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:  # 相对导入 → 一定 first-party
                continue
            mods = [node.module.split(".")[0]]
        if not mods:
            continue
        guarded = id(node) in guarded_ids
        for m in mods:
            # 同一个模块被"兜住"和"没兜住"各引一次时，按**没兜住**算（更严）
            out[m] = out.get(m, True) and guarded
    return out


def third_party_closure(seeds: list[str]) -> dict[str, dict]:
    """从若干 first-party 模块出发，收集第三方 import（含传递 first-party 依赖）。

    返回 `{模块名: {"required": bool, "from": [文件名]}}` —— 带上来源才能让人
    一眼判断该不该声明、以及该声明在哪个插件上。
    """
    seen: set[Path] = set()
    queue: list[Path] = []
    for name in seeds:
        p = _resolve(name)
        if p is not None and p not in seen:
            seen.add(p)
            queue.append(p)

    out: dict[str, dict] = {}
    while queue:
        path = queue.pop()
        for mod, guarded in _imports_of(path).items():
            if mod in sys.stdlib_module_names:
                continue
            dep = _resolve(mod)
            if dep is not None:
                if dep not in seen:
                    seen.add(dep)
                    queue.append(dep)
                continue
            rec = out.setdefault(mod, {"required": False, "from": []})
            rec["required"] = rec["required"] or not guarded
            if path.name not in rec["from"]:
                rec["from"].append(path.name)
    return out


def analyze() -> dict:
    """跑一遍审计，返回机器可读结果。"""
    if str(_M2) not in sys.path:
        sys.path.insert(0, str(_M2))
    import plugin_manifest  # noqa: PLC0415

    core = core_packages()
    plugins = plugin_manifest.load_all()
    declared_all = {import_name_of(x) for p in plugins for x in p.extras.get("python", [])}
    # 比对用归一化键（小写）：`PIL` 是唯一的例外 —— import 名大写，安装名 `Pillow`。
    # 不归一化就会把"已声明 Pillow"误报成缺口。
    allowed_keys = {normalize(x) for x in core | declared_all | set(EXEMPT)}

    report: dict = {
        "core": sorted(core),
        "exempt": EXEMPT,
        "declared_all": sorted(declared_all),
        "plugins": [],
        "undeclared": {},
    }
    for p in sorted(plugins, key=lambda x: x.order):
        seeds = list(p.routers)
        seeds += [h["module"] for h in p.hooks]
        if p.health:
            seeds.append(p.health["module"])
        closure = third_party_closure(seeds)
        missing = {
            m: sorted(r["from"])
            for m, r in sorted(closure.items())
            if r["required"] and normalize(m) not in allowed_keys
        }
        optional = sorted(m for m, r in closure.items() if not r["required"] and normalize(m) not in allowed_keys)
        for m, srcs in missing.items():
            report["undeclared"].setdefault(m, sorted(set(srcs)))
        report["plugins"].append(
            {
                "id": p.id,
                "routers": list(p.routers),
                "declared": sorted({import_name_of(x) for x in p.extras.get("python", [])}),
                "missing": missing,
                "optional_undeclared": optional,
                "third_party": sorted(closure),
            }
        )
    report["ok"] = not report["undeclared"]
    return report


def _print_human(rep: dict) -> None:
    print(f"核心依赖 {len(rep['core'])} 个 · 已声明 extras {len(rep['declared_all'])} 个 · 豁免 {len(rep['exempt'])} 个")
    if not rep["undeclared"]:
        print("✅ 所有插件的**必需**第三方 import 都已被核心或 extras 覆盖")
    else:
        print(f"\n❌ 未声明（必需）{len(rep['undeclared'])} 个：")
        for mod, srcs in rep["undeclared"].items():
            print(f"     {mod:22s} ← {', '.join(srcs)}")
    for p in rep["plugins"]:
        if p["optional_undeclared"]:
            print(f"\nℹ️  {p['id']} 可选（有 ImportError 兜底，声明与否都合法）：{p['optional_undeclared']}")
    print(
        "\n提示：`extras.python` 的**归属**是人工策展（哪个插件被关掉时该包可以省掉），\n"
        "      本工具的门禁只要求「被某处声明」—— 见 `EXEMPT` 表与 `docs/插件化设计.md` §8.1 预设。"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="插件可选依赖审计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="列出每个插件的完整第三方闭包")
    args = ap.parse_args(argv)
    rep = analyze()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep)
        if args.verbose:
            for p in rep["plugins"]:
                print(f"\n{p['id']}: {p['third_party']}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

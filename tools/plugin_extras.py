"""按**启用集**算出要装哪些插件 extras —— 第 5 步的"省几个 GB"就落在这里。

问题
----
以前 `tools/setup_env.ps1` 是"一刀全装"：`pip install -r requirements.txt`（含
torch 2GB）+ demucs + qwen-tts。而用户可能只想换音色 —— 那 2GB 白下。
插件化把"哪个能力要哪个包"写进了 `m2_server/plugins/<id>/plugin.json` 的
`extras.python`，本模块把它算成一个**可安装的包清单**。

启用集从哪来
------------
1. `outputs/plugins.json` 的 `disabled` 列表（用户在设置页关掉的能力）——
   **这是真相源**：关掉的能力，它的重包不该再装。
2. 新机器还没有那个文件时，用 `-Preset` 给的起点（见 `plugin_manifest.PRESETS`，定义与
   `docs/插件化设计.md` §8.1 一致）。
3. `requires` 闭包：启用 A 就必须启用 A 依赖的 B（否则 A 起来也是 broken）。

为什么用 Python 写而不是直接写在 PowerShell 里
---------------------------------------------
`setup_env.ps1` 的职责是"执行安装"，而"算清单"是纯逻辑 —— 放在 Python 里才能
被 pytest 直接钉住（PowerShell 在本仓库没有测试手段），也能被 `doctor.py` 复用
做**逐项对账**（第 5 步的验收方式）。

用法
----
    python tools/plugin_extras.py                 # 人读：启用集 + 要装的包
    python tools/plugin_extras.py --preset light
    python tools/plugin_extras.py --json          # 机器读（setup_env.ps1 用这个）
    python tools/plugin_extras.py --check         # 逐项对账：这些包当前装了没
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_M2 = ROOT / "m2_server"

# 套餐预设**定义在 `plugin_manifest.PRESETS`**（`docs/插件化设计.md` §8.1 的表）。
# 不放本模块：设置页要展示预设、开关接口要套用预设，三处不能各写一份。
# 本模块每次调用时从 manifest 读，保证与后端永远一致。

# 必须从 **CUDA 索引** 装的包。直接从 PyPI 装会拿到 CPU 版 →
# "显存不可用、推理静默失效"（README 警告过的那条红线），所以单独拎出来。
CUDA_INDEX_PACKAGES = ("torch", "torchaudio")
DEFAULT_CUDA_TAG = "cu128"
CUDA_INDEX_URL = "https://download.pytorch.org/whl/{tag}"


def _manifest():
    """导入 `plugin_manifest`（它在 m2_server/ 下，需要先把路径塞进 sys.path）。"""
    if str(_M2) not in sys.path:
        sys.path.insert(0, str(_M2))
    import plugin_manifest  # noqa: PLC0415

    return plugin_manifest


def resolve(preset: str | None = None, only: list[str] | None = None) -> dict:
    """算出一份"启用集 → 要装的包"清单。

    参数
    ----
    preset : `plugin_manifest.PRESETS` 的键；`None` 时取默认预设。给了 `only` 就忽略它。
    only   : 显式指定启用的插件 id（高级用法 / 测试用）。
    """
    pm = _manifest()
    plugins = {p.id: p for p in pm.load_all()}

    if only:
        wanted = pm.expand(set(only))
        preset_used = "(显式 -Plugins)"
    else:
        name = preset or pm.DEFAULT_PRESET
        if name not in pm.PRESETS:
            raise KeyError(f"未知预设 {name!r}，可选：{sorted(pm.PRESETS)}")
        wanted = pm.preset_ids(name)
        preset_used = name

    # 用户关掉的能力：再减掉一次。但**被别的启用插件依赖**的不能真减 ——
    # 宁可多装一个包，也不要让"关掉 A"把"还在用的 B"弄缺件。
    # 保留与否统一由 manifest 的 `enabled_ids()` 判定（后端挂载用的是同一份逻辑，
    # 两处各判一次必然漂），这里只做「取交集」。
    disabled = pm.disabled_ids()
    on = pm.enabled_ids(disabled)
    effective_off = {pid for pid in wanted if pid not in on}
    kept = {pid for pid in disabled if pid in wanted and pid in on}
    enabled = sorted(wanted - effective_off, key=lambda i: plugins[i].order)

    pkgs: list[str] = []
    external: dict[tuple, dict] = {}
    models: dict[tuple, dict] = {}
    # 逐插件的 extras：`doctor.py` 要回答"缺的这个包是哪个能力要的"，
    # 只给一个并集的话用户还是不知道该开/关哪个能力。
    by_plugin: dict[str, dict] = {}
    for pid in enabled:
        p = plugins[pid]
        by_plugin[pid] = {
            "name": p.name,
            "python": list(p.extras.get("python", [])),
        }
        for pkg in p.extras.get("python", []):
            if pkg not in pkgs:
                pkgs.append(pkg)
        # 去重键：同一个环境变量指向的东西就是一个东西（三个插件都写了 VM_RVC_ROOT，
        # 但 label 各不相同 —— 不去重会在报告里列三遍同一个 RVC 整合包）。
        for item in p.extras.get("external", []):
            external.setdefault((item.get("kind"), item.get("env") or item.get("label")), item)
        for item in p.extras.get("models", []):
            models.setdefault((item.get("kind"), item.get("env") or item.get("label")), item)

    cuda = [x for x in pkgs if x.lower() in CUDA_INDEX_PACKAGES]
    pip = [x for x in pkgs if x.lower() not in CUDA_INDEX_PACKAGES]
    return {
        "preset": preset_used,
        "enabled": enabled,
        "enabled_count": len(enabled),
        "disabled_by_user": sorted(effective_off, key=lambda i: plugins[i].order),
        "kept_despite_disabled": sorted(kept, key=lambda i: plugins[i].order),
        "python": pkgs,
        "python_cuda": cuda,
        "python_pip": pip,
        "by_plugin": by_plugin,
        "external": list(external.values()),
        "models": list(models.values()),
    }


def check_installed(python: str, packages: list[str], timeout: int = 240) -> dict[str, bool]:
    """逐个问解释器 `importlib.util.find_spec` 能不能找到这些包。

    一次子进程查完（逐个起进程在 Windows 上要几秒 × N）。用 `find_spec` 而不是
    `import`：不执行包代码，快且没有副作用（torch 的 import 要好几秒）。
    """
    if not packages:
        return {}
    script = (
        "import importlib.util, json, sys\n"
        "names = json.loads(sys.argv[1])\n"
        "out = {}\n"
        "for n in names:\n"
        "    try:\n"
        "        out[n] = importlib.util.find_spec(n) is not None\n"
        "    except Exception:\n"
        "        out[n] = False\n"
        "print(json.dumps(out))\n"
    )
    from audit_plugin_deps import import_name_of  # noqa: PLC0415  （同目录，可移植名）

    probe = {pkg: import_name_of(pkg) for pkg in packages}
    try:
        proc = subprocess.run(
            [python, "-c", script, json.dumps(sorted(set(probe.values())))],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return dict.fromkeys(packages, False)
    try:
        found = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return dict.fromkeys(packages, False)
    return {pkg: bool(found.get(mod)) for pkg, mod in probe.items()}


def _print_human(rep: dict) -> None:
    print(f"预设：{rep['preset']} · 启用 {rep['enabled_count']} 个能力")
    if rep["disabled_by_user"]:
        print(f"（按你的设置关掉：{'、'.join(rep['disabled_by_user'])}）")
    if rep["kept_despite_disabled"]:
        print(f"（你关掉了 {'、'.join(rep['kept_despite_disabled'])}，但还有启用中的能力依赖它 → 仍然装上）")
    print(f"\n要装的 pip 包（{len(rep['python'])} 个）：")
    if rep["python_cuda"]:
        print(f"  从 CUDA 索引装：{' '.join(rep['python_cuda'])}")
    print(f"  普通安装：{' '.join(rep['python_pip']) or '（无）'}")
    if rep["external"]:
        print(f"\n外部依赖（pip 装不了，需自行准备 {len(rep['external'])} 项）：")
        for item in rep["external"]:
            size = f" · 约 {item['size_hint_mb']}MB" if item.get("size_hint_mb") else ""
            print(f"  [{item.get('kind')}] {item.get('label')}{size}")
    if rep["models"]:
        print(f"\n模型权重（{len(rep['models'])} 项）：")
        for item in rep["models"]:
            size = f" · 约 {item['size_hint_mb']}MB" if item.get("size_hint_mb") else ""
            print(f"  {item.get('label')}{size}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按启用集算插件 extras")
    ap.add_argument("--preset", choices=sorted(_manifest().PRESETS), default=None)
    ap.add_argument("--plugins", nargs="*", default=None, help="显式指定启用的插件 id")
    ap.add_argument("--json", action="store_true", help="输出 JSON（setup_env.ps1 消费）")
    ap.add_argument("--check", action="store_true", help="逐项对账：这些包当前装了没")
    ap.add_argument("--python", default=None, help="对账用的解释器（默认项目 .venv）")
    args = ap.parse_args(argv)

    rep = resolve(preset=args.preset, only=args.plugins)

    if args.check:
        py = args.python or str(ROOT / ".venv" / "Scripts" / "python.exe")
        if not Path(py).exists():
            py = sys.executable
        rep["installed"] = check_installed(py, rep["python"])
        rep["missing"] = sorted(k for k, v in rep["installed"].items() if not v)
        rep["check_python"] = py

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep)
        if args.check:
            miss = rep["missing"]
            print(f"\n对账（{rep['check_python']}）：{len(rep['python']) - len(miss)}/{len(rep['python'])} 已装")
            if miss:
                print(f"  缺：{'、'.join(miss)}")
                print(f'  补：python tools/setup_env.ps1 -Preset {rep["preset"]}')
            else:
                print("  启用集所需的包都齐了")
    return 1 if args.check and rep["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

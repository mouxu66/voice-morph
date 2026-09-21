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


def first_party_closure(seeds: list[str]) -> set[str]:
    """从若干 first-party 模块出发，收集**可达的全部 first-party 模块名**（含自身）。

    与 `third_party_closure` 走同一个 import 图，只是收集**另一边**：
    那边问「要装什么包」，这里问「会拉起哪些自家模块」。

    用途是回答一个光看代码答不出来的问题：**关掉插件 X，哪些模块就不再被加载了？**
    以及反过来 —— **哪些模块绝对关不掉**（因为另一个还开着的插件也要它）。
    """
    seen: set[Path] = set()
    names: set[str] = set()
    queue: list[Path] = []
    for name in seeds:
        p = _resolve(name)
        if p is not None and p not in seen:
            seen.add(p)
            names.add(name.split(".")[0])
            queue.append(p)

    while queue:
        path = queue.pop()
        for mod, _guarded in _imports_of(path).items():
            if mod in sys.stdlib_module_names:
                continue
            dep = _resolve(mod)
            if dep is not None:
                names.add(mod.split(".")[0])
                if dep not in seen:
                    seen.add(dep)
                    queue.append(dep)
    return names


# first-party 模块里**不属于任何插件闭包、也不是运行时入口可达**的那些。
# 它们要么是死代码，要么是只有开发/验收脚本才用的一次性工具 —— 两种都合理，
# 但**必须显式登记**：默默躺着的话，下一个人分不清「这是故意的」还是「漏接了」。
#
# 口径与 `EXEMPT` 一致：只放真正在干活的条目，加一条要能说清它为什么不该进插件闭包。
# 键取模块名（`m2_server/*.py` 的 stem）。
#
# `kind` 是给人看的分类，不影响判定：
#   devtool     —— 人手动跑的验收/生成工具，不在任何请求链路上
#   test-infra  —— pytest 基建 / 用例（不是运行时模块）
#   dead        —— 已确认无任何引用的遗留代码，**建议清理**
#
# ⚠️ 这里**没有** subprocess 类：子进程入口的归属是**算得出来的**（见 `_SCRIPT_PATH_RE`），
#    2026-09-21 起由工具自动归属，不需要人来记。曾在此登记过 5 条（audition_score /
#    cascade_stream / offline_vc_infer / qwen3_tts_service / play_worker），
#    机械判定落地后它们全变成「过期登记」，已删 —— 能算的就别手写。
ORPHAN_OK: dict[str, tuple[str, str]] = {
    "enhance_calib": ("devtool", "DeepFilterNet 降噪强度标定（P2-5 验收工具），人手动跑，产出结论写进配置"),
    "qwen3_verify": ("devtool", "拉起 TTS worker 做端到端验证的排查脚本"),
    "make_test_audio": ("devtool", "生成合成测试音频，无真实素材时验证链路"),
    "conftest": ("test-infra", "pytest 的 sys.path/conftest 基建"),
    "test_wechat_voice": ("test-infra", "wechat_voice 的逻辑层回归用例（放在 m2_server 下而非 tests/）"),
    "_audio_backend": ("dead", "★ 死代码：两个 patch_* 函数全仓零调用，其要绕的 torchcodec ABI 问题也已无人提及 —— 建议删"),
    "fast_tts": ("dead", "★ 死代码：被 qwen3_tts_service 的 faster 后端取代（其注释明写「彻底移除 fast_tts.py」）—— 建议删"),
}

# 子进程入口的**发现方式**（2026-09-21 加）：模块里出现 `"xxx.py"` 字面量，
# 且 `xxx` 是 first-party 模块 → 视为「本模块会把这个脚本当子进程起」。
#
# 为什么必须机械化：`audit_plugin_deps.py` 的 docstring 早就承认「子进程依赖看不见，
# 必须靠人读代码声明」—— 于是这 5 个子进程入口在归属清点里全成了「没人管」。
# 而它们其实**归属明确**：谁起它，就属于谁（audition_score 归 sound.audition，
# cascade_stream 归 sound.rvc-live……）。这个关系在源码里是**看得见**的
# （`SCORE_PY = Path(__file__).resolve().parent / "audition_score.py"`），
# 只是不在 import 图里。补上这条边，5 条人工登记就能删掉。
_SCRIPT_PATH_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*)\.py["']""")


def _script_refs(module: str) -> set[str]:
    """模块源码里以 `"xxx.py"` 字面量指到的 first-party 模块（子进程入口）。

    见 `_SCRIPT_PATH_RE` 的说明：这是 import 图之外的第二类依赖边，
    也是「子进程入口的归属」唯一能从源码里算出来的依据。
    """
    p = _resolve(module)
    if p is None:
        return set()
    try:
        src = p.read_text(encoding="utf-8")
    except OSError:
        return set()
    out: set[str] = set()
    for stem in _SCRIPT_PATH_RE.findall(src):
        if stem == module:
            continue
        if _resolve(stem) is not None:
            out.add(stem)
    return out


def analyze_ownership(plugins: list) -> dict:
    """first-party 模块的插件归属：谁独占、谁共用、谁没人管。

    为什么需要（2026-09-21 补）：插件化把能力做成可关之后，「关掉它到底省掉什么」
    这个问题一直没人答得上来。`rvc_common` 被 9 个插件拉进去，`config` / `runtime` /
    `common` 几乎人人要 —— 光看 `plugins/*/plugin.json` 完全看不出来，
    只能现场读 import 图。

    三种归属，含义完全不同（这正是必须算出来、不能猜的原因）：
      · **独占**（只有 1 个插件可达）：关掉那个插件，这个模块就真的不再被加载 →
        它才适合写进该插件的「省掉什么」描述里。
      · **共用**（≥2 个插件可达）：**关谁都关不掉它**。谁要是把它当成某个插件的
        私产、在"关掉时跳过加载"的清单里写上它，另一个插件就断链了。
      · **无人可达**：不在任何插件闭包里，也不在运行时入口闭包里 → 死代码或开发工具，
        需要在 `ORPHAN_OK` 里显式登记。

    另外单独算 **`always_on`**：从 `server` 入口可达的模块。它们和插件开关无关，
    任何时候都在进程里 —— 「关掉插件能省内存」的说法对它们不成立。

    ★ 依赖边有**两类**，都要走：`import` 之外还有**子进程入口**
    （`Path(__file__).parent / "audition_score.py"` 这种）。只走 import 会让
    5 个子进程入口全变成「没人管」；它们是归属明确的，只是不在 import 图里。
    两类边合并后做**不动点**传播：A 起 B、B 起 C，则 C 也归 A 的所有者。

    ⚠️ 读结果时注意「**薄共用**」：一个模块被 N 个插件可达，可能只是因为大家都
    `from cascade import _cascade_alive` 取了**一个**小工具，而不是真有结构性耦合。
    实测 `cascade` 归 9 个插件就是这种情况（7 个模块只为拿一个存活探测函数）。
    这类模块的**正解是把那个符号挪进中立模块**（`runtime` 之类），而不是维持现状 ——
    否则「关掉 sound.rvc-live 就能卸载 cascade」这句话是假的。本工具只如实报告
    可达性，不替你做这个重构决定。
    """
    # ---- 边：import（按文件解析）+ 子进程引用（按源码字符串） ----
    edges: dict[str, set[str]] = {}

    def refs_of(module: str) -> set[str]:
        if module in edges:
            return edges[module]
        p = _resolve(module)
        out: set[str] = set()
        if p is not None:
            for mod, _guarded in _imports_of(p).items():
                if mod in sys.stdlib_module_names:
                    continue
                if _resolve(mod) is not None:
                    out.add(mod.split(".")[0])
        out |= _script_refs(module)
        edges[module] = out
        return out

    def closure(seed_set: set[str]) -> set[str]:
        """沿两类边做传递闭包。"""
        seen = set(seed_set)
        stack = list(seed_set)
        while stack:
            cur = stack.pop()
            for nxt in refs_of(cur):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    seeds_by_plugin: dict[str, set[str]] = {}
    for p in plugins:
        s = list(p.routers)
        s += [h["module"] for h in p.hooks]
        if p.health:
            s.append(p.health["module"])
        seeds_by_plugin[p.id] = {x.split(".")[0] for x in s}

    # `always_on` = **不经过任何插件 router** 就能从进程入口到达的模块。
    # ⚠️ 绝不能把插件 seeds 也算进 always_on：那样它的闭包会吞掉所有 router 的依赖，
    #    于是「独占/共用」全被扣空（实测：共用 19 → 0、独占 30）。2026-09-21 踩过。
    #    它要回答的是「关掉插件也省不掉的基建有哪些」，所以种子只能是入口本身。
    always_on = closure({"server", "plugin_loader"})

    per_plugin: dict[str, set[str]] = {}
    for pid, seeds in seeds_by_plugin.items():
        # 扣掉 always_on：那些是运行时基建，关谁都不会卸载，算进任何插件的
        # 「独占集」都是噪声（会让人以为关掉该插件能省下 config/runtime）。
        # 但插件自己的种子若恰好也在基建闭包里，仍要保留 —— 它就是那个插件的入口。
        per_plugin[pid] = closure(seeds) - (always_on - seeds)

    owners: dict[str, set[str]] = {}
    for pid, mods in per_plugin.items():
        for m in mods:
            owners.setdefault(m, set()).add(pid)

    shared = {m: sorted(ps) for m, ps in owners.items() if len(ps) >= 2}
    exclusive = {m: sorted(ps)[0] for m, ps in owners.items() if len(ps) == 1}

    candidates = {p.stem for p in _M2.glob("*.py")}
    unowned = sorted(candidates - set(owners) - always_on)

    return {
        "per_plugin": {k: sorted(v) for k, v in sorted(per_plugin.items())},
        "shared": {m: ps for m, ps in sorted(shared.items())},
        "exclusive": exclusive,
        "always_on": sorted(always_on),
        "unowned": unowned,
        "orphan_ok": {k: {"kind": v[0], "why": v[1]} for k, v in ORPHAN_OK.items()},
        "unregistered": [m for m in unowned if m not in ORPHAN_OK],
        "stale_orphan_entries": sorted(k for k in ORPHAN_OK if k not in unowned),
    }


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
    report["ownership"] = analyze_ownership(plugins)
    report["ok"] = not report["undeclared"] and not report["ownership"]["unregistered"]
    return report


def _print_ownership(own: dict, verbose: bool) -> None:
    """first-party 模块的归属报告 —— 回答「关掉插件到底省掉什么」。"""
    dead = [m for m, v in own["orphan_ok"].items() if v["kind"] == "dead"]
    print("\n──────── first-party 模块归属 ────────")
    print(
        f"共用（≥2 插件可达）{len(own['shared'])} 个 · "
        f"独占（仅 1 插件可达）{len(own['exclusive'])} 个 · "
        f"运行时基建 {len(own['always_on'])} 个 · "
        f"无人可达 {len(own['unowned'])} 个"
    )

    if own["shared"]:
        print("\n★ 共用模块 —— **关掉任何一个插件都卸载不掉它们**（另一个还开着就要用）：")
        for m, ps in sorted(own["shared"].items(), key=lambda kv: (-len(kv[1]), kv[0]))[:15]:
            print(f"     {m:22s} {len(ps):2d} 个插件：{', '.join(ps[:6])}{' …' if len(ps) > 6 else ''}")
        if len(own["shared"]) > 15:
            print(f"     …（另有 {len(own['shared']) - 15} 个，见 --json）")
        print("     ⚠️ 谁要是把这些当成某个插件的私产、写进「关掉时跳过加载」的清单，")
        print("        另一个插件就断链了 —— 这就是「关谁会断链」的答案。")

    if own["always_on"]:
        print(f"\n运行时基建（从 server 入口可达，与插件开关无关）：{', '.join(own['always_on'])}")

    if verbose:
        print("\n独占模块（关掉该插件即可省掉）：")
        by_plug: dict[str, list[str]] = {}
        for m, pid in own["exclusive"].items():
            by_plug.setdefault(pid, []).append(m)
        for pid in sorted(by_plug):
            print(f"     {pid:20s} ← {', '.join(sorted(by_plug[pid]))}")

    if own["unowned"]:
        print(f"\n无人可达 {len(own['unowned'])} 个：")
        for m in own["unowned"]:
            rec = own["orphan_ok"].get(m)
            if rec is None:
                # ★ 未登记的要**显眼**：它就是这个门禁失败的原因。
                # 曾把这段提示关在 `if not --ownership-only` 里，结果 check.py 的
                # ownership 步只报「失败」不报「哪个模块」—— 门禁红了却看不出为什么。
                print(f"   ❌ {m:22s} [未登记] 分不清是死代码还是漏接")
            elif rec["kind"] == "dead":
                print(f"   ⚠️ {m:22s} [dead] {rec['why']}")
            else:
                print(f"      {m:22s} [{rec['kind']}] {rec['why']}")
    if dead:
        print(f"\n⚠️ 其中 {len(dead)} 个是已确认的死代码，建议清理：{', '.join(dead)}")
    if own["stale_orphan_entries"]:
        print(
            f"\n⚠️ 这些登记已过期（不再是孤儿了），请从 ORPHAN_OK 删掉："
            f"{', '.join(own['stale_orphan_entries'])}"
        )


def _print_third_party(rep: dict) -> None:
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
    # 输出编码不是装饰（2026-09-21 实测）：Windows 下 stdout 被**重定向**时（管道/文件 ——
    # pytest 的子进程、CI 日志、`> out.txt` 都是）按 ANSI(cp936) 编码，而本文件要打印的
    # `⚠️` / `✅` / `❌` 是 GBK **之外**的字 → UnicodeEncodeError，报告在「共用模块」
    # 那一段（第 481 行）当场断掉。控制台直连时不触发（走 WriteConsoleW），所以它只在
    # 重定向/子进程里现形。真切不了也无所谓，不该因编码设置失败而挂掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="插件可选依赖 + 模块归属审计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="列出每个插件的完整第三方闭包 / 独占模块")
    ap.add_argument(
        "--ownership-only",
        action="store_true",
        help="只看 first-party 模块归属（「关谁会断链」），不查第三方依赖",
    )
    args = ap.parse_args(argv)
    rep = analyze()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    if not args.ownership_only:
        _print_third_party(rep)
    _print_ownership(rep["ownership"], args.verbose)
    if args.verbose and not args.ownership_only:
        for p in rep["plugins"]:
            print(f"\n{p['id']}: {p['third_party']}")

    own = rep["ownership"]
    if not rep["ok"]:
        if own["unregistered"]:
            print(
                f"\n❌ 这些 first-party 模块没人可达也没登记 —— 分不清是死代码还是漏接：\n"
                f"     {', '.join(own['unregistered'])}\n"
                f"     修法：确认它的用途后加进 tools/audit_plugin_deps.py 的 ORPHAN_OK"
                f"（带 kind 与理由）；确实是遗留下来的就删掉。"
            )
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

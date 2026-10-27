r"""`README.md` / `docs/交接-AI必读.md` 里的**数字**必须与源码一致（2026-10-02）。

### 为什么需要

已有的 `test_docs_references.py` 守的是「引用能不能解析」（§编号、死链）。
这一份守的是另一类腐烂：**文档里的数字**。

这类数字漂移最便宜也最难察觉 —— 改一个数字不报错、少一个能力不报错、
路径挪个地方也不报错，只有「文档说有 26 个 router、清单里其实 29 个」这种
**两边都在、只是对不上**的状态会一直躺着。而 README 恰恰是这类数字最密集的地方：
能力数、router 数、模块清单、打包目录、文件行数。

实测抓到的（2026-10-02 逐条对源码核实）：

| 文档写的 | 源码实际 | 怎么发现的 |
|---|---|---|
| 「21 个能力（7 核心 + 14 可选）」 | **22**（7 + 15） | `ls m2_server/plugins/` |
| 「**26 个独立 router**」 | **29**（全开预设下） | `plugin_manifest.expand()` 后取 routers 并集 |
| 「12 个按域路由模块」 | 12 个确实在，但清单里**漏了 `ab_chain`** | 逐个 `Path.is_file()` |
| 「插件总数 20 → 21」 | 22 | 同上 |
| 输出目录只写 `release2/` | `web/release/` 仍存在（8-29 的旧产物） | `ls web/` |

### 为什么不用「解析数字再比对」

想过写成「从文档里正则抓 `(\d+) 个 router` 再跟清单比」——**放弃了**：
那是在给「人怎么写这句话」建正则，改个措辞就假红，而门禁假红一次就再也不会被信任。
这里改成**反向声明**：每个要守的数字在 `_EXPECTED` 里写死，旁边写清它由什么算出，
文档里对应那句话必须出现这个数字。改代码后门禁红，红了就照提示改文档 ——
**这才是「门禁在守真相」而不是「门禁在守措辞」**。

### 边界

只守**会漂移且有人会照着查**的数字。不守行数（`README` 里的模块行数随时在动，
守它只会逼人改文档而没人受益）、不守能力的行为描述。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_M2 = _ROOT / "m2_server"
_PLUGINS = _M2 / "plugins"

#: 读后端源码用（拿真实的 plugin_manifest 结论，不重算一遍逻辑）。
sys.path.insert(0, str(_M2))


# --------------------------------------------------------------------------
# 真相源：全部从 plugin_manifest / 插件目录现算
# --------------------------------------------------------------------------
def _truth() -> dict:
    """能力数、router 数、前端路由数 —— 全部现算，不写死。"""
    import plugin_manifest as pm

    plugins = pm.by_id()
    all_ids = list(plugins)
    enabled = pm.expand(all_ids)  # 全开预设
    routers: set[str] = set()
    for pid in enabled:
        routers.update(plugins[pid].routers or [])
    return {
        "plugins_total": len(plugins),
        "plugins_core": sum(1 for p in plugins.values() if p.is_core),
        "plugins_optional": sum(1 for p in plugins.values() if not p.is_core),
        "routers_max": len(routers),
        "frontend_routes": sum(len(plugins[pid].routes or []) for pid in all_ids),
        "nav_routes": sum(
            1
            for pid in all_ids
            for r in (plugins[pid].routes or [])
            if (r.get("nav") or {}).get("label")
        ),
    }


@pytest.fixture(scope="module")
def truth() -> dict:
    t = _truth()
    # 空集哨兵：全算成 0 的话下面所有「文档写了 N」都对得上，门禁变空转
    assert t["plugins_total"] > 0 and t["routers_max"] > 0, f"plugin_manifest 读出空结果：{t}"
    return t


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 一、能力数与 router 数
# --------------------------------------------------------------------------
def test_readme_capability_count_matches_manifest(truth):
    """README 说「共 N 个能力（X 核心 + Y 可选）」→ N/X/Y 必须等于清单实数。"""
    text = _read(_ROOT / "README.md")
    hits = re.findall(
        r"共\s*(\d+)\s*个能力[（(]\s*(\d+)\s*个核心\s*\+\s*(\d+)\s*个可选\s*[）)]",
        text,
    )
    assert hits, (
        "README 里找不到「共 N 个能力（X 核心 + Y 可选）」这句 —— "
        "改写这句话之前，先把 _EXPECTED 里的对应条目一并更新，"
        "否则这个门禁会在某次措辞调整后悄悄变成空转"
    )
    want = (str(truth["plugins_total"]), str(truth["plugins_core"]), str(truth["plugins_optional"]))
    for found in hits:
        assert found == want, (
            f"README 说 {found[0]} 个能力（{found[1]} 核心 + {found[2]} 可选），"
            f"清单实际 {want[0]}（{want[1]} + {want[2]}）"
            f" —— 见 m2_server/plugins/（实数 {truth['plugins_total']} 个目录）"
        )


def test_readme_router_count_matches_manifest(truth):
    """README 说「N 个独立 router」→ N 必须等于**全开预设下**的唯一 router 模块数。

    用全开而不是当前启用集：文档描述的是后端的**能力上限**，
    而当前启用集会随用户开关变化（standard 预设只挂 16 个），拿它当真相源
    会让 README 随用户设置漂移。
    """
    text = _read(_ROOT / "README.md")
    hits = re.findall(r"[「\*\s]*(\d+)\s*个独立\s*router", text)
    assert hits, "README 里找不到「N 个独立 router」—— 门禁空转了，见上面 docstring"
    for found in hits:
        assert int(found) == truth["routers_max"], (
            f"README 说 {found} 个独立 router，全开预设下实际 {truth['routers_max']} 个"
            "（`plugin_manifest.expand(全部 id)` 后各插件 routers 的并集）"
        )


def test_plan_section_does_not_claim_router_count(truth):
    """「未完成清单」那一节不该复述 router 数 —— 它是规划，不是接口清单。

    留这条是因为 2026-10-02 的改正里发现 `ROADMAP.md` 确实有一处「插件总数 20 → 21」，
    而它同期的 21 已经不是真的了。数字在规划稿里只会腐化。

    2026-10-27：`ROADMAP.md` 已并入 `交接-AI必读.md` §6，**规则跟着搬** —— 盯 §6 那一段。
    事实数字（§2 代码地图）不在此列：那是带日期的实测值，本来就该写下来。

    只看**正文行**：这一节里的引用块（`> ` 开头）是在解释规则本身，不是复述数字。
    """
    text = _read(_ROOT / "docs" / "交接-AI必读.md")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## 6 "))
    end = next((i for i, ln in enumerate(lines[start + 1 :], start + 1) if ln.startswith("## ")), len(lines))
    offenders: list[str] = []
    for i, ln in enumerate(lines[start:end], start + 1):
        if ln.startswith("> "):
            continue
        if re.search(r"(\d+)\s*个\s*router|router\s*数", ln):
            offenders.append(f"交接 §6:{i} → {ln.strip()[:70]}")
    assert not offenders, (
        "「未完成清单」正文里有 router 数量表述 —— 接口清单属于 README，"
        f"规划只写能力边界。当前实数是 {truth['routers_max']}（会漂移，别写进来）：\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# 二、README 的模块清单必须都真在
# --------------------------------------------------------------------------
#: README「目录结构」里点名的 `m2_server/*.py`。少一个 → 那行是幽灵。
_README_NAMED_MODULES = (
    "server.py",
    "runtime.py",
    "system_api.py",
    "voices_api.py",
    "raw_media_api.py",
    "pipeline_api.py",
    "clips_api.py",
    "tts_api.py",
    "mine_api.py",
    "capture_api.py",
    "ab_api.py",
    "ab_chain.py",
    "audio_api.py",
    "media_api.py",
    "rvc_dataset_api.py",
    "market_api.py",
    "pet_market_api.py",
    "config.py",
    "common.py",
    "history.py",
    "clip_qc.py",
    "speaker_sep.py",
    "loopback_capture.py",
    "cascade.py",
    "rvc_live.py",
    "offline_vc.py",
    "seed_vc.py",
    "audiobook.py",
    "effects.py",
    "wechat_voice.py",
    "finetune.py",
    "history_api.py",
    "qwen3_tts.py",
    "qwen3_tts_service.py",
    "plugin_loader.py",
    "plugin_manifest.py",
)


def test_readme_named_modules_all_exist():
    """README 点名的每个 `m2_server/*.py` 都必须在。

    2026-10-02 实测抓到 `ab_chain` 被漏在清单外（它 2026-09 就存在了）——
    漏列的代价比列错小，但一样会让「照 README 找文件」的人扑空。
    """
    missing = [m for m in _README_NAMED_MODULES if not (_M2 / m).is_file()]
    assert not missing, f"README 目录结构里点了名但仓库里没有：{missing}"


def test_readme_directory_tree_covers_every_api_module():
    """反向：`m2_server/` 下所有**路由模块**都该在 README 的清单里出现。

    这是防「README 忘了列新 router」的那一半 —— 上一条防「列了不存在的」，
    这条防「存在了没列的」。两边合起来才是完整覆盖。
    """
    import plugin_manifest as pm

    plugins = pm.by_id()
    routers: set[str] = set()
    for pid in plugins:
        routers.update(plugins[pid].routers or [])

    text = _read(_ROOT / "README.md")
    unlisted = sorted(f"{m}.py" for m in routers if f"{m}.py" not in text)
    assert not unlisted, (
        f"这些 router 模块存在但 README 没提：{unlisted}"
        " —— 在 README「目录结构」补一行（插件化之后，路由模块与路由是声明式的）"
    )


# --------------------------------------------------------------------------
# 三、打包输出目录
# --------------------------------------------------------------------------
def test_packager_output_dir_is_what_readme_says():
    """electron-builder 的 `directories.output` 与 README 说的一致。

    README 曾经同时留着 `release/` 和 `release2/`，因为 electron-builder
    改过一次目录名而文档没跟着改。本机 `web/release/`（8-29 的旧产物）至今还在，
    让人以为两条路径都有效。
    """
    pkg = json.loads((_ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    out = pkg["build"]["directories"]["output"]
    text = _read(_ROOT / "README.md")
    assert f"`{out}/" in text or f"`{out}`" in text, (
        f"electron-builder 输出目录是 `{out}/`，README 里没提它 —— "
        "打包后找不到产物的人会先去翻文档"
    )
    # README 提到别的 release 目录时，必须明确说它已废弃
    for stale in ("`release/`", "`web/release/`"):
        if stale in text:
            idx = text.index(stale)
            window = text[max(0, idx - 220) : idx + 220]
            assert any(
                w in window for w in ("不是", "旧", "废弃", "已停用", "8-29")
            ), f"README 提到 {stale} 却没说它是旧的 —— 那是 8 月的遗留产物，别让人照着找"

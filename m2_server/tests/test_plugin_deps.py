"""插件可选依赖（`extras.python`）的门禁 —— 第 5 步「拆 core/extra」的守护。

为什么需要这一组
----------------
第 5 步把 `torch` / `demucs` / `librosa` 这些重包从 `requirements.txt`（核心）
挪进了各插件的 `extras.python`。这一挪同时打开两个失效面：

1. **往回漂**：谁顺手把 `torch` 加回 `requirements.txt`，"只装核心"就白拆了 ——
   而且不会有任何症状（装多了不会报错），只能靠门禁。
2. **漏声明**：`extras.python` 是手写的。漏一个包的症状是「装完核心环境、应用
   能启动、点到某个功能才炸」（或被 try/except 兜成"这个按钮没反应"）。

对策分两半：
* **往回漂** → `test_core_requirements_exclude_plugin_extras`（集合判据）。
* **漏声明** → `test_no_undeclared_plugin_dependencies`，直接跑
  `tools/audit_plugin_deps.py` 的静态 import 图审计（口径与局限见该文件顶部）。
* **凭印象写** → `_FROZEN_EXTRAS` 冻结快照：加/删一个包都必须动这张表，
  而表里每条都写着"为什么是它、归哪个插件"，改动时被迫过一遍脑子。

这三条**都能真红**（不是"永远绿"的摆设）：下面 `test_gate_*` 两条用 monkeypatch
做变异验证 —— 把审计的豁免表挖掉一格、把核心依赖清空，门禁必须变红。
（`tools/audit_plugin_deps.py` 的判定逻辑本身另在 `test_closure_*` 里用临时文件
单测，覆盖「被 ImportError 兜住 → optional」这条最容易写错的分支。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
_ROOT = _M2.parent
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import audit_plugin_deps as apd  # noqa: E402
import plugin_manifest  # noqa: E402

# 冻结快照：每个插件「装了它才用得上」的非核心 pip 包。
#
# ⚠️ 改这张表 = 改安装面。加一条请顺手回答两件事：**谁 import 它**、**为什么不是核心**。
# 判据由 `tools/audit_plugin_deps.py` 给出（`python tools/audit_plugin_deps.py -v`）。
_FROZEN_EXTRAS: dict[str, list[str]] = {
    # 微信链路：截图定位（Pillow）、进程/窗口操作（psutil / uiautomation）。
    # 后两个是惰性导入且有 try/except，缺了微信发送会"看着没反应"—— 属于最难查的一类。
    "hook.wechat": ["Pillow", "psutil", "uiautomation"],
    # 桌宠「录当前声音」WASAPI 内录；唯一没被 try 兜住的顶层导入之一。
    "pet.companion": ["pyaudiowpatch"],
    # 试音间：两把尺子。torch/torchaudio 是打分器子进程（sys.executable，跑主 .venv）；
    # transformers 被 tools/natscore_local 硬 import；modelscope 出 CAM++ 声纹（SECS），
    # hdbscan 给说话人聚类 —— 后两个失败会被兜成 score_error，只降级不报错。
    "sound.audition": ["torch", "torchaudio", "transformers", "modelscope", "hdbscan"],
    # 效果器变调/变速；缺失时该效果直通（effects.py 自带 ImportError 兜底）。
    "sound.effects": ["librosa"],
    # 微调：语料质检用 speaker_sep 的声纹（同试音间那套）。
    "sound.ft": ["modelscope", "hdbscan"],
    # 离线变声：Seed-VC 推理用**主 .venv** 跑子进程（seed_vc.py:39），故需 torch；
    # librosa 供「调音建议」（pitch_advice.pyin，无兜底）；
    # deepfilter-stream 供输入降噪（audio_enhance.available() 探测降级）。
    # RVC 那半边跑在 D:\RVC\.venv，不在此列（见 extras.external）。
    "sound.offline-vc": ["torch", "torchaudio", "librosa", "deepfilter-stream"],
    # 实时变声：cascade_stream 的流式降噪（同上，探测降级）。
    "sound.rvc-live": ["deepfilter-stream"],
    # 文字转语音：qwen-tts 实际跑在 tts_trial/venv312（独立环境），
    # 这里声明是为了让 setup_env 知道"启用 TTS 就得准备这个环境"。
    "sound.tts": ["qwen-tts"],
    # 训练变声：demucs 去 BGM 走 `sys.executable -m demucs`（主 .venv），
    # 故必须连 torch 一起声明 —— 否则 demucs 会从 PyPI 拉 **CPU 版** torch，
    # 变成"能跑但不用显卡"（README 警告过的静默失效）。切片质检另需声纹两件套。
    "sound.workshop": ["demucs", "torch", "torchaudio", "modelscope", "hdbscan"],
}


def _declared() -> dict[str, list[str]]:
    """只返回**非空**的 `extras.python`（空列表是"这个插件不要额外包"，不值得进快照）。"""
    return {
        p.id: list(p.extras.get("python", []))
        for p in plugin_manifest.load_all()
        if p.extras.get("python")
    }


# --------------------------------------------------------------- 冻结 / 归属


def test_declared_extras_are_frozen():
    """`extras.python` 是冻结快照：少一条、多一条、挪个插件都要显式改这张表。"""
    assert _declared() == _FROZEN_EXTRAS


def test_every_extra_is_a_plausible_pypi_name():
    """包名合法性 —— 防手滑（大写随意的 `Torch`、带空格的 `deepfilter stream`）。"""
    for pid, pkgs in _declared().items():
        for pkg in pkgs:
            assert pkg == pkg.strip(), f"{pid}: {pkg!r} 有首尾空白"
            assert " " not in pkg, f"{pid}: {pkg!r} 含空格，不是合法包名"
            assert pkg.lower() == pkg or pkg in ("Pillow",), f"{pid}: {pkg!r} 大小写可疑"


# --------------------------------------------------------------- 拆 core/extra


def test_core_requirements_exclude_plugin_extras():
    """核心 `requirements.txt` 不许出现任何插件的 extras。

    这是第 5 步唯一的不变量：核心 = 「不装它应用就起不来」，extras = 「不装它只有
    某几个能力不可用」。一个包同时出现在两边，等于把可选依赖又拉回了必装 ——
    而**不会有任何症状**（多装不报错），所以只能靠这条守着。
    """
    core = {apd.normalize(x) for x in apd._requirements(_ROOT / "requirements.txt")}
    extras = {apd.normalize(x) for pkgs in _declared().values() for x in pkgs}
    overlap = sorted(core & extras)
    assert not overlap, f"这些包同时是核心依赖和插件 extras，拆 core/extra 会失效：{overlap}"


def test_core_requirements_have_no_torch():
    """点名 torch：它是本步要省掉的 2GB，别被"顺手加回去"。"""
    core = {apd.normalize(x) for x in apd._requirements(_ROOT / "requirements.txt")}
    assert "torch" not in core
    assert "torchaudio" not in core


# --------------------------------------------------------------- 静态审计门禁


def test_no_undeclared_plugin_dependencies():
    """跑静态 import 图审计：任何**必需**的第三方 import 都必须被声明。

    口径（详见 `tools/audit_plugin_deps.py`）：非标准库、非本仓库模块、且**没有**
    被 `try/except ImportError` 兜住的导入 = 必需。被兜住的算 optional，声明与否
    都合法（声明 = 让这个可选增强默认可用）。
    """
    rep = apd.analyze()
    assert rep["ok"], "有未声明的插件依赖：\n" + "\n".join(
        f"  {m} ← {', '.join(srcs)}" for m, srcs in rep["undeclared"].items()
    )


def test_every_declared_extra_is_reachable_from_its_plugin():
    """反向：声明的包必须真的出现在该插件（或其 `requires` 闭包）的代码里。

    防的是"凭印象写依赖" —— 例如给微调写上 peft（那份依赖其实在 RVC 整合包里）。
    子进程依赖（`-m demucs`、打分器脚本）静态图看不见，故允许在 `subprocess_only`
    里显式豁免，每条都要写明理由。
    """
    subprocess_only = {
        # 命令行字符串里的依赖，静态 import 图抓不到
        # demucs 由 m1_workshop/pipeline.py 以 `sys.executable -m demucs` 拉起
        # （该模块是 importlib 动态加载，静态图整段看不见），torch 随之进主 .venv。
        "sound.workshop": {"demucs", "torch", "torchaudio"},
        # cascade_stream.py 是**子进程脚本**（由 cascade.py 以 RVC venv 拉起），
        # 主进程静态图看不到它 —— 故它用的流式降噪包要在这里说明。
        "sound.rvc-live": {"deepfilter_stream"},
        # qwen-tts 实际跑在 tts_trial/venv312，主进程只发 HTTP 请求。
        "sound.tts": {"qwen_tts"},
        # 打分器是独立子进程（sys.executable），它 import torch/transformers。
        "sound.audition": {"torch", "torchaudio", "transformers"},
        # seed_vc 子进程用主 .venv 跑 inference_v2.py，故 torch 在这里。
        "sound.offline-vc": {"torch", "torchaudio"},
    }
    for pid, pkgs in _declared().items():
        plugin = next(p for p in plugin_manifest.load_all() if p.id == pid)
        seeds = list(plugin.routers) + [h["module"] for h in plugin.hooks]
        if plugin.health:
            seeds.append(plugin.health["module"])
        closure = {apd.normalize(m) for m in apd.third_party_closure(seeds)}
        exempt = {apd.normalize(x) for x in subprocess_only.get(pid, set())}
        for pkg in pkgs:
            key = apd.normalize(apd.import_name_of(pkg))
            assert key in closure or key in exempt, (
                f"{pid} 声明了 {pkg}，但它的 router/hook 闭包里没人 import 它 —— "
                f"要么写错插件了，要么该加进 _SUBprocess_ONLY 并说明理由"
            )


# --------------------------------------------------------------- 变异验证（门禁真会红）


def test_gate_goes_red_when_an_exemption_is_removed(monkeypatch):
    """变异 1：把豁免表挖掉一格（`huggingface_hub` 是 transformers 的传递依赖），门禁必须红。

    证明的是「`ok` 不是恒真」—— 若审计逻辑退化成"永远通过"，这条会失败。

    ⚠️ 别换成 `sounddevice` 之类做变异：它被 `try/except` 兜住（optional）、
    且在 `requirements-dev.txt` 里也有，挖掉豁免**不会**变红 —— 拿它做变异
    会得到一个假绿的"验证"。豁免表里只有真正抑制告警的条目才配留在这。
    """
    monkeypatch.delitem(apd.EXEMPT, "huggingface_hub")
    rep = apd.analyze()
    assert rep["ok"] is False
    assert "huggingface_hub" in rep["undeclared"]


def test_gate_goes_red_when_core_dependencies_vanish(monkeypatch):
    """变异 2：假装核心依赖一个都没装，门禁必须红。

    守住的是「核心依赖表真的被读进来了」—— 若哪天 `core_packages()` 返回空集而
    门禁还是绿的，说明它根本没在看核心那一侧。
    """
    monkeypatch.setattr(apd, "core_packages", lambda: set())
    rep = apd.analyze()
    assert rep["ok"] is False
    assert "numpy" in rep["undeclared"]


# --------------------------------------------------------------- 判定逻辑单测


def test_closure_separates_required_from_guarded(tmp_path, monkeypatch):
    """`try/except ImportError` 兜住的导入算 optional，没兜的算 required。

    这条是审计工具最容易写错的地方（也是它相对"人肉 grep"的唯一增量），
    故用临时文件直接钉死，不依赖仓库现状。
    """
    mod = tmp_path / "demo_mod.py"
    mod.write_text(
        "import numpy\n"
        "try:\n"
        "    import optional_thing\n"
        "except ImportError:\n"
        "    optional_thing = None\n"
        "def later():\n"
        "    import lazy_required\n"
        "    return lazy_required\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(apd, "_SOURCE_ROOTS", (tmp_path,))
    closure = apd.third_party_closure(["demo_mod"])
    assert closure["numpy"]["required"] is True
    assert closure["lazy_required"]["required"] is True, "惰性导入没兜底 → 仍算必需"
    assert closure["optional_thing"]["required"] is False, "被 ImportError 兜住 → optional"


def test_closure_does_not_follow_stdlib_or_first_party(tmp_path, monkeypatch):
    """标准库与本仓库模块不进"第三方"名单（否则报告会被 json/os/pathlib 淹没）。"""
    (tmp_path / "sibling.py").write_text("import json\nimport numpy\n", encoding="utf-8")
    (tmp_path / "entry.py").write_text("import json\nimport sibling\n", encoding="utf-8")
    monkeypatch.setattr(apd, "_SOURCE_ROOTS", (tmp_path,))
    closure = apd.third_party_closure(["entry"])
    assert "json" not in closure
    assert "sibling" not in closure
    assert "numpy" in closure, "传递到的 first-party 模块的依赖也要算进来"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("torch", "torch"),
        ("torch>=2.9", "torch"),
        ("uvicorn[standard]", "uvicorn"),
        ("Pillow", "pil"),
        ("python-dotenv", "dotenv"),
        ("qwen-tts", "qwen_tts"),
    ],
)
def test_name_normalization(raw, expected):
    """包名归一化：安装名 ↔ import 名（`Pillow`→`PIL` 是唯一的常见例外）。

    注：注释行由 `_requirements()` 剥掉（`#` 之前全丢），不在 `normalize` 的职责里。
    """
    got = apd.import_name_of(raw)
    assert got == expected


def test_requirements_parser_skips_comments_and_flags(tmp_path):
    """`_requirements()` 要剥掉行尾注释与 `-r` / `--index-url` 这类指令。"""
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# 头部注释\n"
        "\n"
        "python-dotenv  # .env 自动加载（config.py）\n"
        "-r other.txt\n"
        "--index-url https://example.invalid/simple\n"
        "uvicorn[standard]\n",
        encoding="utf-8",
    )
    assert apd._requirements(req) == {"dotenv", "uvicorn"}

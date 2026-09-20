"""`tools/plugin_extras.py` 的门禁 —— 第 5 步「按启用集装」的算账逻辑。

为什么单独测
------------
`setup_env.ps1` 里那段"装什么"现在只剩"执行"（读 JSON → 调 pip），
真正的判断全在这个模块里。而 PowerShell 在本仓库没有测试手段，
所以这里的用例就是**安装面唯一的防线**：

* 套餐之间必须是包含关系（轻量 ⊂ 标准 ⊂ 全能）—— 不然"换套餐"会**少装**东西，
  而少装的症状是"某个页面点进去报 ModuleNotFoundError"，很难联想到安装脚本。
* `requires` 闭包必须补全 —— `sound.audition` 依赖 `sound.offline-vc`，
  只启用前者时后者的 extras 也得装上。
* `torch` / `torchaudio` 必须落在 **CUDA 桶**里 —— 混进普通桶就是
  "装了 CPU 版、显存不可用、推理静默失效"（README 警告过的那条红线）。
* 用户关掉的能力不该再装它的重包，**但**如果别的启用插件依赖它，就不能真去掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import plugin_extras as pe  # noqa: E402


@pytest.fixture(autouse=True)
def _no_user_state(monkeypatch):
    """默认假装"用户一个都没关"，免得本机 outputs/plugins.json 影响断言。

    需要验禁用逻辑的用例自己再 monkeypatch 覆盖。
    """
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: set())


def _ids(rep: dict) -> set[str]:
    return set(rep["enabled"])


# ------------------------------------------------------------------ 预设


def test_presets_are_nested():
    """轻量 ⊂ 标准 ⊂ 全能 —— 换套餐只该"多装"，不该"少装"。"""
    light = _ids(pe.resolve(preset="light"))
    standard = _ids(pe.resolve(preset="standard"))
    full = _ids(pe.resolve(preset="full"))
    assert light < standard < full, (sorted(light - standard), sorted(standard - full))


def test_core_preset_is_core_only():
    """`core` 预设只含核心插件（跑测试 / CI 用），不含任何可选能力。"""
    rep = pe.resolve(preset="core")
    assert rep["enabled"], "core 预设不该是空的"
    for pid in rep["enabled"]:
        assert pid.startswith("core."), f"core 预设里混进了 {pid}"
    assert rep["python"] == [], f"core 预设不该装任何 extras，实际 {rep['python']}"


def test_light_preset_is_the_cheapest_real_option():
    """轻量预设必须真的"轻"：不含 demucs / qwen-tts / modelscope 这些重包。"""
    pkgs = {p.lower() for p in pe.resolve(preset="light")["python"]}
    assert pkgs == {"torch", "torchaudio", "librosa", "deepfilter-stream"}, pkgs


def test_standard_preset_is_the_default():
    assert pe.DEFAULT_PRESET == "standard"
    assert pe.resolve()["preset"] == "standard"


def test_unknown_preset_raises():
    """写错预设名要**大声报**，不能静默退化成"什么都不装"。"""
    with pytest.raises(KeyError, match="未知预设"):
        pe.resolve(preset="standrad")


# ------------------------------------------------------------------ requires 闭包


def test_requires_closure_pulls_in_dependencies():
    """只启用试音间，也必须启用它依赖的离线变声（否则起来就是 broken）。"""
    rep = pe.resolve(only=["sound.audition"])
    assert "sound.offline-vc" in rep["enabled"]
    assert "sound.audition" in rep["enabled"]


def test_requires_closure_adds_extras_of_dependencies():
    """依赖被拉进来后，它的 extras 也要跟着进安装清单。"""
    only_audition = {p.lower() for p in pe.resolve(only=["sound.audition"])["python"]}
    assert "deepfilter-stream" in only_audition, "离线变声的降噪包没跟进来"


def test_broken_requires_reference_raises():
    """清单里写了不存在的依赖 id → 抛错（静默忽略会让安装清单悄悄少东西）。"""
    pm = pe._manifest()
    real = pm.load_all()
    broken = [p for p in real if p.id == "sound.audition"]
    assert broken, "前置条件变了：sound.audition 不在清单里"
    bad = type(real[0])(
        **{**broken[0].__dict__, "requires": ("sound.nope",)}
    )
    patched = [bad if p.id == "sound.audition" else p for p in real]
    original = pm.load_all
    pm.load_all = lambda: patched
    try:
        with pytest.raises(KeyError, match="sound.nope"):
            pe.resolve(only=["sound.audition"])
    finally:
        pm.load_all = original


# ------------------------------------------------------------------ CUDA 分流


def test_torch_goes_into_the_cuda_bucket():
    """torch / torchaudio 必须单独拎出来走 CUDA 索引。

    混进普通 `pip install` 就是"CPU 版 torch" —— 能跑、不用显卡、不报错，
    是本项目踩过的那类静默失效。
    """
    rep = pe.resolve(preset="full")
    assert set(rep["python_cuda"]) == {"torch", "torchaudio"}
    assert not {"torch", "torchaudio"} & set(rep["python_pip"])
    assert set(rep["python"]) == set(rep["python_cuda"]) | set(rep["python_pip"])


def test_cuda_index_url_template_is_wellformed():
    url = pe.CUDA_INDEX_URL.format(tag="cu128")
    assert url.startswith("https://download.pytorch.org/whl/")
    assert url.endswith("cu128")


# ------------------------------------------------------------------ 用户关掉的


def test_disabled_plugin_drops_its_extras(monkeypatch):
    """关掉「实时变声」→ 不再装 deepfilter-stream。这就是"省几个 GB"的落点。

    挑 `sound.rvc-live` 是因为**没人依赖它**（`sound.audiobook` → `sound.tts`、
    `sound.ft`/`sound.mine` → `sound.workshop` 那种关系不存在）—— 否则会走到
    下面那条"被依赖则保留"的分支，测不到"真去掉"。
    """
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: {"sound.rvc-live"})
    rep = pe.resolve(preset="full")
    assert "sound.rvc-live" not in rep["enabled"]
    assert "deepfilter-stream" in rep["python"], "离线变声也声明了它，故仍在清单里"
    assert "sound.rvc-live" in rep["disabled_by_user"]


def test_disabled_drops_a_package_no_one_else_needs(monkeypatch):
    """关掉「音效器」→ librosa 一起走（离线变声也声明了它，故这里用 effects 的独有包验证口径）。"""
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: {"sound.effects"})
    rep = pe.resolve(preset="full")
    assert "sound.effects" not in rep["enabled"]
    assert "sound.effects" in rep["disabled_by_user"]


def test_disabled_but_depended_on_is_kept(monkeypatch):
    """被别的**启用**插件依赖的能力，即使被关也要保留 —— 否则依赖方会缺件。

    这是刻意的取舍：宁可多装一个包，也不要让"关掉 A"把"还在用的 B"弄坏。
    真实例子：`sound.audiobook`（有声书）依赖 `sound.tts`，所以关掉 TTS 后
    qwen-tts 仍然会被装上 —— 报告里的 `kept_despite_disabled` 就是给用户解释这件事的。
    """
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: {"sound.tts"})
    rep = pe.resolve(preset="full")
    assert "sound.tts" in rep["enabled"]
    assert "sound.tts" in rep["kept_despite_disabled"]
    assert "sound.tts" not in rep["disabled_by_user"]


def test_disabled_but_not_enabled_at_all_is_ignored(monkeypatch):
    """关掉一个**本来就没在启用集里**的能力 → 不该出现在任何报告字段里。"""
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: {"sound.rvc-live"})
    rep = pe.resolve(preset="light")
    assert "sound.rvc-live" not in rep["enabled"]
    assert "sound.rvc-live" not in rep["disabled_by_user"]
    assert "sound.rvc-live" not in rep["kept_despite_disabled"]


def test_disabled_ids_are_reported(monkeypatch):
    pm = pe._manifest()
    monkeypatch.setattr(pm, "disabled_ids", lambda: {"sound.rvc-live"})
    rep = pe.resolve(preset="full")
    assert rep["disabled_by_user"] == ["sound.rvc-live"]


# ------------------------------------------------------------------ 报告结构


def test_external_entries_are_deduplicated():
    """三个插件都写了 `VM_RVC_ROOT` → 报告里只该出现一次（否则像要装三个整合包）。"""
    rep = pe.resolve(preset="full")
    envs = [e.get("env") for e in rep["external"] if e.get("env")]
    assert len(envs) == len(set(envs)), envs
    assert "VM_RVC_ROOT" in envs


def test_by_plugin_maps_packages_to_owners():
    """逐插件归属要能对上 —— `doctor.py` 靠它说"缺的这个包是谁要的"。"""
    rep = pe.resolve(preset="full")
    assert rep["by_plugin"]["sound.tts"]["python"] == ["qwen-tts"]
    assert rep["by_plugin"]["sound.workshop"]["python"] == [
        "demucs", "torch", "torchaudio", "modelscope", "hdbscan",
    ]
    owners = {pkg for v in rep["by_plugin"].values() for pkg in v["python"]}
    assert owners == set(rep["python"])


def test_import_name_mapping_for_check():
    """`check_installed` 要用 import 名探测：`Pillow` 的模块名是 `PIL`。"""
    from audit_plugin_deps import import_name_of

    assert import_name_of("Pillow") == "pil"
    assert import_name_of("deepfilter-stream") == "deepfilter_stream"
    assert import_name_of("qwen-tts") == "qwen_tts"


def test_check_installed_reports_missing_without_raising(tmp_path):
    """对账一个**不存在**的包要返回 False，而不是抛异常（doctor 不该被它拖垮）。"""
    got = pe.check_installed(sys.executable, ["definitely_not_a_real_package_xyz"])
    assert got == {"definitely_not_a_real_package_xyz": False}
    assert pe.check_installed(sys.executable, []) == {}

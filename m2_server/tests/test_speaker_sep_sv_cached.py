# -*- coding: utf-8 -*-
"""`speaker_sep.sv_cached()` 单测（2026-10-11）。

★ 守的是「**没缓存就如实说没有**」：这个判定决定随访 / 质检**要不要真的去算声纹** ——
算之前会 `pipeline(...)` → **联网下载 ~30MB 权重**。写错只有两个方向，每个都要有对应用例：
  误报「有」→ 断网机上卡住主流程（或在用户请求里悄悄联网）；
  误报「没有」→ 永远降级成「未测」（未测≠不合格，但独立声纹就白接了）。
用例全部指向临时目录，**不依赖本机是否真的缓存过**（否则换台机器就红）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "m2_server",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import speaker_sep  # noqa: E402


def _cache_model(root: Path, name: str) -> None:
    (root / "models" / name).mkdir(parents=True, exist_ok=True)


def test_finds_the_model_under_the_cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path))
    _cache_model(tmp_path, "damo--speech_campplus_sv_zh-cn_16k-common")
    assert speaker_sep.sv_cached() is True


def test_other_models_cached_do_not_count_as_the_sv_model(tmp_path, monkeypatch):
    """目录名前缀相同但**不是** SV 权重的（同厂的 paraformer / asr）不能算命中。"""
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path))
    _cache_model(tmp_path, "damo--speech_paraformer-large_asr_nat-zh")
    assert speaker_sep.sv_cached() is False


def test_missing_cache_root_is_false_not_an_error(tmp_path, monkeypatch):
    """没下过任何 modelscope 模型的机器（`models/` 都不存在）→ False，不许抛。"""
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "nope"))
    assert speaker_sep.sv_cached() is False


def test_default_root_is_home_cache_when_the_env_var_is_unset(tmp_path, monkeypatch):
    """没设 `MODELSCOPE_CACHE` 时回落到 `~/.cache/modelscope`（Windows 上即 USERPROFILE）。"""
    monkeypatch.delenv("MODELSCOPE_CACHE", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    _cache_model(tmp_path / ".cache" / "modelscope",
                 "damo--speech_campplus_sv_zh-cn_16k-common")
    assert speaker_sep.sv_cached() is True

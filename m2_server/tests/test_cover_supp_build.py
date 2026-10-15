# -*- coding: utf-8 -*-
"""`experiments/cover_supp_build.py`（2026-10 补录混训集构建）的守卫。

这批数据要喂训练，构建逻辑错了会**静默污染训练集**，而且训完根本看不出来：

1. **旧素材必须逐字节不变** —— 本轮的单变量是"新增 4 条宽频录音"，旧的被改一个
   样本，A/B 就多一个说不清的变量（t03 还对旧素材做过预加重，更不能混）。
2. **新旧 stem 撞名必须拒绝** —— 否则解码出的新文件会静默覆盖一条旧素材。
3. **安全门**：构建会 rmtree 目标目录，只允许重建 `myvoice_supp*`，手滑传错路径
   不能真删别的数据集。
4. 新素材解码产物必须是 40k/单声道/有内容（训练预处理器按 40k 切，SR 错了全链 silently 漂）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"
for _p in (_ROOT / "m2_server", _EXP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load():
    spec = importlib.util.spec_from_file_location("cover_supp_build", _EXP / "cover_supp_build.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_supp_build"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def b():
    return _load()


def _wav(path: Path, sr: int = 40000, secs: float = 0.5, ch: int = 1, amp: float = 0.3):
    n = int(sr * secs)
    t = np.arange(n) / sr
    mono = (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    x = np.stack([mono] * ch, axis=1) if ch == 2 else mono
    sf.write(str(path), x, sr, subtype="PCM_16")
    return path


def _layout(tmp: Path):
    old = tmp / "old"
    new = tmp / "new"
    old.mkdir()
    (new / "speak").mkdir(parents=True)
    (new / "sing").mkdir(parents=True)
    _wav(old / "ppt12_0001.wav")
    _wav(old / "ppt12_0002.wav")
    for rel in ("speak/词语.m4a", "speak/句子.m4a", "sing/长音和滑音.m4a", "sing/唱歌.m4a"):
        (new / rel).write_bytes(b"fake-m4a")
    items = [
        ("speak/词语.m4a", "supp2026_words"),
        ("speak/句子.m4a", "supp2026_sentences"),
        ("sing/长音和滑音.m4a", "supp2026_longglide"),
        ("sing/唱歌.m4a", "supp2026_sing"),
    ]
    return old, new, items


# ------------------------------------------------------------- 纯函数

def test_stem_collision_between_old_and_new_is_rejected(b):
    with pytest.raises(ValueError, match="冲突"):
        b.check_stem_collisions(["ppt12_0001", "x"], ["ppt12_0001"])


def test_stem_collision_within_new_items_is_rejected(b):
    with pytest.raises(ValueError, match="重复"):
        b.check_stem_collisions(["a"], ["supp_x", "supp_x"])


def test_stem_collision_passes_when_disjoint(b):
    b.check_stem_collisions(["a", "b"], ["c", "d"])  # 不抛即可


@pytest.mark.parametrize("kw,match", [
    (dict(sr=22050), "采样率"),
    (dict(ch=2), "单声道"),
    (dict(secs=0.01), "时长"),
])
def test_verify_wav_rejects_bad_shapes(b, tmp_path, kw, match):
    p = _wav(tmp_path / "x.wav", **kw)
    with pytest.raises(ValueError, match=match):
        b.verify_wav(p)


def test_verify_wav_returns_duration(b, tmp_path):
    p = _wav(tmp_path / "x.wav", secs=1.0)
    assert b.verify_wav(p) == pytest.approx(1.0, abs=0.02)


# ----------------------------------------------------------------- build

def _fake_decode_factory(calls):
    def fake_decode(src: Path, dst: Path):
        calls.append(src.name)
        _wav(dst, secs=0.4)  # 假解码器：造一条合法的 40k mono wav
    return fake_decode


def test_build_copies_old_byte_identical_and_adds_new(b, tmp_path):
    old, new, items = _layout(tmp_path)
    dst = tmp_path / "myvoice_supp_test_40k"
    calls: list[str] = []

    man = b.build(old, new, dst, items, decode=_fake_decode_factory(calls))

    # 旧素材逐字节一致
    for name in ("ppt12_0001.wav", "ppt12_0002.wav"):
        assert (old / name).read_bytes() == (dst / name).read_bytes()
    # 4 条新素材都解了码、落成规定名字
    assert len(calls) == 4
    for _rel, stem in items:
        assert (dst / f"{stem}.wav").exists()
    assert man["old_copied_byte_identical"] == 2
    assert man["files_total"] == 6
    assert man["sr"] == 40000
    # manifest 在数据集目录**外**（放进去会被 train.preprocess 当成第 86 个"音频"）
    assert not (dst / "manifest.json").exists()
    meta = json.loads((tmp_path / "myvoice_supp_test_40k_manifest.json").read_text(encoding="utf-8"))
    assert meta["files_total"] == 6 and len(meta["new"]) == 4


def test_build_safety_gate_blocks_unprefixed_target(b, tmp_path):
    old, new, items = _layout(tmp_path)
    sentinel = tmp_path / "other_dataset_40k"
    sentinel.mkdir()
    (sentinel / "important.wav").write_bytes(b"DO-NOT-DELETE")
    with pytest.raises(ValueError, match="安全门"):
        b.build(old, new, sentinel, items, decode=lambda s, d: None)
    assert (sentinel / "important.wav").read_bytes() == b"DO-NOT-DELETE"


def test_build_rejects_missing_old_and_new(b, tmp_path):
    old, new, items = _layout(tmp_path)
    with pytest.raises(FileNotFoundError):
        b.build(old, new / "nope", tmp_path / "myvoice_supp_x_40k", items)
    with pytest.raises(FileNotFoundError):
        b.build(tmp_path / "no-old", new, tmp_path / "myvoice_supp_x_40k", items)


def test_build_collision_aborts_before_wiping_existing_dst(b, tmp_path):
    old, new, items = _layout(tmp_path)
    # 让新旧撞名
    items_bad = [("speak/词语.m4a", "ppt12_0001")]
    (new / "speak" / "词语.m4a").write_bytes(b"x")
    dst = tmp_path / "myvoice_supp_keep_40k"
    dst.mkdir()
    (dst / "sentinel.txt").write_text("keep")
    with pytest.raises(ValueError, match="冲突"):
        b.build(old, new, dst, items_bad, decode=lambda s, d: None)
    # 撞名在 rmtree 之前检出：已有目录不许被清空
    assert (dst / "sentinel.txt").exists()


def test_build_is_rebuildable(b, tmp_path):
    old, new, items = _layout(tmp_path)
    dst = tmp_path / "myvoice_supp_rebuild_40k"
    dec = _fake_decode_factory([])
    b.build(old, new, dst, items, decode=dec)
    man2 = b.build(old, new, dst, items, decode=dec)  # 再来一遍不报错、不残留
    assert man2["files_total"] == 6
    assert len(list(dst.glob("*.wav"))) == 6

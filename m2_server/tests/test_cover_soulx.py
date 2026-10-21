"""`experiments/cover_soulx.py`（SoulX-Singer-SVC 六臂）的守卫。

SoulX 这一轮的危险点和三臂页不同：**六条臂里有三条只差 cfg/步数**，
文件名相近（`..._cfg2_s32` / `..._cfg3_s32` / `..._cfg4_s48`），
一旦映射或路径写串，听的人会把"同一份东西"听成两个答案。
这里守三件事：

1. 六条臂 key 唯一，窗口都在真唱时长以内，cfg/步数合法；
2. `soulx.json` 的 rows 不带 path / key，映射不重不漏；
3. 页面只有甲乙丙丁戊己六条，不出现模型名与参数。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_soulx.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def soulx():
    return _mod("cover_soulx_under_test", SCRIPT)


@pytest.fixture()
def rows():
    return [
        {"key": f"arm_{i}", "path": f"/tmp/{i}.wav", "f0": 186.1,
         "hf_db": -82.4, "cam_anchor": 0.70 + i * 0.01, "cam_speech_ref": 0.2}
        for i in range(6)
    ]


def test_six_arms_are_valid(soulx):
    """六条直转臂 + 两条串联 = 八条；窗口不越过 73.7s；cfg/步数为正。"""
    keys = [k for k, _w, _a, _c, _s in soulx.ARMS]
    assert len(keys) == 6 and len(set(keys)) == 6
    assert len(soulx.ARMS) + len(soulx.CASCADES) == 8
    assert len(soulx.ARMS) + len(soulx.CASCADES) <= len(soulx.AB.LABELS)
    for key, (start, dur), auto, cfg, steps in soulx.ARMS:
        assert start >= 0 and dur > 0 and start + dur <= 73.7, f"{key} 窗口越界"
        assert cfg > 0 and steps > 0
        assert isinstance(auto, bool)


def test_key_keeps_map_only(soulx, rows):
    """rows 里不许留 path/key；map 必须一一对应；notes 只进 json 不进页面。"""
    key = soulx.build_key(rows, list(range(6)))
    assert len(key["map"]) == 6 == len(set(key["map"].values()))
    assert set(key["map"]) == set(soulx.AB.LABELS[:6])
    for row in key["rows"].values():
        assert "path" not in row and "key" not in row
    assert len(key["notes"]) == len(soulx.ARMS) + len(soulx.CASCADES)
    assert key["mapfile"] == "answer.json"


def test_page_hides_names_and_params(soulx, rows, tmp_path, monkeypatch):
    """页面只给甲乙丙丁戊己，不泄露 SoulX / cfg / 窗口 / 文件名。"""
    monkeypatch.setattr(soulx.AB, "_encode", lambda *a, **k: None)
    anchor = tmp_path / "anchor.wav"
    anchor.write_bytes(b"")
    key = soulx.build_key(rows, list(range(6)))
    html = soulx.AB.build_page(rows, anchor, key, tmp_path).read_text(encoding="utf-8")
    for bad in ("soulx", "SoulX", "cfg", "ref37", "noshift", ".wav", "F5"):
        assert bad not in html, f"页面泄露了 {bad!r}"
    assert "6 条盲听" in html and "clips/6.mp3" in html

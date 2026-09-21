"""normalize_market_imgs：市场配图留白归一化工具。

背景（2026-09-21 用户截图报障）：30 张市场配图都是方形画布，但图形占画布比例
从 53% 到 94% 不等；前端在 48px 圆角盒（`rounded-xl`，半径 25%）里 `object-cover`
满铺 → 占比大的"顶到圆角边"，占比小的显得小一圈。本工具把带透明通道的图统一到
"内容最长边 = 画布 × 0.84"。

本文件锁三件事：
  1. 变换本身的正确性（bbox / 缩放 / 居中 / 幂等）
  2. **真实素材的门禁** —— 以后往 assets 塞一张没归一化的图，这里必须红
  3. 门禁**真的咬得住**（变异测试：注入真违规，断言点名）
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools" / "normalize_market_imgs.py"
_ASSETS = _ROOT / "m2_server" / "assets" / "market_imgs"


def _load_tool():
    """按路径加载 tools/normalize_market_imgs.py（tools 不是包，不能直接 import）。"""
    if not _TOOL.exists():
        pytest.skip(f"未找到 {_TOOL}")
    spec = importlib.util.spec_from_file_location("normalize_market_imgs", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["normalize_market_imgs"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _square_with_content(canvas: int, ratio: float) -> Image.Image:
    """造一张 canvas×canvas 的透明画布，中心放一个边长 = canvas×ratio 的不透明方块。"""
    im = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    side = round(canvas * ratio)
    off = (canvas - side) // 2
    ImageDraw.Draw(im).rectangle((off, off, off + side - 1, off + side - 1), fill=(220, 30, 30, 255))
    return im


# ---------------------------------------------------------------- 基本判定


def test_has_transparency(tool):
    """不透明满幅图（alpha 恒 255）必须判为"无透明"，否则会被误归一化。"""
    assert tool.has_transparency(_square_with_content(64, 0.5)) is True
    opaque = Image.new("RGB", (64, 64), (10, 20, 30))
    assert tool.has_transparency(opaque) is False
    # 调色板 + transparency 也是真透明（本库 25 张主力形态）
    pal = Image.new("P", (64, 64), 0)
    pal.putpalette([10, 20, 30] + [0, 0, 0] * 255)
    pal.info["transparency"] = 0
    assert tool.has_transparency(pal) is True


def test_content_bbox_exact_and_none(tool):
    im = _square_with_content(100, 0.5)  # 50×50 居中 → (25,25,75,75)
    assert tool.content_bbox(im) == (25, 25, 75, 75)
    assert tool.content_bbox(Image.new("RGBA", (32, 32), (0, 0, 0, 0))) is None


def test_normalize_lands_on_target_and_centered(tool):
    """最长边 = 画布×target，且居中（左右/上下留白相等）。"""
    im = _square_with_content(200, 0.5)
    out, meta = tool.normalize_image(im, target=0.8)
    bb = tool.content_bbox(out)
    assert max(bb[2] - bb[0], bb[3] - bb[1]) == 160  # 200×0.8
    assert bb[0] == 200 - bb[2]  # 水平居中
    assert bb[1] == 200 - bb[3]  # 垂直居中
    assert meta["resized"] is True


def test_normalize_is_idempotent(tool):
    """跑两遍必须字节不变 —— 否则每次同步都会把图越缩越小。"""
    first, _ = tool.normalize_image(_square_with_content(300, 0.9), target=0.84)
    second, meta = tool.normalize_image(first, target=0.84)
    assert tool.encode_png(first) == tool.encode_png(second)
    assert meta["resized"] is False  # 已在目标附近：只居中，不重采样


def test_normalize_near_target_does_not_resample(tool):
    """已在目标 ±容差内：不得重采样（避免抗锯齿把 bbox 撑大后反复微缩）。"""
    im = _square_with_content(250, 0.845)  # 距 0.84 仅 0.005
    out, meta = tool.normalize_image(im, target=0.84)
    assert meta["resized"] is False
    assert tool.content_bbox(out)[2] - tool.content_bbox(out)[0] == round(250 * 0.845)


def test_opaque_full_bleed_is_left_alone(tool, tmp_path):
    """不透明满幅图不参与归一化：跑一遍后字节不变（本库 5 张 512×512 属于此类）。"""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "opaque.png"
    Image.new("RGB", (128, 128), (200, 100, 50)).save(f)
    before = f.read_bytes()
    assert tool.run(src, None, 0.84, tool.DEFAULT_ALPHA_THRESHOLD, False, None) == 0
    assert f.read_bytes() == before


def test_audit_ignores_opaque_and_flags_unnormalized(tool, tmp_path):
    """audit：不透明满幅图不报，未归一化的透明图报。"""
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (128, 128), (200, 100, 50)).save(src / "opaque.png")
    _square_with_content(200, 0.5).save(src / "small.png")
    _square_with_content(200, 0.84).save(src / "good.png")
    names = [p["name"] for p in tool.audit_dir(src)]
    assert names == ["small"]


def test_already_normalized_tolerates_one_pixel_bleed(tool):
    """★ 已合规 + 居中偏差 1px → 必须仍判为"已合规"（不重采样）。

    **这是 `_CENTER_TOLERANCE_PX` 的变异测试守卫**：把该常量改回 0 本用例会红。
    （文件级的 `test_second_run_writes_nothing` 做不到这一点 —— 它注入的是硬边合成
    方块，量化后边缘干净、正好落在目标尺寸上，走不到"偏差 1px"那条路。真实素材里
    只有 `diyin.png` 会：量化把最外一列 alpha 压到阈值以下，bbox 从 519 缩到 518。）

    为什么必须有这条：LANCZOS 抗锯齿 + PNG 量化会让归一化过的图 bbox 偏 1~2px。
    若用精确相等判定"已居中"，归一化过的图永远判不出"已合规"→ 每跑一遍重采样一次、
    体积逐次漂移（实测 280,047 → 278,531 → …）。
    """
    im = tool.normalize_image(_square_with_content(618, 0.9), target=0.84)[0]
    bb = tool.content_bbox(im)
    shifted = Image.new("RGBA", im.size, (0, 0, 0, 0))
    shifted.paste(im.crop(bb), (bb[0] + 1, bb[1]))  # 人为右移 1px

    out, meta = tool.normalize_image(shifted, target=0.84)
    assert meta["action"] == "skip"
    assert meta["reason"] == "already-normalized"
    assert tool.encode_png(out) == tool.encode_png(shifted)  # 原样返回，未重采样


def test_second_run_writes_nothing(tool, tmp_path):
    """★ 跑第二遍必须**一个文件都不写**（字节零变化）—— 文件级集成守卫。

    覆盖"第一遍真写 → 第二遍全判为已合规"这条端到端路径。
    注意：本用例单独**不足以**守住居中容差（注入的是硬边方块，见
    `test_already_normalized_tolerates_one_pixel_bleed` 的说明），两条一起才有意义。
    """
    dst = tmp_path / "assets"
    dst.mkdir()
    for f in _ASSETS.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    _square_with_content(618, 0.5).save(dst / "diyin.png")  # 人为弄坏一张，确保第一遍真写

    assert tool.run(dst, None, 0.84, tool.DEFAULT_ALPHA_THRESHOLD, False, None) == 0
    first = {f.name: f.read_bytes() for f in dst.iterdir() if f.is_file()}
    assert tool.audit_dir(dst) == []  # 第一遍后应全部合规

    assert tool.run(dst, None, 0.84, tool.DEFAULT_ALPHA_THRESHOLD, False, None) == 0
    second = {f.name: f.read_bytes() for f in dst.iterdir() if f.is_file()}
    assert second == first, "第二遍改动了文件：" + repr(
        sorted(k for k in first if first[k] != second.get(k))
    )


# ---------------------------------------------------------------- 真实素材门禁


@pytest.mark.skipif(not _ASSETS.is_dir(), reason="未找到市场配图目录")
def test_real_assets_are_all_normalized(tool):
    """门禁：assets 里每张带透明通道的配图，内容最长边占比都要落在 target ± 容差内。

    红 → 说明有人塞了没归一化的图（或改了 target 却没重跑工具），
    修：`python tools/normalize_market_imgs.py` 然后重推远程图库。
    """
    problems = tool.audit_dir(_ASSETS)
    assert problems == [], (
        "以下配图留白不合规，请跑 tools/normalize_market_imgs.py："
        + repr([(p["name"], p.get("ratio")) for p in problems])
    )


def test_gate_bites_when_one_image_regresses(tool, tmp_path):
    """★ 变异测试：把一张真素材换成未归一化的，门禁必须**点名它**（且只点名它）。

    没有这条，`test_real_assets_are_all_normalized` 全绿也不能说明它有效 ——
    判据可能根本不会红（本项目铁律：守护测试必须做变异测试）。
    """
    dst = tmp_path / "assets"
    dst.mkdir()
    for f in _ASSETS.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    assert tool.audit_dir(dst) == []  # 前置：基线是绿的

    _square_with_content(618, 0.95).save(dst / "nv_yuner.png")  # 注入真违规

    problems = tool.audit_dir(dst)
    assert [p["name"] for p in problems] == ["nv_yuner"]
    assert problems[0]["reason"] == "ratio-out-of-range"
    # 用 approx：618×0.95 取整成 587 边，587/618 = 0.94984，不等于字面 0.95
    assert problems[0]["ratio"] == pytest.approx(0.95, abs=0.005)

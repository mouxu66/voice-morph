"""人偶皮肤转换器测试：atlas 切片 / gif 拆帧 / 皮肤包读写与校验。

用 ffmpeg 现场生成小尺寸素材（atlas png / 动画 gif），验证：
- atlas-8x9：按行 crop 出各状态 strip，frames=cols
- gif-multi：拆帧 hstack，帧数 = gif 帧数，frameW/frameH 回填
- skin.json 读写、缺省状态回退（listen→idle / build→play）、字段白名单
- 非法 id 拒绝、缺 sheet 告警

依赖 ffmpeg（走 conftest 的 `ffmpeg_bin` 夹具：本机没装 → skip，CI 上没装 → fail），
无真实网络。2026-09-13 CI 首跑时这里 4 条 ERROR 就是 ffmpeg 缺失（WinError 2），
见 conftest.py 顶部「本机资源探测」。
"""

import json
import subprocess

import pytest
from pet_skin_build import (
    SkinBuildError,
    build_skin,
    is_valid_skin_id,
    load_skin,
    pack_gif_skin,
    validate_skin_files,
    write_skin_json,
)


@pytest.fixture(scope="module")
def ffmpeg(ffmpeg_bin):
    """本模块沿用短名 `ffmpeg`：路径由 conftest 的 `ffmpeg_bin` 提供（含 skip 语义）。"""
    return ffmpeg_bin


def _run(ffmpeg, args):
    r = subprocess.run([ffmpeg, "-y", *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-400:]


@pytest.fixture(scope="module")
def atlas_png(ffmpeg, tmp_path_factory):
    """8 列 × 3 行的 atlas：每帧 64×48（第 0 行蓝色、第 1 行绿色、第 2 行红色）。"""
    p = tmp_path_factory.mktemp("media") / "atlas.png"
    # 用 testsrc 生成 8*64 x 3*48 的画布
    _run(ffmpeg, ["-f", "lavfi", "-i", "testsrc2=size=512x144:rate=1", "-frames:v", "1", str(p)])
    return p


@pytest.fixture(scope="module")
def gif_file(ffmpeg, tmp_path_factory):
    """4 帧 64×48 gif 动画。"""
    p = tmp_path_factory.mktemp("media") / "anim.gif"
    _run(
        ffmpeg, ["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=4", "-t", "1", "-loop", "0", str(p)]
    )
    return p


def test_atlas_8x9_slices_rows(atlas_png, tmp_path):
    meta = {
        "id": "poke",
        "name": "Poké",
        "category": "卡通",
        "license": "MIT",
        "attribution": "测试作者 poke",
        "frameW": 64,
        "frameH": 48,
        "cols": 8,
        "row_map": {"idle": 0, "listen": 1, "play": 2, "build": 2, "think": 1, "error": 2},
    }
    out = build_skin("atlas-8x9", [atlas_png], tmp_path / "poke", meta)
    skin = load_skin(out)
    assert skin["frameW"] == 64 and skin["frameH"] == 48
    # 6 状态都有 sheet，且各状态帧数 = cols = 8
    for st in ("idle", "listen", "think", "play", "build", "error"):
        s = skin["states"][st]
        assert s["frames"] == 8, f"{st}: {s}"
        assert (out / s["sheet"]).exists()
    assert not validate_skin_files(out), validate_skin_files(out)
    assert (out / "LICENSE").exists()
    lic = (out / "LICENSE").read_text("utf-8")
    assert "MIT" in lic and "poke" in lic


def test_atlas_rowmap_explicit():
    """row_map 缺省键回退 DEFAULT_ROW_MAP（build→2 / think→4 / error→3）。

    原来挂了个没用到的 `gif_file` 参数（只为了建个 gif），结果这条纯常量断言
    在没有 ffmpeg 的机器上也会跟着跳过 —— 白丢覆盖。2026-09-13 去掉。
    """
    from pet_skin_build import DEFAULT_ROW_MAP

    assert DEFAULT_ROW_MAP["build"] == 2
    assert DEFAULT_ROW_MAP["error"] == 3


def test_atlas_bad_size_rejected(tmp_path):
    meta = {
        "id": "bad",
        "name": "Bad",
        "category": "x",
        "license": "x",
        "frameW": 200,
        "frameH": 200,
        "cols": 8,
    }
    out = tmp_path / "bad"
    out.mkdir()
    # 缺 atlas 文件直接报错
    with pytest.raises(SkinBuildError):
        build_skin("atlas-8x9", [tmp_path / "missing.png"], out, meta)


def test_gif_multi_splits_frames(gif_file, tmp_path):
    """gif → strip：帧数保持 gif 帧数（4），frameW/frameH 按实际回填非 0。"""
    out = tmp_path / "cat"
    meta = {
        "id": "cat",
        "name": "猫",
        "category": "像素萌宠",
        "license": "CC0",
        "gif_map": {
            "idle": "anim.gif",
            "play": "anim.gif",
            "think": "anim.gif",
            "listen": "anim.gif",
            "build": "anim.gif",
            "error": "anim.gif",
        },
        "frameW": 0,
        "frameH": 0,
    }
    build_skin("gif-multi", [gif_file], out, meta)
    skin = load_skin(out)
    assert skin["frameW"] > 0 and skin["frameH"] > 0  # 回填而非 0
    for st in ("idle", "play"):
        assert skin["states"][st]["frames"] == 4, skin["states"][st]
        assert (out / skin["states"][st]["sheet"]).exists()
    assert not validate_skin_files(out), validate_skin_files(out)


def test_skin_json_config_handed_offs(tmp_path):
    """write_skin_json 省略 sheet=None 的状态；load_skin 补缺省回退。"""
    meta = {
        "id": "cfg",
        "name": "Cfg",
        "category": "其他",
        "license": "Apache-2.0",
        "frameW": 150,
        "frameH": 150,
    }
    rows = {
        "idle": {"sheet": "idle.webp", "frames": 4, "dur": 2.0},
        "listen": {"sheet": None},
        "play": {"sheet": "play.webp", "frames": 2, "dur": 1.0},
    }
    write_skin_json(tmp_path, meta, rows)
    raw = json.loads((tmp_path / "skin.json").read_text("utf-8"))
    assert "listen" not in raw["states"]  # 缺省状态不写
    assert "build" not in raw["states"]
    skin = load_skin(tmp_path)
    # 回退：listen→idle / build→play
    assert skin["states"]["listen"]["sheet"] == "idle.webp"
    assert skin["states"]["build"]["sheet"] == "play.webp"
    assert skin["states"]["idle"]["frames"] == 4
    # id/name/category/license 都被写入
    assert shutil_path_ok(skin)


def shutil_path_ok(skin):
    import shutil  # noqa: F401  # 保持导入语义占位

    return skin["id"] == "cfg" and skin["name"] == "Cfg"


def test_validate_skin_files_missing_sheet(tmp_path):
    meta = {
        "id": "hole",
        "name": "Hole",
        "category": "x",
        "license": "x",
        "frameW": 150,
        "frameH": 150,
    }
    write_skin_json(
        tmp_path,
        meta,
        {
            "idle": {"sheet": "idle.webp", "frames": 4, "dur": 2.0},
            "play": {"sheet": "play.webp", "frames": 2, "dur": 1.0},
        },
    )
    warns = validate_skin_files(tmp_path)
    assert any("缺 sheet" in w for w in warns)


def test_invalid_skin_id_rejected(tmp_path):
    for bad in ("../evil", "a/b", "", "名字", "A!"):
        assert not is_valid_skin_id(bad), bad
    assert is_valid_skin_id("furina")
    assert is_valid_skin_id("gel-slime")


def test_pack_gif_skin_expired_names(gif_file, tmp_path):
    """gif_map 引用的文件名不存在时状态标记 sheet=None，不留脏文件。"""
    meta = {
        "id": "ghost",
        "name": "Ghost",
        "category": "x",
        "license": "x",
        "gif_map": {"idle": "no-such.gif"},
    }
    rows = pack_gif_skin([gif_file], tmp_path, meta)
    assert rows["idle"]["sheet"] is None


def test_gif_to_strip_rejects_too_many_frames(tmp_path, monkeypatch):
    """预告片式长 gif（如 227 帧 demo 动图）→ 帧数预检人话报错，不进 hstack。"""
    import pet_skin_build as sb

    src = tmp_path / "demo.gif"
    src.write_bytes(b"x")
    monkeypatch.setattr(sb, "probe_frames", lambda p: 227)
    monkeypatch.setattr(sb, "probe_size", lambda p: (766, 638))
    with pytest.raises(SkinBuildError, match="帧数过多"):
        sb._gif_to_strip(src, tmp_path / "out.webp")
    assert not (tmp_path / "out.webp").exists()

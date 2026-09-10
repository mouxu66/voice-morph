"""人偶皮肤市场核心测试：清单 / 应用状态 / sheet 白名单 / 安装编排 / 卸载。

- 内置 bundle（furina）物化：从 assets/pet-skins 拷到 tmp 输出目录
- 远端皮肤安装：monkeypatch _download_to 直写文件（不碰真实网络），
  验证 gif 源 zip → 转换 → 物化 → preview 全链路
- applied/sheet 安全：非法 id / 非白名单 sheet 均拒绝
- 单任务互斥：安装进行中重复 install 报 409 语义错误

用例隔离：patch pet_market.PET_SKINS_DIR / STATE_FILE 到 tmp。
"""
import json
import threading
import time
import zipfile

import pytest

import pet_market
from pet_market import PetMarketError


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """把 pet_market 的输出目录/状态文件切到 tmp，避免碰真实 outputs。"""
    monkeypatch.setattr(pet_market, "PET_SKINS_DIR", tmp_path / "pet-skins")
    monkeypatch.setattr(pet_market, "STATE_FILE", tmp_path / "pet-skins" / "state.json")
    return pet_market


def test_manifest_has_licensed_items(iso):
    items = iso.get_manifest()
    assert len(items) >= 4
    ids = [i["id"] for i in items]
    assert "furina" in ids and "gel-slime" in ids and "mika" in ids and "pixel-cat" in ids
    for it in items:
        assert it["license"]                                  # 全部标注许可
        assert it["attribution"]                              # 全部有来源署名
        if not it["bundle"]:
            assert it["source_urls"], it["id"]                # 远端皮肤必须有下载源
            for u in it["source_urls"]:
                iso._validate_url(u)                          # 下载源必须过白名单


def test_apply_state_roundtrip(iso, tmp_path):
    assert iso.load_applied() == iso.DEFAULT_SKIN             # 默认 furina
    iso.save_applied("gel-slime")
    assert iso.load_applied() == "gel-slime"


def test_applied_skin_returns_config(iso):
    res = iso.applied_skin()
    assert res["id"] == iso.DEFAULT_SKIN
    assert res["frameW"] == 150 and res["frameH"] == 150
    assert "idle" in res["states"] and "error" in res["states"]
    # furina bundle 已在测试前物化（applied_skin 内 ensure_bundle 兜底）
    assert (iso.PET_SKINS_DIR / "furina" / "skin.json").exists()


def test_bundle_materializes_from_assets(iso):
    d = iso.ensure_bundle(iso.find_manifest_item("furina"))
    assert d.joinpath("skin.json").exists()
    assert d.joinpath("idle.webp").exists()
    assert d.joinpath("LICENSE").exists()


def test_apply_bundle_works(iso):
    iso.apply("furina")
    assert iso.load_applied() == "furina"


def test_sheet_file_whitelist(iso):
    """sheet 必须出现在该皮肤 skin.json 状态表内；他人皮肤/任意文件拒绝。"""
    iso.apply("furina")
    p = iso.sheet_file("furina", "idle.webp")
    assert p.name == "idle.webp"
    with pytest.raises(PetMarketError):
        iso.sheet_file("furina", "LICENSE")                   # 非状态 sheet
    with pytest.raises(PetMarketError):
        iso.sheet_file("furina", "../etc/passwd")
    with pytest.raises(PetMarketError):
        iso.sheet_file("burina", "idle.webp")                 # 未安装（不存在）


def test_invalid_skin_id_rejected(iso):
    with pytest.raises(PetMarketError):
        iso.install("../evil")
    with pytest.raises(PetMarketError):
        iso.apply("a/b")


def test_uninstall_bundle_only_resets(iso, tmp_path):
    """内置 bundle 不物理删除，仅复位应用态。"""
    iso.apply("furina")
    assert (iso.PET_SKINS_DIR / "furina" / "skin.json").exists()
    r = iso.uninstall("furina")
    assert r["reset_applied"] is True
    assert iso.load_applied() == iso.DEFAULT_SKIN
    assert (iso.PET_SKINS_DIR / "furina" / "skin.json").exists()   # 未删文件


# ---- 远端皮肤安装编排（monkeypatch 下载层） ----

def _fake_gif_zip(tmp_path) -> str:
    """造一个含单帧 gif 的 zip 源码包（模拟 OpenGameArt 猫素材 zip）。"""
    import subprocess
    from common import find_ffmpeg
    gif = tmp_path / "anim.gif"
    r = subprocess.run(
        [find_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=2",
         "-t", "1", "-loop", "0", str(gif)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-300:]
    zp = tmp_path / "cat.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(gif, "cat idle.gif")
        zf.write(gif, "cat walking.gif")
    return str(zp)


def test_install_remote_gif_skin(iso, tmp_path, monkeypatch):
    """pixel-cat（gif-multi + zip 源）安装：下载 → 解压 → 转换 → 校验 → preview。"""
    zip_path = _fake_gif_zip(tmp_path)

    def fake_download(url: str, dst, box):
        import shutil
        shutil.copyfile(url_loc(url), dst)
        box["bytes"] = dst.stat().st_size

    def url_loc(url: str):
        # 把白名单 URL 映到本地 zip（_download_to 被替换，不校验域名）
        return zip_path

    monkeypatch.setattr(iso, "_download_to", fake_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)   # 跳过 ffmpeg 预览

    st = iso.install("pixel-cat")
    assert st["status"] in ("downloading", "done")
    deadline = time.time() + 60
    while time.time() < deadline and iso.is_busy():
        time.sleep(0.05)
    st = iso.progress()
    assert st["status"] == "done", st
    d = iso.skin_dir("pixel-cat")
    skin = json.loads((d / "skin.json").read_text("utf-8"))
    assert skin["frameW"] > 0 and skin["frameH"] > 0
    assert "idle" in skin["states"]
    assert (d / skin["states"]["idle"]["sheet"]).exists()

    # 安装完成后可应用
    iso.apply("pixel-cat")
    assert iso.load_applied() == "pixel-cat"


def test_install_busy_rejects_second(iso, tmp_path, monkeypatch):
    """安装进行中时重复 install 抛忙错误（409 语义）。"""

    def _slow_download(url, dst, box):
        time.sleep(2.0)
        dst.write_bytes(b"x")

    monkeypatch.setattr(iso, "_download_to", _slow_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    st = iso.install("pixel-cat")
    assert st["status"] == "downloading"
    try:
        with pytest.raises(PetMarketError, match="进行中"):
            iso.install("mika")
    finally:
        deadline = time.time() + 10
        while time.time() < deadline and iso.is_busy():
            time.sleep(0.05)


def test_uninstall_remote_removes_dir(iso, tmp_path, monkeypatch):
    """远端皮肤卸载物理删除目录；应用态复位默认。"""
    test_install_remote_gif_skin(iso, tmp_path, monkeypatch)  # 复用安装流程
    assert (iso.PET_SKINS_DIR / "pixel-cat" / "skin.json").exists()
    r = iso.uninstall("pixel-cat")
    assert r["uninstalled"] == "pixel-cat"
    assert not (iso.PET_SKINS_DIR / "pixel-cat").exists()
    assert iso.load_applied() == iso.DEFAULT_SKIN


# ---- API 壳（路由存在性 + 错误映射 404/409/400） ----

@pytest.fixture()
def iso_api(monkeypatch, tmp_path):
    monkeypatch.setattr(pet_market, "PET_SKINS_DIR", tmp_path / "pet-skins")
    monkeypatch.setattr(pet_market, "STATE_FILE", tmp_path / "pet-skins" / "state.json")
    return pet_market


def test_api_manifest_and_applied(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app)
    r = c.get("/api/pet-market/manifest")
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 4
    r2 = c.get("/api/pet-market/applied")
    assert r2.status_code == 200
    assert r2.json()["id"]
    r3 = c.get("/api/pet-market/installed")
    assert r3.status_code == 200
    assert isinstance(r3.json()["items"], list)


def test_api_invalid_skin_id_400(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app)
    r = c.post("/api/pet-market/install", json={"skin_id": "../evil"})
    assert r.status_code == 400
    r2 = c.post("/api/pet-market/apply", json={"skin_id": "no-such"})
    assert r2.status_code == 404        # 清单无此皮肤


def test_api_bundle_apply_roundtrip(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app)
    r = c.post("/api/pet-market/apply", json={"skin_id": "furina"})
    assert r.status_code == 200
    assert r.json()["applied"] == "furina"
    r2 = c.get("/api/pet-market/sheet/furina/idle.webp")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/webp")
    r3 = c.get("/api/pet-market/sheet/furina/LICENSE")
    assert r3.status_code == 400        # 非状态 sheet → 业务错误映射 400
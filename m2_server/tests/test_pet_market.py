"""人偶皮肤市场核心测试：清单 / 应用状态 / sheet 白名单 / 安装队列 / 搜索详情 / 卸载。

- 内置 bundle（furina）物化：从 assets/pet-skins 拷到 tmp 输出目录
- 远端皮肤安装：monkeypatch _download_to 直写文件（不碰真实网络），
  验证 gif 源 zip → 转换 → 物化 → preview 全链路
- 多任务安装队列：同 id 重复入队 409；不同 id 排队并先后完成；可取消排队/进行中任务
- applied/sheet 安全：非法 id / 非白名单 sheet 均拒绝
- 搜索/详情：本地模糊搜索命中；详情含帧尺寸/状态表/许可全文

用例隔离：patch pet_market.PET_SKINS_DIR / STATE_FILE 到 tmp。
"""

import json
import time
import warnings
import zipfile

import pet_market
import pytest
from pet_market import PetMarketError


def _drain_install_queue(timeout: float = 10.0) -> None:
    """等 worker 收尾，然后把模块级安装队列状态**强制归零**（跨用例隔离用）。

    为什么必须显式复位（2026-09-18 实测）：`_TASKS` / `_QUEUE` / `_ALL_IDS` /
    `_RUNNING` 都是**模块全局**，`monkeypatch` 不会帮你复位。而 `_install_worker`
    是**先把任务标成终结态、再**在 `finally` 里 `_RUNNING -= 1`，中间还隔着一次
    `shutil.rmtree`。上一条用例用 `while is_busy()` 收尾时，`is_busy()` 只看任务状态、
    看不到 `_RUNNING`，于是循环一退出就进下一条用例 —— 此刻 `_RUNNING` 还是 1。
    下一条用例把 `MAX_CONCURRENT_INSTALLS` 设成 1，`_kick()` 的
    `_RUNNING < MAX_CONCURRENT_INSTALLS` 直接不成立 → worker 永不启动，任务永远停在
    `queued`。实测症状：`test_install_queue_two_then_both_done` 报
    `AssertionError: {'pixel-cat': 'queued', 'pixel-capybara': 'queued'}`，
    紧随其后的 `test_cancel_queued_task` 再报 `「pixel-cat」已在任务中` ——
    **一次失败被放大成一串**，而且单跑那两条用例都是绿的（只在同文件连跑时复现）。
    """
    deadline = time.time() + timeout
    # 只等 **worker 线程**收尾（`_RUNNING > 0`），不等任务终结 ——
    # 本文件有几条用例（如 `test_busy_counts_queued_task`）故意把任务留在
    # 排队/下载态来断言 `is_busy()`，等"全部终结"会白等满超时。
    # 残留的**任务记录**下面直接清掉即可，真正会跨用例作乱的是线程和 `_RUNNING`。
    while time.time() < deadline and pet_market._RUNNING > 0:
        time.sleep(0.02)
    if pet_market._RUNNING > 0:
        # 别静默吞掉：worker 卡住时下一条用例只会以"任务永远 queued"的形式失败，
        # 完全看不出真因。这里发一条 pytest 警告把 `_RUNNING` 的值和怀疑对象写出来。
        # 实测触发场景：沙箱/杀软的删除守卫把 worker `finally` 里的 `shutil.rmtree`
        # 卡住（`[safe-delete] SAFE_DELETE_BULK_CONFIRM_REQUIRED`），线程收不了尾。
        warnings.warn(
            f"pet_market 安装 worker 未在 {timeout}s 内收尾"
            f"（_RUNNING={pet_market._RUNNING}）。多半是上一条用例留下了一个卡住的"
            " worker 线程（例如 shutil.rmtree 被安全删除守卫拦住）。"
            "本 fixture 只能清任务表、**不能**改写 `_RUNNING`（强置 0 会被仍在跑的"
            " worker 收尾时压成负数，让 _kick() 一次放出超并发上限的 worker），"
            "所以下一条用例可能以「任务永远 queued」的形式失败。",
            stacklevel=2,
        )
    with pet_market._TASK_LOCK:
        pet_market._TASKS.clear()
        pet_market._QUEUE.clear()
        pet_market._ALL_IDS.clear()


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """把 pet_market 的输出目录/状态文件切到 tmp，避免碰真实 outputs。

    同时保证**两条用例之间安装队列是干净的**（见 `_drain_install_queue`）。
    """
    monkeypatch.setattr(pet_market, "PET_SKINS_DIR", tmp_path / "pet-skins")
    monkeypatch.setattr(pet_market, "STATE_FILE", tmp_path / "pet-skins" / "state.json")
    _drain_install_queue()
    yield pet_market
    _drain_install_queue()


def test_manifest_has_licensed_items(iso):
    items = iso.get_manifest()
    assert len(items) >= 4
    ids = [i["id"] for i in items]
    assert "furina" in ids and "gel-slime" in ids and "mika" in ids and "pixel-cat" in ids
    for it in items:
        assert it["license"]  # 全部标注许可
        assert it["attribution"]  # 全部有来源署名
        if not it["bundle"]:
            assert it["source_urls"], it["id"]  # 远端皮肤必须有下载源
            for u in it["source_urls"]:
                iso._validate_url(u)  # 下载源必须过白名单


def test_apply_state_roundtrip(iso, tmp_path):
    assert iso.load_applied() == iso.DEFAULT_SKIN  # 默认 furina
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
        iso.sheet_file("furina", "LICENSE")  # 非状态 sheet
    with pytest.raises(PetMarketError):
        iso.sheet_file("furina", "../etc/passwd")
    with pytest.raises(PetMarketError):
        iso.sheet_file("burina", "idle.webp")  # 未安装（不存在）


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
    assert (iso.PET_SKINS_DIR / "furina" / "skin.json").exists()  # 未删文件


# ---- 远端皮肤安装编排（monkeypatch 下载层） ----


def _fake_gif_zip(ffmpeg: str, tmp_path) -> str:
    """造一个含单帧 gif 的 zip 源码包（模拟 OpenGameArt 猫素材 zip）。

    `ffmpeg` 由 conftest 的 `ffmpeg_bin` 夹具传入：本机没装 ffmpeg → skip，
    CI 上没装 → fail（2026-09-13 这三条就是 WinError 2，见 conftest.py 顶部）。
    """
    import subprocess

    gif = tmp_path / "anim.gif"
    r = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=2",
            "-t",
            "1",
            "-loop",
            "0",
            str(gif),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-300:]
    zp = tmp_path / "cat.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(gif, "cat idle.gif")
        zf.write(gif, "cat walking.gif")
    return str(zp)


def test_install_remote_gif_skin(iso, tmp_path, monkeypatch, ffmpeg_bin):
    """pixel-cat（gif-multi + zip 源）安装：入队 → 下载 → 解压 → 转换 → 校验 → preview。"""
    zip_path = _fake_gif_zip(ffmpeg_bin, tmp_path)

    def fake_download(url: str, dst, box):
        import shutil

        shutil.copyfile(zip_path, dst)
        box["bytes"] = dst.stat().st_size

    monkeypatch.setattr(iso, "_download_to", fake_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)  # 跳过 ffmpeg 预览

    st = iso.install("pixel-cat")
    assert st["status"] in ("queued", "downloading", "done")
    deadline = time.time() + 60
    while time.time() < deadline and iso.is_busy():
        time.sleep(0.05)
    items = iso.progress()["items"]
    done = next(t for t in items if t["skin_id"] == "pixel-cat")
    assert done["status"] == "done", done
    d = iso.skin_dir("pixel-cat")
    skin = json.loads((d / "skin.json").read_text("utf-8"))
    assert skin["frameW"] > 0 and skin["frameH"] > 0
    assert "idle" in skin["states"]
    assert (d / skin["states"]["idle"]["sheet"]).exists()

    # 安装完成后可应用
    iso.apply("pixel-cat")
    assert iso.load_applied() == "pixel-cat"


def test_install_same_id_rejects(iso, tmp_path, monkeypatch):
    """同一皮肤已在队列/进行中时重复 install 抛「已在任务中」（409 语义）。"""

    def _slow_download(url, dst, box):
        time.sleep(2.0)
        dst.write_bytes(b"x")

    monkeypatch.setattr(iso, "_download_to", _slow_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    iso.install("pixel-cat")
    with pytest.raises(PetMarketError, match="任务中"):
        iso.install("pixel-cat")
    deadline = time.time() + 10
    while time.time() < deadline and iso.is_busy():
        time.sleep(0.05)


def test_install_queue_two_then_both_done(iso, tmp_path, monkeypatch, ffmpeg_bin):
    """两个不同皮肤先后入队：第一个下载时第二个排队，第一个完成后第二个自动接续，最终都完成。"""
    zip_path = _fake_gif_zip(ffmpeg_bin, tmp_path)
    pix = {
        "size": [4, 4],
        "palette": {"0": [255, 255, 255, 255], "1": [0, 0, 0, 255]},
        "frames": [
            {"name": "idle0", "pixels": [[0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1], [0, 1, 1, 0]]},
            {"name": "idle1", "pixels": [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]]},
        ],
    }

    def _fake(url, dst, box):
        import shutil

        if ".zip" in url:
            shutil.copyfile(zip_path, dst)
        else:
            dst.write_text(json.dumps(pix), "utf-8")
        box["bytes"] = dst.stat().st_size

    monkeypatch.setattr(iso, "_download_to", _fake)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    iso.MAX_CONCURRENT_INSTALLS = 1  # 单槽 → 第二个必然排队
    try:
        iso.install("pixel-cat")
        iso.install("pixel-capybara")
        # 轮询等 worker 真的进入下载/转换态。别用固定 `time.sleep(0.2)`：
        # 机器一忙（整套用例连跑、CI 上并发）这个窗口就不够，
        # 而失败形态是"两个都还 queued"——看不出是等得不够还是队列坏了。
        deadline = time.time() + 10
        items: dict = {}
        while time.time() < deadline:
            items = {t["skin_id"]: t["status"] for t in iso.progress()["items"]}
            if items.get("pixel-cat") in ("downloading", "installing"):
                break
            time.sleep(0.02)
        assert items["pixel-cat"] in ("downloading", "installing"), items
        assert items["pixel-capybara"] == "queued", items  # 第二个在排队
        deadline = time.time() + 60
        while time.time() < deadline and iso.is_busy():
            time.sleep(0.05)
        items = {t["skin_id"]: t["status"] for t in iso.progress()["items"]}
        assert items["pixel-cat"] == "done", items  # 先完成的在做
        assert items["pixel-capybara"] == "done", items  # 排队的自动接续
        assert (iso.PET_SKINS_DIR / "pixel-capybara" / "skin.json").exists()
    finally:
        iso.MAX_CONCURRENT_INSTALLS = 2


def test_cancel_queued_task(iso, tmp_path, monkeypatch):
    """排队中的任务可取消：出队并标记 cancelled，不触发 worker。"""

    def _slow_download(url, dst, box):
        time.sleep(3.0)
        dst.write_bytes(b"x")

    monkeypatch.setattr(iso, "_download_to", _slow_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    # 占满两个并发槽，让第三个任务真正排队
    iso.MAX_CONCURRENT_INSTALLS = 1
    try:
        iso.install("pixel-cat")
        time.sleep(0.1)  # 确保 worker 已进入下载
        iso.install("mika")  # 占用唯一并发槽 → 排队
        r = iso.cancel("mika")
        assert r["status"] == "cancelled"
        items = iso.progress()["items"]
        mika = next(t for t in items if t["skin_id"] == "mika")
        assert mika["status"] == "cancelled", mika
    finally:
        iso.MAX_CONCURRENT_INSTALLS = 2
        deadline = time.time() + 15
        while time.time() < deadline and iso.is_busy():
            time.sleep(0.05)


def test_cancel_active_task(iso, tmp_path, monkeypatch):
    """进行中的任务可取消：下载循环看到 cancel 标记 → 状态 cancelled。"""

    def _interruptible_download(url, dst, box):
        # 手动模拟下载循环检查 cancel 标记
        for _ in range(100):
            if box.get("cancel"):
                raise iso.PetMarketError("已取消")
            time.sleep(0.01)
        dst.write_bytes(b"x")

    monkeypatch.setattr(iso, "_download_to", _interruptible_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    iso.install("pixel-cat")
    time.sleep(0.05)
    iso.cancel("pixel-cat")
    deadline = time.time() + 15
    while time.time() < deadline and iso.is_busy():
        time.sleep(0.05)
    items = iso.progress()["items"]
    t = next(x for x in items if x["skin_id"] == "pixel-cat")
    assert t["status"] == "cancelled", t


def test_cancel_no_task_rejects(iso):
    with pytest.raises(PetMarketError, match="无此安装任务"):
        iso.cancel("nobody")


# ---- 搜索与详情 ----


def test_search_filters_manifest(iso):
    """按名称/描述/分类/作者/许可模糊命中；分类过滤生效；带 installed/applied 标记。"""
    iso.apply("furina")
    hits = iso.search("像素")
    assert hits and all(
        "像素" in (h.get("name") or "") or "像素" in (h.get("category") or "") for h in hits
    )
    by_lic = iso.search("MIT")
    assert all("MIT" in (h.get("license") or "").upper() for h in by_lic)
    cats = iso.search("", "像素萌宠")
    assert cats and all((h.get("category") or "") == "像素萌宠" for h in cats)
    furina = next(h for h in iso.search("水神"))
    assert furina["installed"] is True and furina["applied"] is True
    assert iso.search("zzz-no-match") == []


def test_detail_returns_license_and_states(iso):
    """已安装 bundle 详情：帧尺寸/状态表/许可全文齐全；未安装皮肤 states 为空。"""
    d = iso.detail("furina")
    assert d["installed"] is True
    assert d["frameW"] == 150 and d["frameH"] == 150
    assert "idle" in d["states"] and "error" in d["states"]
    assert "MIT" in d["license_text"] and d["source_urls"] == []
    d2 = iso.detail("gel-slime")  # 未安装远端皮肤
    assert d2["installed"] is False
    assert d2["states"] == {} and d2["license_text"] == ""
    assert d2["source_urls"]  # 有源链接
    with pytest.raises(PetMarketError, match="无此皮肤"):
        iso.detail("no-such")


def test_uninstall_remote_removes_dir(iso, tmp_path, monkeypatch, ffmpeg_bin):
    """远端皮肤卸载物理删除目录；应用态复位默认。"""
    test_install_remote_gif_skin(iso, tmp_path, monkeypatch, ffmpeg_bin)  # 复用安装流程
    assert (iso.PET_SKINS_DIR / "pixel-cat" / "skin.json").exists()
    r = iso.uninstall("pixel-cat")
    assert r["uninstalled"] == "pixel-cat"
    assert not (iso.PET_SKINS_DIR / "pixel-cat").exists()
    assert iso.load_applied() == iso.DEFAULT_SKIN


def test_busy_counts_queued_task(iso, tmp_path, monkeypatch):
    """刚入队（worker 还没进入 downloading）也算忙。

    回归：is_busy() 曾只看 _ACTIVE_STATUSES（不含 queued），导致
    ① 卸载在排队任务未跑完时被放行；② 测试轮询 `while is_busy()`
    会在安装开始前就退出 → 误判「安装未完成」。
    """

    def _slow_download(url, dst, box):
        time.sleep(1.5)
        dst.write_bytes(b"x")

    monkeypatch.setattr(iso, "_download_to", _slow_download)
    monkeypatch.setattr(iso, "_make_preview", lambda d: None)
    iso.MAX_CONCURRENT_INSTALLS = 1
    try:
        iso.install("pixel-cat")  # 占满唯一并发槽
        iso.install("mika")  # 排队中
        st = {t["skin_id"]: t["status"] for t in iso.progress()["items"]}
        assert st["mika"] == "queued", st
        assert iso.is_busy() is True, "排队中的任务也必须算忙"
        with pytest.raises(PetMarketError, match="任务"):
            iso.uninstall("mika")  # 排队未落地 → 拒绝卸载
    finally:
        iso.MAX_CONCURRENT_INSTALLS = 2
        deadline = time.time() + 15
        while time.time() < deadline and iso.is_busy():
            time.sleep(0.05)


# ---- API 壳（路由存在性 + 错误映射 404/409/400） ----


@pytest.fixture()
def iso_api(monkeypatch, tmp_path):
    monkeypatch.setattr(pet_market, "PET_SKINS_DIR", tmp_path / "pet-skins")
    monkeypatch.setattr(pet_market, "STATE_FILE", tmp_path / "pet-skins" / "state.json")
    return pet_market


def test_api_manifest_and_applied(iso_api):
    pytest.importorskip("fastapi")
    import server
    from fastapi.testclient import TestClient

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
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    r = c.post("/api/pet-market/install", json={"skin_id": "../evil"})
    assert r.status_code == 400
    r2 = c.post("/api/pet-market/apply", json={"skin_id": "no-such"})
    assert r2.status_code == 404  # 清单无此皮肤


def test_api_bundle_apply_roundtrip(iso_api):
    pytest.importorskip("fastapi")
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    r = c.post("/api/pet-market/apply", json={"skin_id": "furina"})
    assert r.status_code == 200
    assert r.json()["applied"] == "furina"
    r2 = c.get("/api/pet-market/sheet/furina/idle.webp")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/webp")
    r3 = c.get("/api/pet-market/sheet/furina/LICENSE")
    assert r3.status_code == 400  # 非状态 sheet → 业务错误映射 400


def test_api_search_detail_cancel(iso_api, monkeypatch):
    pytest.importorskip("fastapi")
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    r = c.get("/api/pet-market/search", params={"q": "像素", "cat": "像素萌宠"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and all((i.get("category") or "") == "像素萌宠" for i in items)
    r2 = c.post("/api/pet-market/apply", json={"skin_id": "furina"})
    assert r2.status_code == 200
    d = c.get("/api/pet-market/detail/furina").json()
    assert d["installed"] is True and "idle" in d["states"] and d["license_text"]
    assert c.get("/api/pet-market/detail/no-such").status_code == 404
    # 队列：非法/不存在 id → 400/404；取消不存在任务 → 404
    assert c.post("/api/pet-market/install", json={"skin_id": "../evil"}).status_code == 400
    assert c.post("/api/pet-market/install", json={"skin_id": "no-such"}).status_code == 404
    assert c.post("/api/pet-market/cancel", json={"skin_id": "no-such"}).status_code == 404

    # 并发撞车：慢下载期间同 id 重复 install → 409；随后可取消
    def _slow(url, dst, box):
        time.sleep(1.0)
        dst.write_bytes(b"x")

    monkeypatch.setattr(pet_market, "_download_to", _slow)
    monkeypatch.setattr(pet_market, "_make_preview", lambda d: None)
    assert c.post("/api/pet-market/install", json={"skin_id": "mika"}).status_code == 200
    assert c.post("/api/pet-market/install", json={"skin_id": "mika"}).status_code == 409
    assert c.post("/api/pet-market/cancel", json={"skin_id": "mika"}).status_code == 200
    deadline = time.time() + 15
    while time.time() < deadline and pet_market.is_busy():
        time.sleep(0.05)
    # progress 形状：{items, active, queued}
    pr = c.get("/api/pet-market/progress").json()
    assert isinstance(pr.get("items"), list)
    assert "active" in pr and "queued" in pr

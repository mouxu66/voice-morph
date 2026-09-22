"""GitHub 扫描器测试：许可把关 / 候选探测 / 假 API 端到端扫描 / 发现 Tab / ext 清单。

核心语义：
  - 「扫描即上线」：试转通过的候选写入 ext 清单，find_manifest_item 可见 → 可直接安装
  - 把关：无宽松许可、树太大、试转失败、atlas（需人工 meta）都不上线
用例隔离：monkeypatch pet_market.PET_SKINS_DIR/STATE_FILE/EXT_FILE 到 tmp，
并 patch pet_scan 的 _gh_get（假 GitHub API）与 _fetch_source（假源文件下载）。
"""

import json
import time
import urllib.parse
from pathlib import Path

import pet_market
import pet_scan
import pytest
from pet_scan import ScanError


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """隔离 pet_market 输出目录 + ext 清单文件到 tmp。"""
    monkeypatch.setattr(pet_market, "PET_SKINS_DIR", tmp_path / "pet-skins")
    monkeypatch.setattr(pet_market, "STATE_FILE", tmp_path / "pet-skins" / "state.json")
    monkeypatch.setattr(pet_market, "EXT_FILE", tmp_path / "pet-scan-ext.json")
    return pet_market


@pytest.fixture(autouse=True)
def _reset_scan(iso):
    """每用例前重置扫描器单例状态（模块级 _SCAN 跨测试共享）。"""
    pet_scan._reset()
    yield
    pet_scan._reset()


def _mk_gif(ffmpeg_run, tmp_path: Path, name: str, frames: int = 4) -> Path:
    """用 ffmpeg 生成一个真实小 gif（测试替换下载源用）；默认多帧走 hstack 路径。

    `ffmpeg_run` 由 conftest 提供（可执行路径由它自己带上）。夹具同时统一了
    "优先用 winget 完整 build"这个生产侧约定（见 common.find_ffmpeg），
    以及两种 skip：没装 ffmpeg（原来硬编码字面量 `"ffmpeg"`，2026-09-13 CI 上
    直接 WinError 2）、**系统资源不足**建不了子进程（2026-09-22 全量后段实测
    WinError 1450，报出来像产品缺陷）。
    """
    out = tmp_path / name
    r = ffmpeg_run(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=32x32:rate={max(2, frames)}",
            "-t",
            "1",
            "-loop",
            "0",
            str(out),
        ]
    )
    assert r.returncode == 0, r.stderr[-300:]
    return out


def _mk_pixel_json(tmp_path: Path, name: str = "pixel_capybara.json") -> Path:
    d = {
        "size": [4, 4],
        "palette": {"0": [255, 255, 255, 255], "1": [0, 0, 0, 255]},
        "frames": [
            {"name": "idle0", "pixels": [[0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1], [0, 1, 1, 0]]},
            {"name": "idle1", "pixels": [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]]},
        ],
    }
    p = tmp_path / name
    p.write_text(json.dumps(d), "utf-8")
    return p


def _fake_dl_from(tmp_path: Path, sources: dict) -> object:
    """造 fake 下载：按 URL 末尾文件名从 sources 复制真实素材到 dst。"""

    def fake(url: str, dst, box):
        import shutil

        name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        src = sources.get(name)
        if src is None:
            raise pet_market.PetMarketError(f"测试缺源文件: {name}")
        shutil.copyfile(src, dst)
        box["bytes"] = dst.stat().st_size

    return fake


def _fake_fetch_from(tmp_path: Path, sources: dict) -> object:
    """造 fake 源文件下载（pet_scan._fetch_source 签名）：按 path 文件名复制素材。"""
    import shutil

    def fake(repo: dict, path: str, dst, box):
        name = Path(path).name
        src = sources.get(name)
        if src is None:
            raise pet_market.PetMarketError(f"测试缺源文件: {name}")
        shutil.copyfile(src, dst)
        box["bytes"] = dst.stat().st_size

    return fake


def _wait_status(pred, deadline=30.0, interval=0.05):
    t0 = time.time()
    while time.time() - t0 < deadline:
        st = pet_scan.progress_scan()
        if pred(st):
            return st
        time.sleep(interval)
    raise AssertionError(f"等待超时, 当前状态: {pet_scan.progress_scan()}")


# ---- 许可把关与候选探测 ----


def test_license_whitelist(iso):
    assert pet_scan._licission_ok({"license": "MIT"})
    assert pet_scan._licission_ok({"license": "Apache-2.0"})
    assert pet_scan._licission_ok({"license": "CC0-1.0"})
    assert not pet_scan._licission_ok({"license": "GPL-3.0"})  # 传染性许可不收
    assert not pet_scan._licission_ok({"license": "NOASSERTION"})
    assert not pet_scan._licission_ok({"license": ""})  # 无许可
    assert not pet_scan._licission_ok({})  # 缺字段


def test_is_candidate_kinds(iso):
    assert pet_scan._is_candidate("pets/cat_idle.gif", 2048)[0] == "gif"
    assert pet_scan._is_candidate("assets/pixel_capybara.json", 256)[0] == "pixel"
    assert pet_scan._is_candidate("assets/capybara.json", 6)[0] is None  # 极小 → 大小不符
    assert pet_scan._is_candidate("sprites/spritesheet.png", 99999)[0] == "atlas"
    assert pet_scan._is_candidate("node_modules/x/logo.gif", 2048)[0] is None  # 目录排除
    assert pet_scan._is_candidate("docs/readme.gif", 2048)[0] is None  # 目录排除
    assert pet_scan._is_candidate("random/photo.jpg", 2048)[0] is None  # 无关
    assert pet_scan._is_candidate("big/movie.gif", pet_scan.MAX_SOURCE_BYTES + 1)[0] is None  # 超大


def test_find_repos_dedup_and_branch(iso, monkeypatch):
    """repo search 结果按 full_name 去重、取 default_branch、按 stars 倒序。"""

    def fake_gh(path, params=None):
        assert path == "/search/repositories"
        return {
            "items": [
                {
                    "full_name": "a/dup",
                    "default_branch": "main",
                    "license": {"spdx_id": "MIT"},
                    "stargazers_count": 5,
                    "description": "x",
                },
                {
                    "full_name": "a/dup",
                    "default_branch": "main",
                    "license": {"spdx_id": "MIT"},
                    "stargazers_count": 5,
                    "description": "x",
                },  # 重复 → 去重
                {
                    "full_name": "b/low",
                    "default_branch": "master",
                    "license": {"spdx_id": "CC0-1.0"},
                    "stargazers_count": 1,
                    "description": "y",
                },
            ]
        }

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    repos = pet_scan.find_repos()  # 4 个查询词 = 4 次 search 请求，都返回同一列表
    assert [r["full_name"] for r in repos] == ["a/dup", "b/low"]  # stars 倒序
    assert repos[0]["default_branch"] == "main" and repos[0]["license"] == "MIT"
    assert repos[1]["license"] == "CC0-1.0"


# ---- 端到端扫描（假 API + 假下载，真 ffmpeg 试转） ----


def test_scan_pixel_json_goes_live(iso, tmp_path, monkeypatch, ffmpeg_bin):
    """像素 JSON 素材：扫描 → 试转通过 → 上线 ext 清单 → 可直接安装。

    这里 `ffmpeg_bin` 只是守卫：扫描器的"试转"（pixel JSON → webp strip）走真 ffmpeg，
    没有它候选会被静默判为 built_fail，表现为 `len(items) == 0` 这种**看不出真因**的红
    （2026-09-13 CI 实测）。
    """
    pix = _mk_pixel_json(tmp_path)

    def fake_gh(path, params=None):
        if path.startswith("/search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "owner/pix-pet",
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                        "stargazers_count": 42,
                        "description": "pixel pet",
                    }
                ]
            }
        if path == "/repos/owner/pix-pet/git/trees/main":
            return {
                "tree": [
                    {"type": "blob", "path": "assets/pixel_capybara.json", "size": 512},
                    {"type": "blob", "path": "README.md", "size": 200},
                ]
            }
        raise AssertionError(f"不应请求 {path}")

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    fake_dl = _fake_dl_from(tmp_path, {"pixel_capybara.json": pix})
    monkeypatch.setattr(
        pet_scan, "_fetch_source", _fake_fetch_from(tmp_path, {"pixel_capybara.json": pix})
    )
    # install 走的是 pet_market._download_to（安装 worker 在 pet_market 线程里）
    monkeypatch.setattr(pet_market, "_download_to", fake_dl)
    monkeypatch.setattr(pet_market, "_make_preview", lambda d: None)

    pet_scan.start_scan()
    st = _wait_status(lambda s: s["status"] in ("done", "failed"))
    assert st["status"] == "done", st

    items = pet_market.get_ext_items()
    assert len(items) == 1 and items[0]["id"].startswith("scan-owner-pix-pet")
    assert pet_scan.progress_scan()["built_ok"] == 1

    # 扫描即上线：find_manifest_item 能看到 → install 走队列可安装
    entry = items[0]
    assert pet_market.find_manifest_item(entry["id"]) is not None
    st2 = pet_market.install(entry["id"])
    assert st2["status"] in ("queued", "downloading", "done")
    # 等安装任务到终结态（is_busy 只在激活态为真，避开 queued→激活的空窗竞态）
    deadline = time.time() + 60
    while time.time() < deadline:
        cur = pet_market._TASKS[entry["id"]]["status"]
        if cur in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert cur == "done", pet_market._TASKS[entry["id"]]["error"]
    d = pet_market.skin_dir(entry["id"])
    skin = json.loads((d / "skin.json").read_text("utf-8"))
    assert "idle" in skin["states"]
    # 发现 Tab 标记已安装
    disc = pet_scan.discovery_items()
    assert disc and disc[0]["installed"] is True


def test_scan_gif_merges_states(iso, tmp_path, monkeypatch, ffmpeg_run):
    """同一仓库多个 gif：合并成一个皮肤，idle/play 按文件名关键字分状态。"""
    idle = _mk_gif(ffmpeg_run, tmp_path, "idle.gif")
    walk = _mk_gif(ffmpeg_run, tmp_path, "walking.gif")
    gif_map = pet_scan._gif_map_from_names(["idle.gif", "walking.gif"])
    assert gif_map["idle"] == "idle.gif" and gif_map["play"] == "walking.gif"

    def fake_gh(path, params=None):
        if path.startswith("/search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "o/pet",
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                        "stargazers_count": 3,
                        "description": "",
                    }
                ]
            }
        if path == "/repos/o/pet/git/trees/main":
            return {
                "tree": [
                    {"type": "blob", "path": "gif/idle.gif", "size": 2048},
                    {"type": "blob", "path": "gif/walking.gif", "size": 2048},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    monkeypatch.setattr(
        pet_scan,
        "_fetch_source",
        _fake_fetch_from(tmp_path, {"idle.gif": idle, "walking.gif": walk}),
    )
    pet_scan.start_scan()
    st = _wait_status(lambda s: s["status"] in ("done", "failed"))
    assert st["status"] == "done", st
    items = pet_market.get_ext_items()
    assert len(items) == 1
    assert items[0]["source_type"] == "gif-multi"
    assert items[0]["meta"]["gif_map"]["idle"] == "idle.gif"


def test_scan_skips_unlicensed_repo(iso, tmp_path, monkeypatch):
    """无宽松许可的仓库跳过（lic_skip +1），不试转、不上线。"""

    def fake_gh(path, params=None):
        if path.startswith("/search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "o/no-lic",
                        "default_branch": "main",
                        "license": {"spdx_id": "NOASSERTION"},
                        "stargazers_count": 99,
                        "description": "",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    pet_scan.start_scan()
    st = _wait_status(lambda s: s["status"] in ("done", "failed"))
    assert st["status"] == "done", st
    assert st["repos_lic_skip"] == 1 and st["built_ok"] == 0
    assert pet_market.get_ext_items() == []


def test_scan_failed_trial_not_published(iso, tmp_path, monkeypatch):
    """试转失败的候选（源文件缺）不计入 ext 清单，built_fail +1 且记下原因。"""
    _mk_pixel_json(tmp_path)

    def fake_gh(path, params=None):
        if path.startswith("/search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "o/bad",
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                        "stargazers_count": 1,
                        "description": "",
                    }
                ]
            }
        if path == "/repos/o/bad/git/trees/main":
            return {"tree": [{"type": "blob", "path": "pixel.json", "size": 512}]}
        raise AssertionError(path)

    # 下载直接抛错（源不存在）
    def boom(repo, path, dst, box):
        raise pet_market.PetMarketError("404 源不存在")

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    monkeypatch.setattr(pet_scan, "_fetch_source", boom)
    pet_scan.start_scan()
    st = _wait_status(lambda s: s["status"] in ("done", "failed"))
    assert st["status"] == "done", st
    assert st["built_fail"] == 1 and st["built_ok"] == 0
    assert pet_market.get_ext_items() == []
    # 失败原因要透传到前端（不再吞进计数里）
    assert st["fails"] and st["fails"][0]["repo"] == "o/bad"
    assert st["fails"][0]["path"] == "pixel.json"
    assert "404" in st["fails"][0]["reason"]


def test_fetch_source_falls_back_to_raw(iso, tmp_path, monkeypatch):
    """contents API 失败 → 回退 raw 直链（白名单下载器），URL 空格编码正确。"""
    dst = tmp_path / "out.gif"

    def bad_gh(repo, path, d, box):
        raise ScanError("contents API 错误 404: x.gif")

    calls: dict = {}

    def fake_raw(url, d, box):
        calls["url"] = url
        d.write_bytes(b"raw-bytes")

    monkeypatch.setattr(pet_scan, "_gh_download", bad_gh)
    monkeypatch.setattr(pet_scan, "_download_to", fake_raw)
    repo = {"full_name": "o/x", "default_branch": "main"}
    pet_scan._fetch_source(repo, "a b/p.gif", dst, {})
    assert calls["url"] == "https://raw.githubusercontent.com/o/x/main/a%20b/p.gif"
    assert dst.read_bytes() == b"raw-bytes"


def test_scan_cancel(iso, tmp_path, monkeypatch):
    """取消中扫描：状态 → cancelled。"""
    _mk_pixel_json(tmp_path)

    def fake_gh(path, params=None):
        if path.startswith("/search/repositories"):
            return {
                "items": [
                    {
                        "full_name": "o/pet",
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                        "stargazers_count": 1,
                        "description": "",
                    }
                ]
            }
        if path == "/repos/o/pet/git/trees/main":
            return {"tree": [{"type": "blob", "path": "pixel.json", "size": 512}]}
        raise AssertionError(path)

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)

    def slow_dl(repo, path, dst, box):
        for _ in range(200):
            if box.get("cancel"):
                raise pet_market.PetMarketError("已取消")
            time.sleep(0.01)
        dst.write_bytes(b"x")

    monkeypatch.setattr(pet_scan, "_fetch_source", slow_dl)
    pet_scan.start_scan()
    time.sleep(0.1)  # 等 worker 进入下载循环
    pet_scan.cancel_scan()
    st = _wait_status(lambda s: s["status"] in ("cancelled", "failed", "done"))
    assert st["status"] in ("cancelled", "done"), st


def test_scan_rate_limit_fails_cleanly(iso, tmp_path, monkeypatch):
    """GitHub 限流（403 + X-RateLimit-Remaining:0）→ 扫描 failed，提示 GITHUB_TOKEN。"""

    def fake_gh(path, params=None):
        raise pet_scan.ScanError(
            "GitHub API 限流已耗尽。可设置环境变量 GITHUB_TOKEN " "提升配额，或稍等一分钟后再试。"
        )

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    pet_scan.start_scan()
    st = _wait_status(lambda s: s["status"] in ("done", "failed"))
    assert st["status"] == "failed"
    assert "GITHUB_TOKEN" in st["error"]


def test_start_scan_twice_rejects(iso, monkeypatch, tmp_path):
    """扫描进行中再次 start → 409 语义。"""
    _mk_pixel_json(tmp_path)

    def fake_gh(path, params=None):
        time.sleep(1)  # 拖住扫描线程
        return {"items": []} if path.startswith("/search") else {"tree": []}

    monkeypatch.setattr(pet_scan, "_gh_get", fake_gh)
    pet_scan.start_scan()
    with pytest.raises(ScanError, match="已有扫描在进行中"):
        pet_scan.start_scan()
    pet_scan.cancel_scan()
    _wait_status(lambda s: s["status"] in ("cancelled", "done", "failed"))


# ---- 发现清单 / ext 生命周期 ----


def test_add_remove_ext_item(iso):
    item = {
        "id": "scan-owner-x-pet",
        "name": "X 桌宠",
        "category": "卡通",
        "license": "MIT",
        "attribution": "owner/x",
        "source_type": "gif-multi",
        "bundle": False,
        "source_urls": ["https://raw.githubusercontent.com/o/x/main/a.gif"],
        "meta": {},
        "discovery": {"repo": "owner/x", "stars": 1},
    }
    pet_market.add_ext_item(item)
    assert pet_market.get_ext_items()[0]["id"] == "scan-owner-x-pet"
    assert pet_market.find_manifest_item("scan-owner-x-pet") is not None  # 扫描即上线
    # 重复 add 幂等（覆盖）
    pet_market.add_ext_item({**item, "stars": 9})
    assert len(pet_market.get_ext_items()) == 1
    # id 与内置冲突拒绝
    with pytest.raises(pet_market.PetMarketError, match="冲突"):
        pet_market.add_ext_item({**item, "id": "furina"})
    assert pet_market.remove_ext_item("scan-owner-x-pet") is True
    assert pet_market.remove_ext_item("scan-owner-x-pet") is False  # 二次移除
    assert pet_market.find_manifest_item("scan-owner-x-pet") is None


def test_discovery_items_flags_installed(iso, tmp_path, monkeypatch):
    entry = {
        "id": "scan-z-1",
        "name": "z",
        "category": "像素萌宠",
        "license": "MIT",
        "attribution": "o/z",
        "source_type": "pixel-json",
        "bundle": False,
        "source_urls": ["https://raw.githubusercontent.com/o/z/main/p.json"],
        "meta": {},
        "discovery": {"repo": "o/z", "stars": 5, "kind": "pixel"},
    }
    pet_market.add_ext_item(entry)
    disc = pet_scan.discovery_items()
    assert len(disc) == 1 and disc[0]["installed"] is False
    pet_market.remove_ext_item("scan-z-1")
    assert pet_scan.discovery_items() == []
    # 下线不存在 → 404 语义
    with pytest.raises(ScanError, match="不存在"):
        pet_scan.remove_discovery("nope")


def test_api_scan_and_discovery_endpoints(iso):
    pytest.importorskip("fastapi")
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    # 未扫描时：progress=idle、discovery 空
    r = c.get("/api/pet-market/scan/progress")
    assert r.status_code == 200 and r.json()["status"] in ("idle", "running")
    r2 = c.get("/api/pet-market/discovery")
    assert r2.status_code == 200 and r2.json()["items"] == []
    # 扫描进行中重复触发 → 409
    pet_scan._reset(status="running")
    try:
        r3 = c.post("/api/pet-market/scan")
        assert r3.status_code == 409
    finally:
        pet_scan._reset(status="idle")
    # 取消无运行中扫描 → 409
    r4 = c.post("/api/pet-market/scan/cancel")
    assert r4.status_code == 409
    # 下线不存在候选 → 404
    r5 = c.delete("/api/pet-market/discovery/nope")
    assert r5.status_code == 404

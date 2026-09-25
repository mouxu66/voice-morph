"""音效包（`sfx_packs.py`）与它的三条路由。

这里测的是**契约**，不是实现：
  · 包的目录布局与 id 命名空间（`<pack>/<stem>`）—— 它决定"谁能覆盖谁"；
  · zip 安装的**六道门**（成员名 / 符号链接 / 后缀白名单 / 缺 pack.json /
    缺 license / 没有 wav）—— 包是**外部输入**，每一道都对应一种真实的坏包；
  · 安装是**原子**的（先解到 `.tmp-*` 再换进去），所以失败不该留下半截目录；
  · 清单（货架）三类情形都不是异常：没配 / 取不到 / 正常。

所有用例都打在 `tmp_path` 上 —— ⚠️ 包目录是**用户真实数据**
（`<media>/soundboard/packs`）。`test_soundboard.py` 的隔离夹具原本只打了
`IMPORT_DIR`，装包功能加进来后它必须一并打掉 `sfx_packs.PACKS_DIR`，
否则"跑一遍单测往用户目录里装了个包"是**完全可能**发生的
（同一类事故见 `docs/犯错档案-工程.md` §8.36 / §8.37）。
"""

from __future__ import annotations

import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import sfx_lib  # noqa: E402
import sfx_packs  # noqa: E402
import soundboard  # noqa: E402

SR = 48000


# --------------------------------------------------------------- 造数据


def _wav_bytes(seconds: float = 0.3, freq: float = 440.0) -> bytes:
    t = np.arange(int(SR * seconds), dtype=np.float64) / SR
    data = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, data, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _pack_json(**over) -> bytes:
    meta = {
        "name": "街机音效",
        "author": "测试",
        "license": "CC0-1.0",
        "samples": {"coin": {"name": "金币", "tags": ["游戏"]}},
    }
    meta.update(over)
    return json.dumps(meta, ensure_ascii=False).encode("utf-8")


def _good_zip(root: str = "", **over) -> bytes:
    p = f"{root}/" if root else ""
    return _zip(
        {
            f"{p}pack.json": _pack_json(**over),
            f"{p}coin.wav": _wav_bytes(0.2, 880),
            f"{p}laser.wav": _wav_bytes(0.25, 1320),
            f"{p}LICENSE": b"CC0-1.0 - public domain",
        }
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """包目录 + 计数 + 清单地址全部指到临时目录（见文件头）。"""
    monkeypatch.setattr(sfx_packs, "PACKS_DIR", tmp_path / "packs")
    monkeypatch.setattr(soundboard, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.delenv(sfx_packs.INDEX_ENV, raising=False)
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(soundboard.router)
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------- 名字与 id


@pytest.mark.parametrize("raw", ["", ".", "..", "a" * 41, 'q"q', "x\x00y"])
def test_clean_stem_rejects_bad_names(raw):
    assert sfx_packs.clean_stem(raw) is None


def test_clean_stem_is_normalizing_not_validating():
    """★ `../x` / `a/b` 会被"收"成 `x` / `b`，**不是 None**。

    这是刻意的：上传文件名常带目录（浏览器会给全路径），收掉是对的。
    但需要"原样合法"的地方（包 id、清单 id、命名空间的每一段）必须自己比一次
    `clean_stem(v) == v` —— 混起来就是把 `../a` 当成一个普通名字放过去。
    下面 `test_pack_id_must_be_clean_as_written` 钉住了那一半。
    """
    assert sfx_packs.clean_stem("../x") == "x"
    assert sfx_packs.clean_stem("a/b") == "b"
    assert sfx_packs.clean_stem("sub/dir/boom.wav") == "boom"


def test_pack_id_must_be_clean_as_written():
    for bad in ("../a", "a/b", "..", "a" * 41):
        with pytest.raises(sfx_packs.PackError):
            sfx_packs.pack_dir(bad)


@pytest.mark.parametrize(
    "sid", ["boom", "arcade/coin", "a/b/c", "../x/y", "arcade/", "/coin", "arcade/../coin"]
)
def test_split_id_only_accepts_exactly_two_clean_parts(sid):
    got = sfx_packs.split_id(sid)
    if sid == "arcade/coin":
        assert got == ("arcade", "coin")
    elif sid == "boom":
        assert got is None
    else:
        assert got == ("", ""), f"{sid!r} 必须被判为非法"


def test_pack_dir_rejects_traversal():
    with pytest.raises(sfx_packs.PackError):
        sfx_packs.pack_dir("../evil")


# --------------------------------------------------------------- 安装的六道门


def test_install_zip_happy_path_and_catalog_merge():
    res = sfx_packs.install_zip(_good_zip(), "arcade")
    assert res["count"] == 2 and res["license"] == "CC0-1.0"
    d = sfx_packs.PACKS_DIR / "arcade"
    assert (d / "pack.json").is_file() and (d / "coin.wav").is_file()
    assert (d / "LICENSE").is_file(), "署名/许可文件必须原样保留"

    packs = sfx_packs.list_packs()
    assert [p["id"] for p in packs] == ["arcade"] and packs[0]["count"] == 2

    items = {i["id"]: i for i in sfx_lib.list_samples()}
    assert "arcade/coin" in items and "arcade/laser" in items
    assert items["arcade/coin"]["pack"] == "arcade"
    assert items["arcade/coin"]["builtin"] is False
    # 声明里的显示名生效；没声明的 `laser` 用文件 stem，标签回落成包名
    assert items["arcade/coin"]["name"] == "金币"
    assert items["arcade/laser"]["name"] == "laser"
    assert items["arcade/laser"]["tags"] == ["街机音效"]

    # 能播/能混的前提：resolve_path + load_pcm 认这个命名空间 id
    path, builtin = sfx_lib.resolve_path("arcade/coin")
    assert builtin is False and path == (d / "coin.wav").resolve()
    pcm, sr = sfx_lib.load_pcm("arcade/coin")
    assert sr == SR and pcm.size > 0


def test_install_accepts_single_top_dir_layout():
    res = sfx_packs.install_zip(_good_zip(root="arcade-pack"), "arcade")
    assert res["count"] == 2
    assert (sfx_packs.PACKS_DIR / "arcade" / "coin.wav").is_file()


def test_install_derives_id_from_zip_dir_when_not_given():
    res = sfx_packs.install_zip(_good_zip(root="mycoolpack"))
    assert res["id"] == "mycoolpack"


def test_install_rejects_missing_pack_json():
    data = _zip({"coin.wav": _wav_bytes()})
    with pytest.raises(sfx_packs.PackError, match="pack.json"):
        sfx_packs.install_zip(data, "arcade")


def test_install_rejects_missing_license():
    data = _zip({"pack.json": _pack_json(license=""), "coin.wav": _wav_bytes()})
    with pytest.raises(sfx_packs.PackError, match="license"):
        sfx_packs.install_zip(data, "arcade")


def test_install_rejects_pack_without_wav():
    data = _zip({"pack.json": _pack_json(), "notes.txt": b"x"})
    with pytest.raises(sfx_packs.PackError, match=".wav"):
        sfx_packs.install_zip(data, "arcade")


@pytest.mark.parametrize("bad", ["../evil.wav", "/abs/evil.wav", "C:/evil.wav", "payload.exe"])
def test_install_rejects_bad_members_and_writes_nothing(bad, tmp_path):
    data = _zip({"pack.json": _pack_json(), bad: _wav_bytes()})
    with pytest.raises(sfx_packs.PackError):
        sfx_packs.install_zip(data, "arcade")
    assert not (tmp_path / "evil.wav").exists(), "越界成员不许落盘"
    assert not (sfx_packs.PACKS_DIR / "arcade").exists()
    assert not list(sfx_packs.PACKS_DIR.glob(".tmp-*")) if sfx_packs.PACKS_DIR.exists() else True


def test_install_rejects_symlink_member():
    buf = io.BytesIO()
    info = zipfile.ZipInfo("link.wav")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pack.json", _pack_json())
        z.writestr("coin.wav", _wav_bytes())
        z.writestr(info, "target")
    with pytest.raises(sfx_packs.PackError, match="符号链接"):
        sfx_packs.install_zip(buf.getvalue(), "arcade")


def test_install_rejects_unreadable_wav():
    data = _zip({"pack.json": _pack_json(), "coin.wav": b"not audio at all"})
    with pytest.raises(sfx_packs.PackError, match="读不出来"):
        sfx_packs.install_zip(data, "arcade")
    assert not (sfx_packs.PACKS_DIR / "arcade").exists(), "验不过就不许留下包目录"


def test_install_requires_overwrite_for_existing_pack():
    sfx_packs.install_zip(_good_zip(), "arcade")
    with pytest.raises(sfx_packs.PackError) as e:
        sfx_packs.install_zip(_good_zip(), "arcade")
    assert e.value.status == 409
    # 覆盖：新包只有 1 条，旧的两条必须消失（不是"合并"）
    one = _zip({"pack.json": _pack_json(), "coin.wav": _wav_bytes()})
    res = sfx_packs.install_zip(one, "arcade", overwrite=True)
    assert res["count"] == 1
    assert not (sfx_packs.PACKS_DIR / "arcade" / "laser.wav").exists()
    assert not list(sfx_packs.PACKS_DIR.glob(".old-*")), "换包留下的暂存目录要清掉"


def test_list_packs_reports_broken_pack_instead_of_hiding_it():
    d = sfx_packs.PACKS_DIR / "halfway"
    d.mkdir(parents=True)
    (d / "coin.wav").write_bytes(_wav_bytes())
    packs = sfx_packs.list_packs()
    assert packs[0]["id"] == "halfway" and packs[0]["broken"]
    # 坏包不进格子（否则会出现一条点不响的素材）
    assert all(i["pack"] != "halfway" for i in sfx_lib.list_samples())


def test_uninstall_removes_pack():
    sfx_packs.install_zip(_good_zip(), "arcade")
    res = sfx_packs.uninstall("arcade")
    assert res["count"] == 2 and res["ids"] == ["arcade/coin", "arcade/laser"]
    assert not (sfx_packs.PACKS_DIR / "arcade").exists()
    with pytest.raises(sfx_packs.PackError) as e:
        sfx_packs.uninstall("arcade")
    assert e.value.status == 404


def test_pack_sample_icon_flows_into_catalog():
    """包内素材的 icon 也要随 catalog 下发（与出厂素材同一条元数据通路）。

    没有它，包里的素材在格子上永远是 🎧 —— 而「自己录的包」正是最需要图标的那批。
    """
    sfx_packs.install_zip(
        _zip({
            "pack.json": _pack_json(samples={"coin": {"name": "金币", "tags": ["游戏"], "icon": "🪙"}}),
            "coin.wav": _wav_bytes(0.2, 880),
        }),
        "arcade",
    )
    items = {it["id"]: it for it in sfx_lib.list_samples()}
    assert items["arcade/coin"]["icon"] == "🪙"
    assert items["arcade/coin"]["name"] == "金币"


def test_pack_sample_id_does_not_shadow_builtin():
    """裸 id 仍只属于出厂/导入 —— 命名空间化就是为了这条。"""
    sid = next(iter(p.stem for p in sfx_lib.SAMPLES_DIR.glob("*.wav")))
    sfx_packs.install_zip(_zip({"pack.json": _pack_json(), f"{sid}.wav": _wav_bytes()}), "arcade")
    path, builtin = sfx_lib.resolve_path(sid)
    assert builtin is True and path.parent == sfx_lib.SAMPLES_DIR
    assert sfx_lib.resolve_path(f"arcade/{sid}")[1] is False


# --------------------------------------------------------------- 货架（清单）


def _index_file(tmp_path: Path, packs: list[dict]) -> str:
    p = tmp_path / "index.json"
    p.write_text(json.dumps({"packs": packs}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_index_not_configured_is_note_not_error(monkeypatch):
    got = sfx_packs.load_index()
    assert got["items"] == [] and got["error"] == "" and got["note"]


def test_index_reads_local_file_and_marks_installed(tmp_path, monkeypatch):
    sfx_packs.install_zip(_good_zip(), "arcade")
    src = _index_file(
        tmp_path,
        [
            {"id": "arcade", "name": "街机", "license": "CC0-1.0",
             "url": "https://hf-mirror.com/x/y/resolve/main/arcade.zip", "downloads": 12},
            {"id": "retro", "name": "复古", "license": "CC0-1.0",
             "url": "https://hf-mirror.com/x/y/resolve/main/retro.zip"},
        ],
    )
    monkeypatch.setenv(sfx_packs.INDEX_ENV, src)
    got = sfx_packs.load_index()
    assert got["error"] == "" and len(got["items"]) == 2
    flags = {i["id"]: i["installed"] for i in got["items"]}
    assert flags == {"arcade": True, "retro": False}
    assert got["items"][0]["downloads"] == 12


@pytest.mark.parametrize(
    "packs, match",
    [
        ([{"id": "a", "name": "A", "url": "https://hf-mirror.com/a.zip"}], "license"),
        ([{"id": "a", "name": "A", "license": "CC0"}], "url"),
        ([{"id": "a", "name": "A", "license": "CC0", "url": "https://evil.example/a.zip"}], "白名单"),
        ([{"id": "../a", "name": "A", "license": "CC0",
           "url": "https://hf-mirror.com/a.zip"}], "不合法"),
    ],
)
def test_index_rejects_bad_entries_loudly(tmp_path, monkeypatch, packs, match):
    monkeypatch.setenv(sfx_packs.INDEX_ENV, _index_file(tmp_path, packs))
    got = sfx_packs.load_index()
    assert got["items"] == [] and match in got["error"]


def test_index_missing_file_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv(sfx_packs.INDEX_ENV, str(tmp_path / "nope.json"))
    got = sfx_packs.load_index()
    assert "不存在" in got["error"] and got["items"] == []


def test_install_from_index_verifies_sha256(monkeypatch):
    data = _good_zip()
    monkeypatch.setattr(
        sfx_packs, "_fetch_bytes", lambda url, limit, timeout=30.0: data
    )
    monkeypatch.setattr(
        sfx_packs,
        "load_index",
        lambda: {
            "source": "test",
            "error": "",
            "note": "",
            "items": [
                {"id": "arcade", "name": "街机", "license": "CC0-1.0",
                 "url": "https://hf-mirror.com/a.zip", "sha256": "0" * 64, "downloads": 0}
            ],
        },
    )
    with pytest.raises(sfx_packs.PackError, match="sha256 不一致"):
        sfx_packs.install_from_index("arcade")
    assert not (sfx_packs.PACKS_DIR / "arcade").exists()


def test_install_from_index_without_hash_says_so(monkeypatch):
    data = _good_zip()
    monkeypatch.setattr(sfx_packs, "_fetch_bytes", lambda url, limit, timeout=30.0: data)
    monkeypatch.setattr(
        sfx_packs,
        "load_index",
        lambda: {
            "source": "test",
            "error": "",
            "note": "",
            "items": [
                {"id": "arcade", "name": "街机", "license": "CC0-1.0",
                 "url": "https://hf-mirror.com/a.zip", "sha256": "", "downloads": 0}
            ],
        },
    )
    res = sfx_packs.install_from_index("arcade")
    assert res["sha256_verified"] is False and res["count"] == 2


# --------------------------------------------------------------- 路由


def test_routes_list_install_uninstall(client, tmp_path):
    assert client.get("/api/soundboard/packs").json()["packs"] == []
    r = client.post(
        "/api/soundboard/packs/install",
        files={"file": ("arcade.zip", _good_zip(), "application/zip")},
        data={"pack_id": "arcade"},
    )
    assert r.status_code == 200 and r.json()["count"] == 2
    listed = client.get("/api/soundboard/packs").json()
    assert listed["packs"][0]["id"] == "arcade" and listed["samples"] == 2
    assert client.get("/api/soundboard/catalog").json()["items"][-1]["id"].startswith("arcade/")
    gone = client.delete("/api/soundboard/packs/arcade").json()
    assert gone["ok"] and gone["count"] == 2
    assert client.get("/api/soundboard/packs").json()["packs"] == []


def test_route_install_conflict_is_409_then_overwrite(client):
    files = {"file": ("arcade.zip", _good_zip(), "application/zip")}
    assert client.post("/api/soundboard/packs/install", files=files).status_code == 200
    files = {"file": ("arcade.zip", _good_zip(), "application/zip")}
    assert client.post("/api/soundboard/packs/install", files=files).status_code == 409
    files = {"file": ("arcade.zip", _good_zip(), "application/zip")}
    r = client.post(
        "/api/soundboard/packs/install", files=files, data={"overwrite": "true"}
    )
    assert r.status_code == 200


def test_route_available_reports_note_when_unconfigured(client):
    j = client.get("/api/soundboard/packs/available").json()
    assert j["ok"] and j["items"] == [] and j["note"]


def test_route_download_uses_index(client, monkeypatch):
    data = _good_zip()
    monkeypatch.setattr(sfx_packs, "_fetch_bytes", lambda url, limit, timeout=30.0: data)
    monkeypatch.setattr(
        sfx_packs,
        "load_index",
        lambda: {"source": "t", "error": "", "note": "",
                 "items": [{"id": "arcade", "name": "A", "license": "CC0",
                            "url": "https://hf-mirror.com/a.zip", "downloads": 0}]},
    )
    r = client.post("/api/soundboard/packs/download", json={"id": "arcade"})
    assert r.status_code == 200 and r.json()["count"] == 2
    r = client.post("/api/soundboard/packs/download", json={"id": "nope"})
    assert r.status_code == 404


def test_route_delete_pack_sample_never_reaches_the_handler(client):
    """包内素材删不掉，但**不是因为**里有守卫，而是它压根匹配不上路由。

    含 `/` 的 id 是两段，而单条删除的路由只有一段 `{sample_id}`：
    这个只挂了声板 router 的 app 里回 404；真环境会落到 SPA 的 GET 兜底，回 405
    （2026-09-24 在真后端上验过）。两者都是"删不掉"，但**别把它当成守卫**：
    曾经在这里写过一句友好报错，那是**只能靠直接调函数碰到的死代码**，已删。
    为什么不用 `{sample_id:path}` 补上：那条通配会先吃掉
    `DELETE /packs/{pack_id}`（声明顺序即行为），卸载整包会变成"这是包内素材"。
    """
    client.post(
        "/api/soundboard/packs/install",
        files={"file": ("arcade.zip", _good_zip(), "application/zip")},
        data={"pack_id": "arcade"},
    )
    assert client.delete("/api/soundboard/arcade/coin").status_code == 404
    # 关键：整包卸载仍然通（上面那条通配没出现，所以没被遮住）
    assert client.delete("/api/soundboard/packs/arcade").status_code == 200
    assert not (sfx_packs.PACKS_DIR / "arcade").exists()


def test_uninstall_purges_use_counts(client, tmp_path):
    client.post(
        "/api/soundboard/packs/install",
        files={"file": ("arcade.zip", _good_zip(), "application/zip")},
        data={"pack_id": "arcade"},
    )
    soundboard._bump("arcade/coin")
    assert json.loads(soundboard.STATS_FILE.read_text(encoding="utf-8"))["arcade/coin"] == 1
    client.delete("/api/soundboard/packs/arcade")
    assert "arcade/coin" not in json.loads(
        soundboard.STATS_FILE.read_text(encoding="utf-8")
    ), "卸载后残留计数会让重装凭空继承「用过 N 次」"


def test_catalog_marks_only_imported_samples_removable(client, tmp_path):
    client.post(
        "/api/soundboard/import",
        files={"file": ("mine.wav", _wav_bytes(), "audio/wav")},
    )
    client.post(
        "/api/soundboard/packs/install",
        files={"file": ("arcade.zip", _good_zip(), "application/zip")},
        data={"pack_id": "arcade"},
    )
    items = {i["id"]: i for i in client.get("/api/soundboard/catalog").json()["items"]}
    assert items["mine"]["removable"] is True
    assert items["arcade/coin"]["removable"] is False
    assert any(i["builtin"] and not i["removable"] for i in items.values())


def test_real_server_mounts_pack_routes():
    """★ 打**真 server.app**：清单声明了却没挂上的唯一防线（同 test_soundboard 的做法）。

    只测自己 include_router 的小 app 是不够的：挂载是清单驱动的，清单漏挂、
    或 `sound.fx-board` 被关掉时行为还会变，只有真 app 能反映这些。
    """
    import server
    from fastapi.testclient import TestClient as _TC

    c = _TC(server.app, raise_server_exceptions=False)
    r = c.get("/api/soundboard/packs")
    assert r.status_code == 200 and r.json()["ok"] is True

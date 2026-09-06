"""作品库（B1）：收藏/标签元数据 + 过滤 + 批量删除 + zip 导出 单测。

全部本地：monkeypatch 隔离 outputs，zip 直接断言字节内容，不碰网络。
"""
import io
import json
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config as cfg  # noqa: E402
import history  # noqa: E402
from history_api import router  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """history 的输出目录切到临时目录。"""
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    history.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def client(isolated):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _mk_wav(tmp: Path, name: str) -> str:
    (tmp / name).write_bytes(b"RIFF" + name.encode())
    return name


def _reg(wav: str, voice: str = "a") -> str:
    return history.register("tts", voice, wav, f"/api/media/outputs/{wav}", 1.0)


def test_register_backfills_meta(isolated):
    _reg(_mk_wav(isolated, "x.wav"))
    item = history.query()["items"][0]
    assert item["starred"] is False and item["tags"] == []


def test_old_record_read_backfills_meta(isolated):
    """老记录文件里没有 starred/tags 字段 → 读出来补默认，不炸、不用迁移。"""
    _reg(_mk_wav(isolated, "old.wav"))
    lines = history.HISTORY_FILE.read_text("utf-8").splitlines()
    rec = json.loads(lines[0])
    rec.pop("starred"), rec.pop("tags")
    history.HISTORY_FILE.write_text(json.dumps(rec, ensure_ascii=False) + "\n", "utf-8")
    item = history.query()["items"][0]
    assert item["starred"] is False and item["tags"] == []


def test_set_meta_star_and_tags_roundtrip(isolated):
    _reg(_mk_wav(isolated, "m.wav"))
    hid = history.query()["items"][0]["id"]
    rec = history.set_meta(hid, starred=True, tags=["比赛", " 袋鼠 ", "比赛", "x" * 40])
    assert rec["starred"] is True
    assert rec["tags"] == ["比赛", "袋鼠"]           # 去重、去空白、超长拒收
    assert history.query(starred=True)["total"] == 1
    assert history.query(starred=False)["total"] == 0
    assert history.query(tag="比赛")["total"] == 1


def test_set_meta_none_keeps_field(isolated):
    _reg(_mk_wav(isolated, "n.wav"))
    hid = history.query()["items"][0]["id"]
    history.set_meta(hid, tags=["tag1"])
    rec = history.set_meta(hid, starred=True)       # 只改收藏
    assert rec["tags"] == ["tag1"] and rec["starred"] is True


def test_set_meta_missing_returns_empty(isolated):
    assert history.set_meta("nope", starred=True) == {}


def test_clean_tags_cap_and_invalid(isolated):
    bad = ["a", "", "  ", "b c", "d,e", "e;f", "g/h"] + [f"t{i}" for i in range(12)]
    assert len(history.clean_tags(bad)) == history._MAX_TAGS


def test_all_tags_sorted_by_count(isolated):
    for wav, tags in (("1.wav", ["a", "b"]), ("2.wav", ["a"])):
        n = _reg(_mk_wav(isolated, wav))
        history.set_meta(n, tags=tags)
    tags = history.all_tags()
    assert [t["tag"] for t in tags] == ["a", "b"]
    assert tags[0]["count"] == 2


def test_bulk_delete_mixed(isolated):
    ids = [_reg(_mk_wav(isolated, f"bd{i}.wav")) for i in range(3)]
    res = history.bulk_delete(ids[:2] + ["ghost"], keep_file=True)
    assert res["deleted"] == 2
    assert res["ok"] is False and res["failed"][0]["id"] == "ghost"
    assert history.query()["total"] == 1


def test_api_patch_and_filters(client, isolated):
    name = _mk_wav(isolated, "api.wav")
    _reg(name, voice="v1")
    _reg(_mk_wav(isolated, "api2.wav"), voice="v2")
    hid = history.query(voice_id="v1")["items"][0]["id"]

    r = client.patch(f"/api/history/{hid}", json={"starred": True, "tags": ["比赛"]})
    assert r.status_code == 200 and r.json()["item"]["starred"] is True

    assert client.get("/api/history?starred=true").json()["total"] == 1
    assert client.get("/api/history?tag=比赛").json()["total"] == 1
    assert client.get("/api/history?voice_id=v2").json()["total"] == 1
    assert [t["tag"] for t in client.get("/api/history/tags").json()["tags"]] == ["比赛"]


def test_api_patch_validation(client):
    assert client.patch("/api/history/x", json={}).status_code == 400
    assert client.patch("/api/history/x", json={"starred": True}).status_code == 404
    assert client.get("/api/history/tags").json() == {"tags": []}


def test_api_bulk_delete(client, isolated):
    ids = [_reg(_mk_wav(isolated, f"apibd{i}.wav")) for i in range(2)]
    r = client.post("/api/history/bulk_delete", json={"ids": ids})
    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert client.post("/api/history/bulk_delete", json={"ids": []}).status_code == 400


def test_api_export_zip(client, isolated):
    n1, n2 = _mk_wav(isolated, "e1.wav"), _mk_wav(isolated, "e2.wav")
    ids = [_reg(n1), _reg(n2)]
    (isolated / n2).unlink()                        # 模拟文件已被清理

    r = client.post("/api/history/export", json={"ids": ids})
    assert r.status_code == 200
    assert r.headers["x-missing-files"] == "1"      # 缺失文件计数透出
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == [n1]                    # 缺失的跳过，不炸
    assert zf.read(n1) == (b"RIFF" + n1.encode())

    # 全缺失 → 404；空 ids → 400
    assert client.post("/api/history/export", json={"ids": [ids[1]]}).status_code == 404
    assert client.post("/api/history/export", json={"ids": []}).status_code == 400


def test_api_export_dedup_same_name(client, isolated):
    """两条记录指向同名 wav（不同 id）→ zip 内加 id 前缀去重。"""
    name = _mk_wav(isolated, "dup.wav")
    ids = [_reg(name), _reg(name)]
    zf = zipfile.ZipFile(io.BytesIO(client.post("/api/history/export", json={"ids": ids}).content))
    assert len(zf.namelist()) == 2 and len(set(zf.namelist())) == 2

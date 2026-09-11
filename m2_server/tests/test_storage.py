"""B2 存储占用看板：目标扫描 / 受保护目标拒绝清理 / 清理联动历史记录。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config as cfg  # noqa: E402
import history  # noqa: E402
import storage  # noqa: E402
from system_api import router  # noqa: E402


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """把所有可扫描目录切到临时目录（含 history 的落盘文件，防止污染真实 outputs）。"""
    outputs, media, rvc = tmp_path / "outputs", tmp_path / "media", tmp_path / "rvc"
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(cfg, "MEDIA_DIR", media)
    monkeypatch.setattr(cfg, "RVC_ROOT", rvc)
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", outputs / "history.jsonl")
    for d in (outputs / "market", outputs / "qc", outputs / "logs_placeholder",
              media / "clips", media / "ft", media / "voicebank", rvc / "assets" / "weights",
              tmp_path / "m2_server"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "_market_dir", lambda: outputs / "market")
    return tmp_path


@pytest.fixture()
def client(dirs):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _put(base: Path, rel: str, size: int = 1024) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_scan_counts_only_matching_files(dirs):
    _put(dirs / "outputs" / "market", "a.pth", 100)
    _put(dirs / "outputs" / "market", "a.index", 50)
    _put(dirs / "outputs" / "market", "a.pth.part", 10)
    _put(dirs / "outputs" / "market", "downloads.json", 5)
    _put(dirs / "outputs" / "market", "vx_preview.wav", 200)
    _put(dirs / "outputs", "tts_1.wav", 300)

    items = {i["key"]: i for i in storage.scan()}
    assert items["market_downloads"]["bytes"] == 160 and items["market_downloads"]["files"] == 3
    assert items["market_previews"]["bytes"] == 200 and items["market_previews"]["files"] == 1
    assert items["outputs_wav"]["bytes"] == 300
    assert items["market_downloads"]["cleanable"] is True


def test_scan_protected_targets(dirs):
    _put(dirs / "media" / "voicebank", "kangaroo/reference.wav", 999)
    _put(dirs / "rvc" / "assets" / "weights", "k.pth", 555)
    items = {i["key"]: i for i in storage.scan()}
    assert items["voicebank"]["cleanable"] is False and items["voicebank"]["bytes"] == 999
    assert items["rvc_weights"]["cleanable"] is False and items["rvc_weights"]["bytes"] == 555


def test_scan_missing_dirs_zero(dirs):
    items = {i["key"]: i for i in storage.scan(["clips", "ft_corpus"])}
    assert items["clips"]["bytes"] == 0 and items["ft_corpus"]["files"] == 0


def test_clean_only_removes_target_files(dirs):
    _put(dirs / "outputs" / "market", "a.pth", 100)
    _put(dirs / "outputs" / "market", "downloads.json", 5)   # 状态文件不该被误删
    _put(dirs / "outputs", "keep.wav", 10)
    res = storage.clean(["market_downloads"])
    assert res["removed_files"] == 1 and res["freed_bytes"] == 100
    assert (dirs / "outputs" / "market" / "downloads.json").exists()
    assert (dirs / "outputs" / "keep.wav").exists()          # 未勾选的目标不动


def test_clean_rejects_protected(dirs):
    _put(dirs / "media" / "voicebank", "k/reference.wav", 999)
    res = storage.clean(["voicebank", "rvc_weights", "ghost"])
    assert res["removed_files"] == 0
    assert {s["key"] for s in res["skipped"]} == {"voicebank", "rvc_weights", "ghost"}


def test_clean_outputs_wav_also_drops_dead_history(dirs):
    wav = _put(dirs / "outputs", "tts_x.wav", 500)
    history.register("tts", "a", wav.name, "/x", 1.0)
    assert history.query()["total"] == 1
    res = storage.clean(["outputs_wav"])
    assert res["removed_files"] == 1
    assert history.query()["total"] == 0                     # 空链接记录同步摘掉


def test_clean_survives_unlinkable_file(dirs, monkeypatch):
    """某个文件删不掉（被占用）→ 记进 errors，其余照常。"""
    p = _put(dirs / "outputs" / "qc", "r.json", 100)
    real_unlink = Path.unlink

    def stubborn(self, missing_ok=False):
        if self == p:
            raise OSError("文件被占用")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stubborn)
    res = storage.clean(["qc_cache"])
    assert res["removed_files"] == 0 and res["errors"]
    assert res["errors"][0]["file"] == str(p)


def test_api_storage_and_clean(client, dirs):
    _put(dirs / "media" / "clips", "c1.wav", 123)
    r = client.get("/api/system/storage")
    assert r.status_code == 200
    body = r.json()
    assert {i["key"] for i in body["items"]} >= {"market_downloads", "clips", "voicebank"}
    assert body["disks"], "应报告所在盘剩余空间"

    r2 = client.post("/api/system/storage/clean", json={"targets": ["clips"]})
    assert r2.status_code == 200 and r2.json()["freed_bytes"] == 123
    assert client.post("/api/system/storage/clean", json={"targets": []}).status_code == 400

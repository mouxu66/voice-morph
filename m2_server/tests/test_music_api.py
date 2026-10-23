"""music_api 单测：榜单解析、搜索解析、取音边界与进度。

这些用例**不打真网络** —— 面板每打开一次就会打榜单，如果测试也打平台，
CI 会被 B站 412/抖音风控拖成随机红。平台字段用实测抓下来的最小样本。
"""

import sys
import wave
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import music_api  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    """榜单缓存是模块级的，用例之间必须清，否则后一个用例看不到自己的桩。"""
    music_api._CACHE.clear()
    yield
    music_api._CACHE.clear()


def _stub_get(monkeypatch, payload):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(payload)

    monkeypatch.setattr(music_api.requests, "get", fake_get)
    return calls


def test_douyin_chart_parses_title_and_hot(monkeypatch):
    _stub_get(monkeypatch, {
        "music_list": [
            {
                "hot_value": 12345,
                "music_info": {
                    "title": "晴天",
                    "author": "周杰伦",
                    "cover_thumb": {"url_list": ["https://p3.douyinpic.com/a.jpg"]},
                },
            },
            {"hot_value": 1, "music_info": {"title": "", "author": "无题"}},
        ],
    })
    out = music_api.charts(source="douyin")
    assert out["source"] == "douyin"
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["title"] == "晴天"
    assert item["artist"] == "周杰伦"
    assert item["hot"] == 12345
    assert item["rank"] == 1
    assert item["source"] == "douyin"


def test_bilibili_rank_parses_owner_and_skips_short(monkeypatch):
    _stub_get(monkeypatch, {
        "data": {
            "list": [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": "【无损】晴天 - 周杰伦",
                    "duration": 269,
                    "owner": {"name": "音乐UP"},
                    "stat": {"view": 987654},
                    "pic": "//i0.hdslb.com/cover.jpg",
                },
                {"bvid": "BV1short001", "title": "10 秒预告", "duration": 10},
            ],
        },
    })
    out = music_api.charts(source="bilibili")
    assert out["source"] == "bilibili"
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["bvid"] == "BV1xx411c7mD"
    assert item["artist"] == "音乐UP"
    assert item["duration_s"] == 269.0
    assert item["cover"].startswith("https://")
    assert item["url"].endswith("/BV1xx411c7mD")


def test_bilibili_search_strips_em_and_parses_clock(monkeypatch):
    _stub_get(monkeypatch, {
        "data": {
            "result": [
                {
                    "type": "video",
                    "bvid": "BV1yy411c7mE",
                    "title": "<em class=\"keyword\">晴天</em> 周杰伦 完整版",
                    "duration": "4:12",
                    "author": "某UP",
                    "play": 4321,
                    "pic": "//i0.hdslb.com/s.jpg",
                },
                {"type": "article", "title": "不是视频"},
            ],
        },
    })
    out = music_api.search(q="晴天", limit=8)
    assert out["query"] == "晴天"
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["title"] == "晴天 周杰伦 完整版"
    assert item["duration_s"] == 252.0
    assert item["hot"] == 4321


def test_search_rejects_blank_query():
    with pytest.raises(HTTPException) as exc:
        music_api.search(q="   ")
    assert exc.value.status_code == 400


def test_chart_failure_is_502_with_human_text(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise music_api.requests.ConnectionError("connection reset")

    monkeypatch.setattr(music_api.requests, "get", boom)
    with pytest.raises(HTTPException) as exc:
        music_api.charts(source="douyin")
    assert exc.value.status_code == 502
    assert "抖音热歌榜拉不到" in exc.value.detail


def test_chart_cache_only_hits_network_once(monkeypatch):
    calls = _stub_get(monkeypatch, {"music_list": [{"music_info": {"title": "A"}}]})
    first = music_api.charts(source="douyin")
    first["items"][0]["title"] = "被前端改掉了"
    second = music_api.charts(source="douyin")
    assert len(calls) == 1
    assert second["items"][0]["title"] == "A"


def test_fetch_only_allows_bilibili():
    with pytest.raises(HTTPException) as exc:
        music_api._assert_bilibili("https://y.qq.com/n/ryqq/songDetail/123")
    assert exc.value.status_code == 400
    music_api._assert_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
    music_api._assert_bilibili("https://b23.tv/abc123")


def test_fetch_returns_name_and_duration(tmp_path, monkeypatch):
    wav = tmp_path / "song.wav"
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00\x00" * 3200)  # 0.2s

    def fake_fetch(url, job=None):
        return {
            "name": "ytdlp_1.mp3",
            "path": str(wav),
            "url": "/media/session/ytdlp_1.mp3",
            "source_url": url,
        }

    monkeypatch.setattr(music_api.ytdlp_fetch, "fetch", fake_fetch)
    out = music_api.fetch(music_api.FetchReq(
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        title="<em>晴天</em>",
        artist="周杰伦",
    ))
    assert out["ok"] is True
    assert out["name"] == "ytdlp_1.mp3"
    assert out["duration_s"] == 0.2
    assert out["title"] == "晴天"


def test_fetch_progress_tracks_job_and_disappears_after_forget():
    job = music_api.ytdlp_fetch.new_job("pet-test-job-1")
    job.note(stage="下载中", percent=42.0)
    try:
        out = music_api.fetch_progress("pet-test-job-1")
        assert out["found"] is True
        assert out["stage"] == "下载中"
        assert out["percent"] == 42.0
    finally:
        music_api.ytdlp_fetch.forget(job)
    assert music_api.fetch_progress("pet-test-job-1")["found"] is False

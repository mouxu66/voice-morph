"""特效声板后端单测（`soundboard.py` + `board_worker.py` 的协议契约）。

不打真声卡：conftest 把 `VM_SOUNDBOARD=0`（`_spawn_worker` 直接拒播），用例要跑
"播放成功"路径就把 `soundboard._spawn_worker` 换成假 worker —— 假 worker 走**同一套
行协议**（一命令一响应），所以它验证的是真的协议契约，不是自说自话。

这里刻意不 mock `_command`：那会把"命令怎么发、响应怎么读"整段跳过，
而这正是本模块最容易写错的地方（响应错位 = 静默错发，见 `board_worker.py` 头注释）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import soundboard  # noqa: E402

SR = 48000


# --------------------------------------------------------------- 假 worker


class FakeWorker:
    """按真协议应答的假 worker：`{"wav"/"id"}` → playing，`{"stop"}` → stopped。"""

    def __init__(self, err: str | None = None):
        self.commands: list[dict] = []
        self._out: list[str] = []
        self.err = err
        self.alive = True
        # `_command` 走的是 proc.stdin / proc.stdout 这两个流对象，所以假 worker
        # 把自身同时当成两条流（协议一样即可，不必真造两根管道）。
        self.stdin = self
        self.stdout = self

    # --- stdin 侧 ---
    def write(self, s: str) -> None:
        cmd = json.loads(s)
        self.commands.append(cmd)
        if self.err:
            self._out.append(json.dumps({"type": "error", "msg": self.err}))
        elif cmd.get("stop"):
            self._out.append(json.dumps({"type": "stopped"}))
        else:
            self._out.append(
                json.dumps({"type": "playing", "id": cmd.get("id"), "duration_s": 1.4})
            )

    def flush(self) -> None: ...

    def close(self) -> None:
        self.alive = False

    # --- stdout 侧 ---
    def readline(self) -> str:
        return self._out.pop(0) + "\n" if self._out else ""

    def poll(self):
        return None if self.alive else 1

    def kill(self) -> None:
        self.alive = False


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """素材目录、计数文件、worker 全按用例隔离（不许写进真 media/outputs，也不许连真设备）。"""
    monkeypatch.setattr(soundboard, "IMPORT_DIR", tmp_path / "imported")
    monkeypatch.setattr(soundboard, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(soundboard, "_proc", None)
    yield
    monkeypatch.setattr(soundboard, "_proc", None)


@pytest.fixture()
def worker(monkeypatch):
    """把 `_spawn_worker` 换成假 worker（返回同一个实例，便于断言收到的命令）。"""
    w = FakeWorker()
    monkeypatch.setattr(soundboard, "_spawn_worker", lambda: w)
    return w


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(soundboard.router)
    return TestClient(app, raise_server_exceptions=False)


def _wav_bytes(seconds: float = 0.4, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    sf.write(buf, (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr, format="WAV")
    return buf.getvalue()


# --------------------------------------------------------------- catalog


def test_catalog_lists_the_six_factory_samples(client):
    items = client.get("/api/soundboard/catalog").json()["items"]
    assert {i["id"] for i in items} == {"boom", "applause", "alarm", "riser", "ding", "weird"}
    for it in items:
        assert it["builtin"] is True
        assert 0.5 <= it["duration_s"] <= 2.0
        assert it["name"] and it["tags"]
        assert it["count"] == 0
        # 目录枚举**不给文件系统信息**（路径拼接在服务端，前端只拿 id）
        assert set(it) == {"id", "name", "tags", "duration_s", "count", "builtin"}


def test_factory_manifest_matches_sample_files():
    """`samples/manifest.json` 与 wav 一一对应 —— 防「加了音效忘了写名字」这类漂移。"""
    wavs = {p.stem for p in soundboard.SAMPLES_DIR.glob("*.wav")}
    meta = soundboard._meta()
    assert wavs, "出厂素材目录是空的？"
    assert set(meta) == wavs, f"manifest 与 wav 不一致：manifest={sorted(meta)} wav={sorted(wavs)}"
    for sid, m in meta.items():
        assert m["name"], f"{sid} 没有显示名"
        assert isinstance(m["tags"], list)


# --------------------------------------------------------------- play / stop


def test_play_sends_the_resolved_wav_and_returns_immediately(client, worker):
    r = client.post("/api/soundboard/play", json={"id": "boom"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "playing": True, "id": "boom", "duration_s": 1.4}
    assert len(worker.commands) == 1
    cmd = worker.commands[0]
    # 发过去的是**服务端解析出的绝对路径**（前端永远不传路径）+ 默认音量
    assert Path(cmd["wav"]) == (soundboard.SAMPLES_DIR / "boom.wav").resolve()
    assert cmd["gain"] == 0.9


def test_play_passes_explicit_gain(client, worker):
    assert client.post("/api/soundboard/play", json={"id": "ding", "gain": 0.4}).status_code == 200
    assert worker.commands[0]["gain"] == 0.4


def test_play_counts_uses_per_sample(client, worker):
    client.post("/api/soundboard/play", json={"id": "boom"})
    client.post("/api/soundboard/play", json={"id": "boom"})
    client.post("/api/soundboard/play", json={"id": "ding"})
    counts = {i["id"]: i["count"] for i in client.get("/api/soundboard/catalog").json()["items"]}
    assert counts["boom"] == 2 and counts["ding"] == 1 and counts["applause"] == 0


def test_play_unknown_id_is_404(client, worker):
    r = client.post("/api/soundboard/play", json={"id": "nope"})
    assert r.status_code == 404
    assert "没有这个音效" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["../boom", "..\\boom", "sub/boom", "sub\\boom", ".", ".."])
def test_play_rejects_traversal_ids(client, worker, bad):
    r = client.post("/api/soundboard/play", json={"id": bad})
    assert r.status_code in (400, 404)
    assert worker.commands == [], "非法 id 不该把任何命令发到播放器"


def test_play_reports_503_when_player_cannot_start(client, monkeypatch):
    def _boom():
        raise RuntimeError("找不到 RVC venv 解释器: D:/RVC/.venv/Scripts/python.exe")

    monkeypatch.setattr(soundboard, "_spawn_worker", _boom)
    r = client.post("/api/soundboard/play", json={"id": "boom"})
    assert r.status_code == 503
    assert "RVC venv" in r.json()["detail"]


def test_play_surfaces_worker_error_as_503(client, monkeypatch):
    monkeypatch.setattr(soundboard, "_spawn_worker", lambda: FakeWorker(err="device_not_found:x"))
    r = client.post("/api/soundboard/play", json={"id": "boom"})
    assert r.status_code == 503
    assert "device_not_found" in r.json()["detail"]


def test_stop_returns_ok(client, worker):
    assert client.post("/api/soundboard/stop").json() == {"ok": True}
    assert worker.commands == [{"stop": True}]


def test_warm_starts_the_worker(client, worker):
    r = client.post("/api/soundboard/warm")
    assert r.json()["ok"] is True and r.json()["ready"] is True


def test_play_is_refused_in_the_test_env_by_default(client):
    """conftest 的 `VM_SOUNDBOARD=0` 是真防线：没有假 worker 时**不许**碰真设备。

    这条用例的意义不在覆盖代码，而在**证明那道安全网还在** —— 哪天有人把 conftest
    里那行删掉，这里会红，而不是某个用例悄悄往用户的虚拟声卡里放了一声爆炸。
    """
    import os

    assert os.environ.get("VM_SOUNDBOARD") == "0", "conftest 的测试禁播开关丢了？"
    r = client.post("/api/soundboard/play", json={"id": "boom"})
    assert r.status_code == 503
    assert "禁用" in r.json()["detail"]


# --------------------------------------------------------------- import / delete


def test_import_accepts_a_short_wav_and_lists_it_as_user_sample(client):
    r = client.post(
        "/api/soundboard/import",
        files={"file": ("我的音效.wav", _wav_bytes(0.4), "audio/wav")},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert (soundboard.IMPORT_DIR / f"{sid}.wav").is_file()
    items = {i["id"]: i for i in client.get("/api/soundboard/catalog").json()["items"]}
    assert items[sid]["builtin"] is False and items[sid]["tags"] == ["导入"]


def test_import_rejects_too_long(client):
    r = client.post("/api/soundboard/import", files={"file": ("long.wav", _wav_bytes(6.0), "audio/wav")})
    assert r.status_code == 400
    assert "太长" in r.json()["detail"]


def test_import_rejects_too_big(client):
    big = b"RIFF" + b"\0" * (3 * 1024 * 1024)
    r = client.post("/api/soundboard/import", files={"file": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413


def test_import_rejects_empty_and_broken(client):
    assert client.post("/api/soundboard/import", files={"file": ("e.wav", b"", "audio/wav")}).status_code == 400
    r = client.post("/api/soundboard/import", files={"file": ("b.wav", b"not audio", "audio/wav")})
    assert r.status_code == 400


def test_import_rejects_name_that_shadows_a_factory_sample(client):
    """重名直接拒 —— 比"谁覆盖谁"这种隐式规则好查。"""
    r = client.post("/api/soundboard/import", files={"file": ("boom.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]


def test_import_strips_directory_components(client):
    """`../../boom2.wav` → 只留 `boom2`，且**只能**落在导入目录里。"""
    r = client.post(
        "/api/soundboard/import",
        files={"file": ("../../boom2.wav", _wav_bytes(), "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "boom2"
    assert (soundboard.IMPORT_DIR / "boom2.wav").is_file()
    assert not (soundboard.IMPORT_DIR.parent.parent / "boom2.wav").exists()


def test_delete_removes_imported_but_not_factory(client):
    sid = client.post(
        "/api/soundboard/import", files={"file": ("tmp.wav", _wav_bytes(), "audio/wav")}
    ).json()["id"]
    assert client.delete(f"/api/soundboard/{sid}").json() == {"ok": True, "id": sid}
    assert not (soundboard.IMPORT_DIR / f"{sid}.wav").exists()
    r = client.delete("/api/soundboard/boom")
    assert r.status_code == 400
    assert "出厂音效不可删除" in r.json()["detail"]


# --------------------------------------------------------------- 设计不变量


def test_soundboard_never_touches_the_send_lock_or_the_wechat_module():
    """声板与微信发送链路的**唯一交点必须是共享声卡**（设计稿 §四第 3 条 / §七）。

    两件事一起守：
      1. 源码里不许出现 `_send_lock` —— 一旦声板去抢发送锁，"录制中点格子"就要
         干等 TTS 播完，用户主场景直接失效；
      2. `import soundboard` 不许把 `wechat_voice` 拉进进程 —— 那是全仓坑最密集的
         模块（犯错档案-微信.md 通篇），耦合它等于把声板拖进那堆坑，
         而且会连带拉起 UIA/Pillow/微信窗口那一串。
    """
    import ast

    tree = ast.parse(Path(soundboard.__file__).read_text(encoding="utf-8"))
    # 用 AST 而不是字符串搜索：模块头**故意**用散文解释了"为什么不进这把锁"，
    # 字符串匹配会把那段解释本身当成违规（第一版就是这么红的）。
    touched = [
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(tree)
        if isinstance(n, (ast.Name, ast.Attribute))
        and (n.id if isinstance(n, ast.Name) else n.attr) in ("_send_lock", "wechat_voice")
    ]
    assert touched == [], f"声板碰了发送锁/微信模块：{touched}"

    code = "import sys, soundboard; print('wechat_voice' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_M2),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(_M2), "PYTHONIOENCODING": "utf-8"},
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "False", "import soundboard 竟把 wechat_voice 拉进来了"


def test_router_mounts_under_the_expected_prefix():
    """前缀是前端 URL 与审计归属链的锚点（改前缀 = 前端全 404 + 归属失联）。"""
    assert soundboard.router.prefix == "/api/soundboard"


def test_endpoint_is_reachable_on_the_real_app(worker):
    """★ 用 TestClient 打**真 server.app**：这是「清单声明了但没挂上」的唯一防线。

    只测自己 include_router 的小 app 是不够的 —— 挂载是清单驱动的
    （`plugin_manifest.mount_plan()`），漏了 `soundboard` 的话本文件其他用例
    全绿而线上 404。
    """
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.get("/api/soundboard/catalog")
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 6
    assert c.post("/api/soundboard/play", json={"id": "ding"}).status_code == 200


# --------------------------------------------------------------- 真 worker（注入假声卡）
#
# 上面的假 worker 验的是**服务端一侧**的协议用法；`board_worker.py` 自己那一侧
# （预载、抢占、一行一响应、设备找不到时怎么报）在这里验 —— 把 `sounddevice`
# 换成桩再 import 它，既不打真设备，也不依赖 RVC venv（sounddevice 只在那边装）。


class _FakeSD:
    def __init__(self, devices=None):
        self.played: list[tuple] = []
        self.stopped = 0
        self.devices = devices if devices is not None else [
            {"hostapi": 0, "max_output_channels": 2, "name": "扬声器 (VB-Audio Virtual Cable)"}
        ]

    def query_hostapis(self):
        return [{"name": "MME"}]

    def query_devices(self):
        return self.devices

    def play(self, data, sr, device=None):
        self.played.append((data, sr, device))

    def stop(self):
        self.stopped += 1


def _run_board_worker(monkeypatch, tmp_path, commands: list[dict], sd: _FakeSD):
    """在假声卡 + 假 stdin/stdout 下跑一遍真 worker，返回（退出码, 输出行列表）。"""
    import importlib
    import json as _json
    import sys as _sys
    import types

    monkeypatch.setitem(_sys.modules, "sounddevice", types.SimpleNamespace(**{
        k: getattr(sd, k) for k in ("query_hostapis", "query_devices", "play", "stop")
    }))
    monkeypatch.setattr(
        _sys, "argv", ["board_worker.py", "vb-audio virtual cable", str(soundboard.SAMPLES_DIR)]
    )
    bw = importlib.import_module("board_worker")
    out = io.StringIO()
    monkeypatch.setattr(bw.sys, "stdin", io.StringIO("".join(_json.dumps(c) + "\n" for c in commands)))
    monkeypatch.setattr(bw.sys, "stdout", out)
    rc = bw.main()
    _sys.modules.pop("board_worker", None)  # argv/桩都变了，下次要重新 import
    return rc, [ln for ln in out.getvalue().splitlines() if ln.strip()]


def test_board_worker_preloads_and_plays_with_gain(monkeypatch, tmp_path):
    """预载 + 一行一响应 + 抢占（连点两下 = 先 stop 再 play）。"""
    sd = _FakeSD()
    rc, lines = _run_board_worker(
        monkeypatch,
        tmp_path,
        [{"wav": "x", "id": "boom", "gain": 0.5}, {"stop": True}],
        sd,
    )
    assert rc == 0
    msgs = [json.loads(ln) for ln in lines]
    assert msgs[0]["type"] == "ready" and msgs[0]["samples"] == 6, msgs[0]
    assert msgs[1] == {"type": "playing", "id": "boom", "duration_s": 1.4}
    assert msgs[2] == {"type": "stopped"}
    data, sr, dev = sd.played[0]
    assert sr == 48000 and dev == 0
    # 音量乘数真的生效了（0.5 倍 → 峰值 0.45）
    assert abs(float(np.max(np.abs(data))) - 0.45) < 0.02
    # 2 次 stop：① play 之前那次是**抢占**（连点两下 = 重新炸一次，不排队）；
    # ② 最后那次是 {"stop":true} 命令。1 次就说明抢占那次没发生。
    assert sd.stopped == 2


def test_board_worker_reports_missing_device_instead_of_crashing(monkeypatch, tmp_path):
    """找不到 CABLE 时报一行可读的 error 并退 1 —— 而不是堆栈。"""
    sd = _FakeSD(devices=[{"hostapi": 0, "max_output_channels": 2, "name": "扬声器 (Realtek)"}])
    rc, lines = _run_board_worker(monkeypatch, tmp_path, [], sd)
    assert rc == 1
    msg = json.loads(lines[0])
    assert msg["type"] == "error" and msg["msg"].startswith("device_not_found")


def test_board_worker_reports_unreadable_sample(monkeypatch, tmp_path):
    """一条坏素材不该拖垮整个声板：只回 error，后续命令照常处理。"""
    sd = _FakeSD()
    rc, lines = _run_board_worker(
        monkeypatch,
        tmp_path,
        [{"wav": str(tmp_path / "nope.wav"), "id": "nope"}, {"wav": "x", "id": "ding"}],
        sd,
    )
    assert rc == 0
    msgs = [json.loads(ln) for ln in lines[1:]]
    assert msgs[0]["type"] == "error" and "sample_unreadable" in msgs[0]["msg"]
    assert msgs[1]["type"] == "playing" and msgs[1]["id"] == "ding"

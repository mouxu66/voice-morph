"""RVC 常驻 worker（延迟优化）+ 启动预热的单测。

背景（2026-09-10 实测）：一次性 CLI 每条换声 20~25s（几乎全是模型加载），
常驻 worker 第二次起 0.3s。这些用例锁住协议与回退行为，**不得真起 RVC worker**
（会占 GPU 显存且慢），全部用假 Popen 走 JSON 行协议。

2026-09-13：`_fake_rvc_env` 让本文件与"本机是否装了 D:\\RVC"完全解耦
（此前两条回退用例在干净 CI runner 上因环境守卫直接红）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import rvc_convert as rc
import warmup as wm


class _FakeStream:
    def __init__(self, lines: list[str], write_log: list[str]):
        self._lines = list(lines)
        self._log = write_log
        self.closed = False

    def write(self, s: str) -> int:
        self._log.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    """模拟 RVC worker 子进程。

    ready=False（默认）= 处于「握手已完成」状态：stdout 流开头就是任务回复，
    因为真实的 _spawn_worker 已经把 {"ready":true} 那行读掉了。
    ready=True = 用于直接测 _spawn_worker 本身，需要它从 stdout 读到 ready 行。
    """

    def __init__(self, replies: list, ready: bool = False):
        self.written: list[str] = []
        lines = []
        if ready:
            lines.append(json.dumps({"ready": True, "pid": 1}) + "\n")
        lines += [json.dumps(r) + "\n" for r in replies]
        self.stdout = _FakeStream(lines, [])
        self.stdin = _FakeStream([], self.written)
        self.pid = 1
        self.returncode = 0
        self._poll = None

    def poll(self):
        return self._poll

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture(autouse=True)
def _no_real_worker(monkeypatch):
    """任何用例都不许真启动 RVC subprocess / worker。"""
    monkeypatch.setenv("VM_RVC_WORKER", "1")
    monkeypatch.setattr(rc, "_worker", None)
    yield
    monkeypatch.setattr(rc, "_worker", None)


@pytest.fixture(autouse=True)
def _fake_rvc_env(tmp_path, monkeypatch):
    """假装 RVC 运行环境（D:\\RVC\\.venv\\Scripts\\python.exe）存在。

    `rvc_convert()` 开头有一道人话守卫：

        if not RVC_VENV_PY.exists():
            raise RvcError(f"找不到 RVC 运行环境: {RVC_VENV_PY}")

    本文件所有用例都把 `subprocess` 换成了假实现，那个解释器**永远不会被执行**，
    所以只要让 `.exists()` 为真就够了 —— 用例随即与"本机有没有装 RVC"解耦。

    2026-09-13 CI 事故：干净 runner 上没有 D:\\RVC，两条"回退到一次性子进程"
    的用例直接在这道守卫上红了（报的是 RvcError，看着像生产代码坏了）。
    这类守卫该由生产代码保留（用户真缺 RVC 时要给人话），测试侧桩掉即可。
    """
    py = tmp_path / "rvc_venv_python.exe"
    py.write_bytes(b"")
    monkeypatch.setattr(rc, "RVC_VENV_PY", py)
    return py


# ---------------- worker 握手与协议 ----------------

def test_spawn_worker_reads_ready_line(monkeypatch):
    """worker 必须先打 {"ready":true} 才能收任务，否则调用方会停在握手上。"""
    fake = _FakeProc([], ready=True)
    monkeypatch.setattr(rc.subprocess, "Popen", lambda *a, **k: fake)
    proc = rc._spawn_worker()
    assert proc is fake


def test_spawn_worker_raises_when_silent_exit(monkeypatch):
    """worker 什么都没打印就退出（常见 import 失败）→ 必须报清楚，而不是挂死。"""
    fake = _FakeProc([])
    fake.stdout = _FakeStream([], [])
    fake.wait = lambda timeout=None: 1
    fake.returncode = 1
    monkeypatch.setattr(rc.subprocess, "Popen", lambda *a, **k: fake)
    with pytest.raises(rc.RvcError, match="启动失败"):
        rc._spawn_worker()


def test_worker_call_returns_result(monkeypatch):
    fake = _FakeProc([{"id": 1, "ok": True, "duration": 4.7, "convert_s": 0.3}])
    monkeypatch.setattr(rc, "_spawn_worker", lambda: fake)
    res = rc.worker_call({"cmd": "convert", "pth": "x.pth"})
    assert res["duration"] == 4.7
    task = json.loads(fake.written[0])
    assert task["cmd"] == "convert" and task["pth"] == "x.pth"


def test_worker_call_raises_on_task_error(monkeypatch):
    """worker 内部异常要给出来龙去脉（以前只能看到"转换失败"四个字）。"""
    fake = _FakeProc([{"id": 1, "ok": False, "error": "显存不足"}])
    monkeypatch.setattr(rc, "_spawn_worker", lambda: fake)
    with pytest.raises(rc.RvcError, match="显存不足"):
        rc.worker_call({"cmd": "convert"})


# ---------------- 崩溃重启 + 回退 ----------------

def test_worker_call_restarts_dead_worker_once(monkeypatch):
    """worker 崩了自动重启重试一次：显卡掉线/进程被杀不该让用户看到报错。"""
    calls = {"n": 0}

    def _spawn():
        calls["n"] += 1
        if calls["n"] == 1:
            p = _FakeProc([])
            p.stdin.write = _raise_break  # 模拟 broken pipe
            return p
        return _FakeProc([{"id": 1, "ok": True}])

    monkeypatch.setattr(rc, "_spawn_worker", _spawn)
    assert rc.worker_call({"cmd": "convert"})["ok"] is True
    assert calls["n"] == 2


def _raise_break(*a, **k):
    raise BrokenPipeError("worker died")


def test_worker_call_falls_back_to_oneshot(tmp_path, monkeypatch):
    """worker 彻底起不来时退回一次性子进程，整条发送链路不能因此失败。"""
    monkeypatch.setattr(rc, "_spawn_worker",
                        lambda: (_ for _ in ()).throw(rc.RvcError("起不来")))
    monkeypatch.setattr(rc, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(rc, "resolve_model", lambda v: (Path("x.pth"), None))
    src = tmp_path / "tts_1.wav"
    src.write_bytes(b"RIFF")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        # 一次性子进程会写出结果文件，这里模拟它
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_bytes(b"RIFF")

        class R:
            returncode = 0
            stdout = "OK"
            stderr = ""
        return R()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    out = rc.rvc_convert(src, "kangaroo_v2")   # 绝对路径，绕过 OUTPUTS_DIR 拼接
    assert out.name == "tts_1_kangaroo_v2.wav"
    # 退回路径仍必须 cwd=RVC 根（2026-09-10 修过一次这个坑）
    assert seen["cwd"] and "RVC" in str(seen["cwd"]).upper()


def test_worker_disabled_uses_oneshot_directly(tmp_path, monkeypatch):
    """VM_RVC_WORKER=0 → 压根不碰 worker（排查用的一次性链路）。"""
    monkeypatch.setattr(rc, "USE_WORKER", False)
    monkeypatch.setattr(rc, "_spawn_worker",
                        lambda: pytest.fail("关掉 worker 后不该再 spawn"))
    monkeypatch.setattr(rc, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(rc, "resolve_model", lambda v: (Path("x.pth"), None))
    src = tmp_path / "tts_1.wav"
    src.write_bytes(b"RIFF")

    def _run(cmd, **kw):
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_bytes(b"RIFF")

        class R:
            returncode = 0
            stdout = "OK"
            stderr = ""
        return R()

    monkeypatch.setattr(rc.subprocess, "run", _run)
    out = rc.rvc_convert(src, "kangaroo_v2")
    assert out.name.endswith("_kangaroo_v2.wav")


# ---------------- 预热 ----------------

def test_warmup_records_each_step(monkeypatch):
    """TTS 与 RVC 各自计时；任一步失败都要记进 steps，不抛出。"""
    monkeypatch.setattr(wm, "ENABLED", True)
    monkeypatch.setattr(wm, "_state",
                        {"running": False, "done": False, "steps": [],
                         "started_at": 0.0, "finished_at": 0.0, "error": ""})
    monkeypatch.setattr(wm, "_warm_tts", lambda: None)
    monkeypatch.setattr(wm, "_warm_rvc", lambda: None)
    monkeypatch.setattr(wm, "_warm_play_worker", lambda: None)   # 本测试只验 TTS/RVC 计时
    st = wm.run()
    assert [s["step"] for s in st["steps"]] == ["TTS worker", "RVC", "微信播放worker"]
    assert all(s["ok"] for s in st["steps"])


def test_warmup_one_step_failure_does_not_abort(monkeypatch):
    """预热失败只降级（第一下慢），绝不能把整个后端拖垮。"""
    monkeypatch.setattr(wm, "_state",
                        {"running": False, "done": False, "steps": [],
                         "started_at": 0.0, "finished_at": 0.0, "error": ""})
    monkeypatch.setattr(wm, "_warm_tts",
                        lambda: (_ for _ in ()).throw(RuntimeError("GPU 被占")))
    monkeypatch.setattr(wm, "_warm_rvc", lambda: None)
    monkeypatch.setattr(wm, "_warm_play_worker", lambda: None)
    st = wm.run()
    assert st["steps"][0]["ok"] is False and "GPU" in st["steps"][0]["detail"]
    assert st["steps"][1]["ok"] is True


def test_warmup_disabled_no_thread(monkeypatch):
    monkeypatch.setattr(wm, "ENABLED", False)
    started = []
    monkeypatch.setattr(wm.threading, "Thread",
                        lambda *a, **k: started.append(1))
    wm.start_background()
    assert started == []


def test_warmup_endpoint_exists():
    """桌偶/设置面板要能查"准备好了没"，路由别被悄悄改名。

    枚举 app.routes 会撞上 _IncludedRouter（挂载子应用时出现、无 .path 属性），
    故用真实请求确认：返回 200 且 body 是预热状态，而不是 404（漏注册/被改名）。
    """
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.get("/api/system/warmup")
    assert resp.status_code != 404
    body = resp.json()
    assert "running" in body and "steps" in body

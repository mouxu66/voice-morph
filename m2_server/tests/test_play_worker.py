"""微信播放常驻 worker（延迟优化）单测。

验证点：
- `_PlayWorkerHandle` 正确解析 worker 的行协议（playing/done/error）
- `_start_play` 优先用 worker、失败退回一次性子进程
- `_get_play_worker` 懒启动 + 复用 + 死亡后重启
- UIA 校验改为后台线程（不阻塞返回，结果写回历史）
- UI 准备与播放冷导入并行的分支不破坏 _do_send 契约
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wechat_voice as wv  # noqa: E402


# ---------------- 假 worker 进程 / 管道 ----------------

class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    def readline(self):
        if self._i < len(self._lines):
            l = self._lines[self._i]
            self._i += 1
            return l
        return ""


class _FakeStdin:
    def write(self, s):
        return None

    def flush(self):
        return None

    def close(self):
        return None


class _FakePopen:
    def __init__(self, stdout_lines, alive=True):
        self.stdout = _FakeStdout(stdout_lines)
        self.stdin = _FakeStdin()
        self._alive = alive
        self.killed = False

    def poll(self):
        return None if self._alive else 1

    def kill(self):
        self.killed = True
        self._alive = False


# ---------------- _PlayWorkerHandle 协议解析 ----------------

def test_handle_wait_play_start_recognizes_worker_playing():
    """worker 的 `{"type":"playing"}` 应被认作"已开始"（一次性子进程打 PLAYING 也兼容）。"""
    fake = _FakePopen(['{"type":"playing"}\n', '{"type":"done"}\n'])
    h = wv._PlayWorkerHandle(fake)
    assert wv._wait_play_start(h) is True


def test_handle_wait_play_done_returns_on_done():
    fake = _FakePopen(['{"type":"playing"}\n', '{"type":"done"}\n'])
    h = wv._PlayWorkerHandle(fake)
    assert h.wait(timeout=5) == 0


def test_handle_wait_play_done_raises_on_error():
    fake = _FakePopen(['{"type":"playing"}\n', '{"type":"error","msg":"boom"}\n'])
    h = wv._PlayWorkerHandle(fake)
    with pytest.raises(RuntimeError, match="boom"):
        h.wait(timeout=5)


def test_handle_wait_play_done_raises_on_worker_exit():
    """worker 提前退出（stdout 关闭）→ 等待超时/报错。"""
    fake = _FakePopen(['{"type":"playing"}\n'])   # 没有 done
    h = wv._PlayWorkerHandle(fake)
    with pytest.raises(RuntimeError):
        h.wait(timeout=1)


# ---------------- _start_play 派发 ----------------

def test_start_play_returns_handle_when_worker_available(monkeypatch):
    """worker 可用时 _start_play 返回 _PlayWorkerHandle（伪装成 Popen）。"""
    fake = _FakePopen(['{"type":"playing"}\n', '{"type":"done"}\n'])
    monkeypatch.setattr(wv, "_get_play_worker", lambda: fake)
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", True)
    h = wv._start_play(Path("x.wav"))
    assert isinstance(h, wv._PlayWorkerHandle)
    # 命令已写入 worker 的 stdin（这里 noop），且 wait 协议可用
    assert wv._wait_play_start(h) is True


def test_start_play_falls_back_to_oneshot_when_worker_disabled(monkeypatch):
    """VM_WECHAT_PLAY_WORKER=0 → 退回一次性子进程（_PlayWorkerHandle 不被用）。"""
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", False)
    called = {}
    monkeypatch.setattr(wv, "_start_play_oneshot",
                        lambda w: called.setdefault("oneshot", w) or _FakePopen(['PLAYING\n', 'DONE\n']))
    h = wv._start_play(Path("y.wav"))
    assert called.get("oneshot") == Path("y.wav")
    assert not isinstance(h, wv._PlayWorkerHandle)


def test_start_play_falls_back_when_worker_unavailable(monkeypatch):
    """_get_play_worker 返回 None（启动失败）→ 退回一次性，不抛。"""
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", True)
    monkeypatch.setattr(wv, "_get_play_worker", lambda: None)
    called = {}
    monkeypatch.setattr(wv, "_start_play_oneshot",
                        lambda w: called.setdefault("oneshot", w) or _FakePopen(['PLAYING\n', 'DONE\n']))
    wv._start_play(Path("z.wav"))
    assert called.get("oneshot") == Path("z.wav")


# ---------------- _get_play_worker 生命周期 ----------------

def test_get_play_worker_spawns_and_reuses(monkeypatch, tmp_path):
    (tmp_path / "fake_python.exe").write_bytes(b"MZ")
    (tmp_path / "play_worker.py").write_bytes(b"# dummy")
    calls = []

    def fake_popen(args, **kw):
        calls.append(args)
        return _FakePopen(['{"type":"ready"}\n'])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wv, "RVC_VENV_PY", tmp_path / "fake_python.exe")
    monkeypatch.setattr(wv, "PLAY_WORKER_SCRIPT", tmp_path / "play_worker.py")
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", True)
    wv._play_worker_proc = None
    try:
        p1 = wv._get_play_worker()
        assert p1 is not None
        p2 = wv._get_play_worker()
        assert p2 is p1                 # 复用，不二次 spawn
        assert len(calls) == 1
    finally:
        wv._play_worker_proc = None


def test_get_play_worker_restarts_when_dead(monkeypatch, tmp_path):
    (tmp_path / "fake_python.exe").write_bytes(b"MZ")
    (tmp_path / "play_worker.py").write_bytes(b"# dummy")
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (calls.append(a) or _FakePopen(['{"type":"ready"}\n'])))
    monkeypatch.setattr(wv, "RVC_VENV_PY", tmp_path / "fake_python.exe")
    monkeypatch.setattr(wv, "PLAY_WORKER_SCRIPT", tmp_path / "play_worker.py")
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", True)
    wv._play_worker_proc = None
    try:
        p1 = wv._get_play_worker()
        assert p1 is not None
        p1._alive = False               # 模拟 worker 进程退出
        p2 = wv._get_play_worker()
        assert p2 is not p1
        assert len(calls) == 2          # 重新 spawn
    finally:
        wv._play_worker_proc = None


def test_get_play_worker_returns_none_on_spawn_failure(monkeypatch, tmp_path):
    """worker 起不来（读不到 ready）→ 返回 None，调用方退回一次性。"""
    (tmp_path / "fake_python.exe").write_bytes(b"MZ")
    (tmp_path / "play_worker.py").write_bytes(b"# dummy")

    def fake_popen(*a, **k):
        return _FakePopen(['{"type":"error","msg":"no ready"}\n'])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wv, "RVC_VENV_PY", tmp_path / "fake_python.exe")
    monkeypatch.setattr(wv, "PLAY_WORKER_SCRIPT", tmp_path / "play_worker.py")
    monkeypatch.setattr(wv, "PLAY_WORKER_ENABLED", True)
    wv._play_worker_proc = None
    try:
        assert wv._get_play_worker() is None
    finally:
        wv._play_worker_proc = None


# ---------------- 后台校验 ----------------

def test_persist_verify_appends_to_last_history(monkeypatch, tmp_path):
    """后台校验线程把结果补写到最后一条历史。"""
    hist_file = tmp_path / "hist.json"
    hist_file.write_text(json.dumps([{"ts": 1, "outcome": "ok"}]), "utf-8")
    monkeypatch.setattr(wv, "HISTORY_FILE", hist_file)
    wv._persist_verify(['UIA 校验：最新语音 语音5"秒'])
    hist = json.loads(hist_file.read_text("utf-8"))  # 显式编码：GBK 中文 Windows 下默认编码会解码失败
    assert "verify" in hist[-1]
    assert "语音5" in hist[-1]["verify"][0]


def test_persist_verify_writes_injected_path_not_global(monkeypatch, tmp_path):
    """_persist_verify 只写调用方传入的路径，绝不碰全局 HISTORY_FILE。

    复刻 2026-09-18 的污染路径：后台线程在调用方作用域退出后才执行，那一刻全局
    HISTORY_FILE 已经是"另一个"文件。实测证据 —— 跑一遍 tests/ 就会往用户真实的
    outputs/wechat_send_history.json 里塞进一条 `tts_x.wav / 1.0s`，他在桌宠
    「最近发送」看到「1 分钟前 · 1s」，以为是自己刚发的（排查方向被带偏）。
    """
    global_file = tmp_path / "global.json"          # 假装是"真实"历史
    global_file.write_text(json.dumps([{"ts": 1, "outcome": "ok"}]), "utf-8")
    injected = tmp_path / "injected.json"           # 本次真正该写的地方
    injected.write_text(json.dumps([{"ts": 2, "outcome": "ok"}]), "utf-8")
    monkeypatch.setattr(wv, "HISTORY_FILE", global_file)

    steps = ['UIA 校验：最新语音 语音5"秒']
    wv._persist_verify(steps, injected)

    assert json.loads(injected.read_text("utf-8"))[-1]["verify"] == steps
    # 全局那份必须原封不动 —— 它就是用户的真实历史
    assert "verify" not in json.loads(global_file.read_text("utf-8"))[-1]


def test_do_send_captures_history_path_when_thread_starts(monkeypatch, tmp_path):
    """_do_send 起 UIA 校验线程时，历史路径必须在**那一刻**定下来。

    为什么单独测：`_uia_verify_sent` 要扫微信窗口（几百 ms~3s），lambda 体等它返回
    才执行 `_persist_verify`，那时 `_do_send` 早已返回、调用方作用域退出。这里让打桩
    的 `_uia_verify_sent` 在"线程执行中"把 HISTORY_FILE 换掉来复现该时序 ——
    实现若让线程体自己读全局，就会写进换掉后的文件（测试转红）。
    """
    import time

    hist = tmp_path / "wechat_send_history.json"    # 起线程时该写的文件
    other = tmp_path / "other.json"                 # 线程跑起来后被"切走"的全局
    monkeypatch.setattr(wv, "HISTORY_FILE", hist)
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)          # uia_active → 会起校验线程
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakePopen(['PLAYING\n', 'DONE\n']))
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_restore_async", lambda *a, **k: None)   # 本用例只看校验线程
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF")

    def _verify_after_scope_gone(before_msg, expect_s, before_count=-1):
        # 模拟"线程真正跑起来时，起线程那个作用域已经退出、HISTORY_FILE 已被换掉"
        wv.HISTORY_FILE = other
        return ['UIA 校验：最新语音 语音5"秒']

    monkeypatch.setattr(wv, "_uia_verify_sent", _verify_after_scope_gone)

    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"

    for _ in range(100):        # daemon 线程不能 join，轮询等它落盘
        try:
            if "verify" in json.loads(hist.read_text("utf-8"))[-1]:
                break
        except Exception:
            pass
        time.sleep(0.05)

    assert "verify" in json.loads(hist.read_text("utf-8"))[-1]   # 写进"起线程时"的路径
    assert not other.exists()                                    # 被换掉的路径不许被碰


# ---------------- 并行 UI 准备不破坏 _do_send 契约 ----------------

def test_do_send_overlap_ui_prep_still_sends(monkeypatch, tmp_path):
    """UI 准备与播放导入并行的重构后，_do_send 仍正常发出（outcome=ok）。"""
    import config as cfg
    pytest.importorskip("fastapi")
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)   # 不走真实微信/UIA
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakePopen(['PLAYING\n', 'DONE\n']))
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF")   # _do_send 要求文件存在
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"
    assert any("麦克风已切到 CABLE" in s for s in res["steps"])

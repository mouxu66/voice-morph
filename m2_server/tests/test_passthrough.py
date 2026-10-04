"""真声直讲（`passthrough.py` + `passthrough_worker.py`）的协议契约单测。

不打真声卡：conftest 把 `VM_PASSTHROUGH=0`（`_spawn_worker` 直接拒启），用例要跑
"启动成功"路径就把 `passthrough._spawn_worker` 换成假 worker —— 假 worker 走**同一套
行协议**（一命令一响应），所以它验证的是真的协议契约，不是自说自话。

这里刻意不 mock `_command`：那会把"命令怎么发、响应怎么读"整段跳过，
而这正是本模块最容易写错的地方（响应错位 = 静默错判"播完了"）。

三条防线：
  1. 路由：TestClient 挂**只含本 router** 的裸 app（不起整个 server）。
  2. 互斥：与 rvc_live / cascade 抢 CABLE → 409（照 `cascade.py:145` 的既有口径）。
  3. worker 真跑一遍（注入假 sounddevice）：设备解析、16-Ch 否决、**混音是叠加
     而不是替换**、`stop_clip` 不许掐断直通 —— 这几条都在假声卡上验。
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import passthrough  # noqa: E402

SR = 48000


# --------------------------------------------------------------- 假 worker


class FakeWorker:
    """按真协议应答的假 worker。

    与 `test_soundboard.FakeWorker` 的**关键差异**（照抄它会漏掉本模块的要点）：
      · `{"stop_clip":true}` → `stopped_clip`（**不是** `{"stop":true}`）；
      · 没有 `sd.play` 这一层 —— 所以"真声会不会被掐断"只能在真 worker 那一侧验
        （见文件末尾的 `_run_passthrough_worker`）。
    """

    def __init__(self, err: str | None = None, out_device: str = "扬声器 (VB-Audio Virtual Cable)"):
        self.commands: list[dict] = []
        self._out: list[str] = []
        self.err = err
        self.alive = True
        self.out_device = out_device
        self.stdin = self
        self.stdout = self

    # --- stdin 侧 ---
    def write(self, s: str) -> None:
        cmd = json.loads(s)
        self.commands.append(cmd)
        if self.err:
            self._out.append(json.dumps({"type": "error", "msg": self.err}))
        elif cmd.get("stop_clip"):
            self._out.append(json.dumps({"type": "stopped_clip"}))
        else:
            self._out.append(
                json.dumps({"type": "playing", "id": cmd.get("id"), "duration_s": 1.9})
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
def _isolated(monkeypatch):
    """worker 句柄、设备表、**以及 worker 模块缓存**按用例隔离。

    ⚠️ 必须清 `_DEVICES`：它是模块级 dict，`/status` 会读它 —— 上一条用例
    "启动成功"留下的设备名会让下一条"没启动时 device 应为空"的断言**假绿**。

    ⚠️ 必须清 `sys.modules["passthrough_worker"]`：见 `_import_worker` 的告警 ——
    留在缓存里的是"绑着上一条用例假声卡"的旧模块，下一条会静默拿到它。
    """
    monkeypatch.setattr(passthrough, "_proc", None)
    monkeypatch.setattr(passthrough, "_DEVICES", {})
    yield
    monkeypatch.setattr(passthrough, "_proc", None)
    monkeypatch.setattr(passthrough, "_DEVICES", {})
    sys.modules.pop("passthrough_worker", None)


@pytest.fixture()
def worker(monkeypatch):
    """假 worker。

    ⚠️ 假 `_spawn_worker` **必须自己写 `_DEVICES`** —— 真实现是在 `_spawn_worker`
    里从 ready 行解析设备名并落进 `_DEVICES` 的；只替换 `_spawn_worker` 而不补这步，
    `/status` 与 `/start` 的 `device` 会恒为空串（用例会红，但更好的是**别让它红着
    通过**：界面上那个设备名是老师确认"声音到底送去哪"的唯一依据）。
    """
    w = FakeWorker()

    def _spawn():
        passthrough._DEVICES["out"] = w.out_device
        passthrough._DEVICES["in"] = "麦克风阵列 (Senary Audio)"
        return w

    monkeypatch.setattr(passthrough, "_spawn_worker", _spawn)
    return w


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(passthrough.router)
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------- 路由契约


def test_router_mounts_under_the_expected_prefix():
    """前缀是前端 URL 与审计归属链的锚点（改前缀 = 前端全 404 + 归属失联）。"""
    assert passthrough.router.prefix == "/api/passthrough"


def test_status_is_not_running_before_start(client):
    b = client.get("/api/passthrough/status").json()
    assert b == {"ok": True, "running": False, "device": "", "input_device": ""}


def test_start_then_status_reports_device(client, worker):
    b = client.post("/api/passthrough/start").json()
    assert b == {"ok": True, "running": True, "device": worker.out_device}
    s = client.get("/api/passthrough/status").json()
    assert s["running"] is True and s["device"] == worker.out_device


def test_start_is_idempotent(client, worker):
    """连点两次「开启」不该起第二个 worker（两路直通会互相抢麦）。"""
    assert client.post("/api/passthrough/start").json()["running"] is True
    again = client.post("/api/passthrough/start").json()
    assert again == {"ok": True, "already_running": True, "device": worker.out_device}


def test_stop_returns_ok_and_clears_status(client, worker):
    client.post("/api/passthrough/start")
    assert client.post("/api/passthrough/stop").json() == {"ok": True, "running": False}
    s = client.get("/api/passthrough/status").json()
    assert s["running"] is False and s["device"] == ""


def test_stop_without_start_is_harmless(client):
    """没开过也能停（界面上"关了再关"不该报错）。"""
    assert client.post("/api/passthrough/stop").json() == {"ok": True, "running": False}


def test_start_reports_503_when_worker_cannot_start(client, monkeypatch):
    def _boom():
        raise RuntimeError("找不到 RVC venv 解释器: D:/RVC/.venv/Scripts/python.exe")

    monkeypatch.setattr(passthrough, "_spawn_worker", _boom)
    r = client.post("/api/passthrough/start")
    assert r.status_code == 503
    assert "RVC venv" in r.json()["detail"]


def test_start_refuses_in_the_test_env_by_default(client):
    """conftest 的 `VM_PASSTHROUGH=0` 是真防线：没有假 worker 时**不许**碰真麦克风。

    这条用例的意义不在覆盖代码，而在**证明那道安全网还在** —— 哪天有人把 conftest
    里那行删掉，这里会红，而不是某个用例悄悄把测试机的真声送进虚拟声卡
    （若那时正开着通话，就是直播事故）。与 `test_soundboard` 的同名守卫同构。
    """
    import os

    assert os.environ.get("VM_PASSTHROUGH") == "0", "conftest 的直通禁用开关丢了？"
    r = client.post("/api/passthrough/start")
    assert r.status_code == 503
    assert "禁用" in r.json()["detail"]


def test_start_accepts_empty_json_body(client, worker):
    """前端 `passthroughStart()` 发的是 `{}` 或空体，两种都得收（否则开关点了没反应）。"""
    assert client.post("/api/passthrough/start", json={}).status_code == 200
    client.post("/api/passthrough/stop")
    assert client.post("/api/passthrough/start").status_code == 200


# --------------------------------------------------------------- 互斥（三者抢 CABLE）


def test_start_conflicts_with_rvc_live(client, monkeypatch):
    monkeypatch.setattr("rvc_live._live_proc_alive", lambda: True)
    r = client.post("/api/passthrough/start")
    assert r.status_code == 409
    assert "实时变声" in r.json()["detail"]
    assert passthrough.is_running() is False, "冲突时不许已起 worker"


def test_start_conflicts_with_cascade(client, monkeypatch):
    monkeypatch.setattr("rvc_live._live_proc_alive", lambda: False)
    import cascade

    monkeypatch.setattr(cascade, "_cascade_alive", lambda: True)
    r = client.post("/api/passthrough/start")
    assert r.status_code == 409
    assert "级联" in r.json()["detail"]


def test_conflict_check_is_skipped_when_already_running(client, worker, monkeypatch):
    """已经在跑就早返回 —— 这时**不该**再去问 rvc_live（避免无谓导入与假红）。"""
    client.post("/api/passthrough/start")
    monkeypatch.setattr(
        "rvc_live._live_proc_alive",
        lambda: (_ for _ in ()).throw(AssertionError("已运行时不该再查互斥")),
    )
    assert client.post("/api/passthrough/start").status_code == 200


def test_dead_worker_is_reported_as_not_running(client, worker):
    """worker 崩了（poll() != None）→ `is_running()` 必须是 False。

    这是热键分流的判据：若崩了还报 True，热键会走 `play_overlay` →
    命令发不进管道 → 一按热键报 503，且**直通看起来还开着**（UI 骗人）。
    """
    client.post("/api/passthrough/start")
    worker.alive = False
    assert passthrough.is_running() is False
    assert client.get("/api/passthrough/status").json()["running"] is False


# --------------------------------------------------------------- play_overlay 协议


def test_play_overlay_sends_wav_id_and_gain(client, worker):
    client.post("/api/passthrough/start")
    msg = passthrough.play_overlay("C:/tmp/x.wav", "phrase_abc", gain=0.9)
    assert msg["type"] == "playing"
    cmd = worker.commands[-1]
    assert cmd == {"wav": "C:/tmp/x.wav", "id": "phrase_abc", "gain": 0.9}


def test_play_overlay_rejects_non_str_path():
    """路径按 `str()` 化：传 Path 也行（`voice_phrases` 那边就是 Path）。"""
    import inspect

    src = inspect.getsource(passthrough.play_overlay)
    assert "str(wav_path)" in src, "play_overlay 必须把 Path 转成 str 再下行"


def test_worker_error_is_raised_as_runtime_error(client, monkeypatch):
    w = FakeWorker(err="device_not_found:x")

    def _spawn():
        passthrough._DEVICES["out"] = w.out_device
        return w

    monkeypatch.setattr(passthrough, "_spawn_worker", _spawn)
    client.post("/api/passthrough/start")
    with pytest.raises(RuntimeError, match="device_not_found"):
        passthrough.play_overlay("x.wav", "id")


def test_dead_pipe_raises_a_distinct_error(client, worker):
    """管道关了（worker 退出）必须与"响应内容不对"分开报 —— 前者要重启。

    照 `soundboard._command` 的同一判据：`readline()` 回空串 = 管道关了。
    """
    client.post("/api/passthrough/start")
    worker._out.clear()
    worker.readline = lambda: ""  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="已退出"):
        passthrough.play_overlay("x.wav", "id")


# --------------------------------------------------------------- 真 worker（注入假声卡）
#
# 上面验的是**服务端一侧**的协议用法；worker 自己那一侧（设备解析、16-Ch 否决、
# 混音语义、stop_clip 的边界）在这里验 —— 把 `sounddevice` 换成桩再 import 它，
# 既不打真设备，也不依赖 RVC venv（sounddevice / soundfile 只在那边装 soundfile 有）。


class _FakeStream:
    """假流：把 callback 记下来，由用例手动喂输入 / 取输出（可控、可断言）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.callback = kw.get("callback")
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeSD:
    def __init__(self, devices=None):
        self.devices = devices if devices is not None else [
            {"hostapi": 0, "max_input_channels": 1, "max_output_channels": 0,
             "name": "麦克风阵列 (Senary Audio)"},
            {"hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
             "name": "扬声器 (VB-Audio Virtual Cable)"},
        ]
        self.streams: list[_FakeStream] = []

    def query_hostapis(self):
        return [{"name": "MME"}]

    def query_devices(self, idx=None):
        return self.devices[idx] if idx is not None else self.devices

    class default:  # noqa: N801 —— 模拟 sounddevice 的 `sd.default.device`
        device = (0, 0)

    def OutputStream(self, **kw):  # noqa: N802
        s = _FakeStream(**{**kw, "_kind": "OutputStream"})
        self.streams.append(s)
        return s

    def InputStream(self, **kw):  # noqa: N802
        s = _FakeStream(**{**kw, "_kind": "InputStream"})
        self.streams.append(s)
        return s


def _run_worker(
    monkeypatch,
    sd: _FakeSD,
    commands: list[dict],
    argv_extra: list[str] | None = None,
    after: dict[int, object] | None = None,
):
    """在假声卡 + 假 stdin/stdout 下跑一遍真 worker，返回（退出码, 输出行, sd）。

    `after={命令下标: 回调}` 用来在**某条命令处理完之后、下一条之前**观测状态
    （混音断言必须这样测：见 `_ScriptedStdin` 的说明）。
    回调收 `sd`，返回值忽略。

    `_FakeStream` 把两个 callback 留下来了 —— 回调里直接调 `_feed_mic(sd, ...)`
    就能"喂一块真声、取一轮输出"，从而断言**混音语义**（本模块最核心、
    也最容易写错的一条：叠加 vs 替换）。
    """
    import importlib

    _stub_sounddevice(monkeypatch, sd)
    argv = ["passthrough_worker.py", "vb-audio virtual cable"] + (argv_extra or [""])
    monkeypatch.setattr(sys, "argv", argv)
    pw = importlib.import_module("passthrough_worker")
    out = io.StringIO()

    hooks = after or {}

    def _hook(done: int):
        # 夹具侧 hook 收到的是"已完成 `done` 条"；用例写的是**命令下标** n
        # （`after={n: cb}` = "第 n 条处理完之后"），所以这里 +1 对齐。
        cb = hooks.get(done - 1)
        if cb is not None:
            cb(sd)

    monkeypatch.setattr(pw.sys, "stdin", _ScriptedStdin(commands, _hook if hooks else None))
    monkeypatch.setattr(pw.sys, "stdout", out)
    rc = pw.main()
    sys.modules.pop("passthrough_worker", None)  # argv/桩都变了，下次要重新 import
    return rc, [ln for ln in out.getvalue().splitlines() if ln.strip()], sd


class _ScriptedStdin:
    """假 stdin：逐条吐命令，**每条处理完之后**调一次 hook。

    ★ 为什么不能直接 `io.StringIO("".join(...))`：`main()` 会把 stdin 一次读完，
    所有命令都在**同一个** `for` 循环里连续处理 —— 于是"插话叠加期间"这个状态
    在 `main()` 返回后已经不存在了（后一条 `stop_clip` 早就把 clip 清空）。
    表现是插话断言恒读到纯真声（**看起来像实现没生效，其实是夹具太早把命令喂完**）。

    ★ hook 的时机是**"上一条处理完、下一条还没到"**：所以实现是
    `yield 第 i 条 → hook(i) → yield 第 i+1 条`。写在 yield 之前会变成
    "命令还没被 worker 读进去"就观测（实测读数恒为 0，像是增益没生效）。

    ★ 用**迭代器类**而不是 generator：`for raw in sys.stdin` 是惰性拉取，
    hook 在生成器帧里执行 —— 它内部任何 `next()` 抛的 `StopIteration` 会被
    CPython 的 PEP 479 转成 `RuntimeError: generator raised StopIteration`
    （实测：`_streams()` 找不到流时就是这个报错，且**看起来像 worker 的 bug**）。
    写成 `__next__` 的普通类，hook 就在调用方帧里跑，异常语义正常。
    """

    def __init__(self, commands: list[dict], hook=None):
        self._lines = [json.dumps(c) + "\n" for c in commands]
        self._hook = hook
        self._taken = 0  # worker 已取走的条数

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if not self._lines:
            # ★ 最后一条处理完时也要给一次观测机会（worker 马上会去关流）——
            #   否则"最后一条命令之后"的状态永远看不到（如 `stop_clip` 之后再喂真声）。
            if self._hook and not self._flushed:
                self._flushed = True
                self._hook(self._taken)
            raise StopIteration
        # hook 在**取第 i 条之前**触发，参数 i = "已完成 i 条"。
        # 于是 `after={0: cb}` 的语义就是「第 0 条处理完了、第 1 条还没到」——
        # 正是"插话已叠加、还没被 stop_clip 清掉"的观测窗口。
        if self._hook:
            self._hook(self._taken)
        ln = self._lines.pop(0)
        self._taken += 1
        return ln

    _flushed = False


def _streams(sd: _FakeSD):
    """(输入流, 输出流)。

    ⚠️ 按**建流顺序**取，不按 `device` 索引猜：两条流的 device 索引是设备表里的
    位置，用例自己造的表里输出流可能正好是 0 号 —— 按索引找会 StopIteration
    或找错（worker 里 `OutputStream` **先建**，第 261 行）。
    """
    out = next(s for s in sd.streams if "OutputStream" in s.kw.get("_kind", ""))
    inp = next(s for s in sd.streams if "InputStream" in s.kw.get("_kind", ""))
    return inp, out


def test_worker_emits_ready_with_both_device_names(monkeypatch):
    rc, lines, sd = _run_worker(monkeypatch, _FakeSD(), [])
    assert rc == 0
    msg = json.loads(lines[0])
    assert msg["type"] == "ready"
    # 两个设备名都要报出来：界面靠它显示"真声正送往哪"，空串等于界面说不出话
    assert msg["out_device"] == "扬声器 (VB-Audio Virtual Cable)"
    assert msg["in_device"] == "麦克风阵列 (Senary Audio)"
    # 两条流都真起了（只起一条 = 播不出去或收不到麦）
    assert all(s.started for s in sd.streams), "有流没 start"


def test_worker_reports_missing_output_device_instead_of_crashing(monkeypatch):
    """找不到 CABLE 时报一行可读的 error 并退 1 —— 而不是堆栈。"""
    sd = _FakeSD(devices=[
        {"hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "name": "麦克风 (Realtek)"},
        {"hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "name": "扬声器 (Realtek)"},
    ])
    rc, lines, _ = _run_worker(monkeypatch, sd, [])
    assert rc == 1
    msg = json.loads(lines[0])
    assert msg["type"] == "error" and msg["msg"].startswith("device_not_found")


def test_worker_never_picks_the_16ch_endpoint(monkeypatch):
    """★ 16-Ch 否决：`CABLE In 16 Ch` 是同一块声卡的**另一端点**，不在链路里。

    选中它 = 声音灌进没人听的端点，表现是"一切正常但对面听不到"（2026-09-19 事故）。
    这里把 16-Ch 排在**前面**，若否决失效就会选中它（索引 1 而不是 2）。
    """
    sd = _FakeSD(devices=[
        {"hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "name": "麦克风"},
        {"hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
         "name": "CABLE In 16 Ch (VB-Audio Virtual Cable)"},
        {"hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
         "name": "扬声器 (VB-Audio Virtual Cable)"},
    ])
    pw = _import_worker(monkeypatch, sd)
    assert pw._resolve_output("vb-audio virtual cable|cable input") == 2


@pytest.mark.parametrize(
    "dev,want,expect",
    [
        # 正常命中（含中英文两种系统命名）
        ("扬声器 (VB-Audio Virtual Cable)", "VB-Audio Virtual Cable|CABLE Input", True),
        ("CABLE Input (VB-Audio Virtual Cable)", "VB-Audio Virtual Cable|CABLE Input", True),
        # MME 截断到 31 字符：配置里写全名也要能匹配上（前缀比较那一支）
        ("CABLE Input (VB-Audio Virtual Ca", "VB-Audio Virtual Cable|CABLE Input", True),
        # 16-Ch 变体：两个方向都否决
        ("CABLE In 16 Ch (VB-Audio Virtual Cable)", "VB-Audio Virtual Cable|CABLE Input", False),
        ("CABLE In 16 Ch (VB-Audio Virtual Ca", "cable in 16 ch", True),
        # 空名 / 空关键词
        ("", "VB-Audio Virtual Cable", False),
        ("扬声器 (Realtek)", "", False),
        ("扬声器 (Realtek)", "VB-Audio Virtual Cable", False),
    ],
)
def test_device_matching_rules(monkeypatch, dev, want, expect):
    pw = _import_worker(monkeypatch, _FakeSD())
    assert pw._device_matches(dev, want) is expect


def test_worker_falls_back_to_default_input_when_name_unresolvable(monkeypatch):
    """父进程传的麦名解析不到（拔了/改名了）→ 退回系统默认，别让整条链路起不来。"""
    sd = _FakeSD(devices=[
        {"hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "name": "麦克风阵列 (Senary)"},
        {"hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
         "name": "扬声器 (VB-Audio Virtual Cable)"},
    ])
    pw = _import_worker(monkeypatch, sd)
    # 名字对不上 → 退回 default.device[0] = 0（走的是兜底分支，而不是匹配分支）
    assert pw._resolve_input("完全不存在的麦克风") == 0
    assert pw._resolve_input("麦克风阵列") == 0


def test_worker_resample_keeps_pitch_of_24k_source(monkeypatch):
    """TTS 产物常见 24k，库里是 48k —— 不重采样会"音调变尖/变慢"。"""
    pw = _import_worker(monkeypatch, _FakeSD())
    x = np.sin(2 * np.pi * 440 * np.arange(2400) / 24000).astype("float32")
    y = pw._resample(x, 24000, 48000)
    assert abs(len(y) - 4800) <= 1, f"24k→48k 长度不对：{len(y)}"


# --------------------------------------------------------------- 混音语义（★ 本模块的核）


def _feed_mic(sd, samples: np.ndarray) -> np.ndarray:
    """把 `samples` 当真声喂进输入回调，再调一次输出回调取结果。"""
    inp, out = _streams(sd)
    inp.callback(samples.reshape(-1, 1), len(samples), None, None)
    buf = np.zeros(len(samples), dtype="float32")
    out.callback(buf.reshape(-1, 1), len(samples), None, None)
    return buf


def test_overlay_is_added_on_top_of_the_live_mic_not_replacing_it(monkeypatch, tmp_path):
    """★★ 核心不变量：插话**叠加**在真声之上。

    这条正是本模块存在的理由 —— `board_worker` 那条路（`sd.play`）会把真声
    **整个掐断**，老师讲到一半按热键会静音。所以断言必须能同时看出"真声在"
    与"插话也在"：只断言"有声音"的话，替换式实现也会绿（**假绿**）。

    观测时机用 `after={0: ...}`：**第 0 条 wav 命令处理完之后、第 1 条
    `stop_clip` 之前** —— 否则 `stop_clip` 早把插话清空了。
    """
    mic = np.full(480, 0.3, dtype="float32")  # 真声：恒定 0.3
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.full(4800, 0.2, dtype="float32"), SR, subtype="FLOAT")

    seen: dict[str, np.ndarray] = {}
    _run_worker(
        monkeypatch,
        _FakeSD(),
        [{"wav": str(wav), "id": "phrase_x", "gain": 1.0}, {"stop_clip": True}],
        after={
            0: lambda sd: seen.__setitem__("mixed", _feed_mic(sd, mic)),
            1: lambda sd: seen.__setitem__("only_mic", _feed_mic(sd, mic)),
        },
    )

    # 叠加期间：真声 0.3 + 插话 0.2 = 0.5（既不是 0.3 也不是 0.2）
    assert np.allclose(seen["mixed"], 0.5, atol=1e-3), f"没叠加：{seen['mixed'][:5]}"
    # stop_clip 之后：只剩真声 0.3（**直通没被掐断** —— 这是与 soundboard 的分界）
    assert np.allclose(seen["only_mic"], 0.3, atol=1e-3), f"stop_clip 把直通也停了：{seen['only_mic'][:5]}"


def test_overlay_survives_and_keeps_flowing_after_the_clip_ends(monkeypatch, tmp_path):
    """插话播完后真声照常 —— 不能因为"播完了"就把输出流停掉。"""
    mic = np.full(480, 0.25, dtype="float32")
    wav = tmp_path / "short.wav"
    sf.write(str(wav), np.full(240, 0.2, dtype="float32"), SR, subtype="FLOAT")  # 5ms，比一块短

    seen: dict[str, np.ndarray] = {}
    _run_worker(
        monkeypatch,
        _FakeSD(),
        [{"wav": str(wav), "id": "x", "gain": 1.0}],
        after={0: lambda sd: (
            _feed_mic(sd, np.zeros(480, dtype="float32")),  # 第一块：插话 0.2
            seen.__setitem__("after", _feed_mic(sd, mic)),  # 第二块：插话已耗尽 → 纯真声
        )},
    )
    assert np.allclose(seen["after"], 0.25, atol=1e-3), f"插话播完后真声没跟上：{seen['after'][:5]}"


def test_overlay_gain_is_applied(monkeypatch, tmp_path):
    mic = np.zeros(480, dtype="float32")
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.full(4800, 0.8, dtype="float32"), SR, subtype="FLOAT")
    seen: dict[str, np.ndarray] = {}
    _run_worker(
        monkeypatch, _FakeSD(), [{"wav": str(wav), "id": "x", "gain": 0.5}],
        after={0: lambda sd: seen.__setitem__("v", _feed_mic(sd, mic))},
    )
    got = seen["v"]
    assert abs(float(got[0]) - 0.4) < 1e-3, f"增益没生效：{got[0]}"


def test_overlay_clips_instead_of_wrapping(monkeypatch, tmp_path):
    """两路相加超 ±1.0 → 硬限幅。不夹会**回绕**（爆响），比削波难听得多。"""
    mic = np.full(480, 0.9, dtype="float32")
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), np.full(4800, 0.9, dtype="float32"), SR, subtype="FLOAT")
    seen: dict[str, np.ndarray] = {}
    _run_worker(
        monkeypatch, _FakeSD(), [{"wav": str(wav), "id": "x", "gain": 1.0}],
        after={0: lambda sd: seen.__setitem__("v", _feed_mic(sd, mic))},
    )
    got = seen["v"]
    assert float(np.max(got)) <= 1.0 and float(np.min(got)) >= -1.0
    assert abs(float(got[0]) - 1.0) < 1e-6, f"没夹到 1.0：{got[0]}"


def test_overlay_preempts_the_previous_clip(monkeypatch, tmp_path):
    """连按两次热键 = 后一条接管（不排队、不叠两条）—— 与声板抢占语义一致。"""
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    sf.write(str(a), np.full(48000, 0.2, dtype="float32"), SR, subtype="FLOAT")
    sf.write(str(b), np.full(48000, 0.5, dtype="float32"), SR, subtype="FLOAT")
    seen: dict[str, np.ndarray] = {}
    _run_worker(
        monkeypatch, _FakeSD(), [{"wav": str(a), "id": "a"}, {"wav": str(b), "id": "b", "gain": 1.0}],
        after={1: lambda sd: seen.__setitem__("v", _feed_mic(sd, np.zeros(480, dtype="float32")))},
    )
    got = seen["v"]
    assert abs(float(got[0]) - 0.5) < 1e-3, f"没被后一条接管：{got[0]}"


def test_worker_reports_unreadable_sample_and_keeps_going(monkeypatch, tmp_path):
    """一条坏素材不该拖垮直通：只回 error，后续命令照常处理。"""
    good = tmp_path / "good.wav"
    sf.write(str(good), np.full(4800, 0.3, dtype="float32"), SR, subtype="FLOAT")
    sd = _FakeSD()
    rc, lines, _ = _run_worker(
        monkeypatch, sd,
        [{"wav": str(tmp_path / "nope.wav"), "id": "nope"}, {"wav": str(good), "id": "good"}],
    )
    assert rc == 0
    msgs = [json.loads(ln) for ln in lines[1:]]
    assert msgs[0]["type"] == "error" and "sample_unreadable" in msgs[0]["msg"]
    assert msgs[1]["type"] == "playing" and msgs[1]["id"] == "good"


def test_missing_wav_key_reports_error(monkeypatch):
    sd = _FakeSD()
    rc, lines, _ = _run_worker(monkeypatch, sd, [{"id": "no-wav"}])
    assert rc == 0
    assert json.loads(lines[1])["msg"] == "missing wav"


def test_stop_clip_emits_stopped_without_touching_the_stream(monkeypatch):
    """`stop_clip` 只清插话 —— 两条流必须仍然活着（这是整个功能的立足点）。

    ⚠️ 不能断言"退出时 `stopped` 是 False"：worker 的 `finally` 本来就会
    `stop()` + `close()` 两条流（那是正确清理，见 `test_streams_are_closed_on_stdin_eof`）。
    所以判据放在**观测时机**上：`stop_clip` 处理完之后立刻看，那时流必须还在跑。
    """
    seen: dict[str, bool] = {}

    def _observe(sd):
        seen["any_stopped"] = any(s.stopped for s in sd.streams)
        seen["all_started"] = all(s.started for s in sd.streams)

    rc, lines, _ = _run_worker(monkeypatch, _FakeSD(), [{"stop_clip": True}], after={0: _observe})
    assert rc == 0
    assert json.loads(lines[1]) == {"type": "stopped"}
    assert seen["all_started"] is True, "命令处理期间流没起来"
    assert seen["any_stopped"] is False, "stop_clip 竟然把流停了（真声会被掐断）"


def test_streams_are_closed_on_stdin_eof(monkeypatch):
    """父进程关管道（退出）→ worker 必须释放设备，否则下一轮起不来（设备被占）。"""
    sd = _FakeSD()
    rc, _, sd = _run_worker(monkeypatch, sd, [])
    assert rc == 0
    assert all(s.closed for s in sd.streams), "退出时没关流"


def test_worker_uses_no_sd_play_global_stop(monkeypatch):
    """★ 设计不变量：worker 源码里不许出现 `sd.play` / `sd.stop`。

    `sd.play`/`sd.stop` 是 sounddevice 的**全局单例** —— 用了它就必然掐断真声
    （`board_worker` 正是这么写的，所以它接不了本需求）。这条守卫让"哪天有人图省事
    改成 sd.play"当场变红，而不是等到老师讲课时才发现被静音。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_import_worker))
    src = Path(passthrough.__file__).parent.joinpath("passthrough_worker.py").read_text("utf-8")
    tree = ast.parse(src)
    bad = [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in ("play", "stop") and isinstance(n.value, ast.Name)
        and n.value.id == "sd"
    ]
    assert bad == [], f"直通 worker 用了全局单例播放接口：{bad}"


# --------------------------------------------------------------- 辅助


def _stub_sounddevice(monkeypatch, sd: _FakeSD, default_device=(0, 0)):
    """把 `sounddevice` 换成假货，返回假货本身。

    ★ 必须**在 import worker 之前**注入：worker 顶部是 `import sounddevice as sd`，
    是**导入时早绑定**的（本仓 §8.36 的同一个形状）—— 晚一步注入，worker 里那个
    `sd` 仍指向真模块，用例会在真设备表上跑（实测表现为返回值 7 / 1 之类的
    "不是这个表里的索引"，而不是报错）。
    """
    stub = types.SimpleNamespace(
        query_hostapis=sd.query_hostapis,
        query_devices=sd.query_devices,
        default=types.SimpleNamespace(device=default_device),
        OutputStream=sd.OutputStream,
        InputStream=sd.InputStream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", stub)
    return stub


def _import_worker(monkeypatch, sd: _FakeSD | None = None):
    """把 worker 模块 import 进来（只 import 不 main，验纯函数用）。

    ⚠️ 每次都**重 import**：`KEYWORD` / `WANT_INPUT` 是模块级常量，在 import 时
    从 `sys.argv` 早绑定 —— 复用上一次的模块会让"换个 argv 再验一次"变成验的还是
    旧值，**不报错、静默假绿**。

    ⚠️ **用完必须 pop**（见 `_drop_worker`）：本函数 import 出来的模块会把
    monkeypatch 注入的**假 `sounddevice`** 绑死在自己身上；用例结束后 monkeypatch
    还原了 `sys.modules["sounddevice"]`，但那份 `passthrough_worker` 模块仍留在
    `sys.modules` 里 → 下一条用例 `_run_worker` 时 `importlib.import_module`
    **直接命中缓存**，于是它拿到的是"绑着上一条用例的假声卡"的旧模块，
    表现为**看似随机的断言失败**（实测：`overlay` 用例报 `KeyError` /
    读到上一条的设备表）。这类"缓存命中旧模块"是纯函数用例污染函数用例的典型形状。
    """
    import importlib

    _drop_worker()
    monkeypatch.setattr(sys, "argv", ["passthrough_worker.py", "vb-audio virtual cable", ""])
    if sd is not None:
        _stub_sounddevice(monkeypatch, sd)
    return importlib.import_module("passthrough_worker")


def _drop_worker() -> None:
    """把 worker 模块从 `sys.modules` 摘掉（连同它的 import 期状态一起作废）。

    每次跑完真 worker 或验纯函数之后都调一次 —— 见 `_import_worker` 的告警。
    """
    sys.modules.pop("passthrough_worker", None)

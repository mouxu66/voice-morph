# -*- coding: utf-8 -*-
"""A7/A8 实时输入设备选择器 + 降噪开关测试。

覆盖两层：
  1. live_settings 纯函数：默认值/roundtrip/损坏文件回退/原子写
  2. rvc_live 新端点函数：设备校验（400 找不到设备）、成功写入、needs_restart
     （直接调用 FastAPI 端点函数，不起 app；mock 设备枚举与进程探测）
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import live_settings  # noqa: E402
import rvc_live  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """每个用例独立设置文件，避免污染真实 outputs/。"""
    monkeypatch.setattr(live_settings, "SETTINGS_PATH",
                        tmp_path / "live_settings.json")
    # rvc_live 持有的是模块引用，两个模块看到的是同一个 module 对象，
    # patch 模块属性即可（上面的 setattr 已经做到）。这里仅兜底确认。
    assert rvc_live.live_settings is live_settings
    yield


def test_defaults_when_missing():
    s = live_settings.get()
    assert s == {"input_device": "", "denoise": True, "perf_profile": "balanced"}


def test_roundtrip_update():
    live_settings.update(input_device="USB 麦克风", denoise=False)
    s = live_settings.get()
    assert s["input_device"] == "USB 麦克风"
    assert s["denoise"] is False
    # 部分更新：只改一个字段，另一个保持
    live_settings.update(denoise=True)
    s = live_settings.get()
    assert s["input_device"] == "USB 麦克风"
    assert s["denoise"] is True


def test_empty_input_device_means_follow_default():
    live_settings.update(input_device="  ")
    assert live_settings.get()["input_device"] == ""


def test_corrupt_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "live_settings.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(live_settings, "SETTINGS_PATH", p)
    assert live_settings.get() == {"input_device": "", "denoise": True, "perf_profile": "balanced"}
    # 保存覆盖坏文件后恢复正常
    live_settings.update(denoise=False)
    assert live_settings.get()["denoise"] is False


def test_get_returns_copy():
    s1 = live_settings.get()
    s1["denoise"] = False  # 改副本不影响存储
    assert live_settings.get()["denoise"] is True


# ---------------- 端点函数 ----------------

_DEVICES = [{"name": "麦克风阵列 (Senary Audio)", "is_default": True},
            {"name": "CABLE Output (VB-Audio Virtual C", "is_default": False}]


def test_get_endpoint_shape(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_audio_devices()
    assert r["ok"] is True
    assert r["items"] == _DEVICES
    assert r["explicit"] == ""
    assert r["denoise"] is True
    assert r["running"] is False


def test_post_unknown_device_400(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    from rvc_live import LiveDevicesPayload
    with pytest.raises(Exception) as ei:
        rvc_live.rvc_live_audio_devices_set(LiveDevicesPayload(input_device="不存在的麦"))
    assert "找不到输入设备" in str(getattr(ei.value, "detail", ei.value))


def test_post_save_device_and_denoise(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_audio_devices_set(
        rvc_live.LiveDevicesPayload(input_device="cable output", denoise=False))
    # 关键词大小写不敏感模糊匹配（MME 截断名也能对上）
    assert r["ok"] is True
    assert r["input_device"] == "cable output"
    assert r["denoise"] is False
    assert r["needs_restart"] is False
    # 真实落盘
    assert live_settings.get()["input_device"] == "cable output"


def test_post_running_needs_restart(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    r = rvc_live.rvc_live_audio_devices_set(
        rvc_live.LiveDevicesPayload(input_device="麦克风阵列"))
    assert r["running"] is True and r["needs_restart"] is True


def test_resolve_prefers_explicit_device(monkeypatch):
    """显式选择优先于系统默认：候选列表第一项必须是 live_settings 的值。"""
    live_settings.update(input_device="CABLE Output")
    seen = []

    def fake_default():
        seen.append("default")
        return "麦克风阵列 (Senary Audio)"

    monkeypatch.setattr(rvc_live, "_system_default_input", fake_default)

    # 捕获 candidates 顺序：模拟枚举结果命中显式选择（api 字段必须为 MME，
    # 与真实枚举一致——函数按 hostapi==MME 过滤）
    mme = [{"name": "麦克风阵列 (Senary Audio)", "api": "MME", "in": 2, "out": 0},
           {"name": "CABLE Output (VB-Audio Virtual C", "api": "MME", "in": 2, "out": 0},
           {"name": "CABLE Input (VB-Audio Virtual C", "api": "MME", "in": 0, "out": 2}]
    monkeypatch.setattr(rvc_live, "VENV_PY", Path("python"), raising=False)
    import subprocess as _sp

    class _R:
        stdout = __import__("json").dumps(mme) + "\n"
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    inp, _out = rvc_live._resolve_device_names()
    assert inp == "CABLE Output (VB-Audio Virtual C"


# ---------------- 性能档位（perf_profile） ----------------

def test_perf_default_balanced():
    assert live_settings.get()["perf_profile"] == "balanced"


def test_perf_roundtrip_switch():
    live_settings.update(perf_profile="game")
    assert live_settings.get()["perf_profile"] == "game"
    live_settings.update(perf_profile="balanced")
    assert live_settings.get()["perf_profile"] == "balanced"


def test_perf_invalid_profile_ignored():
    """非法档位一律忽略：不静默写坏值，也不覆盖已有合法档位。"""
    live_settings.update(perf_profile="ultra")
    assert live_settings.get()["perf_profile"] == "balanced"
    live_settings.update(perf_profile="game")
    live_settings.update(perf_profile="???")
    assert live_settings.get()["perf_profile"] == "game"


_GPU_SNAP = {"gpu_total_mb": 8192, "gpu_used_mb": 2048, "live_proc_vram_mb": 512}


def test_perf_get_endpoint(monkeypatch):
    monkeypatch.setattr(rvc_live, "_gpu_snapshot", lambda: dict(_GPU_SNAP))
    r = rvc_live.rvc_live_profile_get()
    assert r["ok"] is True
    assert r["profile"] == "balanced"
    assert r["profile_desc"] == "均衡·音质优先"
    assert r["gpu_total_mb"] == 8192
    assert r["gpu_used_mb"] == 2048
    assert r["live_proc_vram_mb"] == 512


def test_perf_set_valid_idle(monkeypatch):
    """变声未运行：只落盘，不触发重启。"""
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert r["ok"] is True
    assert r["profile"] == "game"
    assert r["restarted"] is False
    assert live_settings.get()["perf_profile"] == "game"


def test_perf_set_unknown_400():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="ultra"))
    assert ei.value.status_code == 400
    assert "未知性能档位" in str(ei.value.detail)


def test_perf_set_running_restarts_with_game_flags(monkeypatch):
    """变声运行中切换到 game：stop 后以同音色重启，monitor=False（自我监听关闭）。"""
    calls: list = []
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    monkeypatch.setattr(rvc_live, "_active_exp", lambda: "kangaroo")
    monkeypatch.setattr(rvc_live, "rvc_live_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(rvc_live, "rvc_live_start", lambda *a, **k: calls.append(k))
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert r["restarted"] is True
    assert live_settings.get()["perf_profile"] == "game"
    stop_call, start_kw = calls
    assert stop_call == "stop"
    assert start_kw == {"exp_name": "kangaroo", "monitor": False}


def test_perf_set_same_profile_does_not_restart(monkeypatch):
    """切回当前档位：幂等，不落盘不重启。"""
    live_settings.update(perf_profile="game")
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    monkeypatch.setattr(rvc_live, "rvc_live_stop", lambda: None)
    monkeypatch.setattr(rvc_live, "rvc_live_start", lambda *a, **k: None)
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert r["restarted"] is False


# ---------------- 游戏档卸载 Qwen3-TTS worker（2026-09-15） ----------------
# 核心诉求：边打游戏边变声时「只保留核心功能」—— RVC 实时推理仅占 ~1GB 显存，
# 而语音合成 worker 预热要占 ~4.8GB。切到 game 档把它杀卸载（模型文件本来就在
# 盘里 tts_models/，杀掉进程即释放显存，等于「放回硬盘」），切回再按需懒加载。


@pytest.fixture
def fake_tts_worker(monkeypatch):
    """把 qwen3_tts 生命周期函数换成可记账的假实现，断言卸载行为不碰真 worker。"""
    state = {"alive": False, "shutdown_calls": 0}

    def alive() -> bool:
        return state["alive"]

    def shutdown() -> None:
        state["shutdown_calls"] += 1
        state["alive"] = False

    monkeypatch.setattr(rvc_live.qwen3_tts, "worker_alive", alive)
    monkeypatch.setattr(rvc_live.qwen3_tts, "shutdown_worker", shutdown)
    return state


def test_game_profile_idle_unloads_worker(fake_tts_worker, monkeypatch):
    """未运行变声时切 game：落盘 + 立即卸载 worker（释放显存是即时效果）。"""
    fake_tts_worker["alive"] = True
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert r["profile"] == "game" and r["restarted"] is False
    assert fake_tts_worker["shutdown_calls"] == 1
    assert fake_tts_worker["alive"] is False
    assert r["tts_worker_alive"] is False
    assert r["tts_freed_mb"] == rvc_live.TTS_WORKER_VRAM_MB


def test_game_profile_running_unloads_worker(fake_tts_worker, monkeypatch):
    """变声运行中切 game：stop+start 之外还必须卸载 worker（跑游戏前要把显存让出来）。"""
    fake_tts_worker["alive"] = True
    calls = []
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    monkeypatch.setattr(rvc_live, "_active_exp", lambda: "kangaroo")
    monkeypatch.setattr(rvc_live, "rvc_live_stop", lambda: calls.append("stop"))
    monkeypatch.setattr(rvc_live, "rvc_live_start", lambda *a, **k: calls.append(k))
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert r["restarted"] is True
    assert fake_tts_worker["shutdown_calls"] == 1
    assert r["tts_freed_mb"] == rvc_live.TTS_WORKER_VRAM_MB


def test_balanced_switch_keeps_worker(fake_tts_worker, monkeypatch):
    """切回均衡档：不卸载也不主动拉起（懒加载，等真用上合成再起）。"""
    fake_tts_worker["alive"] = True
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="balanced"))
    assert r["profile"] == "balanced"
    assert fake_tts_worker["shutdown_calls"] == 0
    # 卸载逻辑不在 balanced 路径里，worker 维持原样
    assert fake_tts_worker["alive"] is True
    assert r["tts_freed_mb"] == 0


def test_worker_already_dead_no_repeat_shutdown(fake_tts_worker, monkeypatch):
    """worker 本就未驻留时切 game：不重复杀、不虚报释放量。"""
    fake_tts_worker["alive"] = False
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_profile_set(rvc_live.LiveProfilePayload(profile="game"))
    assert fake_tts_worker["shutdown_calls"] == 0
    assert r["tts_freed_mb"] == 0


def test_sync_helper_game_unloads_balanced_keeps(fake_tts_worker):
    """_sync_worker_for_profile：game 档卸载，balanced 档不动（rvc_live_start 共用）。"""
    fake_tts_worker["alive"] = True
    was_alive = rvc_live._sync_worker_for_profile("game")
    assert was_alive is True
    assert fake_tts_worker["shutdown_calls"] == 1
    # balanced：不杀
    rvc_live._sync_worker_for_profile("balanced")
    assert fake_tts_worker["shutdown_calls"] == 1


def test_profile_get_exposes_tts_worker_alive(fake_tts_worker, monkeypatch):
    """profile GET 带出引擎驻留状态，供前端展示「已卸载/仍在」。"""
    monkeypatch.setattr(rvc_live, "_gpu_snapshot", lambda: dict(_GPU_SNAP))
    fake_tts_worker["alive"] = True
    assert rvc_live.rvc_live_profile_get()["tts_worker_alive"] is True
    fake_tts_worker["alive"] = False
    assert rvc_live.rvc_live_profile_get()["tts_worker_alive"] is False


# ---------------- 启动前显存余量预检（2026-09-15，压测 OOM 崩溃的预防） ----------------
# 压测实测：余量不足 ~2GB 时 RVC 推理进程高负载下 OOM 崩溃。与其跑到一半崩，
# 不如启动前预检拒绝并指路（game 档先卸载 worker 释放 4.8GB 再判余量）。


def _patch_gpu_payload(monkeypatch, total, used, profile="balanced"):
    live_settings.update(perf_profile=profile)
    monkeypatch.setattr(
        rvc_live, "_gpu_snapshot",
        lambda: {"gpu_total_mb": total, "gpu_used_mb": used, "live_proc_vram_mb": None})


def test_vram_precheck_blocks_when_free_too_low(monkeypatch):
    """余量不足安全线 → 返回提示文案（start 会以 409 拒绝启动）。"""
    _patch_gpu_payload(monkeypatch, 8192, 7400)   # free 792 < 2048
    msg = rvc_live._vram_precheck()
    assert msg is not None
    assert "显存余量" in msg and "游戏低占用" in msg


def test_vram_precheck_game_profile_message(monkeypatch):
    """game 档文案走「已卸载后仍不足」分支，不再让用户回头切档。"""
    _patch_gpu_payload(monkeypatch, 8192, 7400, profile="game")
    msg = rvc_live._vram_precheck()
    assert msg is not None
    assert "游戏低占用" not in msg


def test_vram_precheck_passes_when_enough_free(monkeypatch):
    _patch_gpu_payload(monkeypatch, 8192, 3000)   # free 5192 达标
    assert rvc_live._vram_precheck() is None


def test_vram_precheck_skips_when_no_gpu(monkeypatch):
    """nvidia-smi 不可用（无 total）→ 跳过预检，不阻断启动。"""
    _patch_gpu_payload(monkeypatch, None, None)
    assert rvc_live._vram_precheck() is None


def test_start_blocked_by_vram_precheck(monkeypatch, fake_tts_worker):
    """预检不过 → 409 拒绝启动（不切声卡、不拉起进程）。"""
    import sys as _sys
    import cascade
    from pathlib import Path as _Path
    calls = {"applied": 0, "audio": 0}
    monkeypatch.setattr(rvc_live, "_sync_worker_for_profile", lambda p: None)
    monkeypatch.setattr(rvc_live, "_vram_precheck", lambda: "显存余量仅 792 MB，低于安全阈值")
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    monkeypatch.setattr(cascade, "_cascade_alive", lambda: False)
    monkeypatch.setattr(rvc_live, "_model_status", lambda exp=None: True)
    monkeypatch.setattr(rvc_live, "RUNTIME_PY", _Path(_sys.executable))
    monkeypatch.setattr(rvc_live, "_apply_model_config",
                        lambda: calls.__setitem__("applied", 1) or True)
    monkeypatch.setattr(rvc_live, "_audio",
                        lambda action: calls.__setitem__("audio", calls["audio"] + 1) or {"ok": True})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        rvc_live.rvc_live_start(exp_name="kangaroo", monitor=False)
    assert ei.value.status_code == 409
    assert "显存余量" in str(ei.value.detail)
    assert calls["applied"] == 0 and calls["audio"] == 0


# ---------------- 游戏档降低显存探测频率（2026-09-14） ----------------
# _gpu_snapshot 一次要 spawn 3 个 nvidia-smi，status 被前端与桌宠同时轮询时
# 相当于游戏中每秒 1~2 次进程创建（抢 GPU 驱动 → 掉帧）。游戏档把 TTL 拉到 10s。

_EMPTY_GPU_CACHE = {"ts": 0.0, "used": None, "total": None, "proc": None}


def _patch_gpu_probe(monkeypatch):
    """把探测层换成计数器：_nvidia_smi 返回固定行，_find_realtime_pids 置空。

    返回 calls 列表，len(calls) 即「本轮起了几次 nvidia-smi」。
    """
    calls: list[str] = []

    def fake_smi(query):
        calls.append(query)
        return ["1234"]

    monkeypatch.setattr(rvc_live, "_nvidia_smi", fake_smi)
    monkeypatch.setattr(rvc_live, "_find_realtime_pids", lambda: [])
    monkeypatch.setattr(rvc_live, "_gpu_cache", dict(_EMPTY_GPU_CACHE))
    return calls


def test_gpu_probe_balanced_ttl_1s(monkeypatch):
    """均衡档：TTL 1s —— 2s 前的缓存已过期，第二次调用会重新探测（3 → 6 次）。"""
    calls = _patch_gpu_probe(monkeypatch)
    live_settings.update(perf_profile="balanced")

    rvc_live._gpu_snapshot()
    assert len(calls) == 3, "首次快照应三连查（used/total/pid）"

    rvc_live._gpu_cache["ts"] = time.time() - 2.0   # 伪造 2s 前的缓存
    rvc_live._gpu_snapshot()
    assert len(calls) == 6, "均衡档 TTL 1s，2s 前的缓存必须失效"


def test_gpu_probe_game_ttl_10s(monkeypatch):
    """游戏档：TTL 10s —— 2s 前的缓存仍命中（省掉进程创建），11s 前才重探。"""
    calls = _patch_gpu_probe(monkeypatch)
    live_settings.update(perf_profile="game")

    rvc_live._gpu_snapshot()
    assert len(calls) == 3

    rvc_live._gpu_cache["ts"] = time.time() - 2.0
    rvc_live._gpu_snapshot()
    assert len(calls) == 3, "游戏档 2s 前的缓存应命中，不再 spawn nvidia-smi"

    rvc_live._gpu_cache["ts"] = time.time() - 11.0
    rvc_live._gpu_snapshot()
    assert len(calls) == 6, "超过 10s 才该重探"


def test_gpu_ttl_by_profile(monkeypatch):
    live_settings.update(perf_profile="balanced")
    assert rvc_live._gpu_ttl() == rvc_live._GPU_TTL_BALANCED
    live_settings.update(perf_profile="game")
    assert rvc_live._gpu_ttl() == rvc_live._GPU_TTL_GAME


def test_gpu_ttl_falls_back_on_broken_settings(monkeypatch):
    """设置读取异常时按均衡档：宁可多探测，也不让显存条长时间停更。"""

    def boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(live_settings, "get", boom)
    assert rvc_live._gpu_ttl() == rvc_live._GPU_TTL_BALANCED


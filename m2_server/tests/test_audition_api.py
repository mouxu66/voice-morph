"""试音间（audition_api）：备选音色/身体解析、任务状态机、取消、互斥与边界。

全部离线完成：RVC 推理与客观打分都被打桩（`_try_one` / `_score_batch`），
不碰真实 GPU，不发任何网络请求。真机跑推理属于手工冒烟，不在单测里。

两个「必须回归」的真实故障（2026-09-18 首次真机冒烟踩到）：
  1. 打分器在主进程加载会占住 CUDA 上下文 → 后续 RVC 子进程 cuDNN 崩
     → 所以推理与打分必须是两个阶段（`test_score_runs_after_release_*`）。
  2. RVC 子进程报错时，torch 的弃用警告会把真错误挤出 tail，用户看到假原因
     → `_fail_tail` 的过滤（见 test_ab_chain.py）。
"""

import contextlib
import subprocess
import threading
import time
from pathlib import Path

import audition_api as fa
import numpy as np
import pytest
import runtime
import soundfile as sf
from runtime import EXCLUSIVE_TASKS, gpu_holder_reason, hold_gpu, release_gpu


@pytest.fixture(autouse=True)
def _clean_gpu_registry():
    """独占位是模块级全局，测试之间必须清干净（否则后面全被 409 挡住）。"""
    with runtime._EXCLUSIVE_LOCK:
        EXCLUSIVE_TASKS.clear()
    yield
    with runtime._EXCLUSIVE_LOCK:
        EXCLUSIVE_TASKS.clear()


def _reset_state():
    fa._CANCEL.clear()
    if fa._TASK_LOCK.locked():
        with contextlib.suppress(RuntimeError):
            fa._TASK_LOCK.release()
    with fa._STATE_LOCK:
        fa.AUDITION_STATE.update(
            task_id="",
            running=False,
            status="idle",
            mode="",
            total=0,
            finished=0,
            current="",
            current_name="",
            message="",
            error="",
            source_name="",
            text="",
            results=[],
            scoring=False,
            score_finished=0,
            score_total=0,
        )


@pytest.fixture()
def aud_dir(tmp_path, monkeypatch):
    """隔离 AUDITION_DIR，并把任务状态机复位到 idle。"""
    d = tmp_path / "audition"
    d.mkdir(parents=True)
    monkeypatch.setattr(fa, "AUDITION_DIR", d)
    monkeypatch.setattr(fa, "SRC_HARD_CAP", 5)
    monkeypatch.setattr(fa, "SRC_KEEP", 2)
    _reset_state()
    release_gpu("audition")
    yield d
    _reset_state()


def _wav(path, seconds: float = 2.0, sr: int = 16000):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), (0.4 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)
    return path


def _wait_idle(timeout: float = 10.0):
    end = time.time() + timeout
    while time.time() < end:
        if not fa._snapshot().get("running"):
            return fa._snapshot()
        time.sleep(0.02)
    raise AssertionError(f"任务没有在 {timeout}s 内结束：{fa._snapshot()}")


def _stub_infer(
    monkeypatch,
    *,
    failing: set[str] | None = None,
    handle=None,
    cached: bool = False,
    gate: threading.Event | None = None,
):
    """打桩「一件」的试音：写个真 wav 出来，按需失败/阻塞/报告命中缓存。"""
    failing = failing or set()

    def _try_one(voice_id, mode, src, text, pitch, index_rate):
        if handle:
            handle(voice_id, mode, src, text, pitch, index_rate)
        if gate is not None:
            gate.wait(3)
        if voice_id in failing:
            raise RuntimeError(f"音色 [{voice_id}] 没有可推理的模型")
        out = fa.AUDITION_DIR / f"aud_{voice_id}_stub.wav"
        _wav(out)
        return out, cached, ("cache" if cached else "worker")

    monkeypatch.setattr(fa, "_try_one", _try_one)


def _stub_history(monkeypatch):
    import history

    monkeypatch.setattr(history, "register", lambda *a, **k: "x")


# ---------------- runtime 独占位 ----------------


def test_hold_gpu_is_exclusive():
    assert hold_gpu("audition", "试音间正在批量试音") is True
    assert hold_gpu("other", "别的任务") is False  # 已被占就抢不到
    assert gpu_holder_reason() == "试音间正在批量试音"
    release_gpu("audition")
    assert gpu_holder_reason() == ""
    assert hold_gpu("other", "别的任务") is True  # 释放后可用


def test_release_gpu_is_idempotent():
    release_gpu("never-held")  # 不该抛
    assert gpu_holder_reason() == ""


def test_hold_gpu_reason_falls_back_to_name():
    hold_gpu("audition", "")
    assert gpu_holder_reason() == "audition 正在运行"


# ---------------- 备选音色解析 ----------------


def test_display_name_prefers_market_manifest():
    # 市场清单里的中文名优先（市场/音色库/试音间三个页面必须叫同一个名字）
    assert fa._display_name("katoong_lanyangyang") != "katoong_lanyangyang"


def test_display_name_falls_back_to_id_for_unknown():
    assert fa._display_name("no_such_voice_zzz") == "no_such_voice_zzz"


def test_voicebank_for_maps_rvc_exp_back_to_bank():
    """RVC 实验名 → voicebank id 的反向求交（`kangaroo` ↔ `kangaroo_v2`）。"""
    got = fa._voicebank_for("kangaroo_v2")
    if got is None:
        pytest.skip("本机 media/voicebank 下没有 kangaroo（裸 runner）")
    assert got == "kangaroo"


def test_voicebank_for_market_voice_is_none():
    """市场 RVC 权重没有 voicebank 目录 → 文字路径对它不成立。"""
    assert fa._voicebank_for("katoong_manbo") is None


def test_ref_for_returns_none_without_reference():
    assert fa._ref_for("katoong_manbo") is None


# ---------------- 权重解析 ----------------


def test_resolve_weight_unknown_voice_reports_reason(monkeypatch):
    monkeypatch.setattr(fa, "ensure_infer_pth", lambda _v: None)
    monkeypatch.setattr(fa, "_find_pth", lambda _v: None)
    monkeypatch.setattr(fa, "find_manifest_item", lambda _v: None)
    pth, index, err = fa._resolve_weight("ghost_voice")
    assert pth is None and index == ""
    assert "ghost_voice" in err and "不在音色市场清单里" in err


def test_resolve_weight_downloads_when_in_manifest(tmp_path, monkeypatch):
    """未安装但在市场清单里 → 走下载缓存（与市场试听共用同一份）。"""
    staged = tmp_path / "stage" / "katoong_manbo.pth"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"\x00" * 8)
    monkeypatch.setattr(fa, "_find_pth", lambda _v: None)
    monkeypatch.setattr(fa, "ensure_infer_pth", lambda _v: None)
    monkeypatch.setattr(
        fa, "find_manifest_item", lambda v: {"id": v, "download": {"url": "https://x/y.pth"}}
    )
    monkeypatch.setattr(fa, "_ensure_staged", lambda _v, _dl: staged)
    pth, index, err = fa._resolve_weight("katoong_manbo")
    assert pth == staged
    assert index == ""  # 暂存权重无 index → 显式跳过检索
    assert err == ""


def test_resolve_weight_download_failure_is_readable(monkeypatch):
    monkeypatch.setattr(fa, "_find_pth", lambda _v: None)
    monkeypatch.setattr(fa, "ensure_infer_pth", lambda _v: None)
    monkeypatch.setattr(
        fa, "find_manifest_item", lambda v: {"id": v, "download": {"url": "https://x/y.pth"}}
    )

    def _boom(_v, _dl):
        raise RuntimeError("网络断了")

    monkeypatch.setattr(fa, "_ensure_staged", _boom)
    pth, _index, err = fa._resolve_weight("katoong_manbo")
    assert pth is None
    assert "权重下载失败" in err and "网络断了" in err


def test_resolve_weight_manifest_without_download_slot(monkeypatch):
    monkeypatch.setattr(fa, "_find_pth", lambda _v: None)
    monkeypatch.setattr(fa, "ensure_infer_pth", lambda _v: None)
    monkeypatch.setattr(fa, "find_manifest_item", lambda v: {"id": v})
    pth, _index, err = fa._resolve_weight("katoong_manbo")
    assert pth is None and "缺少下载直链" in err


# ---------------- 缓存路径 ----------------


def test_cache_path_is_deterministic_and_param_sensitive():
    src = fa.AUDITION_DIR / "src_1.wav"
    a = fa._cache_path("v1", "audio", src, "", 0, 0.5)
    assert fa._cache_path("v1", "audio", src, "", 0, 0.5) == a
    assert fa._cache_path("v1", "audio", src, "", 12, 0.5) != a  # pitch 变了
    assert fa._cache_path("v2", "audio", src, "", 0, 0.5) != a  # 音色变了
    assert fa._cache_path("v1", "text", None, "你好", 0, 0.5) != a  # 模式变了


def test_duration_s_reads_header(aud_dir):
    assert fa._duration_s(_wav(fa.AUDITION_DIR / "a.wav", seconds=3.0)) == 3.0
    assert fa._duration_s(fa.AUDITION_DIR / "missing.wav") is None


# ---------------- 客观分（子进程） ----------------


def _jobs():
    return [{"key": "v1", "wav": "a.wav", "ref": ""}, {"key": "v2", "wav": "b.wav", "ref": ""}]


def test_score_batch_empty_is_noop():
    assert fa._score_batch([]) == {}


def test_score_batch_timeout_marks_every_key(aud_dir, monkeypatch):
    """打分超时不能让试音失败 —— 只给每个 key 一条可读原因。"""

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(fa.subprocess, "run", _timeout)
    res = fa._score_batch(_jobs())
    assert set(res) == {"v1", "v2"}
    assert all("超时" in r["score_error"] for r in res.values())
    assert all(r["secs"] is None and r["nats"] is None for r in res.values())


def test_score_batch_missing_output_file(aud_dir, monkeypatch):
    class _R:
        returncode = 0
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **k: _R())
    res = fa._score_batch(_jobs())
    assert all("未产出结果" in r["score_error"] for r in res.values())


def test_score_batch_reads_json_output(aud_dir, monkeypatch):
    payload = {
        "v1": {"secs": 0.66, "nats": 2.9, "score_error": ""},
        "v2": {"secs": None, "nats": None, "score_error": "自然度打分失败：缺模型"},
    }

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **_k):
        # cmd = [py, score.py, --jobs, X, --out, Y]
        out = cmd[cmd.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            import json

            json.dump(payload, f)
        return _R()

    monkeypatch.setattr(fa.subprocess, "run", _run)
    assert fa._score_batch(_jobs()) == payload


def test_score_batch_cleans_temp_files(aud_dir, monkeypatch):
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **k: _R())
    fa._score_batch(_jobs())
    assert list(fa.AUDITION_DIR.glob(".score_*")) == []


def test_audition_score_cli_writes_scores(tmp_path, monkeypatch):
    """打分脚本本身：一进一出，任何一把尺子失败都只记 score_error。"""
    import sys

    sys.path.insert(0, str(fa.SCORE_PY.parent))
    import audition_score as fs

    wav = _wav(tmp_path / "w.wav")
    ref = _wav(tmp_path / "ref.wav")
    monkeypatch.setattr(fs, "_secs", lambda _w, _r: 0.5)

    class _N:
        def score(self, _p):
            return 1.25

    monkeypatch.setattr(fs, "_nats_scorer", lambda: _N())
    one = fs.score_one(wav, ref)
    assert one == {"secs": 0.5, "nats": 1.25, "score_error": ""}
    # 没参考音 → 只算自然度，secs 保持 None（绝不拿输出自比出假 1.0）
    two = fs.score_one(wav, None)
    assert two["secs"] is None and two["nats"] == 1.25


def test_audition_score_cli_offline_by_default():
    """必须默认离线：否则 transformers 会去 ping 远端权重新鲜度（实测白等 2 分钟）。"""
    import os
    import sys

    sys.path.insert(0, str(fa.SCORE_PY.parent))
    import audition_score  # noqa: F401  导入期就该设好

    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"


# ---------------- 环境态势 ----------------


def test_env_shape(monkeypatch):
    env = fa.audition_env()  # 只调一次：每次探测要起 PowerShell 查进程，很贵
    for key in (
        "live_running",
        "cascade_running",
        "offline_running",
        "tts_worker",
        "gpu_total_mb",
        "gpu_used_mb",
        "gpu_free_mb",
        "min_free_vram_mb",
        "low_vram",
        "busy_reason",
        "batch_ready",
        "text_ready",
    ):
        assert key in env, f"env 缺字段 {key}"
    assert env["batch_ready"] is True


def test_env_reports_busy_when_gpu_held():
    hold_gpu("audition", "试音间正在批量试音")
    env = fa.audition_env()
    assert env["batch_ready"] is False
    assert env["busy_reason"] == "试音间正在批量试音"


def test_env_reports_low_vram(monkeypatch):
    import rvc_live

    monkeypatch.setattr(
        rvc_live,
        "_gpu_snapshot",
        lambda: {"gpu_total_mb": 8000, "gpu_used_mb": 7000, "live_proc_vram_mb": 0},
    )
    monkeypatch.setattr(rvc_live, "MIN_LIVE_FREE_VRAM_MB", 2048)
    env = fa.audition_env()
    assert env["gpu_free_mb"] == 1000
    assert env["low_vram"] is True
    assert env["batch_ready"] is False


# ---------------- 源音频 ----------------


def test_sources_empty_initially(aud_dir):
    assert fa.audition_sources()["sources"] == []


def test_source_delete_rejects_builtin(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_source_delete("src_builtin")
    assert ei.value.status_code == 400


def test_source_delete_rejects_bad_id(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_source_delete("../etc/passwd")
    assert ei.value.status_code == 400


def test_source_delete_missing_returns_404(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_source_delete("src_404")
    assert ei.value.status_code == 404


def test_source_delete_ok(aud_dir):
    _wav(fa.AUDITION_DIR / "src_111.wav")
    assert fa.audition_source_delete("src_111")["ok"] is True
    assert fa.audition_sources()["sources"] == []


def test_score_pass_does_not_write_into_next_task(aud_dir, monkeypatch):
    """回归：第一轮的分数不能盖进紧接着开的第二轮。

    打分在独占位释放后异步跑，所以"跑着分数、用户又点了开始"是可能的。
    没有任务号闸门的话，新一轮会显示上一轮的分（数字还是"成功"的，最难发现）。
    """
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)

    def _score_batch(jobs):
        # 模拟「打分进行中，用户开了下一轮」：任务号与结果列表都已被换掉
        with fa._STATE_LOCK:
            fa.AUDITION_STATE["task_id"] = "aud_next"
            fa.AUDITION_STATE["results"] = []
        return {j["key"]: {"secs": 9.9, "nats": 9.9, "score_error": ""} for j in jobs}

    monkeypatch.setattr(fa, "_score_batch", _score_batch)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=True))
    _wait_idle()
    time.sleep(0.4)
    assert fa._snapshot()["results"] == []  # 旧分没被塞进新一轮


def test_source_delete_blocked_while_in_use(aud_dir):
    _wav(fa.AUDITION_DIR / "src_222.wav")
    # 保护看的是 source_name + running，**不是** current（那是音色 id）
    with fa._STATE_LOCK:
        fa.AUDITION_STATE["source_name"] = "src_222.wav"
        fa.AUDITION_STATE["running"] = True
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_source_delete("src_222")
    assert ei.value.status_code == 409


def test_active_source_ignores_stale_name_when_idle(aud_dir):
    """任务已经结束 → 残留的 source_name 不该继续锁住删除。"""
    _wav(fa.AUDITION_DIR / "src_333.wav")
    with fa._STATE_LOCK:
        fa.AUDITION_STATE["source_name"] = "src_333.wav"
        fa.AUDITION_STATE["running"] = False
    assert fa._active_source_id() == ""
    assert fa.audition_source_delete("src_333")["ok"] is True


def test_prune_sources_keeps_recent_and_skips_active(aud_dir):
    import os

    for i in range(8):
        p = _wav(fa.AUDITION_DIR / f"src_{i}.wav")
        os.utime(p, (1000 + i, 1000 + i))
    with fa._STATE_LOCK:
        fa.AUDITION_STATE["source_name"] = "src_0.wav"
        fa.AUDITION_STATE["running"] = True
    fa._prune_sources()
    left = {p.stem for p in fa.AUDITION_DIR.glob("src_*.wav")}
    assert "src_0" in left  # 正在用，绝不删
    assert "src_7" in left and "src_6" in left  # 最近的留着
    assert len(left) <= fa.SRC_HARD_CAP


# ---------------- try 的参数校验 ----------------


def test_try_rejects_empty_voice_list(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=[]))
    assert ei.value.status_code == 400


def test_try_rejects_invalid_voice_id(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["../../etc"], source_id="src_1"))
    assert ei.value.status_code == 400


def test_try_rejects_missing_source(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_nope"))
    assert ei.value.status_code == 404


def test_try_rejects_bad_source_id(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="../../x"))
    assert ei.value.status_code == 400


def test_try_requires_text_or_source(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], text=""))
    assert ei.value.status_code == 400


def test_try_rejects_overlong_text(aud_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], text="啊" * 201))
    assert ei.value.status_code == 400


def test_try_rejects_out_of_range_params(aud_dir):
    from fastapi import HTTPException

    _wav(fa.AUDITION_DIR / "src_1.wav")
    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", index_rate=2))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", pitch=99))
    assert ei.value.status_code == 400


def test_try_409_when_gpu_held(aud_dir):
    from fastapi import HTTPException

    _wav(fa.AUDITION_DIR / "src_1.wav")
    hold_gpu("other", "离线变声任务正在运行")
    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1"))
    assert ei.value.status_code == 409
    assert "离线变声任务正在运行" in ei.value.detail


def test_try_409_when_live_running(aud_dir, monkeypatch):
    import rvc_live
    from fastapi import HTTPException

    _wav(fa.AUDITION_DIR / "src_1.wav")
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1"))
    assert ei.value.status_code == 409
    assert "实时变声正在运行" in ei.value.detail


# ---------------- 任务生命周期 ----------------


def test_try_runs_to_completion_and_releases_resources(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    fa.audition_try(
        fa.TryRequest(voice_ids=["kangaroo_v2", "katoong_manbo"], source_id="src_1", score=False)
    )
    st = _wait_idle()
    assert st["status"] == "done"
    assert st["finished"] == 2
    assert [r["status"] for r in st["results"]] == ["done", "done"]
    assert all(r["url"].startswith("/api/media/outputs/") for r in st["results"])
    assert all(r["duration_s"] == 2.0 for r in st["results"])
    # 独占位必须释放，否则后面的任务全被 409 挡住
    assert gpu_holder_reason() == ""
    assert fa._TASK_LOCK.locked() is False


def test_try_dedupes_but_keeps_order(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    fa.audition_try(
        fa.TryRequest(
            voice_ids=["kangaroo_v2", "kangaroo_v2", "katoong_manbo"],
            source_id="src_1",
            score=False,
        )
    )
    st = _wait_idle()
    assert st["total"] == 2
    assert [r["voice_id"] for r in st["results"]] == ["kangaroo_v2", "katoong_manbo"]


def test_try_second_call_409_while_running(aud_dir, monkeypatch):
    from fastapi import HTTPException

    _wav(fa.AUDITION_DIR / "src_1.wav")
    gate = threading.Event()
    _stub_infer(monkeypatch, gate=gate)
    _stub_history(monkeypatch)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    with pytest.raises(HTTPException) as ei:
        fa.audition_try(fa.TryRequest(voice_ids=["v2"], source_id="src_1", score=False))
    assert ei.value.status_code == 409
    gate.set()
    _wait_idle()


def test_per_voice_failure_does_not_abort_others(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch, failing={"bad_voice"})
    _stub_history(monkeypatch)
    fa.audition_try(
        fa.TryRequest(voice_ids=["bad_voice", "good_voice"], source_id="src_1", score=False)
    )
    st = _wait_idle()
    by_id = {r["voice_id"]: r for r in st["results"]}
    assert by_id["bad_voice"]["status"] == "failed"
    assert "没有可推理的模型" in by_id["bad_voice"]["error"]
    assert by_id["good_voice"]["status"] == "done"
    assert st["status"] == "done"  # 单件失败不把整个任务标为 error


def test_cancel_marks_status_cancelled(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    gate = threading.Event()
    _stub_infer(monkeypatch, gate=gate)
    _stub_history(monkeypatch)
    fa.audition_try(fa.TryRequest(voice_ids=["v1", "v2"], source_id="src_1", score=False))
    assert fa.audition_cancel()["cancelled"] is True
    gate.set()
    st = _wait_idle()
    assert st["status"] == "cancelled"
    assert gpu_holder_reason() == ""  # 取消也必须释放独占位


def test_cancel_without_task_is_noop(aud_dir):
    r = fa.audition_cancel()
    assert r["ok"] is True and r["cancelled"] is False


def test_cache_hit_is_reported(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch, cached=True)
    _stub_history(monkeypatch)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    assert _wait_idle()["results"][0]["from_cache"] is True


# ---------------- 打分阶段与推理阶段必须分开 ----------------


def test_score_runs_after_lock_released(aud_dir, monkeypatch):
    """回归：打分必须在独占位释放之后单独跑，不能混在推理循环里。

    真机故障：打分器加载占住本进程 CUDA 上下文 → 下一件 RVC 子进程 cuDNN 崩。
    所以这里断言「推理全部完成且锁已释放」时打分还没开始。
    """
    _wav(fa.AUDITION_DIR / "src_1.wav")
    observed = {}

    def _on_convert(voice_id, *_a):
        observed["lock_held_during_convert"] = fa._TASK_LOCK.locked()

    _stub_infer(monkeypatch, handle=_on_convert)
    _stub_history(monkeypatch)

    def _score_batch_slow(jobs):
        observed["lock_held_when_scoring"] = fa._TASK_LOCK.locked()
        observed["gpu_held_when_scoring"] = gpu_holder_reason()
        observed["keys"] = [j["key"] for j in jobs]
        return {j["key"]: {"secs": 0.5, "nats": 1.0, "score_error": ""} for j in jobs}

    monkeypatch.setattr(fa, "_score_batch", _score_batch_slow)
    fa.audition_try(fa.TryRequest(voice_ids=["v1", "v2"], source_id="src_1", score=True))
    _wait_idle()
    for _ in range(200):  # 打分线程是异步的，等它跑完
        if not fa._snapshot().get("scoring"):
            break
        time.sleep(0.02)

    assert observed["lock_held_during_convert"] is True  # 推理时确实占着
    assert observed["lock_held_when_scoring"] is False  # 打分时已释放
    assert observed["gpu_held_when_scoring"] == ""
    assert observed["keys"] == ["v1", "v2"]
    st = fa._snapshot()
    assert [r["secs"] for r in st["results"]] == [0.5, 0.5]
    assert st["scoring"] is False


def test_score_disabled_skips_scoring_pass(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    called = {"n": 0}

    def _boom(jobs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(fa, "_score_batch", _boom)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    st = _wait_idle()
    time.sleep(0.2)
    assert called["n"] == 0
    assert st["results"][0]["nats"] is None  # 没算就明确是 None，不编造分数


def test_score_failure_keeps_results(aud_dir, monkeypatch):
    """打分整批失败也不能把已经出来的结果弄丢。"""
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    monkeypatch.setattr(
        fa,
        "_score_batch",
        lambda jobs: {
            j["key"]: {"secs": None, "nats": None, "score_error": "打分超时（>300s），已跳过"}
            for j in jobs
        },
    )
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=True))
    _wait_idle()
    for _ in range(200):
        if not fa._snapshot().get("scoring"):
            break
        time.sleep(0.02)
    r = fa._snapshot()["results"][0]
    assert r["status"] == "done" and r["url"]
    assert "打分超时" in r["score_error"]


def test_score_pass_survives_exception(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)

    def _boom(_jobs):
        raise RuntimeError("打分进程炸了")

    monkeypatch.setattr(fa, "_score_batch", _boom)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=True))
    _wait_idle()
    for _ in range(200):
        if not fa._snapshot().get("scoring"):
            break
        time.sleep(0.02)
    st = fa._snapshot()
    assert st["scoring"] is False
    assert st["results"][0]["status"] == "done"  # 结果不受影响


# ---------------- 文字模式 ----------------


def test_text_mode_without_reference_fails_with_clear_reason(aud_dir, monkeypatch):
    monkeypatch.setattr(fa, "_voicebank_for", lambda _v: None)
    with pytest.raises(RuntimeError) as ei:
        fa._try_one("katoong_manbo", "text", None, "你好", 0, 0.5)
    assert "没有参考音" in str(ei.value)
    assert "只能换音色" in str(ei.value)


def test_text_mode_synthesizes_via_tts(aud_dir, monkeypatch, tmp_path):
    """文字路径只做一件事：拿参考音调 TTS 并把字节落盘；同参数第二次走缓存。"""
    bank = tmp_path / "voicebank" / "kangaroo"
    ref = _wav(bank / "reference.wav")
    monkeypatch.setattr(fa, "_voicebank_for", lambda _v: "kangaroo")
    monkeypatch.setattr(fa.cfg, "MEDIA_DIR", tmp_path)

    import qwen3_tts

    payload = ref.read_bytes()
    monkeypatch.setattr(qwen3_tts, "tts", lambda *a, **k: payload)

    out, cached, engine = fa._try_one("kangaroo_v2", "text", None, "你好", 0, 0.5)
    assert out.exists() and out.stat().st_size == len(payload)
    assert cached is False and engine == "tts"

    called = {"n": 0}

    def _count(*a, **k):
        called["n"] += 1
        return payload

    monkeypatch.setattr(qwen3_tts, "tts", _count)
    _out2, cached2, engine2 = fa._try_one("kangaroo_v2", "text", None, "你好", 0, 0.5)
    assert cached2 is True and called["n"] == 0
    assert engine2 == "cache"


# ---------------- HTTP 层（路由注册 + 序列化）----------------
# 用 TestClient 打真实 app：这是"忘了 include_router"这类错误的唯一防线。
# 直接调函数测不出来 —— 函数好好的，但路由根本没挂上去。


@pytest.fixture()
def client():
    import server
    from fastapi.testclient import TestClient

    return TestClient(server.app, raise_server_exceptions=False)


def _all_paths(routes) -> set:
    """递归收集路由路径。

    不能直接 `{r.path for r in app.routes}`：本项目 include_router 后拿到的是
    FastAPI 的 `_IncludedRouter` 包装（没有 `.path`/`.routes`，只有
    `.original_router`），直接取会 AttributeError，写成 try/except 会把
    "没挂上路由"也一起吞掉。
    """
    out: set = set()
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            out.add(p)
        sub = getattr(r, "routes", None) or getattr(
            getattr(r, "original_router", None), "routes", None
        )
        if sub:
            out |= _all_paths(sub)
    return out


def test_audition_routes_are_registered():
    import server

    paths = _all_paths(server.app.routes)
    for p in (
        "/api/audition/env",
        "/api/audition/sources",
        "/api/audition/source",
        "/api/audition/source/builtin",
        "/api/audition/source/{source_id}",
        "/api/audition/try",
        "/api/audition/task",
        "/api/audition/cancel",
    ):
        assert p in paths, f"路由没挂上：{p}"


def test_env_over_http(client):
    r = client.get("/api/audition/env")
    assert r.status_code == 200
    body = r.json()
    assert "batch_ready" in body and "busy_reason" in body


def test_task_over_http(client):
    r = client.get("/api/audition/task")
    assert r.status_code == 200
    body = r.json()
    for key in ("running", "status", "results", "scoring", "score_total"):
        assert key in body, f"task 响应缺字段 {key}"


def test_try_over_http_missing_source_is_404(client):
    r = client.post(
        "/api/audition/try", json={"voice_ids": ["kangaroo_v2"], "source_id": "src_nope"}
    )
    assert r.status_code == 404
    assert "源音频" in r.json()["detail"]


def test_try_over_http_empty_voices_is_400(client):
    r = client.post("/api/audition/try", json={"voice_ids": []})
    assert r.status_code == 400


def test_cancel_over_http_without_task(client):
    r = client.post("/api/audition/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False


def test_source_builtin_over_http(client, aud_dir):
    if not fa.BUILTIN_SRC.exists():
        pytest.skip("本机没有内置源句 assets/preview_source.wav")
    r = client.post("/api/audition/source/builtin")
    assert r.status_code == 200
    body = r.json()
    assert body["source_id"] == "src_builtin"
    assert body["duration_s"] > 0
    assert (fa.AUDITION_DIR / "src_builtin.wav").exists()


# ---------------- 常驻引擎优先 + 回退（2026-09-19 加）----------------
# 4.2s 试音音频：一次性 CLI 墙钟 32s（推理只占 4.2s），常驻 worker 首轮 19.6s、
# 第二轮 1.0s。所以"有没有走常驻引擎"直接决定体验，必须有守卫盯着。


def _stub_convert_paths(monkeypatch, *, worker_ok: bool, worker_enabled: bool = True):
    """打桩两条换声路径，返回调用记录。"""
    calls = {"worker": [], "subprocess": []}
    import rvc_convert

    monkeypatch.setattr(rvc_convert, "USE_WORKER", worker_enabled)

    def _worker_call(task, timeout=300.0):
        calls["worker"].append(task)
        if not worker_ok:
            raise rvc_convert.RvcError("worker 挂了")
        Path(task["output"]).write_bytes(b"RIFF")  # 假装产出了文件
        return {"ok": True}

    def _rvc_link(voice_id, src, out, **kw):
        calls["subprocess"].append(voice_id)
        Path(out).write_bytes(b"RIFF")

    monkeypatch.setattr(rvc_convert, "worker_call", _worker_call)
    monkeypatch.setattr(fa, "_rvc_link", _rvc_link)
    return calls


def _audio_inputs(tmp_path):
    pth = tmp_path / "v.pth"
    pth.write_bytes(b"\x00")
    src = _wav(tmp_path / "src.wav")
    out = tmp_path / "out.wav"
    return pth, src, out


def test_convert_prefers_resident_worker(tmp_path, monkeypatch, aud_dir):
    calls = _stub_convert_paths(monkeypatch, worker_ok=True)
    pth, src, out = _audio_inputs(tmp_path)
    assert fa._convert_with_worker(pth, "", src, out, 0, 0.5) is True
    assert len(calls["worker"]) == 1
    task = calls["worker"][0]
    assert task["cmd"] == "convert"
    assert task["pth"] == str(pth)
    assert calls["subprocess"] == []  # 走了 worker 就不该再起子进程


def test_convert_returns_false_when_worker_fails(tmp_path, monkeypatch, aud_dir):
    calls = _stub_convert_paths(monkeypatch, worker_ok=False)
    pth, src, out = _audio_inputs(tmp_path)
    assert fa._convert_with_worker(pth, "", src, out, 0, 0.5) is False
    assert len(calls["worker"]) == 1  # 试过
    assert calls["subprocess"] == []  # 但回退决定权在调用方


def test_convert_respects_worker_disabled(tmp_path, monkeypatch, aud_dir):
    calls = _stub_convert_paths(monkeypatch, worker_ok=True, worker_enabled=False)
    pth, src, out = _audio_inputs(tmp_path)
    assert fa._convert_with_worker(pth, "", src, out, 0, 0.5) is False
    assert calls["worker"] == []  # VM_RVC_WORKER=0 时连试都不试


def test_try_one_uses_worker_and_reports_engine(tmp_path, monkeypatch, aud_dir):
    calls = _stub_convert_paths(monkeypatch, worker_ok=True)
    pth, src, _out = _audio_inputs(tmp_path)
    monkeypatch.setattr(fa, "_resolve_weight", lambda _v: (pth, "", ""))
    _out_path, cached, engine = fa._try_one("v1", "audio", src, "", 0, 0.5)
    assert cached is False and engine == "worker"
    assert calls["subprocess"] == []


def test_try_one_falls_back_to_subprocess(tmp_path, monkeypatch, aud_dir):
    calls = _stub_convert_paths(monkeypatch, worker_ok=False)
    pth, src, _out = _audio_inputs(tmp_path)
    monkeypatch.setattr(fa, "_resolve_weight", lambda _v: (pth, "", ""))
    _out_path, cached, engine = fa._try_one("v1", "audio", src, "", 0, 0.5)
    assert cached is False and engine == "subprocess"
    assert calls["subprocess"] == ["v1"]


def test_result_records_engine(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    monkeypatch.setattr("rvc_convert.stop_worker", lambda: None)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    st = _wait_idle()
    assert st["results"][0]["engine"] == "worker"


def _stub_engine_lifecycle(monkeypatch, order: list):
    """记录「收引擎」与「打分」的先后顺序。"""
    monkeypatch.setattr("rvc_convert.stop_worker", lambda: order.append("stop_worker"))

    def _score(jobs):
        order.append("score")
        return {j["key"]: {"secs": 0.1, "nats": 0.2, "score_error": ""} for j in jobs}

    monkeypatch.setattr(fa, "_score_batch", _score)


def test_batch_stops_worker_before_scoring(aud_dir, monkeypatch):
    """回归（2026-09-19 A/B 实测）：必须**先收掉常驻引擎再打分**。

    常驻 worker 与打分子进程在这台机器上不能共存 —— worker 活着时打分报
    「页面文件太小 os error 1455」，只卸引擎缓存也救不了（worker 进程本身占着
    1.5GB+ 提交内存，实测打分子进程直接崩）。所以顺序必须是先 stop 再 score。
    """
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    order: list = []
    _stub_engine_lifecycle(monkeypatch, order)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=True))
    _wait_idle()
    for _ in range(200):
        if not fa._snapshot().get("scoring"):
            break
        time.sleep(0.02)
    assert order == ["stop_worker", "score"], f"顺序错了：{order}"
    assert fa._snapshot()["results"][0]["secs"] == 0.1


def test_stop_worker_called_even_without_scoring(aud_dir, monkeypatch):
    """打分关掉也要收引擎：不然它会一直占着内存，下一次推理/打分照样受影响。"""
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)
    order: list = []
    _stub_engine_lifecycle(monkeypatch, order)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    _wait_idle()
    for _ in range(100):
        if order:
            break
        time.sleep(0.02)
    assert order == ["stop_worker"]


def test_stop_worker_failure_does_not_break_task(aud_dir, monkeypatch):
    _wav(fa.AUDITION_DIR / "src_1.wav")
    _stub_infer(monkeypatch)
    _stub_history(monkeypatch)

    def _boom():
        raise RuntimeError("收引擎失败")

    monkeypatch.setattr("rvc_convert.stop_worker", _boom)
    fa.audition_try(fa.TryRequest(voice_ids=["v1"], source_id="src_1", score=False))
    st = _wait_idle()
    assert st["status"] == "done"
    assert st["results"][0]["status"] == "done"


def test_env_reports_rvc_worker(monkeypatch):
    env = fa.audition_env()
    assert "rvc_worker" in env
    assert set(env["rvc_worker"]) >= {"enabled", "alive", "pid"}

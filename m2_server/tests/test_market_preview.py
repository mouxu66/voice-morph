"""市场音色自动试听（A2）：状态机 / 源句缓存 / GPU 忙 skipped / 推理成功。

全部本地完成：源句来自参考音真人声截段（不再用 TTS——本机 Qwen3-TTS 对真实
锚点会吐纯静音，见 2026-09-06 懒羊羊试听静音修复），RVC 推理子进程用 stub
替代，不碰真实网络、不用真实 GPU。
"""

import json
import time

import config
import market_preview as mp
import numpy as np
import pytest
import soundfile as sf


def _tone_wav(path, seconds: float = 2.0, sr: int = 16000, amp: float = 0.5):
    """写一段有声 wav（正弦），用于真人声截段与 audible 校验的桩。"""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sf.write(str(path), (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)


@pytest.fixture()
def market_dir(tmp_path, monkeypatch):
    """隔离 outputs/market：避免测试间通过模块级 MARKET_DIR 互相污染。

    同时把 BUILTIN_SRC 指到不存在路径，让「voicebank 参考音截取」旧路径仍可被
    测试到；内置源句优先的行为由 test_ensure_source_prefers_builtin_clean_src 专测。
    """
    d = tmp_path / "market"
    d.mkdir(parents=True)
    monkeypatch.setattr(mp, "MARKET_DIR", d)
    monkeypatch.setattr(mp, "BUILTIN_SRC", tmp_path / "builtin_missing.wav")
    return d


@pytest.fixture()
def rvc_tmp(tmp_path, monkeypatch):
    """RVC 根目录 + 伪 .venv + 权重，落位一个可推理的市场音色。"""
    rvc = tmp_path / "rvc"
    (rvc / "assets" / "weights").mkdir(parents=True)
    idx = rvc / "logs" / "demo_voice" / "added_demo_voice.index"
    idx.parent.mkdir(parents=True)
    idx.write_bytes(b"\x00")
    pth = rvc / "assets" / "weights" / "demo_voice.pth"
    pth.write_bytes(b"\x80\x02" + b"\x00" * 64)
    (rvc / ".venv" / "Scripts").mkdir(parents=True)
    (rvc / ".venv" / "Scripts" / "python.exe").write_bytes(b"MZ")
    monkeypatch.setattr(config, "RVC_ROOT", rvc)
    monkeypatch.setattr(mp, "RVC_VENV_PY", rvc / ".venv" / "Scripts" / "python.exe")
    return rvc


def _wait_inflight(voice_id: str, timeout: float = 5.0):
    """等待后台生成线程结束（避免 daemon 线程与 monkeypatch 还原竞态）。"""
    deadline = time.time() + timeout
    while voice_id in mp._inflight and time.time() < deadline:
        time.sleep(0.05)


@pytest.fixture()
def fake_voicebank(tmp_path, monkeypatch):
    vb = tmp_path / "voicebank"
    d = vb / "vb_demo"
    d.mkdir(parents=True)
    _tone_wav(d / "reference.wav", seconds=6.0)
    monkeypatch.setattr(mp, "VOICEBANK", vb)
    return vb


def test_status_missing_for_unknown_voice(rvc_tmp, market_dir):
    """未生成过、无 pth 的音色 → missing。"""
    st = mp.status("nope")
    assert st["status"] == "missing" and st["url"] == ""


def test_preview_no_rvc_venv_marks_failed(tmp_path, market_dir, monkeypatch):
    """RVC 运行环境缺失 → failed 且带可读原因。"""
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "no_python.exe")
    mp._do_generate("x_voice")
    st = mp.status("x_voice")
    assert st["status"] == "failed"
    assert "RVC" in st["error"]
    assert not (mp.MARKET_DIR / "x_voice_preview.wav").exists()


def test_preview_gpu_busy_marks_skipped(tmp_path, market_dir, monkeypatch):
    """实时/离线变声占用 RVC 环境 → skipped，不静默排队（前端可手动重试）。"""
    (tmp_path / "assets" / "weights").mkdir(parents=True)
    (tmp_path / "assets" / "weights" / "b_voice.pth").write_bytes(b"\x80\x02abc")
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "py.exe")
    monkeypatch.setattr(mp, "_gpu_busy", lambda: "离线变声任务正在运行")
    mp._do_generate("b_voice")
    st = mp.status("b_voice")
    assert st["status"] == "skipped"
    assert "离线变声任务正在运行" in st["error"]


def test_ensure_source_from_reference_and_cache(tmp_path, fake_voicebank, market_dir):
    """源句来自参考音真人声截段（~5s 有声），缓存后二次调用不重新截取。"""
    src1 = mp._ensure_source()
    x, sr = sf.read(str(src1))
    assert x.size > 0 and float(np.sqrt(np.mean(x**2))) > mp._MIN_RMS
    assert abs(len(x) / sr - 5.0) < 0.5  # 中段 ~5s
    mtime1 = src1.stat().st_mtime
    src2 = mp._ensure_source()
    assert src2 == src1 and src2.stat().st_mtime == mtime1, "缓存有效时不应重新截取"


def test_ensure_source_rejects_silent_cache_and_ref(tmp_path, fake_voicebank, market_dir):
    """缓存静音 → 弃用重截；参考音全静音 → 可读报错，绝不返回静音源句。"""
    # 缓存为静音 → 应被弃用并重建为有声
    sf.write(str(mp._src_wav()), np.zeros(16000, dtype=np.float32), 16000)
    src = mp._ensure_source()
    assert float(np.sqrt(np.mean(sf.read(str(src))[0] ** 2))) > mp._MIN_RMS
    # 参考音也静音 → RuntimeError
    import soundfile as sf2

    sf2.write(
        str(fake_voicebank / "vb_demo" / "reference.wav"), np.zeros(16000, dtype=np.float32), 16000
    )
    (mp._src_wav()).unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match="静音"):
        mp._ensure_source()


def test_ensure_source_fails_without_voicebank(tmp_path, market_dir, monkeypatch):
    """无任何 voicebank 且无缓存 → 可读原因，而不是裸异常。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(mp, "VOICEBANK", empty)
    with pytest.raises(RuntimeError, match="参考音色"):
        mp._ensure_source()


def test_ensure_source_prefers_builtin_clean_src(tmp_path, market_dir, fake_voicebank):
    """内置干净源句优先于 voicebank 截取（修"试听全是袋鼠味"根因）：
    只要 BUILTIN_SRC 存在且有声，即使 voicebank 有可用的参考音也不截取。"""
    _tone_wav(mp.BUILTIN_SRC, seconds=3.0)
    src = mp._ensure_source()
    assert src == mp.BUILTIN_SRC
    x, sr = sf.read(str(src))
    assert float(np.sqrt(np.mean(x**2))) > mp._MIN_RMS


def test_ensure_source_builtin_missing_falls_back(tmp_path, market_dir, fake_voicebank):
    """内置源句缺失/无声 → 退回 voicebank 截取路径，不报错。"""
    _tone_wav(mp._src_wav())  # 无声内置 + 有声缓存
    assert mp._ensure_source() == mp._src_wav()
    (mp._src_wav()).unlink(missing_ok=True)  # 无缓存 → 从 voicebank 截取
    src = mp._ensure_source()
    assert src.exists() and src != mp.BUILTIN_SRC


def test_generate_success_marks_ready(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """完整链路（stub 推理子进程）→ 试听落盘，status=ready 且 url 可访问。"""
    _tone_wav(mp.MARKET_DIR / "_preview_src.wav")  # 预置有声源句缓存

    def fake_run(cmd, **kw):  # stub：模拟推理子进程写出 wav
        out = cmd[cmd.index("--output") + 1]
        _tone_wav(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    st = mp.status("demo_voice")
    assert st["status"] == "ready"
    assert st["url"].endswith("demo_voice_preview.wav")
    assert st["url"].startswith("/api/media/")
    sc = json.loads(mp._sidecar("demo_voice").read_text("utf-8"))
    assert sc["status"] == "ready"


def test_generate_silent_output_marks_failed(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """推理产出静音 wav → failed + 删除产物 + sidecar 带原因（绝不假 ready）。"""
    _tone_wav(mp.MARKET_DIR / "_preview_src.wav")

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        sf.write(out, np.zeros(16000, dtype=np.float32), 16000)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    st = mp.status("demo_voice")
    assert st["status"] == "failed"
    assert "静音" in st["error"]
    assert not (mp.MARKET_DIR / "demo_voice_preview.wav").exists()


def test_status_silent_wav_not_ready(rvc_tmp, market_dir):
    """历史遗留的静音试听 wav 不得判 ready（懒羊羊线上 bug 回归）。"""
    sf.write(str(mp._out_wav("demo_voice")), np.zeros(8000, dtype=np.float32), 16000)
    assert mp.status("demo_voice")["status"] == "failed"


def test_generate_dedupe_via_inflight(rvc_tmp, market_dir):
    """同一音色重复触发：第二次直接返 generating，不重复起线程。"""
    mp._inflight.add("demo_voice")
    st = mp.generate("demo_voice")
    assert st["status"] == "generating"
    mp._inflight.discard("demo_voice")


def test_generate_missing_pth_marks_failed(rvc_tmp, market_dir):
    """音色未安装（无 pth）→ failed + 可读原因，绝不拿空路径去推理
    （Windows 空 Path==curdir 的坑：Path("").exists() 为 True 会误过守卫）。"""
    mp._do_generate("missing_voice")
    st = mp.status("missing_voice")
    assert st["status"] == "failed"
    assert "没有可推理" in st["error"]


def test_generate_ready_short_circuits_inflight(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """已 ready 的音色即使线程/重装也不重复触发。"""
    _tone_wav(mp.MARKET_DIR / "_preview_src.wav")

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        _tone_wav(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")  # 造 ready
    mp._inflight.add("demo_voice")
    st = mp.generate("demo_voice")  # ready 优先 → 直接返回，不进 inflight 分支
    assert st["status"] == "ready"
    mp._inflight.discard("demo_voice")


def test_try_auto_preview_never_raises(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """安装收尾触发不应抛异常（含异常路径）；线程快速终结不残留 inflight。"""
    monkeypatch.setattr(mp, "RVC_VENV_PY", tmp_path / "no_python.exe")  # 无环境 → 线程快速 failed
    mp.try_auto_preview("demo_voice")
    mp.try_auto_preview("")  # 空 id 也不抛
    _wait_inflight("demo_voice")
    _wait_inflight("")
    assert mp._inflight == set()


# ---- 源句指纹失效（2026-09-07）：换源句后旧试听必须自动重生成 ----


def test_stale_cache_without_fingerprint(rvc_tmp, market_dir):
    """指纹机制之前的旧缓存（sidecar 无 src_fp）→ 判 missing，触发重新生成。

    线上场景：换内置干净源句后，此前生成的试听仍带袋鼠腔且无任何失效机制。
    """
    _tone_wav(mp.MARKET_DIR / "_preview_src.wav")  # 源句存在，可算当前指纹
    _tone_wav(mp._out_wav("demo_voice"), seconds=3.0)  # 有声旧试听
    mp._mark("demo_voice", "ready")  # 旧格式：不写 src_fp
    assert mp.status("demo_voice")["status"] == "missing"


def test_stale_after_source_changed(rvc_tmp, market_dir):
    """源句一换（内容/文件变）→ 指纹不匹配 → 判 missing，不再返回老音频。"""
    src = mp.MARKET_DIR / "_preview_src.wav"
    _tone_wav(src, seconds=2.0)
    _tone_wav(mp._out_wav("demo_voice"), seconds=3.0)
    mp._mark("demo_voice", "ready", src_fp=mp._source_fingerprint(src))
    assert mp.status("demo_voice")["status"] == "ready"  # 指纹匹配 → 有效

    _tone_wav(src, seconds=6.0)  # 换源句（重写，size/mtime 变）
    assert mp.status("demo_voice")["status"] == "missing"  # 指纹不匹配 → 过期


def test_generate_records_source_fingerprint(rvc_tmp, market_dir, tmp_path, monkeypatch):
    """生成成功时把源句指纹写进 sidecar，重查为 ready（而非被判过期）。"""
    src = mp.MARKET_DIR / "_preview_src.wav"
    _tone_wav(src)

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        _tone_wav(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    sc = json.loads(mp._sidecar("demo_voice").read_text("utf-8"))
    assert sc["src_fp"] == mp._source_fingerprint(src)
    assert mp.status("demo_voice")["status"] == "ready"


def test_mark_preserves_existing_fingerprint(rvc_tmp, market_dir):
    """failed/skipped/generating 等非就绪态不应抹掉已有指纹（否则会反复重生成）。"""
    src = mp.MARKET_DIR / "_preview_src.wav"
    _tone_wav(src)
    fp = mp._source_fingerprint(src)
    mp._mark("demo_voice", "ready", src_fp=fp)
    mp._mark("demo_voice", "failed", "boom")
    sc = json.loads(mp._sidecar("demo_voice").read_text("utf-8"))
    assert sc["src_fp"] == fp


# ---------------- 未安装先试听（预下载模型 → 转换，2026-09-07） ----------------


def test_generate_with_download_uses_staged_override(market_dir, monkeypatch, tmp_path):
    """未安装 + 带直链 → 走 _worker_pre：_ensure_staged 拿暂存权重，转换用覆盖参数。"""
    monkeypatch.setattr(mp, "_gpu_busy", lambda: "")
    rec = {}

    def fake_staged(vid, dl):
        rec["dl"] = dict(dl)
        return tmp_path / "staged.pth"

    def fake_gen(vid, pth_override=None, index_override=None):
        rec["pth"] = pth_override
        rec["idx"] = index_override

    monkeypatch.setattr(mp, "_ensure_staged", fake_staged)
    monkeypatch.setattr(mp, "_do_generate", fake_gen)
    mp._inflight.clear()
    r = mp.generate("pv_item", download={"url": "https://hf-mirror.com/u/rvc.pth"})
    assert r["status"] == "generating"
    _wait_inflight("pv_item")
    assert rec["pth"] == tmp_path / "staged.pth"
    assert rec["idx"] == "", "暂存权重无 index，应显式传空跳过检索"
    assert rec["dl"]["url"].endswith(".pth")


def test_generate_installed_ignores_download(market_dir, rvc_tmp, monkeypatch):
    """已安装音色即使带了直链也走本地模型路径（不做预下载）。"""
    monkeypatch.setattr(mp, "_gpu_busy", lambda: "")
    rec = {}
    monkeypatch.setattr(mp, "_do_generate", lambda vid, **kw: rec.update(kw, vid=vid))
    mp._inflight.clear()
    mp.generate("demo_voice", download={"url": "https://hf-mirror.com/x/x.pth"})
    _wait_inflight("demo_voice")
    assert rec["vid"] == "demo_voice"
    assert "pth_override" not in rec and "index_override" not in rec


def test_ensure_staged_reuses_valid_cache(monkeypatch, tmp_path):
    """缓存里已有有效权重 → 直接复用，不发起下载（安装时 DownloadManager 幂等同理）。"""
    staged = tmp_path / "vx.pth"
    staged.write_bytes(b"PK\x03\x04" + b"\x00" * 16)

    class FakeMgr:
        download_dir = tmp_path

        def start(self, **kw):  # noqa: ANN003
            raise AssertionError("缓存有效时不应重新下载")

    import market_download as md

    monkeypatch.setattr(md, "get_manager", lambda: FakeMgr())
    assert mp._ensure_staged("vx", {"url": "https://hf-mirror.com/x/x.pth"}) == staged


def test_ensure_staged_downloads_when_missing(monkeypatch, tmp_path):
    """缓存缺失 → 用 DownloadManager 起 preview_<id> 任务下载到 <voice_id>.pth。"""
    staged = tmp_path / "vy.pth"

    class FakeMgr:
        download_dir = tmp_path
        started = None

        def start(self, name, url, mirror_url=None, sha256=None, filename=None):
            self.started = (name, filename)
            staged.write_bytes(b"\x80\x02" + b"\x00" * 16)  # 模拟下载完成落盘
            return {"status": "downloading"}

        def task_status(self, name):
            return {"status": "done"}

    fake = FakeMgr()
    import market_download as md

    monkeypatch.setattr(md, "get_manager", lambda: fake)
    out = mp._ensure_staged("vy", {"url": "https://hf-mirror.com/y/y.pth"})
    assert out == staged
    assert fake.started == ("preview_vy", "vy.pth"), "任务名 preview_*、文件名与安装共用 <id>.pth"


# -------- A2 健壮性增强（2026-09-10）：A 输出质量关 / B 瞬态重试 / C GPU忙补生成 --------


def _sine(path, seconds: float = 3.0, amp: float = 0.4, sr: int = 16000):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sf.write(str(path), (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)


# ---- A：输出质量关（防"有声但废"漏过纯响度检查）----


def test_quality_ok_passes_normal(tmp_path):
    """正常有声 wav（峰值合理、时长合理、无 NaN）→ 合格。"""
    p = tmp_path / "ok.wav"
    _sine(p)
    assert mp._quality_ok(p) == (True, "")


def test_quality_rejects_clipping(tmp_path):
    """大量样本顶到满幅 → 判削顶破音（_audible 查不出的"有声但废"）。"""
    p = tmp_path / "clip.wav"
    x = np.zeros(16000 * 3, dtype=np.float32)
    x[:2000] = 1.0  # 占比 ~4.2% > 2% 阈值
    sf.write(str(p), x, 16000)
    ok, why = mp._quality_ok(p)
    assert not ok and "削顶" in why


def test_quality_rejects_nan(tmp_path):
    """输出含 NaN → 判模型崩溃（不放过成 ready，否则播放器直接哑/爆）。"""
    p = tmp_path / "nan.wav"
    x = (0.4 * np.ones(16000 * 3)).astype(np.float32)
    x[0] = np.nan
    sf.write(str(p), x, 16000, subtype="FLOAT")  # PCM16 会把 NaN 量化掉，须写 float 保真
    ok, why = mp._quality_ok(p)
    assert not ok and "NaN" in why


def test_quality_rejects_too_short(tmp_path):
    """输出被截断成极短（<0.5s）→ 不合格。"""
    p = tmp_path / "short.wav"
    sf.write(str(p), np.full(1600, 0.4, dtype=np.float32), 16000)  # 0.1s
    ok, why = mp._quality_ok(p)
    assert not ok and "过短" in why


# ---- B：瞬态失败自愈（子进程偶发崩溃重试一次）----


def test_transient_retry_succeeds_on_second_attempt(rvc_tmp, market_dir, monkeypatch):
    """首次推理子进程崩（如 CUDA OOM）→ 自动重试 → 第二次成功 → ready。"""
    _sine(mp.MARKET_DIR / "_preview_src.wav")
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        # _gpu_busy 会先走 rvc_live 的 powershell 进程枚举（同样经 subprocess.run），
        # 不含 --output 的调用不是 RVC 推理：返回空结果，别计数。
        if "--output" not in cmd:
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})
        calls["n"] += 1
        out = cmd[cmd.index("--output") + 1]
        if calls["n"] == 1:
            return type("R", (), {"returncode": 1, "stderr": "CUDA out of memory", "stdout": ""})
        _sine(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    assert calls["n"] == 2, "应重试一次"
    assert mp.status("demo_voice")["status"] == "ready"


def test_transient_retry_exhausted_marks_failed(rvc_tmp, market_dir, monkeypatch):
    """两次都崩 → failed 且带原因 + 清理半成品产物。"""
    _sine(mp.MARKET_DIR / "_preview_src.wav")

    def fake_run(cmd, **kw):
        return type(
            "R", (), {"returncode": 1, "stderr": "CUDA error: device-side assert", "stdout": ""}
        )

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    st = mp.status("demo_voice")
    assert st["status"] == "failed"
    assert "RVC 推理失败" in st["error"]
    assert not (mp.MARKET_DIR / "demo_voice_preview.wav").exists()


def test_transient_retry_cleans_partial_output(rvc_tmp, market_dir, monkeypatch):
    """首次失败会留下半成品 wav → 重试成功后以新产物为准，不被旧残file干扰。"""
    _sine(mp.MARKET_DIR / "_preview_src.wav")
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        out = cmd[cmd.index("--output") + 1]
        if calls["n"] == 1:
            sf.write(out, np.zeros(1600, dtype=np.float32), 16000)  # 半成品（截断）
            return type("R", (), {"returncode": 1, "stderr": "boom", "stdout": ""})
        _sine(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._do_generate("demo_voice")
    assert mp.status("demo_voice")["status"] == "ready"


# ---- C：GPU 忙标 skipped 后延时自动补生成 ----


def test_worker_backoff_recovers_after_gpu_busy(rvc_tmp, market_dir, monkeypatch):
    """安装收尾自动触发时 GPU 忙 → skipped，后台延时后空闲则自动补生成到 ready。"""
    _sine(mp.MARKET_DIR / "_preview_src.wav")
    busy_calls = {"n": 0}

    def fake_busy():
        busy_calls["n"] += 1
        return "离线变声任务正在运行" if busy_calls["n"] == 1 else ""

    monkeypatch.setattr(mp, "_gpu_busy", fake_busy)
    monkeypatch.setattr(mp, "_BACKOFF_S", 0)  # 测试不等 20s

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        _sine(out, seconds=3.0)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "OK"})

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp._inflight.clear()
    mp._worker("demo_voice")
    _wait_inflight("demo_voice")
    assert mp.status("demo_voice")["status"] == "ready"
    assert busy_calls["n"] >= 2, "补生成前应重新检查 GPU 占用"


def test_worker_backoff_stays_skipped_when_still_busy(rvc_tmp, market_dir, monkeypatch):
    """延时后 GPU 仍忙 → 维持 skipped（不硬跑、不误标 failed），交前端手动重试。"""
    _sine(mp.MARKET_DIR / "_preview_src.wav")
    monkeypatch.setattr(mp, "_gpu_busy", lambda: "实时变声正在运行")
    monkeypatch.setattr(mp, "_BACKOFF_S", 0)
    monkeypatch.setattr(
        mp.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("GPU 忙时不应推理")),
    )
    mp._inflight.clear()
    mp._worker("demo_voice")
    _wait_inflight("demo_voice")
    st = mp.status("demo_voice")
    assert st["status"] == "skipped"
    assert "实时变声正在运行" in st["error"]

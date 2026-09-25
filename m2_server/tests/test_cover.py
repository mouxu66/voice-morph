"""翻唱链路（`cover_api`）的守卫测试。

这个模块的每一步都"不会报错，只是成品不对" —— 所以每条断言都盯着
**成品里有没有该有的东西**，而不是"函数被调用了"：

    · 少了伴奏 → 拿到的是一首清唱，程序一路成功（最坏的结果：用户发出去才发现）
    · 变调算反 → 男声变女声却更低了
    · 中间产物没删 → 一首歌的分离结果（几百 MB）烂在磁盘上

真跑 demucs/RVC 要几分钟且吃 GPU，所以这里用**替身**把它们换掉，
只验本模块自己的逻辑：串起来的顺序、合回的 filter、清理的范围。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def cover(monkeypatch, tmp_path):
    """载入 cover_api 并把会话目录指到 tmp_path（防真实 outputs 污染）。"""
    import config as cfg

    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    import cover_api

    monkeypatch.setattr(cover_api, "MAX_UPLOAD_BYTES", 1024 * 1024)
    # 会话目录每次现读 cfg，所以 patch cfg 就够了
    return cover_api


# ---------------------------------------------------------------- 合回伴奏


def test_mix_back_keeps_both_tracks(cover, tmp_path, monkeypatch):
    """★ 合成的 filter 必须**同时**吃进人声与伴奏。

    这是整条链路最容易悄悄坏掉的一步：只喂人声也能成功产出一个 wav，
    只是成品是清唱。断言 filter 里 two inputs 且 amix inputs=2。
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")

    v = tmp_path / "vocals.wav"
    a = tmp_path / "no_vocals.wav"
    out = tmp_path / "mixed.wav"
    cover.mix_back(v, a, out)

    assert len(calls) == 1
    cmd = calls[0]
    assert str(v) in cmd and str(a) in cmd, "人声与伴奏都必须作为输入"
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc, "两个输入必须被 amix 混合"
    assert "duration=longest" in fc, "伴奏通常比人声长，按 longest 才不会切掉尾巴"
    # ★ normalize=0：默认 amix 会把两轨各降一半音量，听感像"伴奏被压扁了"
    assert "normalize=0" in fc


def test_mix_back_applies_gains(cover, tmp_path, monkeypatch):
    """音量滑块要真的进 filter —— 否则用户拖了没反应。"""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")

    cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav", gains=(1.5, 0.8))
    fc = calls[0][calls[0].index("-filter_complex") + 1]
    assert "volume=1.5" in fc
    assert "volume=0.8" in fc


def test_mix_back_raises_when_ffmpeg_fails(cover, tmp_path, monkeypatch):
    """ffmpeg 失败必须报错 —— 静默返回一个不存在的路径会让用户在结果区看空气。"""
    monkeypatch.setattr(
        cover.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    with pytest.raises(RuntimeError, match="合成失败"):
        cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav")


# ---------------------------------------------------------------- 分离


def test_separate_requires_both_stems(cover, tmp_path, monkeypatch):
    """★ 分离出人声但缺伴奏时必须报错，不能"凑合只用有人声那轨"。

    静默降级成清唱的后果：用户听到一首没有伴奏的歌，比直接失败困惑得多。
    """
    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")

    def fake_run(cmd, **kw):
        stem = Path(cmd[cmd.index("-o") + 1]) / cover.DEMUCS_MODEL / src.stem
        stem.mkdir(parents=True, exist_ok=True)
        (stem / "vocals.wav").write_bytes(b"RIFF")  # 只有人声，没有伴奏
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="缺少伴奏轨"):
        cover.separate_song(src, 1)


def test_separate_reports_model_download_hint(cover, tmp_path, monkeypatch):
    """demucs 失败时文案要提到"首次要下模型" —— 否则用户以为软件坏了。"""
    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")
    monkeypatch.setattr(
        cover.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no module named demucs"),
    )
    with pytest.raises(RuntimeError, match="demucs 模型"):
        cover.separate_song(src, 2)


def test_separate_uses_two_stems_flag(cover, tmp_path, monkeypatch):
    """★ `--two-stems vocals` 不能少 —— 少了就拿不到伴奏轨，整条链就断了。"""
    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        stem = Path(cmd[cmd.index("-o") + 1]) / cover.DEMUCS_MODEL / src.stem
        stem.mkdir(parents=True, exist_ok=True)
        (stem / "vocals.wav").write_bytes(b"RIFF")
        (stem / "no_vocals.wav").write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    cover.separate_song(src, 3)
    cmd = seen[0]
    assert "--two-stems" in cmd
    assert cmd[cmd.index("--two-stems") + 1] == "vocals"


def test_separate_output_lands_in_session_dir(cover, tmp_path):
    """★ 分离产物必须在会话目录下。

    放 outputs 根的话，"退出即删"（`session_out.purge`）收不到它 ——
    一首歌的分离结果几百 MB，几次下来磁盘就满了，而用户完全不知道哪来的。
    """
    d = cover._separate_dir()
    assert cover.session_out.is_session(d) or cover.session_out.DIRNAME in str(d)
    assert str(d).startswith(str(tmp_path)), "必须落在（被 patch 的）会话根下"


# ---------------------------------------------------------------- 变调建议


def test_pitch_suggest_direction(cover, tmp_path, monkeypatch):
    """★ 变调方向的符号不能反。

    目标音色的基准音高**高**于人声 → 要**升**（正半音）。

    这里不真跑 librosa（要 numpy/librosa 且慢），直接换掉 `_median_f0`
    让人声 200Hz、参考音 400Hz —— 正好一个八度，期望 +12。
    """
    monkeypatch.setattr(cover, "_median_f0", lambda path, np, librosa: 400.0 if "ref" in str(path) else 200.0)
    vb = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo"
    vb.mkdir(parents=True, exist_ok=True)
    (vb / "reference.wav").write_bytes(b"RIFF")

    pitch = cover._pitch_suggest(tmp_path / "v.wav", "kangaroo")
    assert pitch == 12, "参考音高一个八度 → 升 12 半音"


def test_pitch_suggest_lower_is_negative(cover, tmp_path, monkeypatch):
    monkeypatch.setattr(cover, "_median_f0", lambda path, np, librosa: 100.0 if "ref" in str(path) else 200.0)
    vb = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo"
    vb.mkdir(parents=True, exist_ok=True)
    (vb / "reference.wav").write_bytes(b"RIFF")

    assert cover._pitch_suggest(tmp_path / "v.wav", "kangaroo") == -12


def test_pitch_suggest_zero_when_no_reference(cover, tmp_path):
    """没有参考音时给 0（不调），而不是抛 —— 建议值算不出来时"不动"最安全。"""
    assert cover._pitch_suggest(tmp_path / "v.wav", "不存在的音色") == 0


def test_pitch_suggest_survives_analysis_explosion(cover, tmp_path, monkeypatch):
    """分析炸了也要返回 0 而不是把整个请求带崩（同 `quality_verdict` 的纪律）。"""
    def boom(*a, **k):
        raise RuntimeError("librosa 炸了")

    monkeypatch.setattr(cover, "_median_f0", boom)
    vb = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo"
    vb.mkdir(parents=True, exist_ok=True)
    (vb / "reference.wav").write_bytes(b"RIFF")
    assert cover._pitch_suggest(tmp_path / "v.wav", "kangaroo") == 0


# ---------------------------------------------------------------- 状态与并发


def test_status_has_step_field_for_progress_ui(cover):
    """`step` 是前端"到哪一步了"的唯一依据 —— 少了它用户面对几分钟的进度条会以为卡死。"""
    assert "step" in cover.COVER_STATE
    assert cover.COVER_STATE["status"] == "idle"


def test_gpu_holder_blocks_new_job(cover, monkeypatch):
    """训练等独占任务在跑时要拒绝（避免争抢显卡），且给出理由。"""
    import runtime

    monkeypatch.setattr(runtime, "gpu_holder_reason", lambda: "RVC 训练正在运行")
    assert cover._gpu_guard() == "RVC 训练正在运行"


def test_gpu_guard_allows_when_idle(cover, monkeypatch):
    import runtime

    monkeypatch.setattr(runtime, "gpu_holder_reason", lambda: "")
    assert cover._gpu_guard() == ""


def test_cover_state_is_shared_not_rebound(cover):
    """★ COVER_STATE 必须被原地更新（`.update()` / 项赋值）。

    整体重新赋值会让模块里其它地方持有的旧引用失联 —— 前端轮询到的永远是初始值，
    表现为"点了开始但进度不动"。这是 `PIPELINE_STATE` 同一类坑（见 runtime 注释）。
    """
    before = id(cover.COVER_STATE)
    cover.COVER_STATE.update(status="running", percent=42.0)
    assert id(cover.COVER_STATE) == before
    assert cover.COVER_STATE["percent"] == 42.0

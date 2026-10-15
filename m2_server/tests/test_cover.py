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
import types
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
    # 探测那遍要读真峰值，喂 b"RIFF" 会炸 —— 本用例只验 argv，峰值用桩顶掉
    monkeypatch.setattr(cover, "_true_peak", lambda p: 0.5)

    v = tmp_path / "vocals.wav"
    a = tmp_path / "no_vocals.wav"
    out = tmp_path / "mixed.wav"
    cover.mix_back(v, a, out)

    assert calls, "没有调用 ffmpeg"
    cmd = calls[0]
    assert str(v) in cmd and str(a) in cmd, "人声与伴奏都必须作为输入"
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc, "两个输入必须被 amix 混合"
    assert "duration=longest" in fc, "伴奏通常比人声长，按 longest 才不会切掉尾巴"
    # ★ normalize=0：默认 amix 会把两轨各降一半音量，听感像"伴奏被压扁了"
    assert "normalize=0" in fc
    # 最后一遍写的是成品（不带 float 开关）
    assert "pcm_f32le" not in calls[-1], "成品不该写成 float32"
    assert str(out) == calls[-1][-1]


def test_mix_back_probe_is_float32(cover, tmp_path, monkeypatch):
    """★ 探测那一遍**必须**要求 float32 —— 这不是风格问题，是能不能防住削波的前提。

    pcm_s16 落盘时超过满刻度的和**先被削到 1.000**，于是"量到的峰永远 = 1.000"、
    缩放永不生效（本轮实测踩过：改完第二遍混音仍有 47,608 个削波点）。
    float32 保留真实幅度（可以 >1.0）才算得出正确的缩放系数。

    ⚠️ 这条断言的是**argv**（与 ffmpeg 的契约）。行为上的证明在
    `test_mix_back_scales_proportional_when_probe_clips`：那里的桩会**照着
    s16 的语义钳位**，所以漏了这个 flag 就必然算出错的缩放。
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cover, "_true_peak", lambda p: 0.5)

    cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav")

    assert len(calls) == 2, f"应跑两遍（探测 + 成品），实际 {len(calls)}"
    probe = calls[0]
    assert "pcm_f32le" in probe, "探测那遍必须是 float32，否则量到的峰永远是被削平的 1.000"


def test_mix_back_applies_gains(cover, tmp_path, monkeypatch):
    """音量滑块要真的进 filter —— 否则用户拖了没反应。"""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cover, "_true_peak", lambda p: 0.5)

    _, applied = cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav",
                                gains=(1.5, 0.8))
    fc = calls[-1][calls[-1].index("-filter_complex") + 1]
    assert "volume=1.5" in fc
    assert "volume=0.8" in fc
    assert applied == (1.5, 0.8), "不过顶时增益必须原样用"


def test_mix_back_scales_proportional_when_probe_clips(cover, tmp_path, monkeypatch):
    """★ 超顶时两侧增益**等比缩小**，而不是硬削波。

    桩**照 s16 的语义钳位**（超过 1.0 的样本被削平）—— 所以如果探测那遍漏了
    `pcm_f32le`，量到的峰就变成 1.0，缩放系数趋近 1，这条必红。这就是那个
    静默失效模式的行为级证明（同族：`test_cover_my_voice.py` 的真 ffmpeg 版）。
    """
    import numpy as np

    seen_gains: list[tuple[float, float]] = []

    def fake_run(cmd, **kw):
        fc = cmd[cmd.index("-filter_complex") + 1]
        vg = float(fc.split("[0:a]volume=")[1].split("[v]")[0])
        ag = float(fc.split("[1:a]volume=")[1].split("[a]")[0])
        seen_gains.append((vg, ag))
        # amix normalize=0 是**相加**：两轨各 0.9 幅度 ⇒ 真峰 = (vg + ag) × 0.9
        peak = (vg + ag) * 0.9
        if "pcm_f32le" not in cmd:          # ← 成品那遍 / 漏了 flag 的探测那遍
            peak = min(peak, 1.0)           # s16 的硬削波语义：顶到 1.000 就不再高
        out_p = Path(cmd[-1])
        out_p.write_bytes(b"RIFF")          # _mix_once 会查 out.exists()
        np.save(str(out_p) + ".npy", np.asarray([peak], dtype="float64"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # _true_peak 走内存里的 npy，复刻"读回落盘文件"的语义
    def fake_peak(path):
        return float(np.abs(np.load(str(path) + ".npy")).max())

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cover, "_true_peak", fake_peak)

    _, applied = cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav",
                                gains=(3.0, 1.0))

    # 真峰 3.0×0.9 + 1.0×0.9 = 3.6 ⇒ k = 0.98/3.6
    assert applied[0] < 3.0 and applied[1] < 1.0, f"超顶时应等比缩小，实际 {applied}"
    assert applied[0] / applied[1] == pytest.approx(3.0, rel=1e-3), "配比必须保持不变"
    assert seen_gains[-1] == applied, "成品那遍必须用缩放后的增益"
    # 若探测漏了 float32：量到 1.0 ⇒ k≈0.98 ⇒ applied≈(2.94, 0.98)，上面的断言会红
    assert applied[0] == pytest.approx(3.0 * 0.98 / 3.6, rel=1e-3), (
        f"缩放系数不对（{applied}）—— 探测那遍是不是没写 float32？"
    )


def test_mix_back_leaves_quiet_material_untouched(cover, tmp_path, monkeypatch):
    """本来就不过顶的素材**不许动增益** —— 缩放是"救命"，不是常规操作。

    防的是"防削波"被写成"一律缩一点"：那会让所有正常翻唱平白变轻。
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cover, "_true_peak", lambda p: 0.4)

    _, applied = cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav",
                                gains=(2.0, 1.0))
    assert applied == (2.0, 1.0), f"不该动增益，实际 {applied}"
    assert len(calls) == 2, "仍然要跑两遍（探测 + 成品），只是第二遍的增益没变"


def test_mix_back_cleans_up_its_probe_file(cover, tmp_path, monkeypatch):
    """探测文件是临时垃圾，必须清掉 —— 否则每跑一首歌就在会话目录里留一个 float wav。"""
    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cover.subprocess, "run", fake_run)
    monkeypatch.setattr(cover, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(cover, "_true_peak", lambda p: 0.5)

    out = tmp_path / "mixed.wav"
    cover.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", out)
    leftovers = [p.name for p in tmp_path.iterdir() if "probe" in p.name]
    assert not leftovers, f"探测文件没清掉：{leftovers}"


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


@pytest.fixture
def fake_librosa(monkeypatch):
    """往 sys.modules 里塞一个**假** librosa，让 `import librosa` 能成功。

    ★ 这是必需的，不是多此一举。librosa 在本项目是**可选依赖**，而且 CI
      （.venv-ci）**刻意不装它** —— requirements-dev.txt 白纸黑字写着「两个刻意
      不装的包，别『顺手补上』」，因为 test_effects.py 有两条用例专门验证
      「librosa 缺失时效果器直通」这条降级路径，装了反而会把它们 skip 掉。

      于是任何依赖 librosa 的测试都必须自己解决 import。这三条的本意就写着
      「不真跑 librosa」—— 它们替换了 `_median_f0`，**只是漏了 import 这一步**。
      漏掉的后果是典型的「本机绿、CI 红」：.venv 里手工装过 librosa 所以过，
      CI 上 `_pitch_suggest` 走 except 返回 0 → 断言 12 却得到 0。

      假模块够用即可：`_median_f0` 已被替换，librosa 的属性一个都不会被真正用到。
      `setitem` 会在用例结束后自动还原（monkeypatch 的保证）。
    """
    monkeypatch.setitem(sys.modules, "librosa", types.ModuleType("librosa"))


def test_pitch_suggest_direction(cover, tmp_path, monkeypatch, fake_librosa):
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


def test_pitch_suggest_lower_is_negative(cover, tmp_path, monkeypatch, fake_librosa):
    monkeypatch.setattr(cover, "_median_f0", lambda path, np, librosa: 100.0 if "ref" in str(path) else 200.0)
    vb = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo"
    vb.mkdir(parents=True, exist_ok=True)
    (vb / "reference.wav").write_bytes(b"RIFF")

    assert cover._pitch_suggest(tmp_path / "v.wav", "kangaroo") == -12


def test_pitch_suggest_zero_when_no_reference(cover, tmp_path, fake_librosa):
    """没有参考音时给 0（不调），而不是抛 —— 建议值算不出来时"不动"最安全。

    这条原本在无 librosa 的环境里**因为错误的原因通过**（import 失败直接落到
    兜底的 return 0），加了假模块后才真正测到「没有参考音」这一条路径。
    """
    assert cover._pitch_suggest(tmp_path / "v.wav", "不存在的音色") == 0


def test_pitch_suggest_zero_when_librosa_missing(cover, tmp_path, monkeypatch):
    """★ librosa 真的缺失时 → 给 0（不调），不许抛。

    覆盖 `_pitch_suggest` 的 except 兜底 —— 「建议值算不出来时不动」是这个模块
    的核心安全保证，却一直没有测试盯着。用 `sys.modules[...] = None` 强制 import
    失败（Python 约定：值为 None 表示「该模块不可导入」，抛 ImportError）。
    这样这条**在任何环境都真跑** —— 不能靠「碰巧没装 librosa」来触发，
    否则在装了 librosa 的开发机上就永远测不到。
    """
    monkeypatch.setitem(sys.modules, "librosa", None)
    vb = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo"
    vb.mkdir(parents=True, exist_ok=True)
    (vb / "reference.wav").write_bytes(b"RIFF")
    assert cover._pitch_suggest(tmp_path / "v.wav", "kangaroo") == 0


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


def test_status_has_vocal_gain_applied_field(cover):
    """★ 完成面板要能显示"人声 ×N.NN"（2026-09-27 修）。

    实际增益以前只塞在 `message` 里，而 message 仅 running 时渲染 —— 自动配平
    （默认开）改了你听到的音量比例，界面却只字不提，用户对不上滑块与听感。
    0.0 = 本轮还没算出来，前端据此不渲染这一段而不是显示 ×0.00。
    """
    assert "vocal_gain_applied" in cover.COVER_STATE
    assert cover.COVER_STATE["vocal_gain_applied"] == 0.0


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


# ---------------------------------------------------------------- 完成面板如实性


def test_worker_reports_the_gain_actually_applied(cover, tmp_path, monkeypatch):
    """★ 完成面板显示的必须是**真的用到的**增益，而不是自动配平算出来的那个。

    `mix_back` 会在和超满刻度时**等比缩小**两侧增益（防硬削波，2026-10-14 /
    犯错指南 #174）。若 `_cover_worker` 仍把缩放**前**的 `gains` 写进状态，面板就会
    显示"人声 ×2.71"而实际听到的是 ×1.44 —— 而这正是 `vocal_gain_applied` 这个字段
    存在的理由（2026-09-27：用户对不上滑块与听感）。

    退化形态是静默的：数值看着是个正经数字，成品也完全正常，只有"界面数字与声音不符"。
    """
    import soundfile as sf
    import numpy as np

    # 让会话目录落在 tmp_path 下（session_out 每次现读 cfg → cover 夹具已 patch）
    fake = tmp_path / "cover_in.wav"
    sf.write(str(fake), np.zeros(4410, dtype="float32"), 44100)
    monkeypatch.setattr(cover.session_out, "new_path", lambda stem: tmp_path / f"{stem}.wav")

    vocal = tmp_path / "rvc.wav"
    accomp = tmp_path / "acc.wav"
    for p in (vocal, accomp):
        sf.write(str(p), (0.3 * np.sin(np.linspace(0, 90, 4410))).astype("float32"), 44100)

    monkeypatch.setattr(cover, "separate_song", lambda src, stamp: (vocal, accomp))
    monkeypatch.setattr(cover, "rms_db", lambda p: -20.0)
    monkeypatch.setattr(cover, "auto_vocal_gain", lambda v, a: 3.0)
    # ★ mix_back 返回"缩放后"的增益（真实行为）；这里模拟超顶缩到 1.5
    def fake_mix_back(v, a, o, gains):
        sf.write(str(o), np.zeros(4410, dtype="float32"), 44100)  # ⑥ 会用 sf.read 读它
        return Path(o), (1.5, 0.5)

    monkeypatch.setattr(cover, "mix_back", fake_mix_back)
    rvc_mod = types.ModuleType("rvc_convert")
    rvc_mod.rvc_convert = lambda *a, **k: vocal
    monkeypatch.setitem(sys.modules, "rvc_convert", rvc_mod)

    cover._cover_worker(fake, "v1", tmp_path / "v.pth", -12, 0.5, 1.0, 1.0, False, True, False, 1)

    assert cover.COVER_STATE["status"] == "done", cover.COVER_STATE.get("error")
    assert cover.COVER_STATE["vocal_gain_applied"] == 1.5, (
        f"面板应显示 mix_back 实际用的 1.5，实际 {cover.COVER_STATE['vocal_gain_applied']}"
        "（用缩放前的 3.0 就是界面与听感不符）"
    )


def _setup_worker_smoke(cover, tmp_path, monkeypatch):
    """搭一个最小可跑的 worker 环境，返回 (fake, 喂给 RVC 的输入记录)。"""
    import soundfile as sf
    import numpy as np

    fake = tmp_path / "cover_in.wav"
    sf.write(str(fake), np.zeros(4410, dtype="float32"), 44100)
    monkeypatch.setattr(cover.session_out, "new_path", lambda stem: tmp_path / f"{stem}.wav")
    vocal, accomp = tmp_path / "rvc.wav", tmp_path / "acc.wav"
    for p in (vocal, accomp):
        sf.write(str(p), (0.3 * np.sin(np.linspace(0, 90, 4410))).astype("float32"), 44100)
    monkeypatch.setattr(cover, "separate_song", lambda src, stamp: (vocal, accomp))
    monkeypatch.setattr(cover, "rms_db", lambda p: -20.0)
    monkeypatch.setattr(cover, "auto_vocal_gain", lambda v, a: 1.0)

    def fake_mix_back(v, a, o, gains):
        sf.write(str(o), np.zeros(4410, dtype="float32"), 44100)  # ⑥ 会 sf.read 它
        return Path(o), gains

    monkeypatch.setattr(cover, "mix_back", fake_mix_back)
    sent = []

    rvc_mod = types.ModuleType("rvc_convert")
    rvc_mod.rvc_convert = lambda wav, *a, **k: (sent.append(wav) or vocal)
    monkeypatch.setitem(sys.modules, "rvc_convert", rvc_mod)
    return fake, sent


def test_worker_skips_lead_extraction_by_default(cover, tmp_path, monkeypatch):
    """lead_vocal=False：不碰主唱分离，RVC 直接吃 demucs 的混声轨（行为不变）。"""
    import lead_sep

    called = []
    monkeypatch.setattr(lead_sep, "extract_lead",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("不该调用")))
    fake, sent = _setup_worker_smoke(cover, tmp_path, monkeypatch)
    cover._cover_worker(fake, "v1", tmp_path / "v.pth", -12, 0.5, 1.0, 1.0, False, True, False, 1)
    assert cover.COVER_STATE["status"] == "done", cover.COVER_STATE.get("error")
    assert not called


def test_worker_replaces_vocal_with_lead_when_enabled(cover, tmp_path, monkeypatch):
    """lead_vocal=True：先抠主唱，RVC 拿到的必须是 lead.wav 而不是 demucs 混声。"""
    import soundfile as sf
    import numpy as np
    import lead_sep

    fake, sent = _setup_worker_smoke(cover, tmp_path, monkeypatch)
    lead = tmp_path / "lead_staging.wav"
    sf.write(str(lead), (0.2 * np.sin(np.linspace(0, 50, 4410))).astype("float32"), 44100)
    seen = {}

    def fake_extract(vocals, out_dir, model=lead_sep.DEFAULT_MODEL):
        seen["vocals"] = vocals
        seen["out_dir"] = out_dir
        return lead

    monkeypatch.setattr(lead_sep, "extract_lead", fake_extract)
    cover._cover_worker(fake, "v1", tmp_path / "v.pth", -12, 0.5, 1.0, 1.0, False, True, True, 1)
    assert cover.COVER_STATE["status"] == "done", cover.COVER_STATE.get("error")
    assert seen.get("vocals") is not None, "应当先对 demucs 人声做主唱分离"
    assert sent and sent[0] == lead, f"RVC 应吃主唱轨 {lead}，实际 {sent}"


def test_worker_lead_extraction_error_reaches_state(cover, tmp_path, monkeypatch):
    """主唱分离失败（缺模型等）必须冒泡到 COVER_STATE.error，不能假装成功。"""
    import lead_sep

    fake, sent = _setup_worker_smoke(cover, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise lead_sep.LeadSepError(lead_sep.LeadSepError.Kind.FAILED, "主唱分离失败：测试")

    monkeypatch.setattr(lead_sep, "extract_lead", boom)
    cover._cover_worker(fake, "v1", tmp_path / "v.pth", -12, 0.5, 1.0, 1.0, False, True, True, 1)
    assert cover.COVER_STATE["status"] == "error"
    assert "主唱分离" in cover.COVER_STATE["error"]

"""讲课视频合成单测（2026-10-08）。

被测：`tools/lecture_video.py`（帧序列 + 旁白音轨 → mp4）。

★ 这个文件守的是**这条链路里不报错的错法** —— 2026-10-08 那次出片
（`outputs/lecture_demo/*.mp4`）能成，靠的是一条**没留档的手工命令**；
把它补成工具的价值，全在于"下次不会悄悄错"：

  1. **清单里少一个文件，ffmpeg 退出码仍是 0**（本项目实测踩过）→ 所以
     `precheck` 必须在起进程**之前**逐个 `exists()`，且不许留下半成品。
  2. **旁白被剪**：`-shortest` 收口 + 末帧补齐 = 成片正好等于音轨长度。
     缺了补齐，时间轴比音轨短时最后一句就没了 —— 听感上"正常结束"，没人报错。
  3. **尾部重复行**：concat 解复用器**不给重复出现的最后一帧就不算它的 duration**
     （实测 17 帧 vs 43 帧）→ 有人"清理"掉那行，成片尾巴就短一截。
  4. **帧几何**：奇数宽高编不出 yuv420p、混尺寸出花屏 —— 都必须指名道姓地报错，
     **不许偷偷 crop/scale**（悄悄改画面比报错糟）。
  5. **页与页**：manifest 的 `duration_s` 是报告、wav 才是事实（不一致要告警并按
     实际算）；失败页没有 wav，**必须点名声张**而不是让视频少一页没人知道。

变异测试（本仓传统，见 `docs/犯错档案-工程.md` §8.19）：
    删掉 `precheck` 里那段 `missing` 检查 → `test_missing_frame_fails_before_ffmpeg` 必红；
    删掉 `write_frames_list` 末尾那行重复 → `test_frames_list_repeats_last_frame` 必红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import lecture_video as lv  # noqa: E402

pytest.importorskip("soundfile")
pytest.importorskip("PIL")


# ---------------- 夹具 ----------------

def _png(path: Path, size=(64, 64), color=(200, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def _wav(path: Path, seconds: float, sr: int = 24000) -> Path:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(sr * seconds), dtype=np.float32), sr, subtype="PCM_16")
    return path


def _fake_ffprobe(monkeypatch, ffmpeg_bin: str) -> None:
    """把工具的 ffprobe 指到真 ffprobe（从 ffmpeg 路径**推导**，别做字符串替换 ——
    winget 的目录名里也有 "ffmpeg"，`replace` 会把它一起改成 "ffprobe-9.0-full_build"）。"""
    real = lv.find_ffprobe(ffmpeg_bin)  # 打桩前先取真值
    monkeypatch.setattr(lv, "find_ffprobe", lambda _ff=None: real)


def _timeline(tmp_path: Path, durations=(0.5, 0.5), seconds=1.0) -> lv.Timeline:
    clips = [lv.Clip(image=_png(tmp_path / f"f{i}.png"), duration_s=d)
             for i, d in enumerate(durations)]
    return lv.Timeline(clips=clips, audio=_wav(tmp_path / "voice.wav", seconds), fps=30)


# ---------------- 清单解析 / 生成（纯逻辑） ----------------

class TestFramesList:
    def test_parse_resolves_relative_to_list_dir(self, tmp_path):
        (tmp_path / "frames").mkdir()
        txt = "file 'frames/a.png'\nduration 0.500\nfile 'frames/b.png'\nduration 0.250\n"
        clips = lv.parse_frames_list(txt, tmp_path)
        assert [c.image for c in clips] == [tmp_path / "frames/a.png", tmp_path / "frames/b.png"]
        assert [c.duration_s for c in clips] == [0.5, 0.25]

    def test_parse_ignores_comments_and_blank_lines(self, tmp_path):
        txt = "# 注释\n\nfile 'a.png'\nduration 1.0\n"
        assert len(lv.parse_frames_list(txt, tmp_path)) == 1

    def test_tail_duplicate_is_merged_not_a_second_frame(self, tmp_path):
        """末帧被重复列一遍是 ffmpeg 的约定，不是"同一张图放两次"。"""
        txt = "file 'a.png'\nduration 0.500\nfile 'b.png'\nduration 0.500\nfile 'b.png'\n"
        clips = lv.dedupe_tail(lv.parse_frames_list(txt, tmp_path))
        assert [c.image.name for c in clips] == ["a.png", "b.png"]
        assert clips[-1].duration_s == pytest.approx(0.5)

    def test_frames_list_repeats_last_frame(self, tmp_path):
        """★ 末尾那行不是装饰：不带它，最后一帧的 duration 会被 ffmpeg 丢掉。"""
        clips = [lv.Clip(image=_png(tmp_path / "a.png"), duration_s=0.5),
                 lv.Clip(image=_png(tmp_path / "b.png"), duration_s=0.5)]
        lst = lv.write_frames_list(clips, tmp_path / "frames.txt")
        lines = [ln for ln in lst.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines[-1] == "file 'b.png'", "最后一帧必须再列一遍（ffmpeg 的 duration 约定）"
        # 裸文件名（不带目录）：清单与帧同目录、ffmpeg 以它为 cwd —— 绕开 `D:\` 被当协议前缀
        assert all(ln.startswith("file '") and "/" not in ln and "\\" not in ln
                   for ln in lines if ln.startswith("file "))


# ---------------- 前置校验（不报错的错法） ----------------

class TestPrecheck:
    def test_missing_frame_fails_before_ffmpeg(self, tmp_path):
        """★ ffmpeg 的 concat 遇到缺失文件**退出码仍是 0** —— 必须起进程前拦。"""
        tl = _timeline(tmp_path)
        tl.clips[1].image.unlink()
        with pytest.raises(lv.LectureVideoError, match="不存在"):
            lv.precheck(tl)

    def test_odd_size_is_rejected_not_cropped(self, tmp_path):
        """yuv420p 要偶数宽高 —— 报错让人去改，不许偷偷 crop 1px。"""
        tl = lv.Timeline(clips=[lv.Clip(image=_png(tmp_path / "o.png", (65, 64)), duration_s=0.5)],
                         audio=_wav(tmp_path / "v.wav", 0.5))
        with pytest.raises(lv.LectureVideoError, match="偶数"):
            lv.precheck(tl)

    def test_mixed_sizes_rejected(self, tmp_path):
        tl = lv.Timeline(
            clips=[lv.Clip(image=_png(tmp_path / "a.png", (64, 64)), duration_s=0.5),
                   lv.Clip(image=_png(tmp_path / "b.png", (128, 64)), duration_s=0.5)],
            audio=_wav(tmp_path / "v.wav", 1.0))
        with pytest.raises(lv.LectureVideoError, match="尺寸不一致"):
            lv.precheck(tl)

    def test_frames_must_share_one_directory(self, tmp_path):
        tl = lv.Timeline(
            clips=[lv.Clip(image=_png(tmp_path / "d1/a.png"), duration_s=0.5),
                   lv.Clip(image=_png(tmp_path / "d2/b.png"), duration_s=0.5)],
            audio=_wav(tmp_path / "v.wav", 1.0))
        with pytest.raises(lv.LectureVideoError, match="多个目录"):
            lv.common_frame_dir(tl.clips)

    def test_zero_duration_rejected(self, tmp_path):
        tl = lv.Timeline(clips=[lv.Clip(image=_png(tmp_path / "a.png"), duration_s=0.0)],
                         audio=_wav(tmp_path / "v.wav", 0.5))
        with pytest.raises(lv.LectureVideoError, match="时长"):
            lv.precheck(tl)

    def test_missing_audio_rejected(self, tmp_path):
        tl = lv.Timeline(clips=[lv.Clip(image=_png(tmp_path / "a.png"), duration_s=0.5)],
                         audio=tmp_path / "nope.wav")
        with pytest.raises(lv.LectureVideoError, match="音轨"):
            lv.precheck(tl)


# ---------------- 命令行（封口选项） ----------------

class TestPlanCommand:
    def _cmd(self, tmp_path, fit):
        return lv.plan_command("ffmpeg", tmp_path / "l.txt", tmp_path / "a.wav",
                               tmp_path / "o.mp4", {"width": 64, "height": 64},
                               30, 20, "medium", fit, ["-fps_mode", "cfr"])

    def test_audio_fit_uses_shortest_and_never_dash_t(self, tmp_path):
        """★ 收口只能用 `-shortest`：`-t` 与 concat 解复用器一起用会**变短**（实测）。"""
        cmd = self._cmd(tmp_path, "audio")
        assert "-shortest" in cmd
        assert "-t" not in cmd

    def test_fit_none_has_no_clamp(self, tmp_path):
        assert "-shortest" not in self._cmd(tmp_path, "none")

    def test_output_is_player_compatible(self, tmp_path):
        cmd = self._cmd(tmp_path, "audio")
        assert "yuv420p" in cmd and "+faststart" in cmd
        assert cmd[-1].endswith(".mp4")
        assert "-fps_mode" in cmd or "-vsync" in cmd  # ffmpeg 9 移除了 -vsync，二者必有其一

    def test_fit_audio_extends_last_frame_to_cover_narration(self, tmp_path, monkeypatch):
        """时间轴比音轨短 → 末帧补齐（否则 `-shortest` 会把最后一句剪掉）。"""
        tl = _timeline(tmp_path, durations=(0.2, 0.2), seconds=3.0)
        monkeypatch.setattr(lv, "probe_duration", lambda _fp, _p: 3.0)
        target, note = lv.fit_to_audio(tl, "audio", "ffprobe")
        assert target == pytest.approx(3.0)
        assert tl.clips[-1].duration_s == pytest.approx(0.2 + 2.6)
        assert "补" in note

    def test_fit_trim_does_not_extend(self, tmp_path, monkeypatch):
        tl = _timeline(tmp_path, durations=(0.2, 0.2), seconds=3.0)
        monkeypatch.setattr(lv, "probe_duration", lambda _fp, _p: 3.0)
        target, _note = lv.fit_to_audio(tl, "trim", "ffprobe")
        assert target == pytest.approx(0.4)  # 取较短，末帧不动（明说会剪旁白）
        assert tl.clips[-1].duration_s == pytest.approx(0.2)


# ---------------- 逐页拼装（course 产物） ----------------

def _course_job(tmp_path: Path, pages, *, slides=True, failed=(), gap_ms=0) -> Path:
    """搭一个 course 产物目录：manifest.json + page_%03d.wav（+ 可选幻灯片图）。"""
    job = tmp_path / "course_1"
    job.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, dur in enumerate(pages, start=1):
        bad = i in failed
        if not bad:
            _wav(job / f"page_{i:03d}.wav", dur)
        if slides:
            _png(job / "slides" / f"page_{i:03d}.png")
        entries.append({"page": i, "text": f"第{i}页", "url": "" if bad else f"/u/{i}.wav",
                        "duration_s": dur, "failed": bad, "error": "模拟失败" if bad else ""})
    (job / "manifest.json").write_text(json.dumps(
        {"total_pages": len(pages), "ok_pages": len(pages) - len(failed), "pages": entries},
        ensure_ascii=False), encoding="utf-8")
    return job / "manifest.json"


class TestPagesMode:
    def test_durations_come_from_the_wav_and_gap_joins_the_page(self, tmp_path, monkeypatch, ffmpeg_bin):
        """时长按**实际 wav** 算；`gap-ms` 静音算进该页自己的展示时长。"""
        mf = _course_job(tmp_path, [1.0, 0.5])
        _fake_ffprobe(monkeypatch, ffmpeg_bin)
        tl = lv.timeline_from_pages(mf, mf.parent / "slides", 200, 30,
                                    tmp_path / "voice.wav")
        assert [round(c.duration_s, 2) for c in tl.clips] == [1.2, 0.7]
        assert tl.audio is not None and tl.audio.is_file()
        import soundfile as sf

        d, sr = sf.read(str(tl.audio))
        assert len(d) / sr == pytest.approx(1.5 + 0.2, abs=0.01)  # 页间一处静音

    def test_manifest_drift_warns_and_actual_wins(self, tmp_path, monkeypatch, ffmpeg_bin):
        mf = _course_job(tmp_path, [1.0])
        data = json.loads(mf.read_text("utf-8"))
        data["pages"][0]["duration_s"] = 9.9  # manifest 记错了
        mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _fake_ffprobe(monkeypatch, ffmpeg_bin)
        tl = lv.timeline_from_pages(mf, mf.parent / "slides", 0, 30, tmp_path / "v.wav")
        assert tl.clips[0].duration_s == pytest.approx(1.0, abs=0.02)  # 以实际为准
        assert "9.9" in tl.note and "实际" in tl.note

    def test_failed_page_is_named_not_silently_dropped(self, tmp_path, monkeypatch, ffmpeg_bin):
        """少一页 = 视频里那一页的画面永远不会出现 —— 必须点名声张。"""
        mf = _course_job(tmp_path, [1.0, 1.0], failed=(2,))
        _fake_ffprobe(monkeypatch, ffmpeg_bin)
        tl = lv.timeline_from_pages(mf, mf.parent / "slides", 0, 30, tmp_path / "v.wav")
        assert len(tl.clips) == 1
        assert "跳过失败页 [2]" in tl.note

    def test_missing_slide_image_is_an_error(self, tmp_path, monkeypatch, ffmpeg_bin):
        mf = _course_job(tmp_path, [1.0], slides=False)
        _fake_ffprobe(monkeypatch, ffmpeg_bin)
        with pytest.raises(lv.LectureVideoError, match="幻灯片图"):
            lv.timeline_from_pages(mf, mf.parent / "slides", 0, 30, tmp_path / "v.wav")

    def test_stitch_rejects_mixed_sample_rates(self, tmp_path):
        a = _wav(tmp_path / "a.wav", 0.2, sr=24000)
        b = _wav(tmp_path / "b.wav", 0.2, sr=48000)
        with pytest.raises(lv.LectureVideoError, match="采样率"):
            lv.stitch_page_audio([(1, a), (2, b)], 0, tmp_path / "out.wav")


# ---------------- 真 ffmpeg 端到端 ----------------

@pytest.mark.usefixtures("ffmpeg_bin")
class TestRenderEndToEnd:
    def test_still_mode_makes_a_playable_mp4(self, tmp_path, ffmpeg_bin, monkeypatch):
        monkeypatch.setattr("common.find_ffmpeg", lambda: ffmpeg_bin)
        tl = lv.Timeline(clips=[lv.Clip(image=_png(tmp_path / "s.png"), duration_s=0.5)],
                         audio=_wav(tmp_path / "v.wav", 1.2), fps=30,
                         note="单测：一图一音轨")
        out = tmp_path / "o.mp4"
        info = lv.render(tl, out, preset="ultrafast", crf=30)
        assert out.is_file() and out.stat().st_size > 0
        assert info["out"]["video"]["codec"] == "h264"
        assert info["out"]["video"]["pix_fmt"] == "yuv420p"
        assert info["out"]["video"]["size"] == "64×64"
        assert info["out"]["audio"]["codec"] == "aac"
        # ★ fit=audio 的承诺：成片正好等于旁白长度（末帧补齐 + -shortest 收口）
        assert info["out"]["duration_s"] == pytest.approx(1.2, abs=0.1)
        assert "-shortest" in info["command"]

    def test_sidecar_records_the_exact_command(self, tmp_path, ffmpeg_bin, monkeypatch):
        """成片要能复现 —— 命令、cwd、ffmpeg 版本、逐帧来源都得留在凭据里。"""
        monkeypatch.setattr("common.find_ffmpeg", lambda: ffmpeg_bin)
        tl = _timeline(tmp_path, durations=(0.4, 0.4), seconds=0.8)
        out = tmp_path / "o.mp4"
        info = lv.render(tl, out, preset="ultrafast", crf=30)
        assert info["command"][0] == ffmpeg_bin
        assert info["cwd"] == str(tmp_path.resolve())
        assert info["ffmpeg"].startswith("ffmpeg version")
        assert len(info["clips"]) == 2 and info["target_duration_s"] == pytest.approx(0.8, abs=0.05)

    def test_failed_render_leaves_no_output(self, tmp_path, ffmpeg_bin, monkeypatch):
        """前置校验失败时**不许留下半成品**（0 字节的 mp4 会被当成"出片成功"）。"""
        monkeypatch.setattr("common.find_ffmpeg", lambda: ffmpeg_bin)
        tl = _timeline(tmp_path)
        tl.clips[0].image.unlink()
        out = tmp_path / "o.mp4"
        with pytest.raises(lv.LectureVideoError):
            lv.render(tl, out, preset="ultrafast", crf=30)
        assert not out.exists()

    def test_dry_run_writes_nothing(self, tmp_path, ffmpeg_bin, monkeypatch, capsys):
        monkeypatch.setattr("common.find_ffmpeg", lambda: ffmpeg_bin)
        tl = _timeline(tmp_path)
        out = tmp_path / "o.mp4"
        info = lv.render(tl, out, dry_run=True)
        assert info.get("dry_run") is True and not out.exists()
        assert "[dry-run]" in capsys.readouterr().out

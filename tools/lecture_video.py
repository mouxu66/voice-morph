"""讲课视频合成：帧序列 + 旁白音轨 → mp4（2026-10-08）。

为什么有这个模块
    2026-10-08 的讲稿录课实验**确实出过**两个能看的成片
    （`outputs/lecture_demo/极限的epsilon-delta定义_一页PPT讲课*.mp4`，
    1920×1080 / 30fps / h264 + AAC 48k，时长精确等于旁白音轨 96.30s / 91.74s），
    但**把帧和图音轨拼起来的那条 ffmpeg 命令没有留在任何地方** ——
    仓库、脚本、记忆、文档里只有两头：`render_slides.py` 出 `frames.txt`（帧清单
    含逐帧 duration）、`synth_lecture.py` 出 `voice.wav`（音轨），中间那一步是人
    手工敲的。后果是**成片不可复现**：改一版措辞、换一个音色，没人能照着重跑。

    这个模块就是那一步。它把「帧序列 + 音轨 → mp4」做成一条可复现的流水线，
    并把它旁边那几种**不报错的错法**逐个前置拦掉：

      · **清单里少一个文件，ffmpeg 退出码仍是 0**（只在 stderr 报一句
        `Error during demuxing`）—— 本项目实测踩过（见 `docs/犯错指南.md`
        速查表）。所以帧文件必须在起 ffmpeg **之前**逐个 `exists()`，见 `precheck`。
      · **`-shortest` 与"会不会剪掉旁白"是同一件事的两面**：原脚本靠"尾帧
        duration +0.001 再把最后一帧重复一遍"绕过去，属于知其然。这里默认
        `--fit audio` —— 先按差额把最后一帧补齐到音轨长度，再让 `-shortest` 收口，
        成片**正好等于旁白长度**、且旁白永远不会被剪（讲稿视频里被剪的是最后一句）。
        封口选项三个候选只有它对：不加则尾巴多停 0.43s；`-t` 反而更短（见
        `plan_command` 的实测数字）。
      · **帧尺寸不一致 / 宽高是奇数**：`yuv420p` 要求偶数；混尺寸的 concat 会花屏
        或直接失败。前置用 PIL 只读文件头量一遍，报错要指名道姓。
      · **concat 清单的路径歧义**：`D:\\...` 会被当成协议前缀、相对路径按谁的 CWD
        解析也随版本变。所以清单里一律写**裸文件名**，并让 ffmpeg 以帧所在目录为
        cwd（`_common_frame_dir`）。
      · **`-vsync` 在 ffmpeg 9 已移除**（本机 9.0 full build 实测）→ 统一用
        `-fps_mode cfr`，并对老版本 ffmpeg 回退 `-vsync cfr`（`fps_mode_args`）。

产物旁边落一份 `<out>.timeline.json`：逐帧来源与时长、音轨、ffmpeg 版本与**逐字的
命令行**。下次要复现或追责，读它就行 —— 与 `tools/voicebank_synth.py` 的
`provenance.json` 同一个动机：**手工搬运产生的是静默错误**。

用法
    # ① 复现 2026-10-08 那两个成片（读 demo 的 frames.txt + voice.wav）
    python tools/lecture_video.py --demo-build outputs/lecture_demo/build --out a.mp4

    # ② 讲稿录课的产物：manifest.json + 逐页幻灯片图 + 逐页 wav
    python tools/lecture_video.py --pages outputs/course_1791441354202/manifest.json \\
        --slides outputs/course_1791441354202/slides --out course.mp4 --gap-ms 350

    # ③ 一张图配一条音轨（最小用例）
    python tools/lecture_video.py --still slide.png --audio voice.wav --out x.mp4

    # ④ 完全显式的时间轴（自己写 JSON，字段见 timeline_from_json）
    python tools/lecture_video.py --timeline tl.json --out x.mp4

    # 先看它打算干什么，不执行
    python tools/lecture_video.py --demo-build outputs/lecture_demo/build --out a.mp4 --dry-run

不做的
    · **不生成画面**：本模块只负责"帧 → 成片"。画面从哪来是另一件事
      （`render_slides.py` 那种按讲稿画要点卡，或 PowerPoint/LibreOffice 导出的
      课件页）。见 docs 里的路线说明。
    · **不改帧的几何**：奇数宽高直接报错让人去改，不偷偷 crop/scale 1px。
    · **不猜页间停顿**：`--gap-ms` 由调用方给（course 的 `gap_ms` 目前只在内存里，
      没进 manifest —— 这是已知缺口，所以这里不假装能读出来）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 让 `import common`（find_ffmpeg）在「从仓库根直接跑 python tools/lecture_video.py」时也通
# （与 voicebank_synth.py / voicebank_eval.py 同一段引导代码）
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 逐页幻灯片的文件名候选（相对 --slides 目录）。course 的产物只有 wav，图片由调用方放。
SLIDE_PATTERNS = ("page_{page:03d}.png", "slide{page:03d}.png", "{page:03d}.png",
                  "page_{page:03d}.jpg", "{page}.png")
#: 每页旁白 wav 的候选名（相对 manifest 所在目录）—— 与 course_api 的落盘约定一致
PAGE_AUDIO_PATTERNS = ("page_{page:03d}.wav",)
#: 时长容差（秒）：ffprobe 与 manifest 的 `duration_s` 差超过它就告警（不阻断）
DURATION_TOLERANCE = 0.05
#: 成片时长允许比音轨短多少（fit=audio 时应当 ≥ 0）
MIN_COVER_S = 0.0


class LectureVideoError(RuntimeError):
    """业务错（报错文案直接给用户看）。"""


@dataclass
class Clip:
    """一帧：图片 + 它在成片里占的时长。"""

    image: Path
    duration_s: float


@dataclass
class Timeline:
    clips: list[Clip] = field(default_factory=list)
    audio: Path | None = None  # 单条音轨（pages 模式会先拼成一条再指过来）
    fps: int = 30
    note: str = ""  # 进 sidecar，说明这份时间轴是怎么来的

    @property
    def total_s(self) -> float:
        return sum(c.duration_s for c in self.clips)


# ---------------- ffmpeg / ffprobe ----------------


def find_ffprobe(ffmpeg: str | None = None) -> str:
    """ffprobe 路径：优先与 ffmpeg 同目录（winget 的 full build 两个都在）。"""
    if ffmpeg is None:
        from common import find_ffmpeg

        ffmpeg = find_ffmpeg()
    cand = Path(ffmpeg).parent / "ffprobe.exe"
    if cand.is_file():
        return str(cand)
    same_dir = Path(ffmpeg).parent / "ffprobe"
    if same_dir.is_file():
        return str(same_dir)
    return shutil.which("ffprobe") or "ffprobe"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800):
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def ffmpeg_version(ffmpeg: str) -> str:
    """`ffmpeg -version` 第一行（进 sidecar：成片要能追到是哪个版本编的）。"""
    try:
        return (_run([ffmpeg, "-version"], timeout=60).stdout or "").splitlines()[0].strip()
    except Exception:  # noqa: BLE001 —— 版本读不到不该阻断出片
        return ""


def fps_mode_args(ffmpeg: str) -> list[str]:
    """恒定帧率的写法：新版 `-fps_mode cfr`，老版回退 `-vsync cfr`。

    ffmpeg 9.0 已移除 `-vsync`（本机实测，`-h full` 里只剩 `fps_mode`）——
    写死任一个都会在另一头炸，所以探一次。
    """
    try:
        out = (_run([ffmpeg, "-h", "full"], timeout=60).stdout or "")
    except Exception:  # noqa: BLE001
        out = ""
    return ["-fps_mode", "cfr"] if "-fps_mode" in out else ["-vsync", "cfr"]


def probe_duration(ffprobe: str, path: Path) -> float:
    """媒体时长（秒）。读不出来抛业务错（不要静默返回 0 —— 0 会被当成"没有尾巴"）。"""
    r = _run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        timeout=120,
    )
    txt = (r.stdout or "").strip().splitlines()
    try:
        return float(txt[0])
    except (IndexError, ValueError) as e:
        raise LectureVideoError(
            f"读不出时长：{path.name}（ffprobe: {(r.stderr or '').strip()[:200]}）"
        ) from e


def probe_streams(ffprobe: str, path: Path) -> dict:
    r = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
             timeout=120)
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise LectureVideoError(
            f"ffprobe 读不出媒体信息：{path.name}（{(r.stderr or '').strip()[:200]}）"
        ) from e


# ---------------- 时间轴：三种来源 ----------------


def parse_frames_list(text: str, base_dir: Path) -> list[Clip]:
    """ffmpeg concat 清单 → clips。相对路径按**清单所在目录**解析。

    只认两种指令：`file '...'` 与 `duration <秒>`（其余行忽略 —— ffmpeg 自己的清单
    也可能带 `#` 注释）。清单末尾常见"把最后一帧再列一遍"，那是 ffmpeg 的约定
    （最后一帧的 duration 要靠重复出现才生效），解析时**按出现顺序保留**，
    由 `dedupe_tail` 决定要不要合并，免得凭空多出一帧。
    """
    clips: list[Clip] = []
    cur: Path | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file "):
            val = line[5:].strip().strip("'\"")
            p = Path(val)
            cur = p if p.is_absolute() else (base_dir / p)
        elif line.startswith("duration ") and cur is not None:
            try:
                dur = float(line.split(None, 1)[1].split()[0])
            except (IndexError, ValueError):
                continue
            clips.append(Clip(image=cur, duration_s=dur))
            cur = None
    return clips


def dedupe_tail(clips: list[Clip]) -> list[Clip]:
    """合并清单尾部"重复列出的最后一帧"（ffmpeg 约定，不是第二帧画面）。"""
    if len(clips) >= 2 and clips[-1].image == clips[-2].image:
        merged = Clip(image=clips[-1].image, duration_s=clips[-1].duration_s + clips[-2].duration_s)
        return clips[:-2] + [merged]
    return clips


def timeline_from_demo_build(build: Path, fps: int) -> Timeline:
    """复现 2026-10-08 的成片：`<build>/frames.txt` + `<build>/voice.wav`。"""
    lst = build / "frames.txt"
    wav = build / "voice.wav"
    if not lst.is_file():
        raise LectureVideoError(f"找不到帧清单：{lst}（--demo-build 要指向 render_slides.py 的 build 目录）")
    if not wav.is_file():
        raise LectureVideoError(f"找不到音轨：{wav}（先跑 synth_lecture.py）")
    clips = dedupe_tail(parse_frames_list(lst.read_text(encoding="utf-8"), lst.parent))
    if not clips:
        raise LectureVideoError(f"帧清单里一帧都没解析出来：{lst}")
    return Timeline(clips=clips, audio=wav, fps=fps,
                    note=f"demo build（{lst.name} + {wav.name}）")


def timeline_from_json(path: Path) -> Timeline:
    """显式时间轴：`{"fps":30, "audio":"voice.wav", "clips":[{"image":"a.png","duration_s":1.2}]}`。

    相对路径按 JSON 文件所在目录解析（与清单同一约定）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    clips: list[Clip] = []
    for i, c in enumerate(data.get("clips") or []):
        try:
            img, dur = str(c["image"]), float(c["duration_s"])
        except (KeyError, TypeError, ValueError) as e:
            raise LectureVideoError(f"timeline 第 {i + 1} 条缺 image/duration_s：{c!r}") from e
        p = Path(img)
        clips.append(Clip(image=p if p.is_absolute() else (base / p), duration_s=dur))
    if not clips:
        raise LectureVideoError(f"timeline 里没有 clips：{path}")
    audio = data.get("audio")
    ap = Path(audio) if audio else None
    if ap is not None and not ap.is_absolute():
        ap = base / ap
    return Timeline(clips=clips, audio=ap, fps=int(data.get("fps") or 30),
                    note=f"timeline {path.name}")


def _find_first(directory: Path, patterns, page: int) -> Path | None:
    for pat in patterns:
        p = directory / pat.format(page=page)
        if p.is_file():
            return p
    return None


def stitch_page_audio(pages: list[tuple[int, Path]], gap_ms: int, out: Path) -> Path:
    """逐页旁白 → 一条音轨（页与页之间插 `gap_ms` 静音）。

    ⚠️ 采样率必须一致：不一样就报错（不重采样 —— 悄悄改采样率会让时长与
    manifest 对不上，而"总时长对不上"正是这条链路最容易静默出错的地方）。
    """
    import numpy as np
    import soundfile as sf

    gap = int(24000 * max(0, gap_ms) / 1000)
    pieces: list = []
    sr_ref: int | None = None
    for page, wav in pages:
        d, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if getattr(d, "ndim", 1) > 1:
            d = d.mean(axis=1)
        if sr_ref is None:
            sr_ref = int(sr)
        elif int(sr) != sr_ref:
            raise LectureVideoError(
                f"第 {page} 页的采样率 {sr} 与第 1 页的 {sr_ref} 不一致 —— "
                f"不重采样（会改成片时长对不上），请重跑那一页"
            )
        pieces.append(np.ascontiguousarray(d, dtype=np.float32))
        if gap:
            pieces.append(np.zeros(gap, dtype=np.float32))
    if pieces and gap:
        pieces.pop()  # 末尾不加静音
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32),
             sr_ref or 24000, subtype="PCM_16")
    return out


def timeline_from_pages(
    manifest_path: Path, slides_dir: Path, gap_ms: int, fps: int, out_voice: Path
) -> Timeline:
    """讲稿录课产物：`manifest.json` + 逐页幻灯片图 + 逐页 wav → 时间轴。

    时长以**实际 wav** 为准（manifest 的 `duration_s` 是报告，wav 才是事实；
    两者差超过 `DURATION_TOLERANCE` 就告警）。`--gap-ms` 的静音算进**该页自己的
    展示时长**（页面在停顿期间保持不动，而不是闪一帧黑）。
    """
    from common import find_ffmpeg  # 只为拿 ffprobe

    ffprobe = find_ffprobe(find_ffmpeg())
    if not manifest_path.is_file():
        raise LectureVideoError(f"找不到 manifest：{manifest_path}")
    mf = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = mf.get("pages") or []
    if not pages:
        raise LectureVideoError(f"manifest 里没有 pages：{manifest_path}")
    job_dir = manifest_path.parent

    clips: list[Clip] = []
    audio_pages: list[tuple[int, Path]] = []
    skipped: list[int] = []
    warnings: list[str] = []
    for p in pages:
        page = int(p["page"])
        if p.get("failed") or not p.get("url"):
            skipped.append(page)  # 失败页没有 wav，出不了片 —— 明说，不静默跳过
            continue
        img = _find_first(slides_dir, SLIDE_PATTERNS, page)
        if img is None:
            raise LectureVideoError(
                f"第 {page} 页没有幻灯片图（{slides_dir} 下试过：{'、'.join(SLIDE_PATTERNS)}）"
                f" —— 画面从哪来是另一件事，本工具只负责拼"
            )
        wav = _find_first(job_dir, PAGE_AUDIO_PATTERNS, page)
        if wav is None:
            raise LectureVideoError(f"第 {page} 页没有旁白 wav（{job_dir} 下试过：{'、'.join(PAGE_AUDIO_PATTERNS)}）")
        dur = probe_duration(ffprobe, wav)
        reported = float(p.get("duration_s") or 0.0)
        if reported and abs(reported - dur) > DURATION_TOLERANCE:
            warnings.append(f"第 {page} 页 manifest 记 {reported}s，实际 {dur:.2f}s（以实际为准）")
        clips.append(Clip(image=img, duration_s=dur + max(0, gap_ms) / 1000))
        audio_pages.append((page, wav))
    if not clips:
        raise LectureVideoError(f"没有一页能出片（失败页：{skipped or '无'}）")

    voice = stitch_page_audio(audio_pages, gap_ms, out_voice)
    note = f"course manifest {manifest_path.name}（{len(clips)} 页，gap={gap_ms}ms"
    note += f"，跳过失败页 {skipped}" if skipped else ""
    note += "）"
    if warnings:
        note += "；⚠️ " + "；".join(warnings)
    return Timeline(clips=clips, audio=voice, fps=fps, note=note)


def timeline_from_still(image: Path, audio: Path, fps: int) -> Timeline:
    """一张图配一条音轨（时长 = 音轨）。"""
    from common import find_ffmpeg

    dur = probe_duration(find_ffprobe(find_ffmpeg()), audio)
    return Timeline(clips=[Clip(image=image, duration_s=dur)], audio=audio, fps=fps,
                    note=f"单图 {image.name} + {audio.name}")


# ---------------- 前置校验 / 出片 ----------------


def precheck(tl: Timeline) -> dict:
    """起 ffmpeg 之前把"不报错的错"全拦掉，顺带量出成片几何。"""
    if not tl.clips:
        raise LectureVideoError("时间轴是空的")
    if tl.fps <= 0:
        raise LectureVideoError(f"fps 必须为正：{tl.fps}")
    missing = [c.image for c in tl.clips if not c.image.is_file()]
    if missing:
        # ★ ffmpeg 的 concat 对缺失文件**退出码仍是 0**，所以这一条必须在起进程前查
        head = "、".join(m.name for m in missing[:5])
        raise LectureVideoError(
            f"{len(missing)} 帧文件不存在（ffmpeg 的 concat 遇到这种情况退出码仍是 0，"
            f"所以这里先拦）：{head}{' …' if len(missing) > 5 else ''}"
        )
    bad = [c for c in tl.clips if c.duration_s <= 0]
    if bad:
        raise LectureVideoError(f"{len(bad)} 帧的时长为 0 或负：{bad[0].image.name}")
    if tl.audio is None or not tl.audio.is_file():
        raise LectureVideoError(f"音轨不存在：{tl.audio}")

    from PIL import Image

    sizes: dict[tuple[int, int], list[str]] = {}
    for c in tl.clips:
        try:
            with Image.open(c.image) as im:
                sizes.setdefault(im.size, []).append(c.image.name)
        except Exception as e:  # noqa: BLE001
            raise LectureVideoError(f"读不出图片：{c.image.name}（{type(e).__name__}: {e}）") from e
    if len(sizes) > 1:
        detail = "；".join(f"{w}×{h}×{len(n)}" for (w, h), n in sizes.items())
        raise LectureVideoError(
            f"帧尺寸不一致（{detail}）—— yuv420p 要求全片同尺寸，请先把画面统一"
        )
    (w, h) = next(iter(sizes))
    if w % 2 or h % 2:
        raise LectureVideoError(
            f"帧尺寸 {w}×{h} 不是偶数 —— yuv420p 编不出来。"
            f"本工具**不会偷偷 crop**，请把画面改成偶数宽高（如 {w - w % 2}×{h - h % 2}）"
        )
    return {"width": w, "height": h, "frames": len(tl.clips)}


def common_frame_dir(clips: list[Clip]) -> Path:
    """所有帧的共同目录（清单里写裸文件名 + cwd 指到它，绕开路径歧义）。"""
    dirs = {c.image.parent.resolve() for c in clips}
    if len(dirs) != 1:
        names = "、".join(sorted(str(d) for d in dirs)[:3])
        raise LectureVideoError(f"帧分散在多个目录（{names}…）—— concat 只接受同目录，请先归拢")
    return next(iter(dirs))


def write_frames_list(clips: list[Clip], path: Path) -> Path:
    """写 concat 清单：**裸文件名** + 逐帧 duration，末尾重复最后一帧。

    ⚠️ 末尾那行不是装饰，是**必须**的（2026-10-08 实测，`outputs/_frames_tail_probe.py`）：
    concat 解复用器会给"重复出现的最后一帧"再算一次时长，**不重复则最后一帧的
    duration 直接丢掉** —— 同一份清单（2 张图各 0.5s + 1.0s 音轨）：
    不带尾行 → 17 帧 / 0.567s（末帧被吃掉）；带尾行 → 43 帧 / 1.433s。
    所以「尾行 + `-shortest`」才是原成片那条命令的完整形态（见 `plan_command`）。
    """
    lines = [f"file '{c.image.name}'\nduration {c.duration_s:.3f}\n" for c in clips]
    lines.append(f"file '{clips[-1].image.name}'\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


def fit_to_audio(tl: Timeline, mode: str, ffprobe: str) -> tuple[float, str]:
    """按 `--fit` 决定"视频该有多长"，返回 (目标秒数, 说明)。

    `audio`（默认）：不足就**把最后一帧补齐**到音轨长度 —— 讲稿视频里被剪掉的
    永远是最后一句，所以宁可多停一帧。配合 `-shortest` 收口，成片 = 音轨长度。
    `trim`：不补齐，交给 `-shortest` 取两者较短（时间轴比音轨短时会**剪掉旁白**，
    只有明确知道自己在干什么才用）。
    `none`：不加 `-shortest`，成片 = 时间轴长度（尾部重复行会让它比时间轴多约
    0.4s，音轨先结束就是一段静音）。
    """
    audio_s = probe_duration(ffprobe, tl.audio)  # type: ignore[arg-type]
    if mode == "none":
        return tl.total_s, f"原样 {tl.total_s:.2f}s（音轨 {audio_s:.2f}s，不加收口）"
    if mode == "trim":
        return min(audio_s, tl.total_s), (
            f"取较短（--fit trim）：时间轴 {tl.total_s:.2f}s / 音轨 {audio_s:.2f}s"
        )
    gap = audio_s - tl.total_s
    if gap > 0.001:
        tl.clips[-1] = Clip(image=tl.clips[-1].image, duration_s=tl.clips[-1].duration_s + gap)
        return audio_s, f"末帧补 {gap:.2f}s 到音轨长度（{tl.total_s:.2f}s）"
    return audio_s, f"帧序列已覆盖音轨（{tl.total_s:.2f}s ≥ {audio_s:.2f}s）"


def plan_command(
    ffmpeg: str, list_path: Path, audio: Path, out: Path, geom: dict,
    fps: int, crf: int, preset: str, fit: str, fps_args: list[str]
) -> list[str]:
    """成片的 ffmpeg 命令行（逐字进 sidecar，也是 --dry-run 打印的东西）。

    ★ 封口选项是这条链路最讲究的一处，三个候选**只有 `-shortest` 对**（实测）：
        · 什么都不加：尾部重复行会让视频比时间轴长 0.43s（成片尾巴多停一帧）；
        · `-t <秒>`：**反而更短**（1.0s 的用例只出 0.567s）—— concat 解复用器
          与 `-t` 的相互作用会把末尾几帧丢掉，所以本工具**不用 `-t`**；
        · `-shortest`：输出长度 = min(视频, 音轨)。配合"末帧补齐到音轨"
          （`--fit audio` 的默认行为）就得到**正好等于旁白长度**的成片 ——
          原成片 96.300s / 2889 帧就是这么做出来的。
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_path.name,
        "-i", str(audio),
        *fps_args, "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    ]
    if fit in ("audio", "trim"):
        cmd.append("-shortest")
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def render(
    tl: Timeline, out: Path, *, crf: int = 20, preset: str = "medium", fit: str = "audio",
    dry_run: bool = False, keep_list: bool = False
) -> dict:
    """出片。返回 sidecar（调用方决定落不落盘）。"""
    from common import find_ffmpeg

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    # ★ 一切都绝对化：ffmpeg 是以**帧目录**为 cwd 跑的，这里留相对路径就会被解析到
    # 帧目录下面去（实测：`-i outputs/.../voice.wav` 直接找不到文件）。
    tl.audio = tl.audio.resolve() if tl.audio else None
    for c in tl.clips:
        c.image = c.image.resolve()
    geom = precheck(tl)
    frames_dir = common_frame_dir(tl.clips)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ★ 顺序要紧：**先**按 --fit 定长度（末帧补齐会改 clips[-1]），**再**写清单。
    # 反过来的话清单里还是补之前的时长，`-shortest` 就会把旁白剪掉 ——
    # 实测踩过（末帧补齐没进清单 → 成片 0.57s vs 音轨 1.20s）。
    target_s, fit_note = fit_to_audio(tl, fit, ffprobe)
    # 清单也写进**帧目录**：里面是裸文件名，只有 cwd 一致才找得到（见模块注释的路径歧义）
    list_path = frames_dir / f"{out.stem}.frames.txt"
    write_frames_list(tl.clips, list_path)
    cmd = plan_command(ffmpeg, list_path, tl.audio, out.resolve(), geom,
                       tl.fps, crf, preset, fit, fps_mode_args(ffmpeg))
    info = {
        "tool": "tools/lecture_video.py",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": tl.note,
        "ffmpeg": ffmpeg_version(ffmpeg),
        "cwd": str(frames_dir),
        "command": cmd,
        "fps": tl.fps, "crf": crf, "preset": preset, "fit": fit, "fit_note": fit_note,
        "geometry": geom,
        "target_duration_s": round(target_s, 3),
        "audio": str(tl.audio),
        "clips": [{"image": str(c.image), "duration_s": round(c.duration_s, 3)} for c in tl.clips],
    }
    if dry_run:
        info["dry_run"] = True
        print("[dry-run] cwd:", frames_dir)
        print("[dry-run]", " ".join(cmd))
        return info

    r = _run(cmd, cwd=frames_dir)
    if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        raise LectureVideoError(
            "ffmpeg 出片失败（exit %s）：%s" % (r.returncode, " | ".join(tail) or "没有 stderr")
        )
    if not keep_list:
        list_path.unlink(missing_ok=True)

    # ★ 复核：不是"ffmpeg 返回 0 就完事"—— 时长/轨道/像素格式都要读回来对一遍
    st = probe_streams(ffprobe, out)
    v = next((s for s in st.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in st.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(st.get("format", {}).get("duration") or 0.0)
    problems = []
    if v is None:
        problems.append("没有视频轨")
    if a is None:
        problems.append("没有音频轨")
    if v is not None and v.get("pix_fmt") != "yuv420p":
        problems.append(f"像素格式是 {v.get('pix_fmt')}（播放器兼容性差，应为 yuv420p）")
    if v is not None and (int(v.get("width") or 0), int(v.get("height") or 0)) != (geom["width"], geom["height"]):
        problems.append(f"成片分辨率 {v.get('width')}×{v.get('height')} 与帧不一致")
    audio_s = probe_duration(ffprobe, tl.audio)  # type: ignore[arg-type]
    if fit != "none" and dur + MIN_COVER_S < audio_s - 0.25:
        # 被剪掉的永远是最后一句 —— 这是本工具最不能接受的结果
        problems.append(f"成片 {dur:.2f}s 短于音轨 {audio_s:.2f}s：旁白被剪了")
    if fit == "audio" and abs(dur - audio_s) > 0.15:
        # fit=audio 的承诺是"正好等于旁白长度"（-shortest 收口），差得多说明收口失效
        problems.append(f"成片 {dur:.2f}s 与音轨 {audio_s:.2f}s 相差超过 0.15s：收口失效")
    info["out"] = {
        "path": str(out), "duration_s": round(dur, 3), "bytes": out.stat().st_size,
        "video": None if v is None else {
            "codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
            "size": f"{v.get('width')}×{v.get('height')}", "fps": v.get("r_frame_rate"),
        },
        "audio": None if a is None else {"codec": a.get("codec_name"), "sr": a.get("sample_rate"),
                                        "channels": a.get("channels")},
    }
    if problems:
        raise LectureVideoError("成片自检没过：" + "；".join(problems))
    return info


def _print_summary(info: dict, out: Path) -> None:
    o = info["out"]
    print(f"[ok] {out}  {o['duration_s']:.2f}s  {o['bytes'] / 1024:.0f} KB")
    print(f"     视频 {o['video']['codec']}/{o['video']['pix_fmt']}/{o['video']['size']}"
          f"/{o['video']['fps']}  音频 {o['audio']['codec']}/{o['audio']['sr']}Hz")
    print(f"     帧 {info['geometry']['frames']} 张，{info['fit_note']}")
    print(f"     复现凭据：{out}.timeline.json（含逐字命令行与 ffmpeg 版本）")


def build_timeline(args, out: Path) -> Timeline:
    """按 CLI 选的那一种来源搭时间轴。"""
    if args.demo_build:
        return timeline_from_demo_build(Path(args.demo_build), args.fps)
    if args.timeline:
        return timeline_from_json(Path(args.timeline))
    if args.pages:
        mf = Path(args.pages)
        if not args.slides:
            raise LectureVideoError("--pages 必须同时给 --slides（逐页幻灯片图目录）")
        return timeline_from_pages(mf, Path(args.slides), args.gap_ms, args.fps,
                                   out.with_suffix(".voice.wav"))
    if args.still:
        if not args.audio:
            raise LectureVideoError("--still 必须同时给 --audio")
        return timeline_from_still(Path(args.still), Path(args.audio), args.fps)
    raise LectureVideoError("要指定一种来源：--demo-build / --timeline / --pages / --still")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="帧序列 + 旁白音轨 → mp4（讲课视频，可复现）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demo-build", help="lecture_demo 的 build 目录（读 frames.txt + voice.wav）")
    src.add_argument("--timeline", help="显式时间轴 JSON 文件")
    src.add_argument("--pages", help="course 的 manifest.json（配 --slides 用）")
    src.add_argument("--still", help="一张图（配 --audio，时长=音轨）")
    ap.add_argument("--slides", help="逐页幻灯片图目录（--pages 用）")
    ap.add_argument("--audio", help="音轨（--still 用）")
    ap.add_argument("--out", required=True, help="输出的 mp4")
    ap.add_argument("--fps", type=int, default=30, help="输出帧率（默认 30）")
    ap.add_argument("--crf", type=int, default=20, help="x264 质量（默认 20，越小越清晰）")
    ap.add_argument("--preset", default="medium", help="x264 preset（默认 medium）")
    ap.add_argument("--fit", choices=("audio", "trim", "none"), default="audio",
                    help="时长策略：audio=末帧补齐到音轨（默认，保证旁白不被剪）")
    ap.add_argument("--gap-ms", type=int, default=0, help="--pages 用：页与页之间插多少毫秒静音")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    ap.add_argument("--keep-list", action="store_true", help="保留中间生成的 frames.txt")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    try:
        tl = build_timeline(args, out)
        info = render(tl, out, crf=args.crf, preset=args.preset, fit=args.fit,
                      dry_run=args.dry_run, keep_list=args.keep_list)
        if not args.dry_run:
            sidecar = out.with_suffix(out.suffix + ".timeline.json")
            sidecar.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            _print_summary(info, out)
        return 0
    except LectureVideoError as e:
        print(f"[失败] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

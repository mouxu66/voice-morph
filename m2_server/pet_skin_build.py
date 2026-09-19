"""人偶皮肤转换器：把开源素材源 → 统一「皮肤包」格式。

皮肤包格式（D:/变声/outputs/pet-skins/<id>/）：
  skin.json    元数据 + 各状态动画配置（frameW/frameH/states{state:{sheet,frames,dur}}）
  <state>.webp 每状态一个横向 spritesheet（帧横向拼接，宽 = 帧宽 × 帧数）
  preview.png  市场卡片预览图（可缺省，前端回退占位）
  LICENSE      素材来源许可副本（含 attribution 与原文）

适配器（source_type，由市场 manifest 分派）：
  - atlas-8x9    单张 atlas（Codex / petdex 社区格式：8 列 × 多行，每行一个状态，
                 帧尺寸如 192×208），按行 crop 出横向 strip → webp。
                 行语义默认 [idle,listen,play,build,think,error] = [0,1,2,2,4,3]，
                 manifest 可用 meta["row_map"] 覆盖。
  - existing-150 现成素材：每状态一个文件（如芙宁娜 svg/*.webp），直接复制 + 写
                 skin.json，零转换（主要用于内置 bundle）。
  - gif-multi    若干 gif 动画（OpenGameArt 猫等），把每个 gif 拆帧横向 hstack 成
                 strip → webp。meta["gif_map"] 指定 {state: gif 文件名}。
  - pixel-json   程序化像素宠物 JSON（CanFlyhang/Desktop-Pixel-Pet 格式：size +
                 palette{key:[r,g,b,a]} + frames[{name,pixels: 行×列 palette key}]）。
                 用 numpy 查调色板渲染 RGBA 帧，ffmpeg rawvideo 管道合成 webp strip。

webp 编码复用 common.find_ffmpeg()（完整 build 才有 libwebp muxer）。
本模块不依赖 API/网络（下载在 pet_market.py），可被 CLI 与单测直接调用。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from common import find_ffmpeg

# 状态 key 白名单（对齐桌宠现有状态机，渲染层兜底回退）
SKIN_STATE_KEYS = ("idle", "listen", "think", "play", "build", "error")
# 缺省回退：listen→idle / build→play（渲染层兜底，skin.json 无需显式写）
STATE_FALLBACK = {"listen": "idle", "build": "play"}

# atlas 行语义默认值（Codex/petdex 生态：0=idle 1=look 2=run 3=fail 4=think 5=jump）
DEFAULT_ROW_MAP = {"idle": 0, "listen": 1, "play": 2, "build": 2, "think": 4, "error": 3}

# 各状态默认动画周期（秒），与现有芙宁娜保持一致
DEFAULT_DUR = {"idle": 3.0, "listen": 3.0, "think": 3.0, "play": 1.8, "build": 1.4, "error": 3.0}

MAX_GIF_FRAMES = 96  # gif 帧数上限：横 strip 总宽 = N×frameW，超限滤镜/webp（≤16383px）都扛不住

_SKIN_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class SkinBuildError(Exception):
    """皮肤转换层的业务错误（API 未捕获时透传为 4xx）。"""


def is_valid_skin_id(skin_id: str) -> bool:
    return bool(skin_id) and bool(_SKIN_ID_RE.match(skin_id))


def default_row_map() -> dict[str, int]:
    return dict(DEFAULT_ROW_MAP)


def _run(cmd: list[str], timeout: int = 600) -> None:
    """执行外部命令（ffmpeg/ffprobe），失败抛 SkinBuildError。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:  # pragma: no cover - 环境依赖
        raise SkinBuildError(f"找不到可执行文件: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise SkinBuildError(f"命令超时: {cmd[0]}") from exc
    if r.returncode != 0:
        raise SkinBuildError(f"命令失败 {cmd}: {(r.stderr or '')[-400:]}")


def _run_capture(cmd: list[str], timeout: int = 120) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SkinBuildError(f"命令失败 {cmd}: {(r.stderr or '')[-400:]}")
    return r.stdout


def _ffprobe() -> str:
    """返回 ffprobe 路径（优先与 ffmpeg 同目录，兜底 PATH）。"""
    ff = Path(find_ffmpeg())
    cand = ff.parent / "ffprobe.exe"
    if cand.exists():
        return str(cand)
    return shutil.which("ffprobe") or "ffprobe"  # pragma: no cover


def probe_size(path: Path) -> tuple[int, int]:
    """用 ffprobe 探测图片/视频宽度×高度。"""
    out = json.loads(
        _run_capture(
            [
                _ffprobe(),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams = out.get("streams") or []
    if not streams:
        raise SkinBuildError(f"无法探测媒体信息: {path.name}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def probe_frames(path: Path) -> int:
    """探测动画帧数（gif/webp/apng 用 nb_frames，缺省回退 -count_frames）。"""
    cmd = [
        _ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    n = (r.stdout or "").strip()
    if n.isdigit() and int(n) > 0:
        return int(n)
    r2 = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    n2 = (r2.stdout or "").strip()
    if n2.isdigit() and int(n2) > 0:
        return int(n2)
    return 1


def cut_atlas_row(atlas: Path, out: Path, row: int, frame_w: int, frame_h: int, cols: int) -> None:
    """把 atlas 的第 row 行切成横向 strip（该行本来就是『列数量帧横向排开』）。"""
    cmd = [
        find_ffmpeg(),
        "-y",
        "-i",
        str(atlas),
        "-vf",
        f"crop={cols * frame_w}:{frame_h}:0:{row * frame_h}",
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-q:v",
        "80",
        str(out),
    ]
    _run(cmd)


def _gif_to_strip(src: Path, out: Path) -> None:
    """gif 动画 → 拆帧 + 横向 hstack → 单张 strip webp。

    用 ffprobe 探测帧数 N，动态构造 split=N → 每支路取首帧 → hstack=N。
    单帧（N=1）直接缩放转出，避免 hstack=inputs=1 非法。
    """
    n = probe_frames(src)
    w, h = probe_size(src)
    w = max(2, w - (w % 2))  # hstack 需要偶数宽
    h = max(2, h - (h % 2))
    if n > MAX_GIF_FRAMES or n * w > 16000:
        # 预告片式长 gif（如 repo demo 动图 227 帧）不适合做皮肤：提前人话报错，
        # 别等 hstack 拼出十几万像素宽的滤镜图再吐 Invalid argument。
        raise SkinBuildError(
            f"gif 帧数过多（{n} 帧 × {w}px，横条总宽 {n * w}px）"
            f"不适合转成皮肤动画（上限 {MAX_GIF_FRAMES} 帧）"
        )
    if n <= 1:
        cmd = [
            find_ffmpeg(),
            "-y",
            "-i",
            str(src),
            "-vf",
            f"scale={w}:{h}",
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-lossless",
            "0",
            "-q:v",
            "80",
            str(out),
        ]
        _run(cmd, timeout=900)
        return
    split_out = "".join(f"[b{i}]" for i in range(n))
    pick = "".join(
        f"[b{i}]trim=end_frame=1,setpts=PTS-STARTPTS,scale={w}:{h}[f{i}];" for i in range(n)
    )
    stack_in = "".join(f"[f{i}]" for i in range(n))
    fc = f"[0:v]scale={w}:{h},split={n}{split_out};" f"{pick}{stack_in}hstack=inputs={n}[out]"
    cmd = [
        find_ffmpeg(),
        "-y",
        "-i",
        str(src),
        "-filter_complex",
        fc,
        "-map",
        "[out]",
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-q:v",
        "80",
        str(out),
    ]
    _run(cmd, timeout=900)


def build_skin(source_type: str, sources, out_dir: Path, meta: dict) -> Path:
    """按 source_type 分派，产出皮肤包目录（返回 out_dir 本身）。

    sources:
      atlas-8x9    -> [atlas 文件路径]
      existing-150 -> {state: 文件路径}（免转换复制）
      gif-multi    -> [gif 文件路径列表]（meta["gif_map"] 指定 {state: 文件名}）
    meta 必需字段：id/name/category/license/attribution
    可选：frameW/frameH、row_map、durations、cols
    """
    skin_id = str(meta.get("id") or "")
    if not is_valid_skin_id(skin_id):
        raise SkinBuildError(f"非法皮肤 id: {skin_id!r}")
    out_dir.mkdir(parents=True, exist_ok=True)

    if source_type == "existing-150":
        rows = pack_existing_skin(dict(sources or {}), out_dir, meta)
    elif source_type == "atlas-8x9":
        rows = pack_atlas_skin(sources[0], out_dir, meta)
    elif source_type == "gif-multi":
        rows = pack_gif_skin(list(sources), out_dir, meta)
    elif source_type == "pixel-json":
        rows = pack_pixjson_skin(sources[0], out_dir, meta)
    else:
        raise SkinBuildError(f"未知 source_type: {source_type!r}")

    write_skin_json(out_dir, meta, rows)
    copy_license(out_dir, meta)
    return out_dir


def pack_atlas_skin(atlas: Path, out_dir: Path, meta: dict) -> dict[str, dict]:
    """atlas → 逐状态 strip webp；返回 {state: {sheet, frames, dur}}。"""
    if not atlas or not Path(atlas).exists():
        raise SkinBuildError(f"atlas 不存在: {atlas}")
    fw = int(meta.get("frameW") or 0)
    fh = int(meta.get("frameH") or 0)
    cols = int(meta.get("cols") or 8)
    aw, ah = probe_size(Path(atlas))
    if not fw or not fh:
        if not fw:
            fw = aw // cols
        if not fh:
            raise SkinBuildError("atlas-8x9 需要 frameH（帧高）或由 size 推算")
    if aw < cols * fw or ah < fh:
        raise SkinBuildError(f"atlas 尺寸 {aw}x{ah} 与帧规格 {cols}×{fw}x{fh} 不一致")
    row_map = {k: int(v) for k, v in (meta.get("row_map") or DEFAULT_ROW_MAP).items()}
    durs = {k: float(v) for k, v in (meta.get("durations") or DEFAULT_DUR).items()}
    rows: dict[str, dict] = {}
    for state in SKIN_STATE_KEYS:
        row = row_map[state]
        sheet = f"{state}.webp"
        cut_atlas_row(Path(atlas), out_dir / sheet, row, fw, fh, cols)
        rows[state] = {"sheet": sheet, "frames": cols, "dur": durs.get(state, DEFAULT_DUR[state])}
    meta["frameW"] = fw
    meta["frameH"] = fh
    return rows


def pack_gif_skin(gifs: list[Path], out_dir: Path, meta: dict) -> dict[str, dict]:
    """多个 gif → 每个 gif 拆帧 hstack 成 strip。meta["gif_map"]={state: 文件名}"""
    gif_map = meta.get("gif_map") or {}
    if not gif_map:
        raise SkinBuildError("gif-multi 需要 meta.gif_map={state: 文件名}")
    by_name = {p.name.lower(): p for p in gifs if p and Path(p).exists()}
    durs = {k: float(v) for k, v in (meta.get("durations") or DEFAULT_DUR).items()}
    fw = int(meta.get("frameW") or 0)
    fh = int(meta.get("frameH") or 0)
    rows: dict[str, dict] = {}
    sizes: list[tuple[int, int]] = []
    for state in SKIN_STATE_KEYS:
        name = gif_map.get(state) or ""
        p = by_name.get(name.lower()) if name else None
        if p is None:
            rows[state] = {"sheet": None, "frames": None, "dur": None}
            continue
        sheet = f"{state}.webp"
        out = out_dir / sheet
        _gif_to_strip(p, out)
        w, h = probe_size(out)
        sizes.append((w, h))
        frames = max(1, w // fw) if fw and fh else probe_frames(p)
        rows[state] = {"sheet": sheet, "frames": frames, "dur": durs.get(state, DEFAULT_DUR[state])}
    if sizes:
        max_fw = max(s[0] for s in sizes) // max(
            (r.get("frames") or 1) for r in rows.values() if r.get("frames")
        )
        if not fw:
            meta["frameW"] = max_fw or 32
        if not fh:
            meta["frameH"] = sizes[0][1]
    else:
        meta.setdefault("frameW", 32)
        meta.setdefault("frameH", 32)
    return rows


def pack_existing_skin(state_files: dict, out_dir: Path, meta: dict) -> dict[str, dict]:
    """现有 per-state 文件（如芙宁娜 svg/*.webp）→ 复制 + 记录帧数。"""
    fw = int(meta.get("frameW") or 150)
    durs = {k: float(v) for k, v in (meta.get("durations") or DEFAULT_DUR).items()}
    rows: dict[str, dict] = {}
    for state in SKIN_STATE_KEYS:
        src = state_files.get(state)
        if src is None or not Path(src).exists():
            rows[state] = {"sheet": None, "frames": None, "dur": None}
            continue
        src = Path(src)
        sheet = f"{state}.webp"
        shutil.copyfile(src, out_dir / sheet)
        w, _h = probe_size(src)
        rows[state] = {
            "sheet": sheet,
            "frames": max(1, w // fw),
            "dur": durs.get(state, DEFAULT_DUR[state]),
        }
    return rows


def _pixjson_load(path: Path) -> dict:
    """读像素 JSON（size + palette{key:[r,g,b,a]} + frames[{name,pixels}]）。"""
    data = json.loads(Path(path).read_text("utf-8"))
    size = list(map(int, data.get("size") or [32, 32]))
    if len(size) < 2:
        raise SkinBuildError(f"像素 JSON 缺 size: {path.name}")
    frames = data.get("frames") or []
    if not frames:
        raise SkinBuildError(f"像素 JSON 无帧: {path.name}")
    return {"size": size, "palette": data.get("palette") or {}, "frames": frames}


def _pixjson_frame_rgba(frame: dict, size: list[int], palette: dict):
    """一行「palette key 网格」→ (h, w, 4) uint8 RGBA（未知 key 透明）。"""
    import numpy as np

    w, h = int(size[0]), int(size[1])
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    rows = frame.get("pixels") or []
    for y, row in enumerate(rows[:h]):
        for x, key in enumerate(row[:w]):
            c = palette.get(str(key))
            if not c:
                continue
            r, g, b, a = [int(v) for v in c]
            arr[y, x] = (r & 255, g & 255, b & 255, a & 255)
    return arr


def _pixjson_to_strip(frames, indices: list[int], size: list[int]) -> bytes:
    """若干 RGBA 帧横向 hstack → strip 原始字节（宽 = 帧宽 × 帧数）。"""
    import numpy as np

    chosen = [frames[i] for i in indices]
    strip = np.hstack(chosen) if len(chosen) > 1 else chosen[0]
    return strip.tobytes()


def pack_pixjson_skin(src: Path, out_dir: Path, meta: dict) -> dict[str, dict]:
    """像素 JSON → 皮肤包：numpy 渲染 RGBA → ffmpeg rawvideo 管道 → webp strip。

    JSON 只有一组 idle 帧（通常 2 帧呼吸动画）。映射：
      idle/listen/think → 全部帧（循环呼吸）；listen/think 渲染层回退 idle
      play             → 首帧（干活小动一下）；build 渲染层回退 play
      error            → 尾帧（停顿）
    生成 idle.webp（全帧）+ play.webp（首帧）+ error.webp（尾帧），其余状态
    在 skin.json 省略，渲染层按 STATE_FALLBACK 回退。
    """
    data = _pixjson_load(src)
    size = data["size"]
    fw, fh = size
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = [_pixjson_frame_rgba(f, size, data["palette"]) for f in data["frames"]]
    durs = {k: float(v) for k, v in (meta.get("durations") or DEFAULT_DUR).items()}
    n = len(frames)
    one, rest = [0], list(range(max(1, n - 1), n))  # play 用首帧，error 用尾帧

    def _encode(indices: list[int], fname: str) -> int:
        raw = _pixjson_to_strip(frames, indices, size)
        w = fw * len(indices)
        cmd = [
            find_ffmpeg(),
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{w}x{fh}",
            "-i",
            "-",
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-lossless",
            "0",
            "-q:v",
            "80",
            str(out_dir / fname),
        ]
        p = subprocess.run(cmd, input=raw, capture_output=True, timeout=600)
        if p.returncode != 0:
            raise SkinBuildError(f"pixel-json webp 编码失败 {fname}: {(p.stderr or '')[-300:]}")
        return len(indices)

    rows: dict[str, dict] = {}
    idle_frames = _encode(list(range(n)), "idle.webp")
    rows["idle"] = {
        "sheet": "idle.webp",
        "frames": idle_frames,
        "dur": durs.get("idle", DEFAULT_DUR["idle"]),
    }
    rows["think"] = {
        "sheet": "idle.webp",
        "frames": idle_frames,
        "dur": durs.get("think", DEFAULT_DUR["think"]),
    }
    play_frames = _encode(one, "play.webp")
    rows["play"] = {
        "sheet": "play.webp",
        "frames": play_frames,
        "dur": durs.get("play", DEFAULT_DUR["play"]),
    }
    err_frames = _encode(rest if rest else [0], "error.webp")
    rows["error"] = {
        "sheet": "error.webp",
        "frames": err_frames,
        "dur": durs.get("error", DEFAULT_DUR["error"]),
    }
    # listen → idle、build → play：skin.json 省略，渲染层回退
    meta["frameW"] = fw
    meta["frameH"] = fh
    return rows


def write_skin_json(out_dir: Path, meta: dict, rows: dict[str, dict]) -> None:
    """写 skin.json；缺省状态（sheet=None）省略，渲染层按回退处理。"""
    states = {}
    for state in SKIN_STATE_KEYS:
        r = rows.get(state) or {}
        if r.get("sheet"):
            states[state] = {
                "sheet": r["sheet"],
                "frames": int(r["frames"]),
                "dur": float(r["dur"]),
            }
    skin = {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "category": meta.get("category") or "其他",
        "license": meta.get("license") or "unknown",
        "attribution": meta.get("attribution") or "",
        "frameW": int(meta.get("frameW") or 150),
        "frameH": int(meta.get("frameH") or 150),
        "states": states,
    }
    (out_dir / "skin.json").write_text(json.dumps(skin, ensure_ascii=False, indent=2), "utf-8")


def copy_license(out_dir: Path, meta: dict) -> None:
    """落地素材来源许可（LICENSE 文本 + attribution 说明）。"""
    attr = str(meta.get("attribution") or "").strip()
    lic = str(meta.get("license") or "").strip()
    if not attr and not lic:
        return
    (out_dir / "LICENSE").write_text(
        "\n".join(
            [
                f"来源: {attr}",
                f"许可: {lic}",
                "",
                "本皮肤素材来自第三方开源项目，按上方许可使用与再分发。",
                "随应用分发时保留本文件与 skin.json 中的 attribution/license 字段。",
            ]
        ),
        "utf-8",
    )


def load_skin(skin_dir: Path) -> dict:
    """读 skin.json，补缺省状态回退映射（listen→idle / build→play）。"""
    f = skin_dir / "skin.json"
    if not f.exists():
        raise SkinBuildError(f"缺少 skin.json: {skin_dir}")
    skin = json.loads(f.read_text("utf-8"))
    states = {}
    for key, val in (skin.get("states") or {}).items():
        if key not in SKIN_STATE_KEYS or not val.get("sheet"):
            continue
        states[key] = {
            "sheet": str(val["sheet"]),
            "frames": max(1, int(val.get("frames") or 1)),
            "dur": max(0.2, float(val.get("dur") or 1.0)),
        }
    for key, fb in STATE_FALLBACK.items():
        if key not in states and fb in states:
            states[key] = dict(states[fb])
    skin["states"] = states
    return skin


def validate_skin_files(skin_dir: Path, skin: dict | None = None) -> list[str]:
    """校验每个状态 sheet 存在且尺寸与帧数粗一致；返回告警（不抛）。"""
    skin = skin or load_skin(skin_dir)
    fw, fh = int(skin["frameW"]), int(skin["frameH"])
    warns = []
    for state, s in (skin.get("states") or {}).items():
        p = skin_dir / s["sheet"]
        if not p.exists():
            warns.append(f"{state}: 缺 sheet {s['sheet']}")
            continue
        try:
            w, h = probe_size(p)
        except SkinBuildError:
            continue
        if h != fh or w < fw:
            warns.append(f"{state}: 尺寸 {w}x{h} 与帧规格 {fw}x{fh} 不符")
    return warns

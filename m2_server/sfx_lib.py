"""音效素材库（共享叶子模块，**无路由**）：素材定位 + 短音效混音。

为什么单独一个模块（而不是塞在 `soundboard.py` 里）
--------------------------------------------------
素材有两类消费者，分属**两个插件**：

    sound.fx-board / soundboard.py  实时播（点格子即响）+ 预混端点（/premix）
    sound.effects / effects.py      效果链里的 `mix` 环节（「插入音效」）

「素材在哪」「怎么读成 float32」「怎么混进一段人声」这三件事只该有一份实现。
让 `effects.py` 去 import `soundboard`（一个插件模块、还带 router 与子进程管理）
就是插件间代码耦合；反过来同理。所以抽成本模块 —— 它是叶子：只 import
numpy / soundfile / config，谁都能用，也不把谁拉进进程。

    ⚠️ `soundboard.py` 与 `effects.py` 都**不许**把这里的目录常量在导入期复制成
    自己的模块级变量（`MY_DIR = sfx_lib.IMPORT_DIR` 这种）。本仓库在这一点上
    栽过：早绑定常量让测试的 monkeypatch 静默失效，写入落到**用户的真实数据目录**
    （见 `docs/犯错档案-工程.md` §8.36 / §8.37）。用**函数调用**间接读全局名，
    打到本模块的补丁才会真的生效。

素材来源
--------
· 出厂：`plugins/sound.fx-board/samples/*.wav`（`tools/gen_sfx.py` 程序化合成）
· 导入：`<media>/soundboard/*.wav`（`/import` 写入；用户自己的素材）

文件名 stem 即素材 id。出厂与导入不许重名（导入时直接拒 —— 比"谁覆盖谁"这种
隐式规则好查）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import config as cfg
import numpy as np
import soundfile as sf

#: 出厂素材（随插件走）
SAMPLES_DIR = Path(__file__).resolve().parent / "plugins" / "sound.fx-board" / "samples"
#: 用户导入（跟 media 走）
IMPORT_DIR = cfg.MEDIA_DIR / "soundboard"

#: 混入位置：叠加（与人声同时）/ 拼在开头 / 拼在结尾
MODES = ("layer", "prepend", "append")
#: 单次预混允许的插入条数（防手滑把 200 条塞进来）
MAX_INSERTS = 16
#: 预混的音频上限（秒）：TTS 产物实际都在 1 分钟内，超了就是路径给错了
MAX_SECONDS = 300.0


class SfxError(Exception):
    """素材相关错误（带 HTTP 语义的 status，由调用方翻译成 HTTPException）。

    这里刻意不 import fastapi：本模块要能被 `effects.py`（纯 DSP 上下文）复用，
    也要能被测试单独 import，不该因为异常类型把 web 框架拖进来。
    """

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = int(status)


# --------------------------------------------------------------- 目录


def meta() -> dict:
    """出厂素材的显示名/标签：`samples/manifest.json`（gen_sfx.py 写）。"""
    f = SAMPLES_DIR / "manifest.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def duration_s(path: Path) -> float:
    try:
        return round(float(sf.info(str(path)).duration), 2)
    except Exception:
        return 0.0


def sanitize_stem(raw: str) -> str:
    """把上传文件名收成安全的 stem：去目录、拒穿越、限长、只留常规字符。"""
    stem = Path(raw or "").name  # 去掉任何目录成分（../x.wav → x.wav）
    stem = Path(stem).stem
    bad = [c for c in stem if c in '\\/:*?"<>|' or ord(c) < 32]
    if bad or stem in ("", ".", ".."):
        raise SfxError(f"文件名不合法：{raw!r}")
    if len(stem) > 40:
        raise SfxError("文件名太长（限 40 字符）")
    return stem


def resolve_path(sample_id: str) -> tuple[Path, bool]:
    """素材 id → (文件路径, 是否出厂)。只认这两个目录里的文件名，拒绝一切路径花样。

    穿越防护用「拼接后必须仍在目标目录内」而不是字符串黑名单：后者永远漏
    （`....//`、`%2e%2e`、NT 的 `\\\\?\\\\` 前缀…），而 `is_relative_to` 是判据本身。
    """
    sid = str(sample_id or "")
    if not sid or sid in (".", "..") or any(c in sid for c in "/\\"):
        raise SfxError(f"非法的音效 id：{sid!r}")
    for d, builtin in ((SAMPLES_DIR, True), (IMPORT_DIR, False)):
        p = (d / f"{sid}.wav").resolve()
        if p.is_relative_to(d.resolve()) and p.is_file():
            return p, builtin
    raise SfxError(f"没有这个音效：{sid}", status=404)


def list_samples() -> list[dict]:
    """出厂 + 导入的全部素材（id/name/tags/duration_s/builtin）。

    不含播放计数 —— 那是声板的运营数据（`outputs/soundboard_stats.json`），
    与"素材库有什么"是两件事，所以留在 `soundboard.py` 里叠加。
    """
    m = meta()
    out: list[dict] = []
    for d, builtin in ((SAMPLES_DIR, True), (IMPORT_DIR, False)):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.wav")):
            sid = f.stem
            info = m.get(sid) if builtin else None
            out.append(
                {
                    "id": sid,
                    "name": (info or {}).get("name") or sid,
                    "tags": (info or {}).get("tags") or (["导入"] if not builtin else []),
                    "duration_s": duration_s(f),
                    "builtin": builtin,
                }
            )
    return out


# --------------------------------------------------------------- 读素材（带缓存）


_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[np.ndarray, int]] = {}
_CACHE_MAX = 64


def load_pcm(sample_id: str) -> tuple[np.ndarray, int]:
    """读一条素材 → (float32 单声道, sr)。带进程内缓存（键含 mtime/size，覆盖后自动失效）。

    缓存是必需的：`/premix` 与效果链都可能在一次请求里读同一条素材，
    而用户会连点好几次格子/反复试混 —— 每次都读盘（+ 解码）纯属浪费。
    """
    path, _builtin = resolve_path(sample_id)
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception as e:
        raise SfxError(f"素材读取失败：{path.name}（{e}）") from e
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    pcm = np.ascontiguousarray(data, dtype=np.float32)
    if pcm.size == 0:
        raise SfxError(f"素材是空的：{path.name}")
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:  # 简易 FIFO：素材都很小，不必上 LRU
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (pcm, int(sr))
    return pcm, int(sr)


# --------------------------------------------------------------- 混音


def _mono(x: np.ndarray) -> np.ndarray:
    if x.ndim > 1:
        x = x[:, 0]
    return np.ascontiguousarray(x, dtype=np.float32)


def _num(v, default: float, lo: float, hi: float) -> float:
    """宽容取数：给不出数就用默认，然后 clamp。坏参数不该让整条链炸掉。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = default
    if f != f:  # NaN
        f = default
    return float(min(hi, max(lo, f)))


def _resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """线性重采样（素材多为 48k，而 CABLE/TTS 可能是 44.1k，必须能跨）。

    音效是短促的瞬态，线性插值的高频损失听感上可忽略；换来的是零额外依赖
    （不用为一条爆炸声引入 resampy/soxr）。
    """
    if sr_from <= 0 or sr_to <= 0 or sr_from == sr_to or x.size == 0:
        return x
    n = max(1, int(round(x.size * sr_to / sr_from)))
    t_from = np.arange(x.size, dtype=np.float64) / sr_from
    t_to = np.arange(n, dtype=np.float64) / sr_to
    return np.interp(t_to, t_from, x.astype(np.float64)).astype(np.float32)


def _peak_norm(x: np.ndarray, peak: float = 0.97) -> np.ndarray:
    """防削波：峰值超限时**整体**等比回缩（与 `effects._norm` 同一策略）。

    叠加两路声音必然抬高峰值；不处理就会削出刺耳的爆音，而削波发生在微信
    录完之后、用户听不出原因。整体回缩会让人声也小一点，但保持了相对比例。
    """
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m > peak:
        x = x * (peak / m)
    return x


def mix_into(
    voice: np.ndarray, sr: int, inserts: list[dict], *, peak: float = 0.97
) -> tuple[np.ndarray, list[str]]:
    """把若干音效混进一段人声。返回 (结果 float32, 跳过的说明列表)。

    `inserts` 每项：`{sample, mode, at_s, gain}`

        mode=layer    叠加：`at_s` 秒处**贴上去**，长度不变（与实时声板同语义 ——
                      Windows 共享模式混音就是这件事）
        mode=prepend  拼在开头（"先炸一声，再说话"）
        mode=append   拼在结尾（"说完来一记掌声"）

    坏素材/坏参数只跳过并记录，不抛异常 —— 与 `effects.apply_chain` 的约定一致：
    一条音效读不出来不该让整条语音发不出去。
    """
    out = _mono(voice)
    notes: list[str] = []
    head: list[np.ndarray] = []
    tail: list[np.ndarray] = []
    layers: list[tuple[int, np.ndarray]] = []

    for raw in inserts or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("sample") or "").strip()
        if not sid:
            notes.append("未指定音效")
            continue
        mode = str(raw.get("mode") or "layer").strip().lower()
        if mode not in MODES:
            notes.append(f"未知位置 {mode!r}")
            continue
        gain = _num(raw.get("gain", 0.9), 0.9, 0.0, 1.5)
        at_s = _num(raw.get("at_s", 0.0), 0.0, 0.0, MAX_SECONDS)
        try:
            pcm, ssr = load_pcm(sid)
        except SfxError as e:
            notes.append(str(e))
            continue
        y = _resample(pcm, ssr, int(sr)) * gain
        if y.size == 0:
            notes.append(f"{sid}：空音频")
            continue
        if mode == "prepend":
            head.append(y)
        elif mode == "append":
            tail.append(y)
        else:
            layers.append((int(round(at_s * int(sr))), y))

    if head or tail:
        out = np.concatenate([*head, out, *tail])
    else:
        out = out.copy()  # 别就地改调用方的数组（叠加会 += ）
    for off, y in layers:
        off = max(0, min(off, out.size))  # 越过末尾就贴尾，不制造一截静音尾巴
        need = off + y.size - out.size
        if need > 0:
            out = np.concatenate([out, np.zeros(need, dtype=np.float32)])
        out[off : off + y.size] += y
    return _peak_norm(out, peak), notes

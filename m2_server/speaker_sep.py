"""完整说话人分离（Diarization）与主说话人推荐（纯本地 · 魔搭阿里 CAM++ · 开源许可）。

对整条人声音轨跑完整的 CAM++ speaker-diarization pipeline：

    VAD 抓有效语音 → 1.5s 滑窗分块 → CAM++ 声纹嵌入 → HDBSCAN 聚类
    → 时序合并 / 重叠分配 / 短段平滑

输出按时间排序的说话人分段，并推荐总时长最长的说话人为主说话人；若提供该素材的切片，
还会用 CAM++ 说话人验证（SV）声纹把每个切片分派到对应说话人，方便前端「按说话人分组 /
过滤」选音色。

许可：主模型 `iic/speech_campplus_speaker-diarization_common` 与辅助声纹
`damo/speech_campplus_sv_zh-cn_16k-common` 均 Apache-2.0，免认证、国内直连，
首跑自动下载模型并缓存（~/.cache/modelscope），之后离线可用。
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
from common import find_ffmpeg

_DIAR_MODEL = "iic/speech_campplus_speaker-diarization_common"
_SV_MODEL = "damo/speech_campplus_sv_zh-cn_16k-common"
_FS = 16000
_MIN_CLIP_S = 0.4  # 短于此的切片声纹不稳，不参与说话人分派

_LOCK = threading.Lock()
_DIAR = None
_SV = None


# ---------- 模型单例（lazy，首次联网下载后缓存） ----------


def _get_diar():
    """CAM++ 完整 diarization pipeline 单例。"""
    global _DIAR
    if _DIAR is None:
        with _LOCK:
            if _DIAR is None:
                from modelscope.pipelines import pipeline
                from modelscope.utils.constant import Tasks

                _DIAR = pipeline(task=Tasks.speaker_diarization, model=_DIAR_MODEL)
    return _DIAR


def _get_sv():
    """CAM++ 说话人验证（SV）pipeline 单例，用于把切片分派到说话人。"""
    global _SV
    if _SV is None:
        with _LOCK:
            if _SV is None:
                from modelscope.pipelines import pipeline
                from modelscope.utils.constant import Tasks

                _SV = pipeline(task=Tasks.speaker_verification, model=_SV_MODEL)
    return _SV


# ---------- 音频 IO / 重采样 ----------


def _read16k(path: Path) -> np.ndarray:
    """读音频为 16k 单声道 float32 numpy 数组。"""
    import soundfile as sf

    a, fs = sf.read(str(path), dtype="float32")
    if a.ndim == 2:
        a = a[:, 0]
    if fs == _FS:
        return a
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(_FS, fs)
    return resample_poly(a, _FS // g, fs // g).astype(np.float32)


def _resample16k(audio: Path) -> Path:
    """用完整 ffmpeg 把音轨重采样为 16k 单声道临时 wav。

    原因：CAM++ pipeline 在输入采样率 != 16k 时走 torchaudio.sox_effects 重采样，
    而本机 torchaudio 缺 sox 后端；提前转好即可绕过该分支。
    """
    tmp = Path(tempfile.mkdtemp()) / "diar_16k.wav"
    ff = find_ffmpeg()
    r = subprocess.run(
        [ff, "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", str(_FS), str(tmp)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"音频重采样失败({audio.name}): {r.stderr.strip()[-300:]}")
    return tmp


def _sv_embed(audio16k: np.ndarray):
    """返回 CAM++ 192 维 L2 归一化声纹向量；失败返回 None。"""
    if audio16k.size == 0:
        return None
    try:
        sv = _get_sv()
        d = sv([audio16k], output_emb=True)
        embs = d.get("embs")
        if embs is not None and embs.shape[1] == 192:
            v = np.asarray(embs[0], dtype=np.float64).reshape(-1)
            n = float(np.linalg.norm(v))
            return v / n if n > 0 else v
    except Exception:
        return None
    return None


# ---------- 主流程 ----------


def analyze_audio(audio: Path, clip_paths: list[Path] | None = None) -> dict:
    """对整条人声音轨做说话人分离，推荐主说话人，可选为切片分派说话人。

    audio       : 无 BGM 的人声音轨（demucs 产物优先）。
    clip_paths  : 该素材的切片列表；提供则按声纹分派说话人标签。
    """
    audio = Path(audio)
    if not audio.exists():
        raise RuntimeError(f"人声音轨不存在：{audio.name}")

    tmp16 = _resample16k(audio)
    try:
        res = _get_diar()(audio=str(tmp16))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"说话人分离失败：{e}") from e
    seg_raw = (res or {}).get("text") or []
    if not seg_raw:
        raise RuntimeError("说话人分离未返回任何分段（有效语音可能过短，需 >5s）")

    segs: list[list] = [[float(st), float(ed), int(spk)] for st, ed, spk in seg_raw]

    dur: dict[int, float] = {}
    cnt: dict[int, int] = {}
    for st, ed, spk in segs:
        dur[spk] = dur.get(spk, 0.0) + max(0.0, ed - st)
        cnt[spk] = cnt.get(spk, 0) + 1
    del seg_raw
    total = sum(dur.values()) or 1e-9
    order = sorted(dur, key=lambda s: dur[s], reverse=True)
    main_spk = order[0] if order else -1

    speakers = [
        {
            "id": spk,
            "label": f"说话人{i + 1}",
            "duration": round(dur[spk], 2),
            "segment_count": cnt.get(spk, 0),
            "ratio": round(dur[spk] / total, 3),
            "is_main": spk == main_spk,
        }
        for i, spk in enumerate(order)
    ]
    label_of = {s["id"]: s["label"] for s in speakers}

    clips = _assign_clips(clip_paths or [], tmp16, segs) if clip_paths else []

    segments = [
        {
            "start": round(st, 2),
            "end": round(ed, 2),
            "duration": round(max(0.0, ed - st), 2),
            "spk": spk,
            "label": label_of[spk],
            "is_main": spk == main_spk,
        }
        for st, ed, spk in segs
    ]

    return {
        "ok": True,
        "analyzed": len(segments),
        "n_speakers": len(speakers),
        "main_speaker": main_spk,
        "main_label": label_of.get(main_spk, ""),
        "speakers": speakers,
        "segments": segments,
        "clips": clips,
        "model": _DIAR_MODEL,
    }


def analyze_clips(clip_paths: list[Path]) -> dict:
    """无纯人声轨时的回退：直接用切片声纹聚类出说话人分组（P2-1）。

    对每个可用切片（>=0.4s 且声纹可算）做 CAM++ 声纹，用 HDBSCAN 聚类成若干
    说话人组；总时长占比最大者为主说话人。输出结构与 `analyze_audio` 一致，
    前端可用同一份类型消费。切片无整轨时间轴，segments 的 start/end 以切片
    自身时长近似。
    """
    fs = _FS
    rows: list[dict] = []
    for p in clip_paths:
        try:
            a = _read16k(p)
            if a.shape[0] / fs < _MIN_CLIP_S:
                continue
            e = _sv_embed(a)
            if e is None:
                continue
            rows.append({"name": p.stem, "emb": e, "dur": a.shape[0] / fs})
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        raise RuntimeError(
            "该素材没有可用于说话人分析的切片（切片过短或读取失败）。"
            "建议：换一段人声清晰、无伴奏的素材重新走流水线。"
        )

    n = len(rows)
    emb = np.stack([r["emb"] for r in rows])  # (n, 192)，L2 归一化

    # HDBSCAN 聚类；依赖缺失或聚类退化时退化为单簇（全部算作同一个说话人）
    try:
        from hdbscan import HDBSCAN

        labels = HDBSCAN(
            min_cluster_size=max(2, n // 4), min_samples=1, metric="euclidean"
        ).fit_predict(emb)
        labels = np.asarray(labels, dtype=int)
    except Exception:  # noqa: BLE001
        labels = np.zeros(n, dtype=int)

    # 噪声点(-1)并入欧氏距离最近的簇，避免被当成独立说话人
    uniq = np.unique(labels[labels >= 0]) if (labels >= 0).any() else np.array([], dtype=int)
    if (labels < 0).any() and uniq.size:
        centers = np.stack([emb[labels == k].mean(axis=0) for k in uniq])
        for i in np.flatnonzero(labels < 0):
            labels[i] = uniq[int(np.argmin(((centers - emb[i]) ** 2).sum(axis=1)))]
    if (labels < 0).all():
        labels = np.zeros(n, dtype=int)

    # 重新编号 0..K-1，按时长降序；主说话人 = 时长最长
    dur: dict[int, float] = {}
    cnt: dict[int, int] = {}
    for r, lb in zip(rows, labels, strict=False):
        dur[lb] = dur.get(lb, 0.0) + r["dur"]
        cnt[lb] = cnt.get(lb, 0) + 1
    order = sorted(dur, key=lambda s: dur[s], reverse=True)
    remap = {old: new for new, old in enumerate(order)}
    main_spk = remap[order[0]]
    total = sum(dur.values()) or 1e-9

    speakers = [
        {
            "id": remap[spk],
            "label": f"说话人{i + 1}",
            "duration": round(dur[spk], 2),
            "segment_count": cnt[spk],
            "ratio": round(dur[spk] / total, 3),
            "is_main": remap[spk] == main_spk,
        }
        for i, spk in enumerate(order)
    ]
    label_of = {remap[spk]: f"说话人{i + 1}" for i, spk in enumerate(order)}

    clips = [{"name": r["name"], "spk": remap[lb]} for r, lb in zip(rows, labels, strict=False)]
    done = {r["name"] for r in rows}
    clips += [{"name": p.stem, "spk": None} for p in clip_paths if p.stem not in done]

    segments = [
        {
            "start": 0.0,
            "end": round(r["dur"], 2),
            "duration": round(r["dur"], 2),
            "spk": remap[lb],
            "label": label_of[remap[lb]],
            "is_main": remap[lb] == main_spk,
        }
        for r, lb in zip(rows, labels, strict=False)
    ]

    return {
        "ok": True,
        "analyzed": len(segments),
        "n_speakers": len(speakers),
        "main_speaker": main_spk,
        "main_label": label_of[main_spk],
        "speakers": speakers,
        "segments": segments,
        "clips": clips,
        "model": "clip-embedding cluster (fallback, 无纯人声轨)",
    }


def _assign_clips(clip_paths: list[Path], sound16: Path, segs: list[list]) -> list[dict]:
    """把切片按声纹分派到说话人。

    说话人中心声纹 = 该说话人各 diarization 段 pcm 的 CAM++ 声纹均值（L2 归一化）；
    切片与各中心的余弦相似度取最大者为该切片说话人。
    """
    fs = _FS
    audio = _read16k(sound16)
    centers = _speaker_centers(audio, segs)
    if not centers:
        return [{"name": p.stem, "spk": None} for p in clip_paths]
    keys = list(centers)
    cmat = np.stack([centers[k] for k in keys])  # (n_spk, 192)

    out = []
    for p in clip_paths:
        spk: int | None = None
        try:
            a = _read16k(p)
            if a.shape[0] / fs >= _MIN_CLIP_S:
                e = _sv_embed(a)
                if e is not None:
                    sim = cmat @ e
                    spk = keys[int(np.argmax(sim))]
        except Exception:  # noqa: BLE001
            spk = None
        out.append({"name": p.stem, "spk": spk})
    return out


def main_center(audio: Path) -> tuple[int | None, np.ndarray | None, dict]:
    """返回（主说话人 id，其中心声纹，元信息），供切片质检做"说话人一致性"判定。

    主说话人 = 有效语音总时长最长者；中心声纹 = 其各分段 pcm 的 CAM++ 声纹均值
    （L2 归一化，与 `_sv_embed` 同一空间，可直接点乘求余弦）。

    失败一律返回 (None, None, meta)——质检里声纹维度缺失只是不参与判分，不判废。
    """
    audio = Path(audio)
    meta: dict = {"n_speakers": 0}
    if not audio.exists():
        return None, None, meta
    try:
        tmp16 = _resample16k(audio)
        res = _get_diar()(audio=str(tmp16))
        seg_raw = (res or {}).get("text") or []
        if not seg_raw:
            return None, None, meta
        segs: list[list] = [[float(st), float(ed), int(spk)] for st, ed, spk in seg_raw]
        dur: dict[int, float] = {}
        for st, ed, spk in segs:
            dur[spk] = dur.get(spk, 0.0) + max(0.0, ed - st)
        if not dur:
            return None, None, meta
        main = max(dur, key=lambda s: dur[s])
        meta = {"n_speakers": len(dur), "durations": {str(k): round(v, 2) for k, v in dur.items()}}
        centers = _speaker_centers(_read16k(tmp16), segs)
        return main, centers.get(main), meta
    except Exception:  # noqa: BLE001
        return None, None, meta


def _speaker_centers(audio16k: np.ndarray, segs: list[list]) -> dict[int, np.ndarray]:
    """每个说话人的中心声纹（其分段 pcm 平均后再归一化）。"""
    pool: dict[int, list[np.ndarray]] = {}
    for st, ed, spk in segs:
        s0, s1 = int(st * _FS), int(min(ed, len(audio16k) / _FS) * _FS)
        if s1 - s0 < int(_MIN_CLIP_S * _FS) or s0 < 0 or s1 > audio16k.shape[0]:
            continue
        e = _sv_embed(audio16k[s0:s1])
        if e is not None:
            pool.setdefault(spk, []).append(e)
    centers: dict[int, np.ndarray] = {}
    for spk, es in pool.items():
        m = np.mean(np.stack(es), axis=0)
        n = float(np.linalg.norm(m))
        if n > 0:
            centers[spk] = m / n
    return centers

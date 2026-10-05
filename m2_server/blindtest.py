# -*- coding: utf-8 -*-
"""盲听评测（`sound.blindtest` 插件核心）：主观听测的组织与汇总。

产品语义：把 N 段音频（试音间产物 / 上传件）匿名化成「样本 A/B/C…」打乱顺序，
生成一条**带随机 token 的链接**；评委（通常是手机扫码）在 `listen.html` 上逐个听、
按 MOS 1~5 打分；结果回传到本机汇总成排名 —— 主观分与试音间的客观分（secs/nats）
正好互补：客观分量「像不像」，主观分量「好不好听」。

★ 与既有件的关系（最小耦合）
  - 样本从哪来：桌面端上传（multipart），或**引用 outputs/ 下已有产物**（试音间、
    TTS、翻唱……）。引用只做「复制进会话目录」，不挪原文件、不回头依赖
    sound.audition —— 试音间关着也能用。
  - 打分由人完成，本模块**零重依赖**（纯标准库），不碰 GPU、不碰 torch。

★ 保密面（盲听的意义所在）
  - 样本与真实音色名的对应关系（`label`）**只存在于会话 JSON 与桌面端端点**；
    所有 `/t/{token}` 手机端点返回的形状里**永远没有 label** —— 手机页只看得到
    「样本 A」。哪天有人往手机端点里加 label，盲听就名存实亡了，所以测试钉死这一点。
  - 播放顺序在**创建时**一次性洗牌并固定：评委拿到的顺序与会话 JSON 里的 order
    一致，但没人能从顺序反推身份（创建顺序对评委不可见）。

★ 存储
  - `media/blindtest/sessions/<id>.json`：会话（映射 + 打分记录）。media 是持久
    用户数据（outputs 会被清），评测结果是这个功能唯一值钱的东西，必须活得比
    会话久。
  - `media/blindtest/audio/<id>/<sid>.<ext>`：样本音频副本。复制而非引用是因为
    ①手机端点要按 sid 伺服，直接暴露 outputs 路径等于把映射写在文件名里；
    ②原产物随时可能被清（outputs/.session 退出即删），副本让会话自洽。

★ 滥用面
  - 链接持有者可打分：token 是 128 位随机值，只在局域网里发；提交数上限
    （MAX_RESPONSES）防一个脚本刷爆 JSON。文件上传走 ext 白名单 + 大小上限。
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import statistics
import threading
import time
from pathlib import Path

import config as cfg
from runtime import OUT

# ---------------------------------------------------------------- 常量

#: 样本数边界：1 个样本谈不上「盲听对比」；超过 10 个一次听不完也不记得住。
MIN_SAMPLES = 2
MAX_SAMPLES = 10

#: 单样本大小上限（60s 的 24kHz wav 约 5.6MB；100MB 连长 mp3 都容得下）。
MAX_SAMPLE_BYTES = 100 * 1024 * 1024

#: 单会话打分提交数上限（防脚本刷爆；正常一组评委几十条封顶）。
MAX_RESPONSES = 500

TITLE_MAX = 100
LABEL_MAX = 50
NOTE_MAX = 500

#: 允许的音频扩展名（与 watermark_api / audition 的口径一致，手机浏览器都吃）。
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm"}

_EXT_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}

#: 手机端样本编号：A..J（MAX_SAMPLES=10 对应的字母表）。
_SIDS = [chr(ord("A") + i) for i in range(MAX_SAMPLES)]

_SCORE_RE = re.compile(r"^[1-5]$")


class BlindtestError(Exception):
    """业务错误，`status` 供 API 层映射 HTTP 状态码。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- 路径


def base_dir() -> Path:
    return Path(cfg.MEDIA_DIR) / "blindtest"


def _sessions_dir() -> Path:
    return base_dir() / "sessions"


def _audio_dir(session_id: str) -> Path:
    return base_dir() / "audio" / session_id


# ---------------------------------------------------------------- 会话读写

_SAVE_LOCK = threading.Lock()


def _atomic_write_json(p: Path, data: dict) -> None:
    """原子落盘（tmp + replace），并发打分不会把会话写半截。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def _session_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _load(session_id: str) -> dict:
    p = _session_path(session_id)
    if not p.is_file():
        raise BlindtestError(f"找不到盲听会话 {session_id}", status=404)
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise BlindtestError(f"会话文件损坏：{e}", status=500) from e
    if not isinstance(data, dict) or not data.get("id"):
        raise BlindtestError("会话文件内容不合法", status=500)
    return data


def _save(sess: dict) -> None:
    with _SAVE_LOCK:
        _atomic_write_json(_session_path(sess["id"]), sess)


def _new_id() -> str:
    return f"bt_{int(time.time() * 1000)}"


# ---------------------------------------------------------------- 样本落位


def _copy_sample(src: Path, session_id: str, sid: str) -> str:
    """把一个样本复制进会话音频目录，返回落位文件名（含扩展名）。"""
    ext = src.suffix.lower()
    if ext not in AUDIO_EXTS:
        raise BlindtestError(f"不支持的音频格式 {ext}（支持：{'、'.join(sorted(AUDIO_EXTS))}）")
    if src.stat().st_size > MAX_SAMPLE_BYTES:
        raise BlindtestError(
            f"样本过大（>{MAX_SAMPLE_BYTES // 1024 // 1024}MB）：{src.name}"
        )
    dst_dir = _audio_dir(session_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{sid}{ext}"
    shutil.copyfile(src, dst)
    return dst.name


def _resolve_ref(ref: str) -> Path:
    """outputs/ 相对路径 → 真实文件（穿越防护：必须 resolve 后仍在 outputs 里）。"""
    r = (ref or "").strip().replace("\\", "/")
    if not r or r.startswith(("/", "~")) or ".." in r.split("/"):
        raise BlindtestError(f"样本引用不合法：{ref!r}")
    out_root = Path(OUT).resolve()
    p = (out_root / r).resolve()
    if not p.is_relative_to(out_root) or not p.is_file():
        raise BlindtestError(f"找不到要引用的产物：{ref}（它必须已在本机产物目录里）", status=404)
    return p


def _sanitize_title(title: str) -> str:
    t = (title or "").strip()
    if len(t) > TITLE_MAX:
        raise BlindtestError(f"标题过长（上限 {TITLE_MAX} 字）")
    return t or "未命名盲听"


def _sanitize_labels(labels: list, n: int) -> list[str]:
    """标签是「主办方视角的音色名」，只在桌面端出现；留空就用「样本 N」。"""
    out: list[str] = []
    for i in range(n):
        raw = str(labels[i]) if i < len(labels) else ""
        raw = raw.strip()[:LABEL_MAX]
        out.append(raw or f"样本{i + 1}")
    return out


# ---------------------------------------------------------------- 创建 / 查询


def create(
    title: str,
    labels: list[str],
    files: list[Path] | None = None,
    refs: list[str] | None = None,
) -> dict:
    """建一个盲听会话。

    `files`（上传件的临时落盘路径）与 `refs`（outputs/ 相对路径）二选一或混用，
    展开后的顺序 = 录入顺序 = 标签顺序；播放顺序在此刻随机洗牌。
    """
    sources: list[Path] = []
    for f in files or []:
        if not f.is_file():
            raise BlindtestError("上传样本丢失，请重试")
        sources.append(f)
    for r in refs or []:
        sources.append(_resolve_ref(r))

    n = len(sources)
    if n < MIN_SAMPLES:
        raise BlindtestError(f"至少要 {MIN_SAMPLES} 个样本才能盲听对比")
    if n > MAX_SAMPLES:
        raise BlindtestError(f"一次最多 {MAX_SAMPLES} 个样本（多了听不过来，分组再测）")

    sess_id = _new_id()
    while _session_path(sess_id).exists():  # 同毫秒撞 id → 顺延
        sess_id = f"bt_{int(time.time() * 1000)}_{secrets.token_hex(2)}"

    order = list(range(n))
    secrets.SystemRandom().shuffle(order)  # 创建时洗一次牌并固定（见模块注释「保密面」）
    try:
        samples = []
        for i, src in enumerate(sources):
            sid = _SIDS[i]
            fname = _copy_sample(src, sess_id, sid)
            samples.append({"sid": sid, "file": fname, "order": order[i]})
        samples.sort(key=lambda s: s["order"])
        sess = {
            "id": sess_id,
            "token": secrets.token_urlsafe(16),
            "title": _sanitize_title(title),
            "created_at": int(time.time()),
            "closed": False,
            "labels": _sanitize_labels(labels, n),
            # 样本按 sid 存；order 决定手机端的呈现顺序
            "samples": samples,
            "responses": [],
        }
        _save(sess)
    except Exception:
        shutil.rmtree(_audio_dir(sess_id), ignore_errors=True)  # 建失败不留垃圾
        raise
    return sess


def list_sessions() -> list[dict]:
    """会话摘要（新→旧）。摘要不含映射与逐条打分 —— 那是详情端点的事。"""
    items = []
    for p in sorted(_sessions_dir().glob("bt_*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        items.append(
            {
                "id": d["id"],
                "title": d.get("title") or "",
                "created_at": d.get("created_at") or 0,
                "closed": bool(d.get("closed")),
                "n_samples": len(d.get("samples") or []),
                "n_responses": len(d.get("responses") or []),
            }
        )
    return items


def detail(session_id: str) -> dict:
    """桌面端详情：映射（label）+ 汇总 + 逐条打分。手机端点绝不走这里。"""
    sess = _load(session_id)
    return {
        "id": sess["id"],
        "title": sess.get("title") or "",
        "created_at": sess.get("created_at") or 0,
        "closed": bool(sess.get("closed")),
        "token": sess.get("token") or "",
        "samples": [
            {"sid": s["sid"], "label": (sess.get("labels") or [""])[i], "order": s["order"]}
            for i, s in enumerate(sess.get("samples") or [])
        ],
        "aggregate": aggregate(sess),
        "responses": sess.get("responses") or [],
    }


def find_by_token(token: str) -> dict:
    """按 token 找会话（手机端入口）。token 不匹配一律 404，不区分「不存在/写错」。"""
    t = (token or "").strip()
    if not t:
        raise BlindtestError("缺少盲听会话标识", status=404)
    for p in _sessions_dir().glob("bt_*.json"):
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        if secrets.compare_digest(str(d.get("token") or ""), t):
            return d
    raise BlindtestError("盲听链接无效或已失效", status=404)


# ---------------------------------------------------------------- 打分


def _norm_scores(raw) -> dict[str, int]:
    """校验打分：{sid: 1~5 整数}；未知 sid / 越界 / 非整数一律 400。"""
    if not isinstance(raw, dict) or not raw:
        raise BlindtestError("打分数据为空")
    out: dict[str, int] = {}
    for sid, v in raw.items():
        s = str(sid).strip().upper()
        if s not in _SIDS:
            raise BlindtestError(f"未知样本编号：{sid}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
            raise BlindtestError(f"样本 {s} 的分数必须是 1~5 的整数")
        iv = int(v)
        if not 1 <= iv <= 5:
            raise BlindtestError(f"样本 {s} 的分数必须在 1~5 之间，收到 {iv}")
        out[s] = iv
    return out


def submit(token: str, scores: dict, note: str = "", ua: str = "") -> dict:
    """评委提交一次打分（同一链接可多人多次 —— 评委身份本来就不该被追踪）。"""
    sess = find_by_token(token)
    if sess.get("closed"):
        raise BlindtestError("这场盲听已经收卷了", status=409)
    normed = _norm_scores(scores)
    known = {s["sid"] for s in sess.get("samples") or []}
    unknown = set(normed) - known
    if unknown:
        raise BlindtestError(f"样本编号与本场不符：{'、'.join(sorted(unknown))}")
    if len(normed) != len(known):
        raise BlindtestError(f"还有 {len(known) - len(normed)} 个样本没打分（要全部打完才能交卷）")
    text = (note or "").strip()[:NOTE_MAX]
    with _SAVE_LOCK:
        cur = _load(sess["id"])
        if len(cur.get("responses") or []) >= MAX_RESPONSES:
            raise BlindtestError(f"打分条数已达上限（{MAX_RESPONSES}），这场收摊了", status=409)
        cur.setdefault("responses", []).append(
            {"at": int(time.time()), "ua": (ua or "")[:120], "scores": normed, "note": text}
        )
        _atomic_write_json(_session_path(sess["id"]), cur)
    return {"ok": True, "n_responses": len(cur["responses"])}


# ---------------------------------------------------------------- 汇总


def aggregate(sess: dict) -> list[dict]:
    """逐样本汇总：n / mean / std / min / max，按均分降序排名（rank 从 1 起）。

    只有 1 票的样本 std 记 0（statistics.stdev 需要 ≥2 个点），别让它把表格搞红。
    """
    labels = sess.get("labels") or []
    rows: list[dict] = []
    for i, s in enumerate(sess.get("samples") or []):
        vals = [
            r["scores"][s["sid"]]
            for r in sess.get("responses") or []
            if s["sid"] in (r.get("scores") or {})
        ]
        mean = round(statistics.fmean(vals), 2) if vals else None
        std = round(statistics.pstdev(vals), 2) if len(vals) > 1 else (0.0 if vals else None)
        rows.append(
            {
                "sid": s["sid"],
                "label": labels[i] if i < len(labels) else "",
                "order": s.get("order", 0),
                "n": len(vals),
                "mean": mean,
                "std": std,
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
            }
        )
    scored = [r for r in rows if r["mean"] is not None]
    scored.sort(key=lambda r: (-r["mean"], r["std"], r["sid"]))
    for rank, r in enumerate(scored, start=1):
        r["rank"] = rank
    return rows


def blind_overview(token: str) -> dict:
    """手机端收卷后的只读汇总：有排名、无身份 —— label 一个字段都不给。"""
    sess = find_by_token(token)
    rows = [
        {k: v for k, v in r.items() if k != "label"} for r in aggregate(sess)
    ]
    return {
        "title": sess.get("title") or "",
        "closed": bool(sess.get("closed")),
        "n_responses": len(sess.get("responses") or []),
        "samples": rows,
    }


# ---------------------------------------------------------------- 生命周期


def set_closed(session_id: str, closed: bool) -> dict:
    sess = _load(session_id)
    sess["closed"] = bool(closed)
    _save(sess)
    return {"ok": True, "id": session_id, "closed": bool(closed)}


def delete(session_id: str) -> dict:
    p = _session_path(session_id)
    if not p.is_file():
        raise BlindtestError(f"找不到盲听会话 {session_id}", status=404)
    p.unlink(missing_ok=True)
    shutil.rmtree(_audio_dir(session_id), ignore_errors=True)
    return {"ok": True, "id": session_id}


# ---------------------------------------------------------------- 伺服辅助


def audio_path(token: str, sid: str) -> Path:
    """手机端取样本音频：token + sid → 文件。sid 不在本场 → 404（不做目录枚举）。"""
    sess = find_by_token(token)
    for s in sess.get("samples") or []:
        if str(s.get("sid") or "").upper() == (sid or "").strip().upper():
            p = _audio_dir(sess["id"]) / str(s.get("file") or "")
            if p.is_file():
                return p
    raise BlindtestError("本场没有这个样本", status=404)


def audio_mime(p: Path) -> str:
    return _EXT_MIME.get(p.suffix.lower(), "audio/wav")


def phone_session(token: str) -> dict:
    """手机端会话形状：按播放顺序给 sid + 相对音频路径；label / 桌面字段一律不给。"""
    sess = find_by_token(token)
    samples = sorted(sess.get("samples") or [], key=lambda s: s.get("order", 0))
    tok = sess.get("token") or ""
    return {
        "title": sess.get("title") or "",
        "closed": bool(sess.get("closed")),
        "n_samples": len(samples),
        "samples": [
            {
                "sid": s["sid"],
                "order": s.get("order", 0),
                "url": f"/api/blindtest/t/{tok}/audio/{s['sid']}",
            }
            for s in samples
        ],
    }


#: 供测试断言用（保证「手机面不漏 label」的口径有处可查）。
_PHONE_FORBIDDEN_KEYS = ("labels", "label", "file", "responses")

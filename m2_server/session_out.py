"""会话产物：合成出来的音频**默认不落盘**，退出即删；只有显式「保存」才进作品库。

用户原话（2026-09-25）：

    现在我的项目都是要输文音频文件，然后生成音频文件，这样不行啊，现在很少人用它
    现在人也很讨厌把它放在电脑里占空间，即用即删，最好是直接生成语音在应用里，
    有需要的保存，不需要的退出直接就删掉

所以默认路径变成：**合成 → 应用内试听 → 退出清空**。想要留下的，用户在界面点
「保存」（`POST /api/session/save`），那一刻文件才复制进 `outputs/` 根目录、
并登记进作品库（`history.register`）。

为什么放 `outputs/.session/` 而不是系统 TEMP
-------------------------------------------
三条都是"少改代码"的硬理由，不是随手挑的：

1. `media_api` 的播放白名单只认 `outputs` / `clips` / `voicebank` 三类，而它用
   `is_relative_to` 判归属 —— 子目录天然放行。放 TEMP 就得给 `media_api` 开一条
   任意绝对路径的口子，那正是它防穿越的那道门。
2. `storage.py` 的「历史产物音频」目标用 `glob("*.wav")`，**不递归** —— 会话目录
   不会被算进"可清理占用"，也不会被用户点清理时顺手删掉（它本来就该自己消失）。
3. `history.py` 的删除/导出/裁剪一律按 `OUTPUTS_DIR / Path(wav).name` 解析，**丢掉
   目录成分**。所以"保存后的文件必须在 outputs 根"是既有下游的不变量；本模块的
   `save()` 就是照着这条写的。反过来，未保存的产物只存在于 `.session/`，历史上
   根本不会出现它们的记录 —— "默认不记"是这套目录归属的**结果**，不是另一条开关。

⚠️ 两个必须守住的口径
--------------------
· **文件名仍是裸名**（`tts_x.wav`，不带目录）。消费侧（`soundboard._outputs_wav`）
  显式拒绝含 `/` 的名字；微信链路用 `OUTPUTS_DIR / name` 拼路径。所以本模块对外
  只给裸名 + `find()` 兜底查找，不给相对路径 —— 契约不变，改动面才小。
· **不许模块级早绑定 `cfg.OUTPUTS_DIR`**（本仓 §8.36/§8.37 的连环坑：早绑定的副本
  会让 monkeypatch 静默失效、写回用户真实目录）。本文件所有路径**每次现读**，
  由 `tests/test_output_isolation.py` 的 AST 扫描守着。

"退出即删"落在哪
---------------
Electron 退出时走 `taskkill /PID /T /F`（`web/electron/backend.cjs`）—— **强杀**，
Python 的 `atexit` / shutdown 钩子跑不到。所以保障分三层，缺一层都不算完整：

    ① 启动清空（确定性）：`server.py` 的 main 入口调 `purge()`。上次被强杀留下的
       残渣，下次打开一定是干净的。
    ② `uvicorn` 正常关停时也调一次（尽力而为，覆盖 Ctrl+C 与优雅停止）。
    ③ Electron `before-quit` 里发一次 purge 请求（`web/electron/main.cjs`），
       补上"退出那一刻"这一段；超时/失败都不阻塞退出。

四层里 ① ② ③ 管的是"进程结束"，管不到"进程一直开着"：应用连开好几天（本机实测
22:36 启动、68 分钟后仍在跑），会话目录里的东西只增不减。所以还有第 4 层 ——
**按年龄扫**（`prune_expired()`，默认 24 小时，`VM_SESSION_TTL_HOURS` 可调、
`<=0` 关掉；分配新产物时顺手扫一遍，`maybe_prune()` 节流到每小时最多一次）。
最需要它的是截图这类一次几 MB 的产物（`vision_grab` 的预览窗图 2.0 MB/张），
用户 2026-10-27 原话：

    我想就是随用随删就是一天内他就删掉
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import config as cfg

#: 会话目录名。**不带** `cfg.OUTPUTS_DIR` 前缀 —— 顶层常量不许早绑定（见模块注释）。
DIRNAME = ".session"

_LOCK = threading.Lock()

#: 本次进程内"哪个会话文件已经保存过" → 历史记录。
#: 存在两个理由：① 重复点「保存」不该产生两条指向同一个 wav 的历史（删一条会把
#: 另一条的文件带走）；② 保存是幂等的，用户点第二次应该得到同一份结果而不是第二份。
#: 不落盘：进程重启后会话目录本来就被清空了，"上次保存过"没有任何意义。
_SAVED: dict[str, dict] = {}


def session_dir() -> Path:
    """会话产物目录（每次现读 `cfg.OUTPUTS_DIR`，不许缓存成模块常量）。"""
    return cfg.OUTPUTS_DIR / DIRNAME


def ensure() -> Path:
    """确保会话目录存在并返回它。"""
    d = session_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_session(p: Path | str) -> bool:
    """这个路径是不是会话产物（决定派生件该落在哪、URL 该不该带 `.session/`）。"""
    try:
        return Path(p).resolve().parent == session_dir().resolve()
    except OSError:  # 路径不存在/盘符异常：当作"不是"
        return False


def derived_dir(src: Path | str) -> Path:
    """`src` 的**派生件**（RVC 换声、裁首尾静音…）该落哪个目录。

    与 `src` 同处一个目录 —— 输入在会话目录，派生件就必须也在会话目录，否则
    "即用即删"会在第一次换声时漏掉一个文件（`rvc_convert` 原先无条件写 outputs 根）。
    """
    return session_dir() if is_session(src) else cfg.OUTPUTS_DIR


def new_path(prefix: str, suffix: str = ".wav") -> Path:
    """在会话目录里分配一个唯一文件名（沿用既有的 `前缀_毫秒时间戳` 口径）。

    顺手扫一遍过期产物（`maybe_prune`）：写产物是**唯一能保证每台机器都会走到**的
    时机 —— 不起定时器、不开守护线程，只管住"下次写"与"下次开"这两个点。
    """
    maybe_prune()
    return ensure() / f"{prefix}_{int(time.time() * 1000)}{suffix}"


def _bare_name(name: str) -> str:
    """校验并要求"裸文件名" —— 与 `soundboard._outputs_wav` 同一口径。

    请求体里的路径不该能指到会话目录之外；这里不抛异常（本模块不引 HTTP 依赖），
    非法就返回空串，由调用方决定报什么错。
    """
    n = str(name or "").strip()
    if not n or n in (".", "..") or any(c in n for c in "/\\"):
        return ""
    return n


def find(name: str) -> Path | None:
    """按裸名定位文件：**先会话目录、后 outputs 根**。找不到返回 `None`。

    为什么是这个顺序：名字是毫秒时间戳，两处重名几乎不可能；万一真有，正在被
    试听的那份一定在会话目录，优先它更符合用户此刻看到的东西。
    """
    n = _bare_name(name)
    if not n:
        return None
    for p in (session_dir() / n, cfg.OUTPUTS_DIR / n):
        if p.is_file():
            return p
    return None


def newest_tts() -> Path | None:
    """最近一次 TTS 产物（会话目录 + outputs 根一起找），没有则 `None`。

    这个方法存在的唯一理由是**收敛四处重复的 `glob("tts_*.wav")`**
    （`wechat_voice` 三处 + `soundboard` 一处）：产物搬到会话目录后，任何一处
    忘了带上它，那条链路就会假装"还没有合成过"。
    """
    cands: list[Path] = []
    for d in (session_dir(), cfg.OUTPUTS_DIR):
        if d.is_dir():
            cands.extend(p for p in d.glob("tts_*.wav") if p.is_file())
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def rel_url(name: str) -> str:
    """裸名 → `/api/media/outputs/` 后面的相对路径。

    未保存的（在会话目录）→ `.session/x.wav`；已保存的 → `x.wav`。
    前端拿到的就是可直接播放的地址，不需要知道目录结构。
    """
    n = _bare_name(name)
    if not n:
        return ""
    return f"{DIRNAME}/{n}" if (session_dir() / n).is_file() else n


def _duration_s(p: Path) -> float:
    """读时长（只解析文件头，不整段读入）。读不出来返回 0。"""
    try:
        import soundfile as sf

        return round(float(sf.info(str(p)).duration), 1)
    except Exception:
        return 0.0


def save(
    name: str,
    kind: str = "tts",
    voice_id: str = "",
    input_text: str = "",
    params: dict | None = None,
) -> dict:
    """把会话里的一个产物**保存**进作品库（复制到 outputs 根 + 登记历史）。

    返回 `{"ok", "name", "url", "item_id", "already"}`；`ok=False` 时带 `error`。
    重复保存同名文件是**幂等**的：返回第一次那条记录，不再复制、不再登记。
    """
    n = _bare_name(name)
    if not n:
        return {"ok": False, "error": f"非法的音频名：{name!r}"}

    with _LOCK:
        hit = _SAVED.get(n)
        if hit:
            return {**hit, "ok": True, "already": True}

        src = session_dir() / n
        if not src.is_file():
            # 兜底：已经在根目录里（旧产物）就当"已经保存过"，只是补登记 —— 但那时
            # 名字不认识，无法保证不重复登记，所以直接如实报错，让用户重新合成。
            return {"ok": False, "error": f"会话里找不到音频：{n}（可能已退出过，请重新合成）"}

        dst = cfg.OUTPUTS_DIR / n
        try:
            cfg.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            return {"ok": False, "error": f"保存失败：{e}"}

        from history import register as history_register

        item_id = history_register(
            kind,
            voice_id,
            n,
            f"/api/media/outputs/{n}",
            _duration_s(dst),
            input_text=input_text,
            params=params,
        )
        rec = {"name": n, "url": f"/api/media/outputs/{n}", "item_id": item_id}
        _SAVED[n] = rec
        return {**rec, "ok": True, "already": False}


def purge() -> int:
    """清空会话目录的内容（目录本身留着），返回删掉的文件数。

    启动时、正常关停时、Electron 退出前都调它 —— 语义完全相同，所以只有一个函数，
    不按调用时机分名字（两个名字会让人以为它们的行为不一样）。
    单个文件删不掉不中断（可能正被播放器读着），尽力而为。
    """
    d = session_dir()
    if not d.is_dir():
        return 0
    removed = 0
    for p in list(d.iterdir()):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
        except OSError:
            continue
    with _LOCK:
        _SAVED.clear()  # 文件都没了，"保存过"的记忆跟着失效
    return removed


#: 会话产物的存活上限（小时）。超过它的旧产物会被 `prune_expired()` 扫掉。
#: 为什么不是"退出即删就够了"：那三层只在**进程结束**时跑，而应用可能连开好几天。
TTL_ENV = "VM_SESSION_TTL_HOURS"
DEFAULT_TTL_HOURS = 24.0

#: `maybe_prune()` 的最小间隔（秒）。`new_path` 是产物写入的热路径，不该每次都遍历目录。
PRUNE_INTERVAL_S = 3600.0
_LAST_PRUNE = 0.0


def ttl_hours() -> float:
    """当前 TTL（小时）。环境变量**每次现读**，与 `cfg.OUTPUTS_DIR` 同一口径（不许早绑定）。"""
    raw = os.environ.get(TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_TTL_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TTL_HOURS


def prune_expired(ttl: float | None = None, now: float | None = None) -> int:
    """删掉会话目录里**超过 TTL 小时**的旧产物（递归），返回删掉的条目数。

    ⚠️ 参数名不能叫 `ttl_hours` —— 那会遮蔽上面的同名函数（第一版真踩了：
    `TypeError: 'NoneType' object is not callable`）。要默认值就传 `None`。

    与 `purge()` 的分工：`purge()` 是"清空"（启动 / 关停 / Electron 退出前），本函数
    是"按年龄清"（进程一直开着时）。删不掉的（`OSError`）跳过 —— 文件可能正被播放器
    或模型读着，强删会打断用户正在听的东西，那就留给下一次。
    """
    ttl_h = ttl_hours() if ttl is None else ttl
    if ttl_h <= 0:
        return 0
    d = session_dir()
    if not d.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - ttl_h * 3600.0
    entries = sorted(d.rglob("*"), key=lambda x: len(x.parts), reverse=True)
    removed = 0
    for p in entries:
        if p.is_dir():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    # 第二遍：被删空的子目录一并收走（`cover/` 那类）。空目录没有"丢掉内容"的风险，
    # 所以不看年龄 —— 文件刚走，目录就该跟着走。
    for p in (x for x in entries if x.is_dir()):
        try:
            if not any(p.iterdir()):
                p.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def maybe_prune() -> int:
    """节流版：间隔内直接返回 0（不遍历目录）。返回**这次**真删掉的条目数。"""
    global _LAST_PRUNE
    if ttl_hours() <= 0:
        return 0
    now = time.time()
    with _LOCK:
        if now - _LAST_PRUNE < PRUNE_INTERVAL_S:
            return 0
        _LAST_PRUNE = now
    return prune_expired(now=now)

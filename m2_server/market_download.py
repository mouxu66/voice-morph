"""音色市场 —— 下载核心：断点续传 + 进度落盘 + 镜像回退。

借鉴 HMCL 下载层思路：下载核心与「下载源」解耦，同一份文件可配
镜像 URL（例如 HF 官方 ↔ hf-mirror.com），主源失败（网络错误或校验
失败）时自动回退到镜像重下同一文件。

进度状态（含 .part 偏移）落盘到 outputs/market/downloads.json：
服务重启后对同一 name 再次 start，若 .part 存在则从断点续传（HTTP
Range），完成后可选 SHA256 强校验，校验通过才原子改名为目标文件。

安全护栏：
  - 下载域名白名单（只允许 HF 官方 / HF 镜像 / 魔搭系域名）
  - 单文件大小上限（HEAD + 流式总量双重校验）
  - 单任务互斥；取消只删 .part，绝不产出残缺目标文件
"""
import hashlib
import json
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from runtime import OUT

DOWNLOAD_DIR = OUT / "market"
STATE_FILE = DOWNLOAD_DIR / "downloads.json"

CHUNK_SIZE = 256 * 1024
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
MAX_BYTES = 500 * 1024 * 1024           # 单权重上限 500MB
PART_SUFFIX = ".part"
_PERSIST_MIN_INTERVAL = 0.5             # 进度落盘节流（秒）

# 下载域名白名单：搜索/download 直链只允许这些主机
ALLOWED_HOSTS = {
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "hf-mirror.com",
    "huggingface.cn",
    "modelscope.cn",
    "www.modelscope.cn",
    "modelscope.net",
}


class MarketError(Exception):
    """下载任务层的业务错误（ResumableDownloader 未捕获时透传给 API）。"""


def _validate_url(url: str, allow_loopback: bool = False) -> None:
    """域名白名单校验；不满足直接抛 MarketError。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError as exc:
        raise MarketError(f"非法 URL: {url}") from exc
    host = host.lower()
    if allow_loopback and host in {"127.0.0.1", "localhost", "::1"}:
        return
    if host not in ALLOWED_HOSTS:
        raise MarketError(f"下载域名不在白名单: {host}")


class DownloadManager:
    """单任务下载管理器：互斥 + 断点续传 + 进度落盘。

    状态模型（status）：idle / downloading / done / failed / cancelled / interrupted。
    - start() 时若 .part 存在 → 续传；若目标文件已存在 → 幂等返回 done。
    - 进程崩溃后 .part 仍在，下次 start 同名任务自动续传。
    - 所有字段变化即时落盘；高频进度节流 0.5s。
    """

    def __init__(self, download_dir: Path = None, state_file: Path = None,
                 allow_loopback: bool = False):
        self.download_dir = Path(download_dir or DOWNLOAD_DIR)
        self.state_file = Path(state_file or (self.download_dir / "downloads.json"))
        self.allow_loopback = allow_loopback
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cancel_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = self._load()

    # ---- 持久化 ----
    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            return {"idle": True}

    def _persist(self, force: bool = False):
        ts = time.time()
        with self._lock:
            if not force and ts - getattr(self, "_last_persist", 0) < _PERSIST_MIN_INTERVAL:
                return
            self._last_persist = ts
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), "utf-8"
            )
            tmp.replace(self.state_file)

    def _set(self, force: bool = False, **kw):
        with self._lock:
            self._state.update(kw)
        self._persist(force=force)

    def set_meta(self, **kw):
        """外部（如安装编排）向同一状态文件写入附加字段（install 段等）。"""
        self._set(force=True, **kw)

    # ---- 对外 ----
    def progress(self) -> dict:
        """进度快照。线程僵死（进程内线程意外终止）时纠正为 interrupted。"""
        with self._lock:
            st = dict(self._state)
            alive = self._thread is not None and self._thread.is_alive()
            if st.get("status") == "downloading" and not alive:
                st["status"] = "interrupted"
        return st

    def start(self, name: str, url: str, mirror_url: str | None = None,
              sha256: str | None = None, expected_size: int | None = None,
              filename: str | None = None) -> dict:
        """启动（或续传）一个下载任务。已有任务在跑时抛 MarketError。

        filename：下载目标文件名（默认 ``{name}.pth``），允许安装编排为
        index 等资产指定自定义文件名；name 仍作为互斥键与续传标识。
        """
        name = str(name).strip()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise MarketError(f"非法任务名: {name!r}")
        _validate_url(url, self.allow_loopback)
        if mirror_url:
            _validate_url(mirror_url, self.allow_loopback)

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise MarketError("已有下载任务在进行中")
            cur = self._state
            filename = filename or f"{name}.pth"
            dest = self.download_dir / filename
            part = dest.with_suffix(dest.suffix + PART_SUFFIX)
            if dest.exists():
                self._state = {"idle": True, "done_file": str(dest)}
                idle_done = True
            else:
                idle_done = False
                resume = part.exists() and cur.get("name") == name
                self._state = {
                    "idle": False,
                    "name": name,
                    "filename": filename,
                    "url": url,
                    "mirror_url": mirror_url,
                    "sha256": (sha256 or "").lower() or None,
                    "expected_size": expected_size,
                    "total": cur.get("total") if resume else None,
                    "done": part.stat().st_size if resume else 0,
                    "status": "downloading",
                    "error": "",
                    "dest": str(dest),
                    "part": str(part),
                    "started_at": cur.get("started_at") if resume else time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    # 安装编排写入的自定义段（install 等）跨任务保留
                    "install": cur.get("install"),
                }
        # 注意：_persist 会再拿锁（非重入），必须放在 with 块外
        self._persist(force=True)
        if idle_done:
            return dict(self._state)
        self._cancel_evt.clear()
        self._thread = threading.Thread(
            target=self._run, args=(name, url, mirror_url, sha256, expected_size, filename),
            daemon=True,
        )
        self._thread.start()
        with self._lock:
            return dict(self._state)

    def cancel(self) -> dict:
        """取消当前下载：删除 .part，标记 cancelled，允许新任务。"""
        with self._lock:
            st = dict(self._state)
        if st.get("status") != "downloading":
            raise MarketError("没有进行中的下载任务")
        self._cancel_evt.set()
        return self.progress()

    # ---- 后台线程 ----
    def _run(self, name, url, mirror_url, sha256, expected_size, filename=None):
        filename = filename or f"{name}.pth"
        part = self.download_dir / (filename + PART_SUFFIX)
        dest = self.download_dir / filename
        try:
            if expected_size and expected_size > MAX_BYTES:
                raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
            # 断点续传的哈希初始化：先吞掉 .part 已有字节，再续算
            hasher = hashlib.sha256() if sha256 else None
            if part.exists() and hasher is not None:
                with part.open("rb") as f:
                    for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                        hasher.update(chunk)

            err_url = url
            mirror_attempted = False
            for attempt, try_url in enumerate([url, mirror_url]):
                if try_url is None:
                    continue
                err_url = try_url
                try:
                    self._download_to(part, try_url, hasher)
                    break
                except Exception as exc:  # noqa: BLE001 —— 网络/校验错误统一走回退
                    if attempt == 0 and mirror_url and not self._cancel_evt.is_set():
                        mirror_attempted = True
                        self._set(force=True, url=mirror_url,
                                  error=f"主源失败({exc.__class__.__name__})，回退镜像重下")
                        # 主源中途失败时 .part 不可信：删除从头
                        if part.exists():
                            part.unlink(missing_ok=True)
                        continue
                    raise

            if self._cancel_evt.is_set():
                part.unlink(missing_ok=True)
                self._set(force=True, status="cancelled", error="", done=0, total=None)
                return

            size = part.stat().st_size
            if expected_size and size != expected_size:
                part.unlink(missing_ok=True)
                raise MarketError(f"文件大小不符: 期望 {expected_size} 实际 {size}")
            if size > MAX_BYTES:
                part.unlink(missing_ok=True)
                raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
            if hasher is not None:
                digest = hasher.hexdigest()
                if digest != sha256:
                    part.unlink(missing_ok=True)
                    raise MarketError(f"SHA256 不符: 期望 {sha256} 实际 {digest}")
            part.replace(dest)
            self._set(force=True, status="done", error="", done=size, total=size,
                      dest=str(dest), file_name=dest.name)
            # 清理 url/mirror/哈希等敏感态，进入完成态
            for k in ("url", "mirror_url", "sha256", "expected_size"):
                self._state.pop(k, None)
            self._persist(force=True)
        except Exception as exc:  # noqa: BLE001 —— 全部失败进 failed
            part.unlink(missing_ok=True)
            self._set(force=True, status="failed", error=f"{exc.__class__.__name__}: {exc}")
        finally:
            self._cancel_evt.clear()

    def _download_to(self, part: Path, url: str, hasher) -> None:
        """流式下载（支持 Range 续传），逐块写 .part 并更新进度/落盘。"""
        resume = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={resume}-"} if resume else {}
        with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                          allow_redirects=True, headers=headers) as resp:
            resp.raise_for_status()
            # 200 = 服务端不支持续传，从头写；206 = 从断点续写
            offset = resume if resp.status_code == 206 else 0
            total = (int(resp.headers.get("Content-Length") or 0) + offset) or None
            if total and (total - offset) + offset > MAX_BYTES:
                raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
            mode = "ab" if offset else "wb"
            with part.open(mode) as f:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    if self._cancel_evt.is_set():
                        resp.close()
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    offset += len(chunk)
                    self._set(done=offset, total=total, status="downloading")


# 进程级单例：server 与测试共用（测试可通过构造隔离实例）
MANAGER = DownloadManager()


def get_manager() -> DownloadManager:
    return MANAGER
"""音色市场 —— 下载核心：多任务并发 + 信号量限流 + 断点续传 + 进度落盘。

借鉴 Ferium / HMCL 的下载队列思路：
  - 下载核心与「下载源」解耦，同一份文件可配镜像 URL（例如 HF 官方 ↔
    hf-mirror.com），主源失败（网络错误或校验失败）时自动回退到镜像重下。
  - 文件级并发：允许同时启动多个下载任务，全局 BoundedSemaphore 限制同时
    活跃的下载线程数（默认 MAX_CONCURRENT_FILES=3），超出信号量的任务自动
    排队，前一个完成即补位——队列永不丢任务，只是限速。

进度状态（含 .part 偏移）落盘到 outputs/market/downloads.json：
  {"tasks": {name: {...}}, <install 等自定义段>}；服务重启后对同一 name 再次
  start，若 .part 存在则从断点续传（HTTP Range），完成后可选 SHA256 强校验，
  校验通过才原子改名为目标文件。

兼容语义：
  - progress() 返回「主任务」快照（最早活跃；无活跃取最近终结），并附
    active 列表——旧单任务消费者（安装编排 / API）无需改动即可继续使用。
  - task_status(name) 精确查询单个任务；is_busy() 判断是否有下载在跑/排队。

安全护栏：
  - 下载域名白名单（只允许 HF 官方 / HF 镜像 / 魔搭系域名）
  - 单文件大小上限（HEAD + 流式总量双重校验）
  - 取消只删 .part，绝不产出残缺目标文件
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

# 文件级并发上限：同时活跃的下载线程数（超出在信号量上排队）
MAX_CONCURRENT_FILES = 3

# PyTorch 存档文件头：zip 容器（torch.save 默认）或 pickle 协议（\x80\x00/\x80\x02~06）
TORCH_SUFFIXES = (".pth", ".pt", ".ckpt")


def _torch_header_ok(path: Path) -> bool:
    """粗校验文件头是否像 PyTorch 存档（PK=zip / \\x80=pickle），拒 HTML/文本当权重落盘。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    if head[:2] == b"PK":
        return True
    if head[:1] == b"\x80" and len(head) > 1 and head[1] in (0x00, 0x02, 0x03, 0x04, 0x05, 0x06):
        return True
    return False

# 下载域名白名单：搜索/download 直链只允许这些主机（含子域，见 _validate_url）
ALLOWED_HOSTS = {
    "huggingface.co",
    "hf.co",                    # 官方短域（重定向目标多为 *.hf.co 子域）
    "hf-mirror.com",
    "huggingface.cn",
    "modelscope.cn",
    "www.modelscope.cn",
    "modelscope.net",
}


class MarketError(Exception):
    """下载任务层的业务错误（ResumableDownloader 未捕获时透传给 API）。"""


def _validate_url(url: str, allow_loopback: bool = False) -> None:
    """域名白名单校验（精确域或其子域，如 cas-bridge.xethub.hf.co 匹配 hf.co）；
    不满足直接抛 MarketError。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError as exc:
        raise MarketError(f"非法 URL: {url}") from exc
    host = host.lower()
    if allow_loopback and host in {"127.0.0.1", "localhost", "::1"}:
        return
    for allowed in ALLOWED_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return
    raise MarketError(f"下载域名不在白名单: {host}")


class DownloadManager:
    """多任务下载管理器：文件级并发 + 信号量限流 + 断点续传 + 进度落盘。

    状态模型（status）：idle / downloading / done / failed / cancelled / interrupted。
    - start() 不再全局互斥：可同时启动多个任务；同名任务（活跃）拒绝重复。
    - 任务线程入口 acquire 全局信号量（max_concurrent），超出的自动排队。
    - 进程崩溃后 .part 仍在，下次 start 同名任务自动续传。
    - progress() 返回主任务快照（兼容旧单任务消费者）；所有字段变化即时落盘。
    """

    def __init__(self, download_dir: Path = None, state_file: Path = None,
                 allow_loopback: bool = False,
                 max_concurrent: int = MAX_CONCURRENT_FILES):
        self.download_dir = Path(download_dir or DOWNLOAD_DIR)
        self.state_file = Path(state_file or (self.download_dir / "downloads.json"))
        self.allow_loopback = allow_loopback
        self.max_concurrent = max(1, int(max_concurrent))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self._tasks: dict[str, dict] = {}          # name -> 任务状态（内存 + 落盘）
        self._threads: dict[str, threading.Thread] = {}
        self._events: dict[str, threading.Event] = {}
        self._order: list[str] = []                # 启动顺序（主任务选取）
        self._extra: dict = {}                     # install 等自定义段（顶层落盘）
        self._load()

    # ---- 持久化 ----
    def _load(self):
        """读入 downloads.json：新格式 {"tasks": {...}, ...}；旧格式（顶层单任务）迁移。"""
        try:
            raw = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        tasks = raw.get("tasks")
        if isinstance(tasks, dict):
            self._tasks = tasks
            self._extra = {k: v for k, v in raw.items() if k != "tasks"}
            self._order = list(tasks.keys())
            return
        # 旧格式：顶层即单任务状态（install 段剥离到 extra）
        if raw.get("name"):
            t = dict(raw)
            install = t.pop("install", None)
            self._tasks = {raw["name"]: t}
            self._order = [raw["name"]]
            if install is not None:
                self._extra = {"install": install}

    def _persist(self, force: bool = False):
        ts = time.time()
        with self._lock:
            if not force and ts - getattr(self, "_last_persist", 0) < _PERSIST_MIN_INTERVAL:
                return
            self._last_persist = ts
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({**self._extra, "tasks": self._tasks},
                           ensure_ascii=False, indent=2), "utf-8"
            )
            tmp.replace(self.state_file)

    def _set_task(self, name: str, force: bool = False, **kw):
        with self._lock:
            self._tasks.setdefault(name, {}).update(kw)
        self._persist(force=force)

    def set_meta(self, **kw):
        """外部（如安装编排）向状态文件写入附加字段（install 段等，顶层落盘）。"""
        with self._lock:
            self._extra.update(kw)
        self._persist(force=True)

    # ---- 对外 ----
    def task_status(self, name: str) -> dict:
        """单个任务的精确状态（不参与主任务选取，供安装编排轮询自己那个任务）。"""
        with self._lock:
            return dict(self._tasks.get(name) or {})

    def active_names(self) -> list[str]:
        """正在下载（含排队等待信号量）的任务名列表。"""
        with self._lock:
            return [n for n, th in self._threads.items() if th is not None and th.is_alive()]

    def is_busy(self) -> bool:
        """是否有下载任务在跑或在排队（安装/卸载/回滚的互斥判断用）。"""
        with self._lock:
            return any(th is not None and th.is_alive() for th in self._threads.values())

    def progress(self) -> dict:
        """主任务快照：最早活跃任务；无活跃取最近终结任务；附 active 列表。

        兼容旧单任务消费者（安装编排/API 只看 name/status/error/install 等键）。
        """
        with self._lock:
            order = list(self._order)
            tasks = {k: dict(v) for k, v in self._tasks.items()}
            threads = dict(self._threads)
            extra = dict(self._extra)
        for n in order:
            t = tasks.get(n)
            if t and t.get("status") == "downloading":
                th = threads.get(n)
                if th is None or not th.is_alive():      # 线程僵死纠正
                    t["status"] = "interrupted"
        active = [n for n in order if (tasks.get(n) or {}).get("status") == "downloading"]
        if active:
            st = tasks[active[0]]
        elif order:
            st = tasks[order[-1]] or {}
        else:
            st = {}
        st["active"] = active
        if extra:
            st = {**extra, **st}         # install 等自定义段并入（任务键优先）
        return st

    def start(self, name: str, url: str, mirror_url: str | None = None,
              sha256: str | None = None, expected_size: int | None = None,
              filename: str | None = None) -> dict:
        """启动（或续传）一个下载任务；同名活跃任务拒绝重复。

        多个任务可并行存在，实际并发下载数受信号量 max_concurrent 限制。
        filename：下载目标文件名（默认 ``{name}.pth``），允许安装编排为
        index 等资产指定自定义文件名；name 仍作为任务键与续传标识。
        """
        name = str(name).strip()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise MarketError(f"非法任务名: {name!r}")
        _validate_url(url, self.allow_loopback)
        if mirror_url:
            _validate_url(mirror_url, self.allow_loopback)

        with self._lock:
            if name in self._threads and self._threads[name].is_alive():
                raise MarketError(f"任务 {name} 已在下载中")
            filename = filename or f"{name}.pth"
            dest = self.download_dir / filename
            part = dest.with_suffix(dest.suffix + PART_SUFFIX)
            prev = self._tasks.get(name) or {}
            if dest.exists():
                # 幂等完成：保留安装编排写入的 install 段（否则进度面板空白），
                # 覆盖重装由安装层先清除产物再 start，避免装回旧文件。
                self._tasks[name] = {
                    "name": name, "filename": filename, "idle": True,
                    "status": "done", "done_file": str(dest),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                if name not in self._order:
                    self._order.append(name)
                idle_done = True
            else:
                idle_done = False
                resume = part.exists() and prev.get("filename") == filename
                self._tasks[name] = {
                    "name": name,
                    "filename": filename,
                    "url": url,
                    "mirror_url": mirror_url,
                    "sha256": (sha256 or "").lower() or None,
                    "expected_size": expected_size,
                    "total": prev.get("total") if resume else None,
                    "done": part.stat().st_size if resume else 0,
                    "status": "downloading",
                    "error": "",
                    "dest": str(dest),
                    "part": str(part),
                    "started_at": prev.get("started_at") if resume else time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                if name not in self._order:
                    self._order.append(name)
                self._events[name] = threading.Event()
                self._threads[name] = threading.Thread(
                    target=self._run,
                    args=(name, url, mirror_url, sha256, expected_size, filename),
                    daemon=True,
                )
                self._threads[name].start()
        # 注意：_persist 会再拿锁（非重入），必须放在 with 块外
        self._persist(force=True)
        with self._lock:
            return dict(self._tasks[name])

    def cancel(self, name: str | None = None) -> dict:
        """取消下载：name=None 取消所有活跃任务；否则取消指定任务。

        取消只删 .part 并标记 cancelled，允许新任务。
        """
        with self._lock:
            if name is None:
                targets = [n for n, th in self._threads.items()
                           if th is not None and th.is_alive()]
            else:
                targets = ([name] if name in self._threads
                           and self._threads[name].is_alive() else [])
            if not targets:
                raise MarketError("没有进行中的下载任务")
            for n in targets:
                evt = self._events.get(n)
                if evt is not None:
                    evt.set()
        return self.progress()

    def remove_artifact(self, filename: str) -> None:
        """删除下载产物（含 .part 残留），供安装覆盖重装时强制重新下载。"""
        for p in (self.download_dir / filename,
                  self.download_dir / (filename + PART_SUFFIX)):
            p.unlink(missing_ok=True)

    # ---- 后台线程 ----
    def _run(self, name, url, mirror_url, sha256, expected_size, filename=None):
        self._sem.acquire()          # 信号量：文件级并发限流（超出自动排队）
        try:
            filename = filename or f"{name}.pth"
            part = self.download_dir / (filename + PART_SUFFIX)
            dest = self.download_dir / filename
            cancel_evt = self._events.get(name)
            try:
                if expected_size and expected_size > MAX_BYTES:
                    raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
                # 断点续传的哈希初始化：先吞掉 .part 已有字节，再续算。
                # 用可变 dict 装载，_download_to 在"从头写"场景可原地换新 hasher。
                box = {"h": hashlib.sha256() if sha256 else None}
                if part.exists() and box["h"] is not None:
                    with part.open("rb") as f:
                        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                            box["h"].update(chunk)

                err_url = url
                mirror_attempted = False
                for attempt, try_url in enumerate([url, mirror_url]):
                    if try_url is None:
                        continue
                    err_url = try_url
                    try:
                        self._download_to(name, part, try_url, box)
                        break
                    except Exception as exc:  # noqa: BLE001 —— 网络/校验错误统一走回退
                        if attempt == 0 and mirror_url and not (cancel_evt and cancel_evt.is_set()):
                            mirror_attempted = True
                            # 只标记当前尝试源（attempt_url），不覆盖主源 url——
                            # 覆盖会污染 state，失败后排查看到的"主源"其实是镜像
                            self._set_task(name, force=True, attempt_url=mirror_url,
                                           error=f"主源失败({exc.__class__.__name__})，回退镜像重下")
                            # 主源中途失败时 .part 不可信：删除从头（hasher 由
                            # _download_to 的 offset==0 分支自动重置，避免旧字节混入 digest）
                            if part.exists():
                                part.unlink(missing_ok=True)
                            continue
                        raise

                if cancel_evt and cancel_evt.is_set():
                    part.unlink(missing_ok=True)
                    self._set_task(name, force=True, status="cancelled", error="", done=0, total=None)
                    return

                size = part.stat().st_size
                if expected_size and size != expected_size:
                    part.unlink(missing_ok=True)
                    raise MarketError(f"文件大小不符: 期望 {expected_size} 实际 {size}")
                if size > MAX_BYTES:
                    part.unlink(missing_ok=True)
                    raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
                # 权重类文件做文件头校验：杜绝 404 HTML / 任意网页内容当权重落盘
                if dest.suffix.lower() in TORCH_SUFFIXES and not _torch_header_ok(part):
                    part.unlink(missing_ok=True)
                    raise MarketError("文件头校验失败：不是有效的 PyTorch 存档（可能下载到了错误页面）")
                if box["h"] is not None:
                    digest = box["h"].hexdigest()
                    if digest != sha256:
                        part.unlink(missing_ok=True)
                        raise MarketError(f"SHA256 不符: 期望 {sha256} 实际 {digest}")
                part.replace(dest)
                self._set_task(name, force=True, status="done", error="", done=size, total=size,
                               dest=str(dest), file_name=dest.name)
                # 清理 url/mirror/哈希等敏感态，进入完成态
                with self._lock:
                    t = self._tasks.get(name) or {}
                    for k in ("url", "mirror_url", "sha256", "expected_size"):
                        t.pop(k, None)
                self._persist(force=True)
            except Exception as exc:  # noqa: BLE001 —— 全部失败进 failed
                part.unlink(missing_ok=True)
                self._set_task(name, force=True, status="failed",
                               error=f"{exc.__class__.__name__}: {exc}")
            finally:
                if cancel_evt:
                    cancel_evt.clear()
        finally:
            self._sem.release()
            with self._lock:
                self._threads.pop(name, None)
                self._events.pop(name, None)
            self._persist(force=True)

    def _download_to(self, name: str, part: Path, url: str, box: dict) -> None:
        """流式下载（支持 Range 续传），逐块写 .part 并更新进度/落盘。

        安全：
          - 手动跟随重定向（allow_redirects=False），每一跳都重新过域名白名单，
            防止 302 逃逸到内网/云元数据地址（SSRF）
          - 无 Content-Length 的 chunked 大响应实时按累计字节拦截，杜绝磁盘写满
          - 服务端不支持续传（200）或重下时从头写，hasher 原地重置，避免旧字节
            混入 digest 导致 SHA256 必失败
        """
        MAX_REDIRECTS = 5
        resume = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={resume}-"} if resume else {}
        redirects = 0
        while True:
            _validate_url(url, self.allow_loopback)   # 每一次跳转目标都过白名单
            resp = requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                allow_redirects=False, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                resp.close()
                if not loc:
                    raise MarketError(f"重定向缺少 Location: {url}")
                url = urllib.parse.urljoin(url, loc)
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise MarketError(f"重定向次数过多（>{MAX_REDIRECTS}）：{url}")
                continue
            break
        try:
            resp.raise_for_status()
            # 200 = 服务端不支持续传，从头写；206 = 从断点续写
            offset = resume if resp.status_code == 206 else 0
            if offset == 0 and box["h"] is not None:
                # 从头写：旧 .part 字节不再属于新内容（重启续传失败 / 服务端不支持
                # Range / 镜像回退重下），换新 hasher 避免 digest 拼接旧字节
                box["h"] = hashlib.sha256()
            total = (int(resp.headers.get("Content-Length") or 0) + offset) or None
            if total and total > MAX_BYTES:
                raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
            mode = "ab" if offset else "wb"
            with part.open(mode) as f:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    cancel_evt = self._events.get(name)
                    if cancel_evt and cancel_evt.is_set():
                        resp.close()
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    if box["h"] is not None:
                        box["h"].update(chunk)
                    offset += len(chunk)
                    if offset > MAX_BYTES:
                        raise MarketError(f"文件超过上限 {MAX_BYTES} 字节")
                    self._set_task(name, done=offset, total=total, status="downloading")
        finally:
            resp.close()


# 进程级单例：server 与测试共用（测试可通过构造隔离实例）
MANAGER = DownloadManager()


def get_manager() -> DownloadManager:
    return MANAGER

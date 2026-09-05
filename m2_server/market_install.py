"""音色市场 —— 安装编排：下载 → 落位到 RVC 音色库。

安装 = 用 DownloadManager 串行下载 .pth（必需）与 .index（可选），
完成后把文件落位到 RVC 集成包目录，使 /voices 与 /rvc/voices 自动识别：

    RVC_ROOT/logs/<voice_id>/<voice_id>.pth       查找入口（find_pth 优先名）
    RVC_ROOT/logs/<voice_id>/added_<voice_id>.index  model_ready 判定 + 实时变声检索
    RVC_ROOT/assets/weights/<voice_id>.pth         实时变声推理权重（rtrvc 专用）

阶段性状态（install 段）随 downloads.json 落盘：服务重启后对未完成的安装
重新 start；若 .part 仍在由 DownloadManager 断点续传，已拷贝文件幂等覆盖。

安全：
  - voice_id 只允许 [A-Za-z0-9_-]（避免中文/路径穿越污染 RVC 目录）
  - 下载 URL 仍走 DownloadManager 域名白名单
  - 单安装互斥；下载中不能并发安装（复用 DownloadManager 的互斥）
"""
import json
import re
import shutil
import threading
import time

from market_download import DownloadManager, MarketError, get_manager, _torch_header_ok
import config as cfg

VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

INSTALL_PHASES = ("downloading_pth", "downloading_index", "staging")


class InstallError(MarketError):
    """安装编排层业务错误（409 透传给 API）。"""


class InstallManager:
    """单安装互斥编排器：串行 pth → index → 落位。"""

    def __init__(self, manager: DownloadManager | None = None):
        self.manager = manager or get_manager()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- 对外 ----
    def run(self, voice_id: str, download: dict, index: dict | None = None,
            display_name: str = "", manifest_id: str = "",
            overwrite: bool = False) -> dict:
        """启动安装：校验 → 状态落盘 → 后台线程执行。已有安装/下载在跑抛 InstallError。

        overwrite=False：目标音色已存在（logs/assets 任一，含自训产物）时抛 409，
        绝不静默覆盖。overwrite=True：清除旧下载产物与本地目标后完整重装。
        """
        if not voice_id or not VOICE_ID_RE.match(voice_id):
            raise InstallError(f"非法音色 ID: {voice_id!r}（限字母数字_\\-）")
        if not download or not download.get("url"):
            raise InstallError("缺少权重下载链接")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise InstallError("已有安装任务在进行中")
            st = self.manager.progress()
            if st.get("status") == "downloading":
                raise InstallError("已有下载任务在进行中，请稍后再安装")
            cur = dict(st)
        conflict = self._conflict_paths(voice_id)
        if conflict and not overwrite:
            raise InstallError(
                f"音色 {voice_id} 已存在：{'、'.join(str(p) for p in conflict)}（含自训产物）。"
                "如需覆盖请显式指定 overwrite=true")
        if conflict:
            # 覆盖重装：清除旧下载产物（防止 DownloadManager 幂等分支装回旧文件）
            for suffix in (".pth", ".index"):
                self.manager.remove_artifact(f"{voice_id}{suffix}")
        self.manager.set_meta(install={
            "voice_id": voice_id,
            "display_name": display_name or voice_id,
            "manifest_id": manifest_id,
            "status": "queued",         # queued/downloading_*/staging/installed/failed/cancelled/interrupted
            "phase": "",
            "message": "排队中",
            "percent": 0.0,
            "error": "",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "resume_pth": cur.get("name") == f"install_{voice_id}" and cur.get("status") != "done",
        })
        self._thread = threading.Thread(
            target=self._run,
            args=(voice_id, download, index, display_name, manifest_id), daemon=True,
        )
        self._thread.start()
        return self.progress()

    @staticmethod
    def _conflict_paths(voice_id: str) -> list:
        """返回已存在的本地音色路径（logs/<id>/<id>.pth、assets/weights/<id>.pth）。"""
        out = []
        log_pth = cfg.RVC_ROOT / "logs" / voice_id / f"{voice_id}.pth"
        w_pth = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
        if log_pth.exists():
            out.append(log_pth)
        if w_pth.exists():
            out.append(w_pth)
        return out

    def cancel(self) -> dict:
        """取消进行中的安装（挂起中的下载任务随 manager.cancel 一并取消）。

        仅 queued/downloading_*/staging 活跃状态可取消；installed/failed 及
        残留的 cancelled/interrupted 一律视为"无进行中任务"抛 InstallError（409）。
        """
        ACTIVE = ("queued", "downloading_pth", "downloading_index", "staging")
        with self._lock:
            st = self.progress_locked()
        install = st.get("install") or {}
        if install.get("status") not in ACTIVE:
            raise InstallError("没有进行中的安装任务")
        if st.get("status") == "downloading":
            try:
                self.manager.cancel()
            except MarketError:
                pass                      # 下载任务恰好结束，忽略同锁竞争
        with self._lock:
            st = self.progress_locked()
            install = dict(st.get("install") or {})
            install["status"] = "cancelled"
            install["message"] = "已取消"
            install["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.manager.set_meta(install=install)
        return self.progress()

    def progress(self) -> dict:
        with self._lock:
            return self.progress_locked()

    def progress_locked(self) -> dict:
        """合并下载状态与安装段；线程僵死时纠正 install 为 interrupted。"""
        st = self.manager.progress()
        install = dict(st.get("install") or {})
        alive = self._thread is not None and self._thread.is_alive()
        if install.get("status") in ("queued", "downloading_pth", "downloading_index", "staging") and not alive:
            install["status"] = "interrupted"
            install["message"] = "服务重启/中断，可重新发起安装自动续传"
        st["install"] = install
        return st

    def installed_ids(self) -> list[str]:
        """已装音色 id 集合：RVC logs 下有 <id>.pth 或 assets/weights 有 <id>.pth。"""
        ids: set[str] = set()
        logs = cfg.RVC_ROOT / "logs"
        if logs.exists():
            for d in logs.iterdir():
                if d.is_dir() and (d / f"{d.name}.pth").exists():
                    ids.add(d.name)
        weights = cfg.RVC_ROOT / "assets" / "weights"
        if weights.exists():
            for p in weights.glob("*.pth"):
                ids.add(p.stem)
        return sorted(ids)

    # ---- 溯源与卸载 ----

    @staticmethod
    def _is_market_installed(voice_id: str) -> bool:
        """logs/<id>/source.json 标记了市场来源才算市场安装的音色。"""
        src = cfg.RVC_ROOT / "logs" / voice_id / "source.json"
        try:
            return json.loads(src.read_text(encoding="utf-8")).get("source") == "market"
        except Exception:
            return False

    def uninstall(self, voice_id: str) -> dict:
        """卸载市场安装的音色：清 logs/<id>/（含 source.json）、assets/weights/<id>.pth、
        outputs/market 下载缓存与质检/试听产物。

        仅允许卸载带市场来源标记（source.json）的音色，避免误删自训产物；
        安装/下载进行中拒绝执行。返回被删除的路径列表。
        """
        if not voice_id or not VOICE_ID_RE.match(voice_id):
            raise InstallError(f"非法音色 ID: {voice_id!r}（限字母数字_\\-）")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise InstallError("已有安装任务在进行中，请稍后再卸载")
            st = self.manager.progress()
            if st.get("status") == "downloading":
                raise InstallError("已有下载任务在进行中，请稍后再卸载")
        log_dir = cfg.RVC_ROOT / "logs" / voice_id
        w_pth = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
        if not log_dir.exists() and not w_pth.exists():
            raise InstallError(f"音色 {voice_id} 不存在或已卸载")
        if not self._is_market_installed(voice_id):
            raise InstallError(
                f"音色 {voice_id} 不是市场安装来源（无 source.json 标记），为避免误删自训产物，"
                "请到音色库中删除")
        paths: list = []
        if log_dir.exists():
            paths.append(log_dir)
        if w_pth.exists():
            paths.append(w_pth)
        # 下载缓存与试听 / 质检孤儿产物
        for cand in (
            self.manager.download_dir / f"{voice_id}.pth",
            self.manager.download_dir / f"{voice_id}.pth.part",
            self.manager.download_dir / f"{voice_id}.index",
            self.manager.download_dir / f"{voice_id}.index.part",
            cfg.OUTPUTS_DIR / "market" / f"{voice_id}_preview.wav",
            cfg.OUTPUTS_DIR / "qc" / f"{voice_id}.json",
        ):
            if cand.exists():
                paths.append(cand)
        if not paths:
            raise InstallError(f"音色 {voice_id} 不存在或已卸载")
        for p in paths:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        return {"voice_id": voice_id, "removed": [str(p) for p in paths]}

    def write_source(self, voice_id: str, manifest_id: str, display_name: str):
        """安装落位后写入溯源标记（logs/<id>/source.json），供卸载与音色库角标使用。"""
        src = cfg.RVC_ROOT / "logs" / voice_id / "source.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps({
            "source": "market",
            "manifest_id": manifest_id,
            "display_name": display_name,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 后台线程 ----
    def _run(self, voice_id: str, download: dict, index: dict | None,
             display_name: str, manifest_id: str = ""):
        try:
            self._set_install(status="downloading_pth", phase="下载权重",
                              message="正在下载权重文件 …", percent=_pct_of(0, INSTALL_PHASES))
            self._download_wait(
                voice_id, f"install_{voice_id}", download.get("url"),
                mirror_url=download.get("mirror_url"), filename=f"{voice_id}.pth",
            )
            pth_file = self.manager.download_dir / f"{voice_id}.pth"
            if not pth_file.exists():
                raise InstallError("权重下载未产生文件")

            if index and index.get("url"):
                self._set_install(status="downloading_index", phase="下载索引",
                                  message="正在下载索引文件 …", percent=_pct_of(1, INSTALL_PHASES))
                self._download_wait(
                    voice_id, f"install_{voice_id}_idx", index.get("url"),
                    mirror_url=index.get("mirror_url"), filename=f"{voice_id}.index",
                )

            self._set_install(status="staging", phase="注册音色",
                              message="正在写入音色库 …", percent=_pct_of(2, INSTALL_PHASES))
            self._stage(voice_id)
            self.write_source(voice_id, manifest_id, display_name)

            self._set_install(status="installed", phase="完成", message="安装完成",
                              percent=100.0, error="",
                              updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        except InstallError as exc:
            # 用户主动取消（cancelled 已由 cancel() 写入）时不要覆盖成 failed
            with self._lock:
                cur = (self.manager.progress().get("install") or {}).get("status")
            if cur != "cancelled":
                self._set_install(status="failed", message="安装失败", error=str(exc))
        except Exception as exc:  # noqa: BLE001 —— 未知异常统一 failed
            self._set_install(status="failed", message="安装失败",
                              error=f"{exc.__class__.__name__}: {exc}")

    def _set_install(self, **kw):
        with self._lock:
            st = dict(self.manager.progress())
            install = dict(st.get("install") or {})
            install.update({**kw, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            self.manager.set_meta(install=install)

    def _download_wait(self, voice_id: str, name: str, url: str,
                       mirror_url: str | None, filename: str):
        """用 DownloadManager 下载单个文件并等待落盘；期间检测取消/失败。"""
        st = self.manager.start(name=name, url=url, mirror_url=mirror_url, filename=filename)
        if st.get("status") != "downloading":
            return
        deadline = time.time() + 3600 * 2          # 2h 兜底（正常 55MB 分钟级）
        while time.time() < deadline:
            st = self.manager.progress()
            s, err = st.get("status"), st.get("error") or ""
            if s in ("done", "cancelled", "failed"):
                if s == "done":
                    return
                if s == "cancelled":
                    raise InstallError("下载已取消")
                raise InstallError(f"下载失败: {err}")
            if s == "interrupted":
                raise InstallError("下载中断（服务重启），可重新发起安装续传")
            time.sleep(0.2)
        raise InstallError("下载超时")

    def _stage(self, voice_id: str):
        """把 outputs/market 下载产物落位到 RVC 目录（幂等，重复拷贝覆盖）。"""
        src_pth = self.manager.download_dir / f"{voice_id}.pth"
        src_idx = self.manager.download_dir / f"{voice_id}.index"
        if not src_pth.exists():
            raise InstallError("权重文件缺失，安装中止")
        # 落位前再验一次文件头：杜绝坏文件/网页内容进入 RVC 音色库
        if not _torch_header_ok(src_pth):
            raise InstallError("权重文件头校验失败：不是有效的 PyTorch 存档")
        log_dir = cfg.RVC_ROOT / "logs" / voice_id
        weights_dir = cfg.RVC_ROOT / "assets" / "weights"
        log_dir.mkdir(parents=True, exist_ok=True)
        weights_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_pth, log_dir / f"{voice_id}.pth")
        shutil.copy2(src_pth, weights_dir / f"{voice_id}.pth")
        if src_idx.exists():
            shutil.copy2(src_idx, log_dir / f"added_{voice_id}.index")


def _pct_of(idx: int, phases: tuple) -> float:
    return round(idx / max(1, len(phases)) * 100.0, 1)


# 进程级单例
INSTALLER = InstallManager()


def get_installer() -> InstallManager:
    return INSTALLER

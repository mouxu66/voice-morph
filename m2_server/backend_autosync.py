"""桌面端启动自动同步 —— 源码 → resources/backend 兜底副本。

背景：桌面端（Electron）在本机运行时直接使用源码根（ROOT）的 m2_server 与
web/dist；voice-morph-desktop/resources/backend 下另有一份兜底副本，供"分发到
没有本仓库的机器"时回退（安装版后端与前端静态资源都从那里跑）。此前兜底副本
靠 tools/sync_backend.ps1 手动同步，改完代码忘记跑就会让副本悄悄过期。

本模块在每次后端启动（= 每次打开桌面端）时自动检测源码与副本的差异并镜像同步：

- 同步对：m2_server → m2_server、tools → tools、web/dist → web_dist
- 排除规则与 tools/sync_backend.ps1 保持一致（__pycache__/.pyc/.log/.bak/.tmp）
- 比对方式：相对路径 + 大小 + MD5（与 ps1 相同的 MD5 语义）
- 镜像语义：源里已删除的文件/空目录同步从副本中清掉，防止旧模块残留
- 方向永远 源码 → 副本；检测到"运行形态就是安装副本"（ROOT 即 resources/backend）
  或目标结构不存在时自动跳过，绝不反向覆盖、绝不递归自拷

失败绝不抛出、绝不阻塞启动：同步跑在 daemon 线程里，任何异常只记日志。
设置环境变量 VM_BACKEND_AUTOSYNC=0 可整体关闭。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 与 tools/sync_backend.ps1 的排除正则保持一致的目录/后缀
_EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}
_EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".bak", ".tmp")

# (源相对路径, 副本内目标名)——tools 同步进副本供安装版"运行环境体检"使用
_SYNC_PAIRS: list[tuple[str, str]] = [
    ("m2_server", "m2_server"),
    ("tools", "tools"),
    ("web/dist", "web_dist"),
]


def _excluded(name: str) -> bool:
    return name in _EXCLUDE_DIRS or name.endswith(_EXCLUDE_SUFFIX)


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path) -> dict[str, tuple[int, str]]:
    """收集目录下全部文件的 {相对路径: (大小, md5)}，跳过排除项。"""
    out: dict[str, tuple[int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _excluded(d)]
        for name in filenames:
            if _excluded(name):
                continue
            p = Path(dirpath) / name
            try:
                out[p.relative_to(root).as_posix()] = (p.stat().st_size, _md5(p))
            except OSError:
                continue  # 读不了的文件（被占用等）本轮跳过，下轮再补
    return out


def _prune_empty_dirs(dst: Path, src: Path) -> int:
    """镜像清理：删掉副本中"源里已不存在"的空目录，返回删除数。"""
    removed = 0
    # 自底向上：先处理深层
    dirs = sorted(
        (p for p in dst.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    )
    for d in dirs:
        rel = d.relative_to(dst)
        if (src / rel).is_dir():
            continue
        try:
            d.rmdir()  # 仅当空目录才成功——非空说明还有残留文件，留给下轮
            removed += 1
        except OSError:
            pass
    return removed


def sync_backend_copy(project_root: Path) -> dict:
    """执行一次 源码→兜底副本 的镜像同步。绝不抛异常。

    返回 {"status": "synced"|"skipped", "reason"?: str,
          "pairs"?: {名: {"copied": int, "deleted": int, "errors": int}}}
    """
    result: dict = {"status": "skipped", "reason": "", "pairs": {}}
    try:
        root = Path(project_root).resolve()
        target_root = root / "voice-morph-desktop" / "resources" / "backend"

        # 安装版运行形态：ROOT 本身就是 resources/backend → 没有独立源码可同步
        if root.name == "backend" and root.parent.name == "resources":
            result["reason"] = "安装版运行形态（源码即副本），跳过"
            return result
        # 非桌面端工程目录（没有 voice-morph-desktop/resources 结构）→ 无处可同步
        if not (root / "voice-morph-desktop" / "resources").is_dir():
            result["reason"] = "未找到 voice-morph-desktop/resources，跳过"
            return result
        # 目标与源重叠的病态情况：root 处于副本内部（如 root = <…>/backend/m2_server）。
        # 注意 root 是 target 的祖先属于开发机正常布局，不在拦截之列。
        if target_root == root or target_root in root.parents:
            result["reason"] = "源与目标路径重叠，跳过"
            return result

        result["status"] = "synced"
        for src_rel, dst_name in _SYNC_PAIRS:
            src = root / src_rel
            dst = target_root / dst_name
            if not src.is_dir():
                continue
            stat = {"copied": 0, "deleted": 0, "errors": 0}
            src_files = _walk(src)
            if not src_files:
                # 源目录为空（构建失败/被清空）时不执行删除镜像，防止清空可用分发副本；
                # 本轮只警告，等源恢复后再同步。
                logger.warning("[autosync] %s 源为空（疑似构建中断），跳过清理", src_rel)
                result["pairs"][dst_name] = stat
                continue
            dst_files = _walk(dst) if dst.is_dir() else {}
            for rel, meta in src_files.items():
                if dst_files.get(rel) == meta:
                    continue
                d = dst / rel
                try:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / rel, d)
                    stat["copied"] += 1
                except OSError as e:
                    stat["errors"] += 1
                    logger.warning("[autosync] 复制失败 %s: %s", rel, e)
            for rel in sorted(dst_files.keys() - src_files.keys()):
                f = dst / rel
                try:
                    if f.is_file():
                        f.unlink()
                        stat["deleted"] += 1
                except OSError as e:
                    stat["errors"] += 1
                    logger.warning("[autosync] 清理失败 %s: %s", rel, e)
            if stat["deleted"] or stat["copied"]:
                stat["pruned_dirs"] = _prune_empty_dirs(dst, src)
            if any(stat.values()):
                logger.info(
                    "[autosync] %s -> %s: 更新 %d, 清理 %d%s",
                    src_rel,
                    dst_name,
                    stat["copied"],
                    stat["deleted"],
                    f", 失败 {stat['errors']}" if stat["errors"] else "",
                )
            result["pairs"][dst_name] = stat
    except Exception:  # 同步失败绝不能影响后端启动
        result["status"] = "skipped"
        result["reason"] = "同步过程异常（详见日志）"
        logger.warning("[autosync] 同步异常（忽略）", exc_info=True)
    return result


def autostart_sync(project_root: Path) -> None:
    """后端启动时调用：后台线程执行自动同步，绝不阻塞、绝不抛出。"""
    if os.environ.get("VM_BACKEND_AUTOSYNC", "1").strip().lower() in {"0", "false", "off"}:
        logger.info("[autosync] 已通过 VM_BACKEND_AUTOSYNC=0 关闭")
        return
    threading.Thread(
        target=_run_safe, args=(project_root,), name="backend-autosync", daemon=True
    ).start()


def _run_safe(project_root: Path) -> None:
    try:
        sync_backend_copy(project_root)
    except Exception:  # 双保险：线程内也不许逃出异常
        logger.warning("[autosync] 后台同步线程异常（忽略）", exc_info=True)

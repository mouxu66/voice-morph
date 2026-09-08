"""RVC 相关公共工具：权重查找 / 实验快照 / 推理权重提取。

原先这些逻辑在 rvc_live.py / server.py / offline_vc.py 各写一份，
抽到此处统一，避免「找权重的规则改了、有的模块还在用旧规则」。
"""
import json
from datetime import datetime
from pathlib import Path

import config as cfg
import logging
import subprocess


def find_pth(exp: str, log_dir: Path) -> Path | None:
    """找到该实验可用的 RVC 最终权重：优先 <exp>.pth，否则回退 G_<...>.pth。

    RVC 训练脚本落盘文件名是 G_2333333.pth / D_2333333.pth（并非 <exp>.pth），
    而实时加载、前端 status 都按 <exp>.pth 判定，两者命名不一致会误报"未训练"。
    """
    p = log_dir / f"{exp}.pth"
    if p.exists():
        return p
    return next(log_dir.glob("G_*.pth"), None)


def find_index(exp: str) -> Path | None:
    """找该音色的特征检索库 logs/<exp>/added_*.index；没有则返回 None。

    没有 index 也能推理（index_rate 会被强制置 0），只是音色相似度略降。
    """
    try:
        log_dir, _ = cfg.rvc_exp_dirs(exp)
    except Exception:  # noqa: BLE001
        log_dir = cfg.RVC_ROOT / "logs" / exp
    return next(iter(sorted(log_dir.glob("added_*.index"))), None)


def source_meta(exp: str) -> dict:
    """市场安装元数据：logs/<exp>/source.json（市场安装落盘；自训/导入无此文件）。

    市场音色安装时写入 {"source": "market", "display_name": "卡通·懒羊羊", ...}，
    自训实验没有该文件 → 返回空 dict，调用方据此区分「市场下载」与「自己训练」。
    """
    try:
        log_dir, _ = cfg.rvc_exp_dirs(exp)
        raw = json.loads((log_dir / "source.json").read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def exp_source(exp: str) -> str:
    """来源标记：market（市场安装）/ ""（自训或本地导入）。"""
    return str(source_meta(exp).get("source") or "")


def exp_display_name(exp: str, fallback: str | None = None) -> str:
    """音色中文显示名，优先级：市场 source.json > 实验目录 meta.json > 目录名。

    市场音色安装时自带中文名；自训实验可在 logs/<exp>/meta.json 里写
    {"display_name": "袋鼠骑士 v2"} 自定义，没有就退回英文目录名。
    """
    name = source_meta(exp).get("display_name")
    if not name:
        try:
            log_dir, _ = cfg.rvc_exp_dirs(exp)
            meta = json.loads((log_dir / "meta.json").read_text("utf-8"))
            name = (meta or {}).get("display_name")
        except Exception:  # noqa: BLE001
            name = None
    return str(name) if name else (fallback or exp)


def exp_snapshot(exp: str) -> dict:
    """某实验（音色 ID）的训练产物快照：权重/索引/语料/训练时间。"""
    log_dir, dataset_dir = cfg.rvc_exp_dirs(exp)
    pth = find_pth(exp, log_dir)
    idx = next(log_dir.glob("added_*.index"), None) if log_dir.exists() else None
    mtime = pth.stat().st_mtime if pth is not None and pth.exists() else 0.0
    return {
        "pth_exists": pth is not None,
        "index_exists": idx is not None,
        "model_ready": pth is not None and idx is not None,
        "dataset_count": len(list(dataset_dir.glob("*.wav"))) if dataset_dir.exists() else 0,
        "trained_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "weights_dir": str(log_dir),
    }


def ensure_infer_pth(voice_id: str) -> Path | None:
    """确保拿到可推理的 RVC 权重（rtrvc.get_synthesizer 只认含 weight 键的推理格式）。

    优先 assets/weights/<id>.pth（推理格式，训练时自动提取）；缺失时从训练
    检查点 G_*.pth 用 train.process_ckpt.extract_small_model 提取（幂等缓存）。
    注意：logs/<id>/<id>.pth 若只是 G_*.pth 的拷贝（训练格式，键 model/optimizer…），
    实时加载会 KeyError('weight')，绝不能直接当推理权重用。提取成功后同步拷贝
    一份到 logs/<id>/<id>.pth，供实时变声的 status/start 判定使用。
    """
    import os
    import shutil
    import sys

    infer_pth = cfg.RVC_ROOT / "assets" / "weights" / f"{voice_id}.pth"
    if infer_pth.exists():
        return infer_pth
    log_dir = cfg.RVC_ROOT / "logs" / voice_id
    ckpt = next(iter(sorted(log_dir.glob("G_*.pth"))), None)
    if ckpt is None:
        return None
    try:
        sys.path.insert(0, str(cfg.RVC_ROOT))
        os.environ["PYTHONPATH"] = str(cfg.RVC_ROOT)
        os.environ["weight_root"] = str(cfg.RVC_ROOT / "assets" / "weights")
        from train.process_ckpt import extract_small_model

        (cfg.RVC_ROOT / "assets" / "weights").mkdir(parents=True, exist_ok=True)
        extract_small_model(str(ckpt), voice_id, "48k", 1, f"{voice_id} RVC v2 48k", "v2")
        if not infer_pth.exists():
            return None
        shutil.copy2(infer_pth, log_dir / f"{voice_id}.pth")
        return infer_pth
    except Exception:
        return None


def _find_pids_by_cmdline(pattern: str) -> list[int]:
    """按命令行正则匹配枚举 python.exe 进程 PID；失败返回 [] 并记日志。

    统一封装 PowerShell 进程枚举，rvc_live / cascade 共用，消除重复实现。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match '%s' } | "
             "Select-Object -ExpandProperty ProcessId" % pattern],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except Exception as e:
        logger.warning("枚举进程失败(pattern=%r): %s", pattern, e)
        return []


def _kill_pids(pids: list[int], label: str = "") -> None:
    """强杀进程列表（/T 连带子进程，/F 强制）；单个失败只记日志不中断。"""
    tag = f"[{label}] " if label else ""
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except Exception as e:
            logger.debug("%s停止进程 %s 失败（可忽略）: %s", tag, pid, e)

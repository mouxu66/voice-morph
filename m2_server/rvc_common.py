"""RVC 相关公共工具：权重查找 / 实验快照 / 推理权重提取。

原先这些逻辑在 rvc_live.py / server.py / offline_vc.py 各写一份，
抽到此处统一，避免「找权重的规则改了、有的模块还在用旧规则」。
"""
from datetime import datetime
from pathlib import Path

import config as cfg


def find_pth(exp: str, log_dir: Path) -> Path | None:
    """找到该实验可用的 RVC 最终权重：优先 <exp>.pth，否则回退 G_<...>.pth。

    RVC 训练脚本落盘文件名是 G_2333333.pth / D_2333333.pth（并非 <exp>.pth），
    而实时加载、前端 status 都按 <exp>.pth 判定，两者命名不一致会误报"未训练"。
    """
    p = log_dir / f"{exp}.pth"
    if p.exists():
        return p
    return next(log_dir.glob("G_*.pth"), None)


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

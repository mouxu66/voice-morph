"""磁盘占用统计与选择性清理（B2）。

背景：市场下载缓存、解析切片、历史产物、QC 缓存、日志各自膨胀，而此前只有
/health 与 /diagnose，没有任何统一视图——磁盘被悄悄吃满会直接导致训练/推理失败。

设计：
  - **统计与清理共用同一套目标定义**，避免"看到的"和"删掉的"不是一回事。
  - 每个目标显式声明 `cleanable`。**voicebank（音色档案）与 RVC logs（训练产物）
    永远只读**，不出现在可清理列表里——这两个是不可重建的用户资产。
  - `rvc_weights` 也只读：它是 logs/<id>/<id>.pth 的硬链接副本（见 market_install
    的 _link_or_copy），删了会让实时变声失效，而且因为共享数据块并不真的省空间。
  - 清理只删文件、不删目录结构（目录本身几 KB，留着不影响）。
"""

import shutil
import time
from pathlib import Path

import config as cfg

# 单个目标扫描的文件数上限（防止极端情况下卡死统计）
_SCAN_CAP = 20000


def _size_of(paths) -> tuple[int, int]:
    total, count = 0, 0
    for p in paths:
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


def _glob_files(base: Path, patterns: list[str]) -> list[Path]:
    if not base.exists():
        return []
    out: list[Path] = []
    for pat in patterns:
        for p in base.glob(pat):
            if p.is_file():
                out.append(p)
            if len(out) > _SCAN_CAP:
                return out
    return out


def _tree_files(base: Path) -> list[Path]:
    if not base.exists():
        return []
    out: list[Path] = []
    for p in base.rglob("*"):
        if p.is_file():
            out.append(p)
        if len(out) > _SCAN_CAP:
            break
    return out


def _market_dir() -> Path:
    return cfg.OUTPUTS_DIR / "market"


# ---------------- 目标定义 ----------------

# key -> (标签, 说明, 取文件函数, 是否可清理)
_TARGETS: dict[str, tuple[str, str, object, bool]] = {
    "market_downloads": (
        "市场下载缓存",
        "安装音色时下载的 .pth / .index 与未完成的 .part；卸载时会自动带走，残留的可安全清。",
        lambda: _glob_files(_market_dir(), ["*.pth", "*.index", "*.part"]),
        True,
    ),
    "market_previews": (
        "市场自动试听",
        "装完自动合成的演示音频；删掉后市场卡片会回到「未试听」，可随时重生成。",
        lambda: [
            p
            for p in _glob_files(_market_dir(), ["*_preview.wav", "*_preview.json"])
            if p.name != "downloads.json"
        ],
        True,
    ),
    "outputs_wav": (
        "历史产物音频",
        "outputs 根目录下的 wav（TTS / 离线变声 / Seed-VC / 有声书产物）。会同步清掉对应历史记录。",
        lambda: _glob_files(cfg.OUTPUTS_DIR, ["*.wav"]),
        True,
    ),
    "qc_cache": (
        "质检缓存",
        "音色/切片质检的中间音频与报告；删掉只是下次质检要重算。",
        lambda: _tree_files(cfg.OUTPUTS_DIR / "qc"),
        True,
    ),
    "clips": (
        "解析切片",
        "视频解析出来的音频切片；已用于训练的语料另有副本，这里清掉不影响已有音色。",
        lambda: _tree_files(cfg.MEDIA_DIR / "clips"),
        True,
    ),
    "ft_corpus": (
        "微调语料",
        "音色微调生成的切片与转写；清掉后需要重新解析素材才能再训练。",
        lambda: _tree_files(cfg.MEDIA_DIR / "ft"),
        True,
    ),
    "logs": (
        "运行日志",
        "后端与各类任务的 .log 文件，纯排查用。",
        lambda: _glob_files(cfg.OUTPUTS_DIR, ["*.log"])
        + _glob_files(cfg.ROOT / "m2_server", ["*.log"]),
        True,
    ),
    "rvc_weights": (
        "RVC 实时权重（只读）",
        "assets/weights 下的模型副本，与日志目录硬链接共享数据块——看着占空间，实际不额外花钱，"
        "删了反而会让实时变声失效。",
        lambda: _glob_files(cfg.RVC_ROOT / "assets" / "weights", ["*.pth"]),
        False,
    ),
    "voicebank": (
        "音色档案（只读）",
        "参考音色与其音频，属于不可重建的用户资产，不提供清理。",
        lambda: _tree_files(cfg.MEDIA_DIR / "voicebank"),
        False,
    ),
}

_ORDER = [
    "market_downloads",
    "market_previews",
    "outputs_wav",
    "qc_cache",
    "clips",
    "ft_corpus",
    "logs",
    "rvc_weights",
    "voicebank",
]


def scan(target_keys: list[str] | None = None) -> list[dict]:
    """统计各目标占用；target_keys 为空则统计全部。"""
    keys = [k for k in (target_keys or _ORDER) if k in _TARGETS]
    out = []
    for k in keys:
        label, desc, getter, cleanable = _TARGETS[k]
        files = getter()  # type: ignore[operator]
        total, count = _size_of(files)
        out.append(
            {
                "key": k,
                "label": label,
                "desc": desc,
                "cleanable": cleanable,
                "bytes": total,
                "files": count,
            }
        )
    return out


def disk_usage() -> list[dict]:
    """各挂载点（去重）的剩余空间：outputs / media / RVC 所在盘。"""
    seen: dict[str, dict] = {}
    for label, path in (
        ("产物目录", cfg.OUTPUTS_DIR),
        ("素材目录", cfg.MEDIA_DIR),
        ("RVC 整合包", cfg.RVC_ROOT),
    ):
        try:
            drive = Path(path.resolve().anchor) or str(path)
            total, used, free = shutil.disk_usage(str(path))
        except OSError:
            continue
        if drive in seen:
            continue
        seen[drive] = {
            "path": str(path),
            "drive": drive,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "used_percent": round(used / total * 100, 1) if total else 0.0,
            "label": label,
        }
    return list(seen.values())


def clean(target_keys: list[str]) -> dict:
    """清理指定目标：只删文件，跳过不可清理项，单文件失败不中断。

    返回 {freed_bytes, removed_files, skipped:[{key,reason}], errors:[...]}。
    """
    freed, removed = 0, 0
    skipped: list[dict] = []
    errors: list[dict] = []
    for k in target_keys or []:
        t = _TARGETS.get(k)
        if not t:
            skipped.append({"key": k, "reason": "未知目标"})
            continue
        _label, _desc, getter, cleanable = t
        if not cleanable:
            skipped.append({"key": k, "reason": "该项受保护，不提供清理"})
            continue
        for p in getter():  # type: ignore[operator]
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
                removed += 1
            except OSError as e:
                errors.append({"file": str(p), "error": str(e)})

    # outputs 顶层 wav 清掉后，历史里指向不存在文件的记录一并摘掉，避免留空链接
    if "outputs_wav" in (target_keys or []):
        try:
            import history

            gone = [
                r["id"]
                for r in history._read_all()
                if r.get("wav") and not (cfg.OUTPUTS_DIR / Path(r["wav"]).name).exists()
            ]
            if gone:
                history.bulk_delete(gone, keep_file=True)
        except Exception as e:  # noqa: BLE001 —— 历史清理失败不影响已释放的空间
            errors.append({"file": "history.jsonl", "error": str(e)})

    return {
        "ok": not errors,
        "freed_bytes": freed,
        "removed_files": removed,
        "skipped": skipped,
        "errors": errors[:20],
        "cleaned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

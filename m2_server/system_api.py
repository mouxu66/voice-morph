"""系统级接口：/health 健康检查 + /capabilities 能力加载清单 + /diagnose 环境体检
+ /system/storage 占用看板（B2）。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""

import config as cfg
import plugin_loader
import plugin_manifest
import storage
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from rvc_common import find_pth

router = APIRouter(prefix="/api")


def _try_import_torch():
    """拿 torch 模块；**没装就返回 None**。

    为什么要这层包装（2026-09-20，插件化第 5 步）：torch 是**可选能力**的重依赖
    （`sound.offline-vc` / `sound.workshop` 的 demucs / 打分器），**不是核心依赖** ——
    实测核心插件里没有任何模块在模块级 import 它（模块级的只有 `qwen3_tts_service.py`，
    它跑在独立 `venv312` 子进程里；当年同类的 `fast_tts.py` 已无人引用，2026-09-22 删除）。
    所以「轻量预设」（不装 torch，省 ~2GB）下，体检类端点必须照常 200，
    把「缺 torch」当成**一条可展示的结论**，而不是让它自己 500 ——
    那等于在最需要看「哪里缺」的时候把面板关掉。
    """
    try:
        import torch
    except ImportError:
        return None
    return torch


@router.get("/health")
def health():
    """服务健康检查。

    `cuda` 为 **`null`** 表示「本环境没装 torch」（可选依赖缺失），**不是**「没有 GPU」——
    消费方别把它当故障（见前端 `HealthInfo.cuda` 的注释）。
    """
    torch = _try_import_torch()
    if torch is None:
        return {"status": "ok", "cuda": None}
    return {"status": "ok", "cuda": torch.cuda.is_available()}


@router.get("/capabilities")
def capabilities():
    """路由模块的加载清单：哪些能力真的挂上了、哪些没挂上及原因。

    为什么单独开一个端点而不塞进 `/health`：
    · 语义不同：`/health` 答「服务在不在」，这里答「服务里有哪些能力」。
    · `/health` 要做 GPU 探测（torch 是**可选**依赖），本端点**不依赖任何重库** ——
      在「什么都没装」的环境里它仍然答得出来，而那正是最需要它的时刻。
      （2026-09-20 起 `/health` 自己也能在缺 torch 时降级、不再 500；
      但这条理由仍然成立：**能力清单不该挂在探测类端点上**。）

    消费方：前端顶部降级提示（`SetupBanner`）—— 让「哪个能力不可用」
    从 `backend.log` 走到界面上，而不是只对翻日志的人可见。
    （`GET /api/plugins` 是带 manifest 的**能力**目录，本端点保留作为
    “只是加载状态”的稳定子集 —— 两者由 `test_plugins_endpoint_shape_and_consistency`
    对账，数字对不上时能一眼看出问题出在哪一层。）
    """
    res = plugin_loader.results()
    routers = [r for r in res if r.purpose == plugin_loader.ROUTER_PURPOSE]
    return {
        "ok": all(r.ok for r in res),
        "loaded": sum(1 for r in routers if r.ok),
        "total": len(routers),
        # 按模块名排序，方便前端直接展示与对账
        "broken": [
            {"module": r.module, "purpose": r.purpose, "reason": r.reason}
            for r in sorted((r for r in res if not r.ok), key=lambda r: (r.module, r.purpose))
        ],
    }


@router.get("/plugins")
def plugins():
    """插件目录：每个能力的声明（manifest）+ 三态状态 + 依赖关系。

    第 2 步建清单时**只读**、挂载方式一点没动；第 6 步起端点仍是只读的
    （写走 `POST /api/plugins/{id}/enable|disable` 与 `/api/plugins/preset`），
    但清单里的 `enabled` / `blockedBy` 已经反映开关结果，`presets` 给出套餐定义。

    ⚠️ 开关是**重启生效**的（router 在启动时挂好，热插拔本轮不做）：
    响应里的 `restartRequired` 就是给前端转述用的，别让用户在界面上白点。

    与 `/capabilities` 的分工：
    · `/capabilities` 答「**模块**加载情况」（26 个 router 逐个成功/失败），是加载器的原始账本；
    · 本端点答「**能力**视角」：哪些能力可用、哪些被用户关掉了、每个能力要装什么、
      缺了会怎样。它把 registry 聚合成人看得懂的清单，是前端侧边栏/设置页的输入。

    同样**不 import 任何重库**（同 `/capabilities` 的理由）：
    状态端点不能依赖它要报告的那个东西。

    三态 `ok / broken / disabled` 见 `plugin_manifest.py` 的模块注释 ——
    关键是 `disabled`（用户主动关的）**不计入 broken**，否则关一个插件
    就会弹一条「N 个能力未加载」的降级横幅。
    """
    catalog = plugin_manifest.catalog()
    res = plugin_loader.results()
    routers = [r for r in res if r.purpose == plugin_loader.ROUTER_PURPOSE]
    # 与 /capabilities 对齐的分母：manifest 的「不坏」不能与加载器的账本相矛盾，
    # 所以把两边都报出来，前端/体检对不上时能一眼看出是在哪一层出的问题。
    catalog["loaders"] = {
        "routers": len(routers),
        "loaded": sum(1 for r in routers if r.ok),
        "broken": [r.label for r in res if not r.ok],
    }
    return catalog


class _PresetBody(BaseModel):
    preset: str = Field(..., description=f"预设名，可选：{sorted(plugin_manifest.PRESETS)}")


@router.post("/plugins/preset")
def apply_plugin_preset(body: _PresetBody):
    """套用一个套餐预设（`docs/插件化设计.md` §8.1）。

    **重启生效**：router 是启动时挂好的，改状态文件不会立刻卸载路由
    （热插拔见设计稿 §9，本轮不做）。响应里带 `restartRequired` 就是为了让
    前端能如实说出来 —— 否则用户点了开关却看不到变化，会以为开关坏了。
    """
    try:
        rep = plugin_manifest.apply_preset(body.preset)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"没有这个预设：{body.preset!r}") from exc
    return {**rep, "restartRequired": True}


def _toggle(pid: str, on: bool):
    """`enable` / `disable` 的公共实现。

    404：没有这个能力（前端点了个不存在的 id，多半是前后端版本不一致）。
    400：核心能力不可关 —— 关掉它整个界面就没有意义了，这不是"不允许"，
        是"这个问题不该由开关来回答"，所以不给 409（409 暗示"换个时机再来"）。
    409：关闭守卫 —— 仍有启用中的能力依赖它。detail 里**点名依赖者**，
        用户得知道"想关 A，先关 B"，而不是只看到一个"操作被拒绝"。
    """
    try:
        rep = plugin_manifest.set_enabled(pid, on)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"没有这个能力：{pid!r}") from exc
    if rep["reason"] == "core":
        raise HTTPException(status_code=400, detail=f"{pid} 是核心能力，不能关闭")
    if rep["blocked_by"]:
        raise HTTPException(
            status_code=409,
            detail=f"{pid} 仍被 {('、'.join(rep['blocked_by']))} 依赖；先关掉它们",
        )
    return {**rep, "restartRequired": True}


@router.post("/plugins/{pid}/enable")
def enable_plugin(pid: str):
    """启用一个能力（重启生效）。"""
    return _toggle(pid, True)


@router.post("/plugins/{pid}/disable")
def disable_plugin(pid: str):
    """关闭一个能力（重启生效）；若仍被别的能力依赖 → 409 并点名依赖者。"""
    return _toggle(pid, False)


# 体检项 → 需要它的能力 id。这些能力**全部关掉**时，该项不再报红
# （设计稿 §八「可关」的第 3 条收益）。
#
# ⚠️ 为什么手工维护而不是从清单推导：体检项的 key（`tts_models`）与清单的
# `extras` 之间没有机器可读的对应关系（一个体检项可能横跨几个 env / 几个包）。
# 手工表会漂，所以由 `tests/test_plugin_switch.py::test_diagnose_owners_*`
# 钉住：每个 id 必须在清单里存在，且表里不许出现清单里没有的 key。
_DIAGNOSE_OWNERS: dict[str, tuple[str, ...]] = {
    "tts_models": ("sound.tts",),
    "rvc_root": ("sound.rvc-live", "sound.offline-vc"),
    "rvc_weights": ("sound.rvc-live",),
    "torch": ("sound.offline-vc", "sound.audition", "sound.workshop"),
    "cuda": ("sound.offline-vc", "sound.audition", "sound.workshop"),
}


@router.get("/diagnose")
def diagnose():
    """环境体检：并行检查本机推理所需的各项依赖，返回勾叉清单。

    前端据此展示「哪里缺」，每项带 detail（现状）与 hint（怎么修）。
    后端能响应本接口本身就说明「本地推理服务」已在线（故 backend 项恒 ok）。
    """
    import shutil

    torch = _try_import_torch()

    items: list[dict] = []

    # 1) 本地推理服务（能响应 /diagnose 说明本身已在线）
    items.append(
        {
            "key": "backend",
            "ok": True,
            "label": "本地推理服务",
            "detail": f"已连接 · 端口 {cfg.SERVER_PORT}",
            "hint": "",
        }
    )

    # 2) ffmpeg（音频预处理/导出依赖）
    ff = shutil.which("ffmpeg")
    if ff:
        items.append({"key": "ffmpeg", "ok": True, "label": "ffmpeg", "detail": ff, "hint": ""})
    else:
        items.append(
            {
                "key": "ffmpeg",
                "ok": False,
                "label": "ffmpeg",
                "detail": "未在 PATH 中找到 ffmpeg",
                "hint": "安装 ffmpeg 并加入 PATH；Windows 可用 `winget install ffmpeg` 或 `scoop install ffmpeg`。",
            }
        )

    # 3) Qwen3-TTS 模型 + 分词器
    qwen_ok = cfg.QWEN_MODEL_DIR.exists() and (cfg.QWEN_MODEL_DIR / "config.json").exists()
    tok_ok = cfg.QWEN_TOKENIZER_DIR.exists()
    if qwen_ok and tok_ok:
        items.append(
            {
                "key": "tts_models",
                "ok": True,
                "label": "Qwen3-TTS 模型/分词器",
                "detail": str(cfg.QWEN_MODEL_DIR),
                "hint": "",
            }
        )
    else:
        miss = []
        if not qwen_ok:
            miss.append("模型目录缺失或没有 config.json")
        if not tok_ok:
            miss.append("分词器目录缺失")
        items.append(
            {
                "key": "tts_models",
                "ok": False,
                "label": "Qwen3-TTS 模型/分词器",
                "detail": "；".join(miss),
                "hint": f"确认 VM_QWEN_MODEL_DIR（{cfg.QWEN_MODEL_DIR}）与 VM_QWEN_TOKENIZER_DIR（{cfg.QWEN_TOKENIZER_DIR}）已下载解压到位。",
            }
        )

    # 4) RVC 整合包根目录（实时变声依赖）
    if cfg.RVC_ROOT.exists():
        looks = (
            (cfg.RVC_ROOT / "rvc").exists()
            or (cfg.RVC_ROOT / "infer").exists()
            or (cfg.RVC_ROOT / "logs").exists()
            or (cfg.RVC_ROOT / "tools").exists()
        )
        items.append(
            {
                "key": "rvc_root",
                "ok": True,
                "label": "RVC 整合包",
                "detail": str(cfg.RVC_ROOT)
                + ("" if looks else "（未识别到 rvc/logs 等典型子目录，请确认路径正确）"),
                "hint": "" if looks else "该目录缺少 RVC 典型结构，实时变声可能无法工作。",
            }
        )
    else:
        items.append(
            {
                "key": "rvc_root",
                "ok": False,
                "label": "RVC 整合包",
                "detail": f"目录不存在：{cfg.RVC_ROOT}",
                "hint": "设置环境变量 VM_RVC_ROOT 指向 RVC 整合包根目录（含 rvc/infer/tools 等）。实时变声依赖它。",
            }
        )

    # 5) 默认音色 RVC 权重（pth + index）
    # 未配置默认音色时**不能**拿空实验名去 rvc_exp_dirs()：那会拼出 RVC_ROOT/logs，
    # 反而可能扫到别人的 .pth 报“就绪”（2026-09-13 起默认音色为空，故补这道门）
    # 注意：这里**不能**用早返回 —— 下面还有第 6 项（GPU/CUDA）、以及末尾的汇总，
    # 早返回会静默丢掉那几行（本改动第一版就是这毛病，靠读函数尾才发现）。
    if not cfg.RVC_DEFAULT_EXP:
        items.append(
            {
                "key": "rvc_weights",
                "ok": False,
                "label": "RVC 权重（未指定音色）",
                "detail": "尚未指定默认音色（VM_RVC_EXP 为空）",
                "hint": "先在「音色库」创建音色并训练，或设置 VM_RVC_EXP 指定默认音色。无权重时实时变声不可用，但 TTS/离线变声仍可用。",
            }
        )
    else:
        weights_dir = cfg.rvc_exp_dirs(cfg.RVC_DEFAULT_EXP)[0]
        pth = find_pth(cfg.RVC_DEFAULT_EXP, weights_dir)
        idx = next(weights_dir.glob("added_*.index"), None) if weights_dir.exists() else None
        if pth and idx:
            items.append(
                {
                    "key": "rvc_weights",
                    "ok": True,
                    "label": f"RVC 权重（{cfg.RVC_DEFAULT_EXP}）",
                    "detail": str(pth),
                    "hint": "",
                }
            )
        else:
            items.append(
                {
                    "key": "rvc_weights",
                    "ok": False,
                    "label": f"RVC 权重（{cfg.RVC_DEFAULT_EXP}）",
                    "detail": f"未找到训练好的 .pth 或 .index（{weights_dir}）",
                    "hint": "该音色还没训练 RVC 模型：先在「音色微调」生成语料并训练，或在 RVC 整合包里完成训练。无权重时实时变声不可用，但 TTS/离线变声仍可用。",
                }
            )

    # 6) GPU / CUDA（仅告警，不阻断 CPU 推理）
    #    缺 torch 要单独报一项，**不能**让它把整个体检端点打 500 ——
    #    这个端点的职责就是回答「哪里缺」，缺得最多的时候它最该活着。
    if torch is None:
        cuda = False
        items.append(
            {
                "key": "torch",
                "ok": False,
                "label": "PyTorch",
                "detail": "未安装（可选依赖）",
                "hint": "本地推理与训练需要它。装 GPU 版："
                "`python -m pip install torch torchaudio --index-url "
                "https://download.pytorch.org/whl/cu128`；"
                "只做换音色/试音可不装（属「轻量预设」）。",
            }
        )
    else:
        cuda = torch.cuda.is_available()
        if cuda:
            try:
                dev = torch.cuda.get_device_name(0)
            except Exception:
                dev = "未知 GPU"
            items.append(
                {"key": "cuda", "ok": True, "label": "GPU / CUDA", "detail": dev, "hint": ""}
            )
        else:
            items.append(
                {
                    "key": "cuda",
                    "ok": False,
                    "warn": True,
                    "label": "GPU / CUDA",
                    "detail": "未检测到可用 GPU，将退回 CPU 推理（非常慢）",
                    "hint": "确认已安装对应 CUDA 版本的 PyTorch 且显卡驱动正常；可运行 `nvidia-smi` 验证。",
                }
            )

    # ---- 关掉的能力不再体检 ----
    # 设计稿 §八把这条列为「可关」的收益之一：现在是"没装 RVC 就一片红"，
    # 噪声掩盖真问题。所以**相关能力全部关掉时**，这项直接不报。
    # 不报 ≠ 假装通过：跳过的原因原样列进 `skipped`，用户想看还能看到。
    on = plugin_manifest.enabled_ids()
    kept: list[dict] = []
    skipped: list[dict] = []
    for it in items:
        owners = _DIAGNOSE_OWNERS.get(it["key"])
        if owners and not any(o in on for o in owners):
            skipped.append(
                {
                    "key": it["key"],
                    "label": it["label"],
                    "reason": "相关能力已关闭：" + "、".join(owners),
                }
            )
            continue
        kept.append(it)

    return {
        "all_ok": all(i["ok"] for i in kept),
        "cuda": cuda,
        "items": kept,
        "skipped": skipped,
    }


# ---------------- 存储占用看板（B2） ----------------


class StorageCleanRequest(BaseModel):
    targets: list[str] = Field(..., description="要清理的目标 key（见 /system/storage）")


@router.get("/system/storage")
def system_storage():
    """各目录占用统计 + 所在盘剩余空间；供设置面板做选择性清理。

    cleanable=false 的目标（voicebank / RVC 实时权重）只展示，不接受清理。
    """
    return {"disks": storage.disk_usage(), "items": storage.scan()}


@router.get("/system/warmup")
def system_warmup():
    """模型预热状态：TTS worker 与 RVC 常驻模型是否已就绪。

    前端可据此提示"首次合成较慢"/"已就绪"。未就绪不影响使用，只是第一下慢。
    """
    from warmup import status

    return status()


@router.post("/system/storage/clean")
def system_storage_clean(req: StorageCleanRequest):
    """清理指定目标：只删文件，受保护项跳过并返回原因；单文件失败不中断。"""
    if not req.targets:
        raise HTTPException(status_code=400, detail="targets 不能为空")
    return storage.clean(req.targets)

"""系统级接口：/health 健康检查 + /capabilities 能力加载清单 + /diagnose 环境体检
+ /system/storage 占用看板（B2）。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""

import config as cfg
import plugin_loader
import storage
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from rvc_common import find_pth

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    import torch

    return {"status": "ok", "cuda": torch.cuda.is_available()}


@router.get("/capabilities")
def capabilities():
    """路由模块的加载清单：哪些能力真的挂上了、哪些没挂上及原因。

    为什么单独开一个端点而不塞进 `/health`：
    · `/health` 第一行就是 `import torch`，没装 torch 时它本身就 500 ——
      而「某个能力没装依赖」恰恰是没 torch 的环境里最需要看到的信息，
      把能力清单挂在它上面等于在最需要的时候消失。本端点**不依赖任何重库**。
    · 语义也不同：`/health` 答「服务在不在」，这里答「服务里有哪些能力」。

    消费方：前端顶部降级提示（`SetupBanner`）—— 让「哪个能力不可用」
    从 `backend.log` 走到界面上，而不是只对翻日志的人可见。
    （`docs/插件化设计.md` 第 2 步的 `GET /api/plugins` 会在这个基础上扩成
    带 manifest 的目录，本端点保留作为“只是加载状态”的稳定子集。）
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


@router.get("/diagnose")
def diagnose():
    """环境体检：并行检查本机推理所需的各项依赖，返回勾叉清单。

    前端据此展示「哪里缺」，每项带 detail（现状）与 hint（怎么修）。
    后端能响应本接口本身就说明「本地推理服务」已在线（故 backend 项恒 ok）。
    """
    import shutil

    import torch

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
    cuda = torch.cuda.is_available()
    if cuda:
        try:
            dev = torch.cuda.get_device_name(0)
        except Exception:
            dev = "未知 GPU"
        items.append({"key": "cuda", "ok": True, "label": "GPU / CUDA", "detail": dev, "hint": ""})
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

    return {"all_ok": all(i["ok"] for i in items), "cuda": cuda, "items": items}


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

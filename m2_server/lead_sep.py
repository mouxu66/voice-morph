# -*- coding: utf-8 -*-
"""主唱分离（卡拉OK主唱/伴唱分离）的产品后端。

为什么需要它（2026-10-15，《唯一》实测）：
    demucs 的 `--two-stems vocals` 只能把**所有人声**当一轨抠出来；很多歌的人声
    是"主唱 + 和声/伴唱"叠唱，RVC 是**单基频**模型，RMVPE 在两个音高重叠的帧上
    会在两者间乱跳 → 换声在高音/齐唱处出电音。实测《唯一》185–210s 有三个声纹簇
    几乎全程重叠；先把**主唱单独**抠出来再换声，输出全部落回模型音域、电音消失。

### 环境隔离（重要）
    分离用的 `audio-separator` 依赖较新 torch，装进主 venv 会把在役的
    torchaudio+cu128 顶坏（实测）。所以它跑在**独立 venv**（`<RVC_ROOT>/.venv_sep`）里，
    本模块只通过**子进程**调 `tools/lead_sep_runner.py`，与主环境零耦合 —— 和
    RVC 推理跑在 `D:/RVC/.venv` 是同一个隔离思路。

### 能力是"可选"的
    默认关闭；缺 venv / 缺模型时不崩，由 `status()` 给出"怎么补"的可操作信息，
    翻唱 worker 仅在用户显式开启且环境就绪时才走这一步。
"""
from __future__ import annotations

import enum
import subprocess
from pathlib import Path

import config as cfg

#: 独立 venv 与模型目录都跟随 RVC_ROOT（桌面端经 VM_RVC_ROOT 注入，与 RVC 同源部署）。
SEP_VENV_PY = Path(".venv_sep/Scripts/python.exe")
SEP_MODEL_DIR = Path("sep_models")

#: 默认主唱模型（2026-10-15 blind3 盲听定稿）：
#:   BS Roformer karaoke（frazer+becruily，204MB）—— MVSEP karaoke 榜公开权重里最高档
#:   （10.10 lead SDR）。本机同段实测：男声残留区（80–250Hz）−23.5dB（旧 UVR MDX 系
#:   仅 −1.7），女声主体（300–3.5k）−0.2dB（几乎无损）；用户盲听判"分出来了，确实可以"。
#: ★ 布局要求：ckpt 同目录须放 `config_bs_roformer_karaoke_frazer_becruily.yaml`
#:   （audio-separator 按内置名单映射取这个名，**别改名** —— 见犯错档案-工程 §8.73）。
#: ★ 备选：`mel_band_roformer_karaoke_gabox_v2.ckpt`（用户也判可；913MB。**必须 v2**：
#:   v1 分发是坏的，输出近全通且加载零报错，同见 §8.73）。
DEFAULT_MODEL = "bs_roformer_karaoke_frazer_becruily.ckpt"

#: runner 退出码 → 错误分类。见 tools/lead_sep_runner.py。
RC_ENV = 2
RC_MODEL = 3
RC_SEP = 4


class LeadSepError(RuntimeError):
    """主唱分离失败，``kind`` 是可据以给用户不同提示的机器可读分类。"""

    class Kind(str, enum.Enum):
        ENV_MISSING = "env_missing"      # 独立 venv / runner 不存在
        MODEL_MISSING = "model_missing"  # 模型权重缺失
        FAILED = "failed"                # 跑了但失败（含解码器/输入问题）

    def __init__(self, kind: "LeadSepError.Kind", message: str):
        super().__init__(message)
        self.kind = kind


def venv_python() -> Path:
    return cfg.RVC_ROOT / SEP_VENV_PY


def model_dir() -> Path:
    return cfg.RVC_ROOT / SEP_MODEL_DIR


def runner_path() -> Path:
    # tools/ 与 m2_server/ 同级，都在仓库根下。用 cfg 之外的 ROOT 更稳：
    from runtime import ROOT

    return ROOT / "tools" / "lead_sep_runner.py"


def status(model: str = DEFAULT_MODEL) -> dict:
    """供前端展示"主唱分离能不能用、缺什么"。不抛异常，永远返回 dict。"""
    py = venv_python()
    model_file = model_dir() / model
    runner = runner_path()
    ready = py.exists() and model_file.exists() and runner.exists()
    missing: list[str] = []
    if not py.exists():
        missing.append("分离运行环境（.venv_sep）")
    if not model_file.exists():
        missing.append(f"主唱分离模型（{model}）")
    if not runner.exists():
        missing.append("分离脚本 lead_sep_runner.py")
    return {
        "available": ready,
        "model": model,
        "venv_python": str(py),
        "model_exists": model_file.exists(),
        "missing": missing,
    }


def require_ready(model: str = DEFAULT_MODEL) -> None:
    """开工前断言环境就绪，缺什么抛带分类的 LeadSepError（便于 API 映射成提示）。"""
    st = status(model)
    if st["available"]:
        return
    if not venv_python().exists():
        raise LeadSepError(
            LeadSepError.Kind.ENV_MISSING,
            "主唱分离运行环境未安装（独立环境 .venv_sep 缺失）",
        )
    if not (model_dir() / model).exists():
        raise LeadSepError(
            LeadSepError.Kind.MODEL_MISSING,
            f"主唱分离模型缺失（{model}），请先下载该模型",
        )
    raise LeadSepError(LeadSepError.Kind.ENV_MISSING, "主唱分离脚本缺失：" + "、".join(st["missing"]))


def classify_returncode(rc: int, stderr: str) -> LeadSepError:
    """把 runner 的退出码映射成分类异常（纯函数，便于单测穷举各码）。"""
    tail = (stderr or "").strip().splitlines()[-1:] or [""]
    tail = tail[0][-300:]
    if rc == RC_MODEL:
        return LeadSepError(LeadSepError.Kind.MODEL_MISSING, f"主唱分离模型缺失：{tail}")
    if rc == RC_ENV:
        return LeadSepError(LeadSepError.Kind.ENV_MISSING, f"主唱分离环境异常：{tail}")
    if (rc & 0xFFFFFFFF) >= 0xC0000000:
        # Windows 异常退出码（0xC0000005 访问违例 = 3221225477、0xC00000FD 栈溢出…）：
        # 进程被系统直接终止，stderr 常为空。2026-10-15 实测（RTX 5060 8G 整曲链路）：
        # 同一输入一次崩一次成；崩时系统进程已占 5.5G+ 显存、而分离峰值要 7.3G ——
        # 显存挤爆（native crash）是最常见诱因。给一句可行动的提示，别丢十六进制码给用户。
        return LeadSepError(
            LeadSepError.Kind.FAILED,
            f"分离进程异常退出（系统错误码 {rc}），最常见的原因是显存不足"
            f"（可关闭其他占用显卡的程序后重试，或取消勾选「先剥离和声/伴唱」继续）",
        )
    return LeadSepError(LeadSepError.Kind.FAILED, f"主唱分离失败（退出码 {rc}）：{tail}")


def extract_lead(vocals: Path, out_dir: Path, model: str = DEFAULT_MODEL,
                 timeout_s: int = 3600) -> Path:
    """把混声音轨（demucs 的 vocals.wav）抠成**主唱**，返回 lead.wav。

    同步子进程（调用方已在翻唱后台线程里）。缺环境/模型/失败分别抛
    LeadSepError 对应分类；成功时确保 lead.wav 真实存在。

    ★ 入参先按**调用方 cwd** 归一成绝对路径：runner 子进程的 cwd 是 RVC_ROOT
    （见下），相对路径会被 runner 解析到那儿去 → 报"输入不存在"（2026-10-15
    端到端自测踩到；产品链路本身就是绝对路径，这里是给未来调用者的防护）。
    """
    require_ready(model)
    vocals = Path(vocals).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(venv_python()),
        str(runner_path()),
        str(model_dir()),
        model,
        str(vocals),
        str(out_dir),
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            cwd=str(cfg.RVC_ROOT),
        )
    except subprocess.TimeoutExpired:
        raise LeadSepError(LeadSepError.Kind.FAILED, f"主唱分离超时（>{timeout_s}s）")
    except FileNotFoundError as e:
        raise LeadSepError(LeadSepError.Kind.ENV_MISSING, f"分离环境启动失败：{e}")

    if r.returncode != 0:
        raise classify_returncode(r.returncode, r.stderr or r.stdout)

    lead = out_dir / "lead.wav"
    if not lead.exists():
        raise LeadSepError(
            LeadSepError.Kind.FAILED,
            "主唱分离返回成功但缺少 lead.wav（runner 输出归类异常）",
        )
    return lead

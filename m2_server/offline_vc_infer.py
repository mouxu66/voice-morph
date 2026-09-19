"""离线变声推理（由 m2_server/offline_vc.py 用 D:\\RVC\\.venv 的 python 跑）。

两条链路共用本模块的加载/转换实现：
  - 一次性 CLI：main()，一次加载转一个文件（**每条都重新加载模型，实测 ~20s**）
  - 常驻 worker：serve()，stdin/stdout 讲 JSON 行协议，模型按 (pth,index) 缓存，
    第二次起只剩推理（目标 <2s）。这是「点一下就发」延迟的主战场。
  - 级联实时：cascade_stream 直接 import load_vc/convert_audio

走 RVC 文件级推理链路 infer/vc（Pipeline 自动按静音切块拼接，支持任意长度音频）；
不用 rtrvc.RVC——那是实时块式设计，cache_pitch 固定 1024 帧，整段超 10s 会溢出报错。

用法：
    python offline_vc_infer.py --pth <模型.pth> --index <added_*.index> \
        --input <16k以下任意wav> --output <48k wav> [--pitch 0] [--index-rate 0.5]
    python offline_vc_infer.py --serve            # 常驻 worker，见 serve() 文档
"""

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为下方字符串注解提供类型名，运行时零开销
    import numpy as np


def setup_env() -> str:
    """把 RVC 根目录接进 sys.path 并补齐 infer/vc 需要的环境变量（幂等）。

    级联实时链路在同一进程里常驻复用本模块，故用 setdefault、可重复调用。
    注意：会 chdir 到 RVC 根目录（infer/vc 按相对路径加载 rmvpe 等资源），
    调用方后续文件操作请用绝对路径。
    """
    rvc_root = os.environ.get("VM_RVC_ROOT", r"D:\RVC")
    if rvc_root not in sys.path:
        sys.path.insert(0, rvc_root)
    os.environ["PYTHONPATH"] = rvc_root
    os.chdir(rvc_root)
    os.environ.setdefault("RVC_CUDA_GRAPH", "0")
    os.environ.setdefault("weight_root", "assets/weights")
    os.environ.setdefault("index_root", "logs")
    os.environ.setdefault("rmvpe_root", "assets/rmvpe")
    return rvc_root


@dataclass
class RvcEngine:
    """一次加载、反复使用的 RVC 转换引擎（模型常驻 GPU，避免逐块重载）。"""

    vc: object
    tgt_sr: int
    index: str  # 已校验存在；空串 = 不做特征检索
    if_f0: int
    version: str
    pth: str


def load_vc(pth: str, index: str = "") -> RvcEngine:
    """加载 RVC 模型（离线子进程与级联常驻共用同一套逻辑）。

    不用 rtrvc.RVC：那是实时块式设计，cache_pitch 固定 1024 帧，整段超 10s
    会溢出报错。统一走 infer/vc 的 Pipeline（自动按静音切块拼接，任意长度）。
    """
    setup_env()
    # RVC configs/config.py 在 import 时会 parser.parse_args() 严格解析 sys.argv，
    # 见到调用方自己的参数会直接报错退出；import 期间临时清空，之后还原。
    saved_argv = sys.argv
    sys.argv = [sys.argv[0]]
    try:
        import torch
        from configs.config import Config
        from infer.module.models import (
            SynthesizerTrnMs256NSFsid,
            SynthesizerTrnMs256NSFsid_nono,
            SynthesizerTrnMs768NSFsid,
            SynthesizerTrnMs768NSFsid_nono,
        )
        from infer.vc.modules import VC
        from infer.vc.pipeline import Pipeline
        from infer.vc.utils import load_hubert

        config = Config()
        config.device = torch.device("cuda")
        config.is_half = True

        # 手动填充 VC 实例（get_vc 依赖 weight_root 环境变量与 Gradio 回调，绕开它）
        # weights_only=True：仅允许 dict/list/str/int/tensor 等基础类型反序列化，
        # 防止第三方 ckpt 的 __reduce__ 载荷触发任意代码执行（坏文件此处即报错，不后移）
        cpt = torch.load(pth, map_location="cpu", weights_only=True)
        tgt_sr = cpt["config"][-1]
        cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk
        if_f0 = cpt.get("f0", 1)
        version = cpt.get("version", "v1")

        synth_cls = {
            ("v1", 1): SynthesizerTrnMs256NSFsid,
            ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
            ("v2", 1): SynthesizerTrnMs768NSFsid,
            ("v2", 0): SynthesizerTrnMs768NSFsid_nono,
        }[(version, if_f0)]
        net_g = synth_cls(*cpt["config"], is_half=config.is_half)
        del net_g.enc_q
        net_g.load_state_dict(cpt["weight"], strict=False)
        net_g.eval().to(config.device)
        net_g = net_g.half() if config.is_half else net_g.float()

        vc = VC(config)
        vc.cpt, vc.tgt_sr, vc.if_f0, vc.version = cpt, tgt_sr, if_f0, version
        vc.net_g = net_g
        vc.pipeline = Pipeline(tgt_sr, config)
        vc.hubert_model = load_hubert(config)
    finally:
        sys.argv = saved_argv

    return RvcEngine(
        vc=vc,
        tgt_sr=tgt_sr,
        index=index if (index and os.path.exists(index)) else "",
        if_f0=if_f0,
        version=version,
        pth=pth,
    )


def convert_audio(
    engine: RvcEngine, audio: "np.ndarray", pitch: int = 0, index_rate: float = 0.5
) -> "np.ndarray":
    """把 16k float 音频转成目标音色，返回后处理后的 float32 数组（engine.tgt_sr）。

    级联实时链路对每一句 TTS 输出调用一次；模型常驻，故无逐次加载开销。
    """
    import numpy as np

    audio_in = np.asarray(audio, dtype=np.float32)
    audio_max = np.abs(audio_in).max() / 0.95
    if audio_max > 1:
        audio_in = audio_in / audio_max
    audio_out = engine.vc.pipeline.pipeline(
        engine.vc.hubert_model,
        engine.vc.net_g,
        0,
        audio_in,
        [0.0, 0.0, 0.0],
        pitch,
        "rmvpe",
        engine.index,
        index_rate if engine.index else 0.0,
        engine.if_f0,
        engine.tgt_sr,
        0,
        0.25,
        engine.version,
        0.33,
    )
    return postprocess_audio(audio_out.astype("float32"), engine.tgt_sr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pth", required=True)
    p.add_argument("--index", default="")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--pitch", type=int, default=0, help="变调半音数，男转女 +12")
    p.add_argument("--index-rate", type=float, default=0.5)
    args = p.parse_args()

    # 必须先 setup_env()：infer 模块在 RVC 根下，而 load_vc() 内部才做 chdir +
    # sys.path.insert。放在它之前 import 会直接 ModuleNotFoundError（2026-09-10 修）。
    # setup_env 会 chdir，故先把相对路径钉成绝对路径。
    args.input = os.path.abspath(args.input)
    args.output = os.path.abspath(args.output)
    setup_env()

    from infer.audio import load_audio

    engine = load_vc(args.pth, args.index)
    audio_in = load_audio(args.input, 16000)
    y = convert_audio(engine, audio_in, args.pitch, args.index_rate)

    import soundfile as sf

    sf.write(args.output, y, engine.tgt_sr)
    print(f"OK {len(y) / engine.tgt_sr:.2f}s @{engine.tgt_sr}Hz")


def postprocess_audio(y, out_sr):
    """RVC 输出后处理：RMS 锚定 + 去刺耳高频 + 峰值安全化。

    历史坑：结尾原来是 np.clip(y, -0.99, 0.99) 硬削波——RVC 输出瞬态 + IIR
    低通过冲后大量样本顶到限幅沿（实测 0.17% 样本被削），听感刺耳、NatScore
    直接掉到负值区。改为「只衰减」的峰值归一到 -1.5 dBFS：够响的段不动，
    超限的段整体缩，绝不再产生平顶削波。
    """
    import numpy as np
    from scipy.signal import butter, sosfilt

    y = np.asarray(y, dtype=np.float32)
    # RMS 归一到 -18 dBFS（响度锚点）
    rms = float(np.sqrt((y**2).mean()))
    if rms > 1e-9:
        y = y / rms * (10 ** (-18 / 20))
    # 轻微低通去刺耳高频；低采样率模型（如 22.05k）时截止不得贴 Nyquist
    cutoff = min(11000.0, out_sr * 0.45)
    sos = butter(4, cutoff, fs=out_sr, btype="lowpass", output="sos")
    y = sosfilt(sos, y)
    # 峰值安全化：只衰减到 -1.5 dBFS，不放大（保留 RMS 锚定的响度关系）
    peak = float(np.abs(y).max())
    target = 10 ** (-1.5 / 20)
    if peak > target:
        y = y / peak * target
    return y.astype(np.float32)


def serve() -> int:
    """常驻 worker：stdin 收一行 JSON 任务，stdout 回一行 JSON 结果。

    ⚠️ 为什么要有这个模式（2026-09-10 实测）：一次性 CLI 每条音频都要重新
    `load_vc()`，5s 音频换声实测 **20~25s**，几乎全是模型加载；常驻后同一音色
    第二次起只剩推理，目标是压到 2s 以内。这是「点桌偶→发出语音」端到端延迟
    的最大单项。

    协议（全部 JSON，ensure_ascii=True 输出，避开 Windows 控制台编码问题）：
      先打 {"ready":true,"pid":N}
      任务行 {"id":1,"cmd":"convert"|"warmup"|"ping","pth":..,"index":..,
              "input":..,"output":..,"pitch":0,"index_rate":0.5}
      结果行 {"id":1,"ok":true,"duration":4.7,"sr":48000,
              "load_s":18.2,"convert_s":0.9}        失败时 {"ok":false,"error":..}
    模型按 (pth, index) 缓存。stdin 关闭（父进程退出）即正常退出。
    """
    import json
    import time

    for stream in (sys.stdin, sys.stdout):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", newline="\n")
    setup_env()  # 必须先于任何 infer.* 导入（chdir + sys.path）

    cache: dict[tuple[str, str], RvcEngine] = {}

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _engine(pth: str, index: str) -> tuple[RvcEngine, float]:
        key = (pth, index)
        if key in cache:
            return cache[key], 0.0
        t0 = time.time()
        cache[key] = load_vc(pth, index)
        return cache[key], time.time() - t0

    emit({"ready": True, "pid": os.getpid()})
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            tid = None
            try:
                task = json.loads(line)
                tid = task.get("id")
                cmd = task.get("cmd", "convert")
                if cmd == "ping":
                    emit({"id": tid, "ok": True})
                    continue
                engine, load_s = _engine(task.get("pth", ""), task.get("index", ""))
                if cmd == "warmup":
                    emit({"id": tid, "ok": True, "load_s": round(load_s, 2)})
                    continue
                from infer.audio import load_audio

                # setup_env 会 chdir，调用方传来的相对路径必须提前钉死
                src = os.path.abspath(task["input"])
                dst = os.path.abspath(task["output"])
                audio_in = load_audio(src, 16000)
                t1 = time.time()
                y = convert_audio(
                    engine, audio_in, int(task.get("pitch", 0)), float(task.get("index_rate", 0.5))
                )
                conv_s = time.time() - t1
                import soundfile as sf

                sf.write(dst, y, engine.tgt_sr)
                emit(
                    {
                        "id": tid,
                        "ok": True,
                        "output": dst,
                        "duration": round(len(y) / engine.tgt_sr, 2),
                        "sr": engine.tgt_sr,
                        "load_s": round(load_s, 2),
                        "convert_s": round(conv_s, 2),
                    }
                )
            except Exception as e:  # 单个任务失败不得拖垮 worker
                emit({"id": tid, "ok": False, "error": str(e)[:400]})
    except KeyboardInterrupt:
        pass
    finally:
        # 显存要还给系统：worker 可能长期占着 GPU 却无人调用
        with contextlib.suppress(Exception):
            cache.clear()
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv:
        sys.exit(serve())
    main()

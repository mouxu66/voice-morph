# -*- coding: utf-8 -*-
"""试衣间客观打分（一次性子进程，跑完即退出）。

为什么必须是独立进程（2026-09-18 实测，血泪）：
    打分器 = CAM++ 声纹（speaker_sep）+ NatScore 的 whisper-small 编码器。
    它们一旦在**后端主进程**里加载，就占住了那个进程的 CUDA 上下文。而每件音色的
    RVC 推理是另起子进程跑的 —— 实测父进程占了 ~2GB 后，RVC 子进程会在 GRU 前向
    阶段直接抛 `RuntimeError: cuDNN error: CUDNN_STATUS_EXECUTION_FAILED`，
    症状是「第一个音色成功、第二个起全部失败」。这不是巧合，是上下文竞争。
    所以：推理阶段绝不加载任何打分模型，打分统一由本脚本承担；跑完退出，显存全还。

为什么强制离线（同为实测）：
    transformers 会去 hf-mirror ping whisper-small 的权重新鲜度。本机权重早已
    缓存在 ~/.cache/huggingface（923MB），但那几次 HEAD 请求网络不通 →
    transformers 重试 5 次、每次 read timeout 10s，白白拖了 2 分钟才给出分数。
    离线模式直接用本地缓存，秒级完成；缓存真缺时也会立刻报错而不是干等。

用法（由 fitting_api._score_batch 调用，不建议手工执行）：
    python fitting_score.py --jobs jobs.json --out scores.json
    jobs.json  : [{"key": "kangaroo_v2", "wav": "a.wav", "ref": "reference.wav"}, ...]
                ref 为空串表示该音色没有参考音（只算自然度，不算音色像度）
    scores.json: {"kangaroo_v2": {"secs": 0.66, "nats": 2.9, "score_error": ""}, ...}
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 必须在 import transformers 之前设：这两个开关是 transformers/huggingface_hub
# 在**导入期**读取的，设晚了不生效。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parent.parent


def _prepare_paths() -> None:
    """把项目根与 m2_server 都加进 sys.path（config / speaker_sep 在 m2_server，
    natscore_local 在 tools）。"""
    for p in (ROOT, ROOT / "m2_server", ROOT / "tools"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def _secs(wav: Path, ref: Path):
    import numpy as np
    import speaker_sep
    emb = speaker_sep._sv_embed(speaker_sep._read16k(wav))
    emb_ref = speaker_sep._sv_embed(speaker_sep._read16k(ref))
    return float(np.dot(emb, emb_ref) /
                 (np.linalg.norm(emb) * np.linalg.norm(emb_ref) + 1e-9))


_NATS = None


def _nats_scorer():
    """NatScore 单例（懒加载；权重缺失时给可读原因而不是裸 ImportError）。"""
    global _NATS
    if _NATS is None:
        ckpt = ROOT / "models" / "natscore" / "final.pt"
        if not ckpt.exists():
            raise RuntimeError(f"NatScore 权重缺失：{ckpt}")
        try:
            from natscore_local import load_local
        except ImportError as exc:
            raise RuntimeError(f"NatScore 依赖缺失（需 torch 等）：{exc}") from exc
        _NATS = load_local(str(ckpt))
    return _NATS


def score_one(wav: Path, ref: Path | None) -> dict:
    """一个 wav 的两把尺子。任何一把失败只记 score_error，不抛。"""
    res = {"secs": None, "nats": None, "score_error": ""}
    errs: list[str] = []
    if ref is not None:
        try:
            res["secs"] = round(_secs(wav, ref), 3)
        except Exception as e:  # noqa: BLE001
            errs.append(f"音色相似度打分失败：{e}")
    try:
        res["nats"] = round(float(_nats_scorer().score(str(wav))), 3)
    except Exception as e:  # noqa: BLE001
        errs.append(f"自然度打分失败：{e}")
    res["score_error"] = "；".join(errs)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="试衣间客观打分（一次性子进程）")
    ap.add_argument("--jobs", required=True, help="任务 JSON 路径")
    ap.add_argument("--out", required=True, help="结果 JSON 路径")
    args = ap.parse_args()

    _prepare_paths()

    try:
        jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        Path(args.out).write_text(
            json.dumps({"__error__": f"任务文件读取失败：{e}"}, ensure_ascii=False),
            encoding="utf-8")
        return 2

    out: dict = {}
    for job in jobs:
        key = str(job.get("key") or "")
        wav = Path(str(job.get("wav") or ""))
        ref_s = str(job.get("ref") or "")
        if not key or not wav.exists():
            out[key] = {"secs": None, "nats": None, "score_error": "产物文件不存在"}
            continue
        ref = Path(ref_s) if ref_s else None
        if ref is not None and not ref.exists():
            ref = None
        out[key] = score_one(wav, ref)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

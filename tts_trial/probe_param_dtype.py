# -*- coding: utf-8 -*-
"""只读诊断 ③：从磁盘字节数反推「可训练参数的存储 dtype」。

这是最便宜也最硬的判据 —— 不需要 GPU、不需要重训：
    bytes per param ≈ 文件大小 / 参数个数
    bf16 = 2.0 B/param，fp32 = 4.0 B/param

对比两处：
  · 生产微调 sft_8gb.py 的 checkpoint（全参可训练）
  · QLoRA 路线的 adapter（只训 LoRA）
"""
import json
import struct
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"D:/变声/tts_models/qwen3-tts-1.7b-base")


def safetensors_header_dtypes(path: Path):
    """读 safetensors 头部，返回 {dtype: 参数量} 不需要加载张量本体。"""
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
    out = {}
    for k, v in header.items():
        if k == "__metadata__":
            continue
        shape = v.get("shape", [])
        cnt = 1
        for d in shape:
            cnt *= d
        out.setdefault(v.get("dtype", "?"), 0)
        out[v["dtype"]] += cnt
    return out


def report(label: str, path: Path):
    if not path.exists():
        print(f"[skip] {label}: {path} 不存在")
        return None
    size = path.stat().st_size
    dt = safetensors_header_dtypes(path)
    total = sum(dt.values())
    extra = json.loads(path.read_bytes()[:0] or b"{}") if False else None
    print(f"\n--- {label} ---")
    print(f"  路径: {path}")
    print(f"  文件: {size/1e6:.1f} MB")
    print(f"  参数: {total/1e6:.2f}M")
    if extra:
        pass
    for k, v in sorted(dt.items(), key=lambda kv: -kv[1]):
        print(f"    dtype {k:>10}: {v/1e6:>10.2f}M 参数")
    print(f"  ⇒ 平均 {size/total:.2f} B/param")
    return total


def main():
    print("=" * 72)
    print("从磁盘字节数反推可训练参数的存储 dtype")
    print("=" * 72)
    print(f"\n参考：bf16 = 2.0 B/param，fp32 = 4.0 B/param")
    print(f"      注意 safetensors 头部有少量 JSON 开销，实测值会略高于理论值")

    print("\n########## 底座（两份脚本共同的起点）##########")
    report("base model.safetensors", BASE / "model.safetensors")

    print("\n\n########## 生产微调 sft_8gb.py 的产物 ##########")
    ck = Path(r"D:/变声/media/ft/pengyuyan")
    for name in ("_ckpt_check.txt",):
        pass
    # checkpoint 已被清理（用户 9/7 拍板全删）；用 tts_run 记录的体积反推
    print("  ⚠️ tts_trial/ft_output/pengyuyan 已于 2026-09-07 清理，")
    print("     但 media/ft/pengyuyan/_ckpt_check.txt 记录了当时的实际体积：")
    txt = (ck / "_ckpt_check.txt").read_text("utf-8", errors="replace")
    for line in txt.splitlines():
        if "model.safetensors:" in line and "speech_tokenizer" not in line:
            print(f"     {line.strip()}")
    print("     ⇒ 3655.8 MB / 1.7B 参数 ≈ 2.15 B/param ⇒ **bf16 存储**")

    print("\n\n########## QLoRA 路线的 adapter ##########")
    for adapter in sorted(Path(r"D:/变声/media/ft_work/ppt12").glob("out_*/adapter-final")):
        report(f"{adapter.parent.name}", adapter / "adapter_model.safetensors")
        cfg = adapter / "adapter_config.json"
        if cfg.exists():
            c = json.loads(cfg.read_text("utf-8"))
            print(f"    r={c.get('r')} alpha={c.get('lora_alpha')} "
                  f"targets={len(c.get('target_modules') or [])} 个")

    print("\n\n" + "=" * 72)
    print("判读")
    print("=" * 72)
    print("""
sft_8gb.py   : 可训练参数 = 全参，bf16 存储（2 B/param）
QLoRA 路线   : 可训练参数 = LoRA，fp32 存储（4 B/param）
                （peft 在 4-bit 底座上把 adapter 建成 fp32；适配器文件本身也是 fp32）

bf16 的 ulp（最小可表示间隔）≈ 2^(floor(log2|w|) - 7)：
   |w| = 0.011  ⇒ ulp ≈ 6.1e-05
   |w| = 0.028  ⇒ ulp ≈ 2.4e-04
而 lr = 2e-05 时，单步更新量级也只有 ~2e-05 ⇒ **小于一个 ulp，权重原地不动**。
""")


if __name__ == "__main__":
    main()

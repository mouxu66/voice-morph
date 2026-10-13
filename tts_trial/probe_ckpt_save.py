# -*- coding: utf-8 -*-
"""只读复现：`sft_8gb._save_ckpt()` 在**目标已存在**时崩溃（WinError 32）。

发现经过：受控 A/B 实验第二次跑进同一个输出目录时，两臂都在 epoch 末
`_save_ckpt` 抛 `PermissionError: [WinError 32] 另一个程序正在使用此文件`，
训练本身是好的（194 步、loss 正常下降），**只是存不下来** → 进程 rc=1
→ 生产状态机（finetune.py:448 看 `rc == 0`）会把整轮训练判成 error。

根因链（待本脚本证实）：
    os.link(src, target)  目标已存在 → FileExistsError（OSError 子类，被接住）
    shutil.copy2(src, target)     → 在 src/target 为同一硬链接或目标被占用时抛错
    ⇒ except OSError 里再抛，直接冒泡出 _save_ckpt

本脚本用**真实的 _save_ckpt**（import 现役模块）+ 假 model/accelerator，
连调两次，观察第二次是否崩。不占 GPU、秒级。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (_HERE, os.path.join(_HERE, "Qwen3-TTS", "finetuning")):
    if p not in sys.path:
        sys.path.insert(0, p)

import torch  # noqa: E402


class _FakeTalkerModel:
    def __init__(self):
        self.codec_embedding = torch.nn.Module()
        self.codec_embedding.weight = torch.nn.Parameter(
            torch.zeros(4096, 8, dtype=torch.float32), requires_grad=False)


class _FakeTalker:
    def __init__(self):
        self.model = _FakeTalkerModel()


class _FakeModel:
    """满足 _save_ckpt 需要的三件事：state_dict()、talker.*、以及可循环的迭代。

    _save_ckpt 里用 `unwrapped_model.state_dict()` 取权重，再只读
    `talker.model.codec_embedding.weight[3000]` 写锚点。
    """

    def __init__(self):
        self.talker = _FakeTalker()

    def state_dict(self):
        return {
            "talker.model.codec_embedding.weight": self.talker.model.codec_embedding.weight,
            "speaker_encoder.dummy": torch.zeros(2, 2),
        }


class _FakeAccelerator:
    def unwrap_model(self, m):
        return m


def build_fake_base(root: Path):
    """构造一个最小 'base 模型目录'：顶层 config.json + 几个普通文件 + 一个子目录。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text('{"tts_model_type": "base", "talker_config": {}}',
                                      encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"BASE-WEIGHTS-PLACEHOLDER")
    (root / "vocab.json").write_text('{"a": 1}', encoding="utf-8")
    (root / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    sub = root / "speech_tokenizer"
    sub.mkdir(exist_ok=True)
    (sub / "config.json").write_text('{"x": 1}', encoding="utf-8")


class _Args:
    speaker_name = "probe"


def main():
    import sft_8gb as S

    tmp = Path(tempfile.mkdtemp(prefix="ckpt_repro_"))
    base = tmp / "fake_base"
    out = tmp / "fake_out" / "checkpoint-epoch-0"
    build_fake_base(base)

    # _save_ckpt 用到全局 target_speaker_embedding 与 torch.cuda
    S.target_speaker_embedding = torch.zeros(1, 8, dtype=torch.float32)

    print(f"base = {base}")
    print(f"out  = {out}\n")

    print("--- 第 1 次 _save_ckpt（目标不存在）---")
    try:
        S._save_ckpt(str(out), str(base), _Args(), _FakeAccelerator(), _FakeModel())
        print("  ✅ 成功")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {type(e).__name__}: {e}")
        return

    print("\n--- 第 2 次 _save_ckpt（目标已存在 ← 重训/续训就会这样）---")
    try:
        S._save_ckpt(str(out), str(base), _Args(), _FakeAccelerator(), _FakeModel())
        print("  ✅ 成功（说明可安全覆盖）")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {type(e).__name__}: {e}")
        print("\n  ★ 复现成功：目标目录已有同名文件时，_save_ckpt 直接抛异常。")
        print("    生产影响：同一音色**第二次训练**（或 C2 续训）跑到该 epoch 末会崩，")
        print("    进程 rc=1 → finetune.py:448 把整轮判成 error（尽管权重已训好）。")

    print(f"\n[留档] 复现目录：{tmp}")


if __name__ == "__main__":
    main()

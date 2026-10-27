"""能力探测：能不能「用项目已有音色 + 方言标签」直接出四川话。

背景
----
`modeling_qwen3_tts.py:2113-2122` 显示 `language` 参数会直接查
`codec_language_id`，而 CustomVoice 检查点里有 `sichuan_dialect = 2062`。
看起来只要把 language 传方言名，就能让任意参考音走四川音系。

实测结论（2026-10-22）
--------------------
**不行。** CustomVoice 检查点在 API 层禁掉了零样本克隆：
`create_voice_clone_prompt` 直接 raise ValueError
（"tts_model_type: custom_voice does not support ..."）。
所以这条「一步到位」的路走不通，用已有音色说方言只能走 RVC。

本脚本保留为可复现的能力探测记录，不生成音频。
产出结论写入 docs/交接-AI必读.md §10.4。
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

VENDOR = Path(r"D:/tts_vendor/tf4573")
if (VENDOR / "transformers").is_dir():
    sys.path.insert(0, str(VENDOR))

import torch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "Qwen3-TTS"))
from qwen_tts import Qwen3TTSModel  # noqa: E402

MODEL = r"D:/变声/tts_models/qwen3-tts-1.7b-customvoice"
PROBE_REF = r"D:/变声/media/voicebank/myvoice_supp2026/reference.wav"


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"加载 {MODEL}")
    model = Qwen3TTSModel.from_pretrained(MODEL, dtype=torch.bfloat16, device_map="cuda:0")

    cfg = model.model.config.talker_config
    print(f"可用语言   : {sorted(cfg.codec_language_id)}")
    print(f"方言映射   : {cfg.spk_is_dialect}")
    print(f"内置音色   : {getattr(model, 'get_supported_speakers', lambda: [])()}")

    print(f"\n试探：用已有音色 {PROBE_REF} + language='sichuan_dialect' 做零样本克隆")
    try:
        model.create_voice_clone_prompt(
            ref_audio=PROBE_REF, x_vector_only_mode=True)
    except ValueError as exc:
        print(f"  失败 → {type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("\n结论：CustomVoice 不支持零样本克隆，"
              "已有音色说方言需走 RVC（docs §9）。")
    else:
        print("  意外成功 —— 需重跑 docs §9 的结论")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

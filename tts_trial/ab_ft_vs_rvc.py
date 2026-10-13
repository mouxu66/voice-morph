# -*- coding: utf-8 -*-
"""同素材正面对照：Qwen 微调 vs RVC，同一支 12 分钟演讲视频、同样 3 句文本。

为什么能直接比
--------------
两条线用的是**同一支视频**（`docs/大创申报-2026-10/大创申报PPT.mp4`）切出的
同一批 81 条 / 12.23 分钟素材，且下面 3 句文本**逐字相同**：

    ① 这一节我们讲三个概念，先看第一个。
    ② 把步骤拆开，一步一步做就不会乱。
    ③ 注意这里的条件，符号写反了结论就完全不同。

所以这里量的是用户真正关心的问题：**"用我的声音把这三句念出来，谁更像 / 更清楚"**。

四臂（同一进程、同一参考音 ref_24k.wav ⇒ CAM++ 绝对值可比）
    1. base     纯 Qwen 零样本（没微调）              base_{1,2,3}.wav
    2. qwen_ft  Qwen LoRA 微调（ep09 = 甜点）        ep09_{1,2,3}.wav
    3. rvc      Qwen 零样本 → RVC 12 分钟微调模型     out_tts0{1,2,3}.wav
    4. ceiling  真人原音 → 同一个 RVC（音色上限）      out_real.wav（另一段文本）

⚠️ 两条线的公平性边界（必须与结论一起引用）：
  · Qwen LoRA 是 **text→speech**（直接生成语音）；
  · RVC 是 **voice conversion**（需要一个源音频才能转）。
    它们不是同一个任务 —— RVC 那一臂的"源"是 Qwen 零样本的输出，
    也就是说 **rvc 臂本身就等于"base + RVC"**，天然多了一跳。
  · n=3 句，可懂度 ±0.02 ≈ 一个字；CAM++ 见同一批内的相对值。

用法（.venv，需 faster_whisper）：
    .venv\\Scripts\\python.exe tts_trial\\ab_ft_vs_rvc.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")

from ab_evaluate import cam_emb  # noqa: E402
from asr_cer import accuracy as asr_accuracy  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
LORA_DIR = os.path.join(PPT, "out_ab", "ab_effect_multi")
RVC_DIR = os.path.join(PPT, "myvoice_rvc_full")

TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
]

# (臂名, 说明, [3 个 wav 路径])
ARMS = [
    ("base", "Qwen 零样本（未微调）",
     [os.path.join(LORA_DIR, f"base_{i}.wav") for i in (1, 2, 3)]),
    ("qwen_ft", "Qwen LoRA 微调 ep09",
     [os.path.join(LORA_DIR, f"ep09_{i}.wav") for i in (1, 2, 3)]),
    ("rvc", "Qwen 零样本 → RVC(12min)",
     [os.path.join(RVC_DIR, f"out_tts0{i}.wav") for i in (1, 2, 3)]),
]


def emb(path):
    v = cam_emb(path)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    if x is None or y is None:
        return float("nan")
    d = float(np.linalg.norm(x)) * float(np.linalg.norm(y))
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def audio_stats(path):
    import soundfile as sf

    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    clip = float(np.mean(np.abs(a) >= 0.999)) if a.size else 0.0
    return peak, clip, len(a) / float(sr)


def main():
    for arm, desc, paths in ARMS:
        for p in paths:
            if not os.path.exists(p):
                print(f"[FATAL] 缺文件：{p}")
                return 1

    ref = emb(REF)
    print(f"参考音：{REF}\n")
    wm = WhisperModel("small", device="cuda", compute_type="float16")

    print(f"{'臂':<10}{'说明':<24}{'CAM++':>9}{'可懂度':>9}{'peak':>8}{'削波%':>8}{'时长s':>8}")
    print("-" * 78)
    out = {}
    for arm, desc, paths in ARMS:
        sims, accs, peaks, clips, durs = [], [], [], [], []
        for p, text in zip(paths, TEXTS):
            sims.append(cos(emb(p), ref))
            segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
            back = "".join(s.text for s in segs).replace(" ", "")
            accs.append(asr_accuracy(text, back))
            pk, cl, du = audio_stats(p)
            peaks.append(pk)
            clips.append(cl)
            durs.append(du)
        out[arm] = dict(campp=float(np.mean(sims)), asr=float(np.mean(accs)),
                        peak=float(np.mean(peaks)), clip=float(np.mean(clips)),
                        dur=float(np.mean(durs)))
        r = out[arm]
        print(f"{arm:<10}{desc:<24}{r['campp']:>9.4f}{r['asr']:>9.3f}"
              f"{r['peak']:>8.3f}{r['clip']*100:>8.2f}{r['dur']:>8.2f}")

    # 音色上限（真人原音过同一个 RVC；文本不同，只作上限参照）
    real_out = os.path.join(RVC_DIR, "out_real.wav")
    if os.path.exists(real_out):
        pk, cl, du = audio_stats(real_out)
        print("-" * 78)
        print(f"{'ceiling':<10}{'真人原音 → RVC（上限）':<24}"
              f"{cos(emb(real_out), ref):>9.4f}{'—':>9}{pk:>8.3f}{cl*100:>8.2f}{du:>8.2f}")

    b = out["base"]
    print("\n" + "=" * 78)
    print("相对 base（Qwen 零样本）的增量 —— 这才是「微调 / RVC」各自带来多少")
    print("=" * 78)
    for arm in ("qwen_ft", "rvc"):
        r = out[arm]
        print(f"  {arm:<10} CAM++ {r['campp']-b['campp']:+.4f}   "
              f"可懂度 {r['asr']-b['asr']:+.3f}")
    print(f"\n  qwen_ft − rvc：CAM++ {out['qwen_ft']['campp']-out['rvc']['campp']:+.4f}   "
          f"可懂度 {out['qwen_ft']['asr']-out['rvc']['asr']:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

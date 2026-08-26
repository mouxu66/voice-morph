# 用 Qwen3-TTS 批量生成袋鼠音语料，作为 RVC 训练集（media/rvc_dataset/）
# 用法：
#   python -u qwen3_batch_tts.py                # 全量 20 句
#   python -u qwen3_batch_tts.py --limit 3      # 只生成前 3 句（快速验证）
import argparse
import torch
from pathlib import Path

import soundfile as sf
from qwen_tts import Qwen3TTSModel

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
REF_TEXT = "怕被其他人知道这家店给你一个"
OUT_DIR = Path(r"D:/变声/media/rvc_dataset")

# 20 句训练语料：覆盖日常对话/数字/口语，音节多样，利于 RVC 学习音色
TEXTS = [
    "怕被其他人知道这家店给你一个",
    "老板，我要两个烤串，再来一瓶可乐",
    "今天天气真不错，我们出去走走吧",
    "一二三四五六七八九十",
    "这个周末你有什么安排吗",
    "我跟你说，这家店的汉堡特别好吃",
    "快点快点，电影马上就要开始了",
    "谢谢你啊，下次请你吃饭",
    "别着急，慢慢来，安全第一",
    "昨天晚上我睡得特别香",
    "请问地铁站怎么走啊",
    "他让我转告你，明天开会改到下午",
    "我们是一家人，不用这么客气",
    "这个价格也太贵了吧",
    "手机快没电了，我先挂了啊",
    "明天早上八点，校门口见",
    "妈妈做的菜永远是最好吃的",
    "下雨了，记得带伞",
    "开饭啦，大家都过来吧",
    "坚持锻炼，身体才会越来越好",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(TEXTS), help="生成多少句")
    ap.add_argument("--start", type=int, default=0, help="从第几句开始")
    args = ap.parse_args()
    sel = TEXTS[args.start:args.start + args.limit]

    print(f"加载 Qwen3-TTS 模型中（{MODEL_DIR}）...", flush=True)
    model = Qwen3TTSModel.from_pretrained(
        MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
    print("模型加载完成，提取袋鼠音色向量...", flush=True)
    prompt = model.create_voice_clone_prompt(
        ref_audio=REF_AUDIO, ref_text=REF_TEXT, x_vector_only_mode=False)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for i, text in enumerate(sel, args.start + 1):
        print(f"[{i:02d}/{len(TEXTS)}] 生成：{text[:20]}", flush=True)
        wavs, sr = model.generate_voice_clone(
            text=[text], language=["Chinese"], voice_clone_prompt=prompt)
        path = OUT_DIR / f"qwen_kangaroo_{i:03d}.wav"
        sf.write(str(path), wavs[0], sr)
        dur = len(wavs[0]) / sr
        print(f"      -> {path.name} ({dur:.1f}s)", flush=True)

    print(f"完成：共生成 {len(sel)} 句袋鼠语料，位于 {OUT_DIR}（采样率 {sr}Hz）", flush=True)


if __name__ == "__main__":
    main()

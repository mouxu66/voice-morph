"""Phase 3 补充：fast vs orig 输出的 ASR 可懂度对比。"""
import sys

sys.path.insert(0, r"D:\变声\m2_server")
from faster_whisper import WhisperModel

TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖，你的奶茶已经凉了，麻烦你下楼取一下。"
LONG = ("欢迎来到变声工坊的语音合成测试环节。今天我们要验证的是 CUDA Graph 加速之后，"
        "长文本合成的速度和质量是否依然稳定。")
FILES = [
    ("orig_greedy", r"D:\变声\outputs\phase2_orig_greedy.wav", TEXT),
    ("fast_greedy", r"D:\变声\outputs\phase2_fast_greedy.wav", TEXT),
    ("fast_sampling", r"D:\变声\outputs\phase2_fast_sampling.wav", TEXT),
    ("fast_long", r"D:\变声\outputs\phase2_fast_long.wav", LONG),
]


def cer(ref, hyp):
    ref, hyp = list(ref), list(hyp)
    n, m = len(ref), len(hyp)
    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, m + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1,
                        prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return dp[m] / max(n, 1)


wm = WhisperModel("small", device="cuda", compute_type="float16")
for name, path, ref in FILES:
    segs, _ = wm.transcribe(path, language="zh", vad_filter=True)
    hyp = "".join(s.text for s in segs).replace(" ", "")
    print(f"{name:14s} CER={cer(ref, hyp):.3f}  hyp={hyp}", flush=True)

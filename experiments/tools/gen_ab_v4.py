import os, base64, wave
d = "D:/变声/outputs/ab_test"
tracks = [
    ("A", "原音(袋鼠真声,基准)", "original_017.wav", "t-orig"),
    ("C", "旧base+RVC锚点(错的合成锚)", "base_vc_017.wav", "t-old"),
    ("D", "base+真声锚点 x_vector_only", "base_xvec_real_017.wav", "t-syn"),
    ("E", "base+真声锚点 ICL", "base_icl_real_017.wav", "t-syn"),
]
def info(f):
    p = os.path.join(d, f)
    w = wave.open(p, 'rb'); n = w.getnframes(); sr = w.getframerate(); w.close()
    return f"{n/sr:.2f}s / {sr}Hz"
cards = []
for tid, name, fn, cls in tracks:
    p = os.path.join(d, fn)
    b = base64.b64encode(open(p, 'rb').read()).decode()
    cards.append(f'''<div class="card"><span class="tag {cls}">{tid} · {info(fn)}</span>
<div class="txt">{name}</div>
<audio controls preload="auto" src="data:audio/wav;base64,{b}"></audio></div>''')
html = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>袋鼠骑士 v4 真声锚点</title>
<style>body{{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1220;color:#e8ecf5;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:#9aa6c0;font-size:13px;margin-bottom:18px}}
.card{{background:#1a1f33;border:1px solid #2a3150;border-radius:14px;padding:16px;margin:12px 0}}
.tag{{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;margin-bottom:10px}}
.t-orig{{background:#1d3a2a;color:#7ee0a8}}.t-old{{background:#3a2a1d;color:#e0b07e}}.t-syn{{background:#2a2348;color:#b59cff}}
.txt{{font-size:16px;font-weight:600;margin:6px 0 14px;color:#fff}}
audio{{width:100%;height:42px}}
.note{{color:#9aa6c0;font-size:12px;line-height:1.7;margin-top:16px;border-top:1px solid #2a3150;padding-top:12px}}
code{{background:#11152a;padding:1px 6px;border-radius:5px;color:#ffd479}}</style></head><body>
<h1>袋鼠骑士 · v4（真声锚点对比）</h1>
<div class="sub">同句「现在是2026年8月28号」：A=原声基准，C=旧错锚，D/E=换袋鼠真声(merg_004)后的两版</div>
{''.join(cards)}
<div class="note">
锚点文件：<code>_trash_20260831/merg_004/reference.wav</code>（袋鼠真声金标 20.8s）。<br>
D = x_vector_only（只取说话人音色，韵律交给模型默认→偏快）；E = ICL（吃参考音韵律，但 whisper 转写对齐不准，反而更短）。<br>
实测：换真声锚点后音色方向更对，但 base 模型零样本克隆**只克隆音色、节奏被模型默认速度接管**，故都比原音 3.40s 短、能量更冲。<br>
结论提示：要"像袋鼠原声那样自然慢说"，base 模型难做到；你已在用的 <code>kangaroo</code> RVC 克隆可能才是这个音色的正解。
</div></body></html>'''
open(os.path.join(d, "ab_test_v4.html"), "w", encoding="utf-8").write(html)
print("written", os.path.join(d, "ab_test_v4.html"), os.path.getsize(os.path.join(d, "ab_test_v4.html"))/1024, "KB")

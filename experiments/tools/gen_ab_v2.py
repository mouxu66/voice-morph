import os, base64
d = "D:/变声/outputs/ab_test"
tracks = [
    ("A · 原音（视频切片）", "original_017.wav", "t-orig",
     "袋鼠本人念「现在是2026年8月28号」，3.40s，22050Hz——真声音基准"),
    ("B_old · 袋鼠骑士（旧·电音坏档）", "kangaroo_knight_017.wav", "t-bad",
     "之前全精度底座+adapter合并生成，RMS爆表/满幅削顶=电音，2.00s"),
    ("B_new · 袋鼠骑士（修复·4bit+LoRA）", "qlora_fixed_017.wav", "t-syn",
     "4bit底座(与训练一致)+LoRA直推，能量恢复正常，1.28s（时长偏短，待调参）"),
]
cards = []
for title, fn, cls, note in tracks:
    p = os.path.join(d, fn)
    b = base64.b64encode(open(p, "rb").read()).decode()
    cards.append(f'''<div class="card">
<span class="tag {cls}">{title}</span>
<div class="note">{note}</div>
<audio controls preload="auto" src="data:audio/wav;base64,{b}"></audio></div>''')

html = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>袋鼠骑士 A/B 试听 v2</title>
<style>
body{{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1220;color:#e8ecf5;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:#9aa6c0;font-size:13px;margin-bottom:18px}}
.card{{background:#1a1f33;border:1px solid #2a3150;border-radius:14px;padding:16px;margin:12px 0}}
.tag{{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;margin-bottom:10px}}
.t-orig{{background:#1d3a2a;color:#7ee0a8}}.t-bad{{background:#3a1d1d;color:#ff9a9a}}.t-syn{{background:#2a2348;color:#b59cff}}
.note{{font-size:12px;color:#9aa6c0;margin-bottom:10px;line-height:1.6}}
audio{{width:100%;height:42px}}
.head{{color:#ffd479;font-size:13px;margin:18px 0 6px;font-weight:600}}
</style></head><body>
<h1>袋鼠骑士 · A/B 试听 v2</h1>
<div class="sub">同一句话：<code>现在是2026年8月28号</code>。先听 A 原音，再听 B_new 修复版，最后听 B_old 对比「坏档」长啥样。</div>
<div class="head">核心对比</div>
{''.join(c for c in cards if 'orig' in c or 'syn' in c)}
<div class="head">坏档对照（展示修复前的电音）</div>
{''.join(c for c in cards if 'bad' in c)}
<div class="note" style="margin-top:18px;border-top:1px solid #2a3150;padding-top:12px">
修复手段：训练时底座是 4-bit 量化(Qwen3-TTS+QLoRA)，之前用「全精度底座+LORA合并」推理导致分布错位、输出满幅电音；
本次改用「4-bit底座(load_in_4bit, nf4, 与训练一致)+PeftModel挂adapter直接推理」，并把被跳过的模块(speaker_encoder/codec_embedding/text_embedding/lm_head)统一fp32以匹配LoRA的fp32增量，才正常出声。<br>
已知问题：B_new 仅 1.28s，比原音 3.40s 短，可能是生成步数/语速偏快，可调 max_new_tokens 或采样参数重生成。
</div></body></html>'''
open(os.path.join(d, "ab_test_v2.html"), "w", encoding="utf-8").write(html)
print("written", os.path.join(d, "ab_test_v2.html"), os.path.getsize(os.path.join(d, "ab_test_v2.html"))/1024, "KB")

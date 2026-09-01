import os, base64
d = "D:/变声/outputs/ab_test"
tracks = [
    ("A · 原音（视频切片 · 基准）", "original_017.wav", "t-orig",
     "袋鼠本人念「现在是2026年8月28号」，3.40s / 22050Hz。这是要追的目标。"),
    ("C · base+袋鼠锚点（历史验证方案 · 推荐）", "base_vc_017.wav", "t-syn",
     "纯 bf16 base + kangaroo 锚点，无 LoRA 无量化，x_vector_only 纯声纹。2.80s——时长与能量都最接近原音。"),
    ("B_new · 4bit+LoRA（今天的修复版）", "qlora_fixed_017.wav", "t-bad",
     "修掉了电音饱和，但只有 1.28s（原音的 1/3）→ 语速飞快、听不清。"),
    ("B_old · 全精度+LoRA（最开始的坏档）", "kangaroo_017_placeholder.wav", "t-bad",
     "占位"),
]
# 修正最后一轨文件名
tracks[-1] = ("B_old · 全精度+LoRA（最开始的坏档）", "kangaroo_knight_017.wav", "t-bad",
              "满幅削顶的电音版，RMS 爆到 5706、峰值顶到 int16 上限 32256。")

cards = []
for title, fn, cls, note in tracks:
    p = os.path.join(d, fn)
    if not os.path.exists(p):
        continue
    b = base64.b64encode(open(p, "rb").read()).decode()
    cards.append(f'''<div class="card">
<span class="tag {cls}">{title}</span>
<div class="note">{note}</div>
<audio controls preload="auto" src="data:audio/wav;base64,{b}"></audio></div>''')

html = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>袋鼠骑士 A/B 试听 v3（四轨）</title>
<style>
body{{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1220;color:#e8ecf5;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:#9aa6c0;font-size:13px;margin-bottom:18px}}
.card{{background:#1a1f33;border:1px solid #2a3150;border-radius:14px;padding:16px;margin:12px 0}}
.tag{{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;margin-bottom:10px;font-weight:600}}
.t-orig{{background:#1d3a2a;color:#7ee0a8}}.t-bad{{background:#3a1d1d;color:#ff9a9a}}.t-syn{{background:#1d2a4a;color:#9ecbff}}
.note{{font-size:12px;color:#9aa6c0;margin-bottom:10px;line-height:1.6}}
audio{{width:100%;height:42px}}
.head{{color:#ffd479;font-size:13px;margin:18px 0 6px;font-weight:600}}
table{{border-collapse:collapse;margin:12px 0;font-size:12px}}
th,td{{border:1px solid #2a3150;padding:5px 12px;text-align:left}}
th{{color:#9aa6c0}}td{{color:#e8ecf5}}
</style></head><body>
<h1>袋鼠骑士 · A/B 试听 v3</h1>
<div class="sub">同一句话：<code>现在是2026年8月28号</code>。重点听 <b>A 原音</b> 与 <b>C base+锚点</b> 的差距。</div>

<table>
<tr><th>轨</th><th>时长</th><th>RMS</th><th>峰值</th><th>判断</th></tr>
<tr><td>A 原音</td><td>3.40s</td><td>2649</td><td>16228</td><td>基准</td></tr>
<tr><td>C base+锚点</td><td>2.80s</td><td>3016</td><td>14912</td><td>最接近原音</td></tr>
<tr><td>B_new 4bit+LoRA</td><td>1.28s</td><td>3514</td><td>17744</td><td>过短→语速飞快</td></tr>
<tr><td>B_old 全精度+LoRA</td><td>2.00s</td><td>5706</td><td>32256</td><td>满幅电音</td></tr>
</table>

<div class="head">推荐方案 vs 基准</div>
{''.join(cards[:2])}
<div class="head">LoRA 两版（对照，说明为何听不清）</div>
{''.join(cards[2:])}

<div class="note" style="margin-top:18px;border-top:1px solid #2a3150;padding-top:12px">
<b>为什么会"听不清"（来自 2026-08-28 已排查结论）</b><br>
1. 「听不清」≠ 声音坏了，而是 <b>文本标签错</b>：训练/ICL 用的 ref_text 是 whisper 转写的同音错字（如"我真的恨上些OK"），模型学到错误的 text→speech 映射，给干净文本就吐乱码。<br>
2. <b>LoRA 在本项目不可用</b>：训练标签=whisper 错字，LoRA 会把 base 原本正确的映射改歪。纯 base 无 LoRA 反听字字清晰。<br>
3. <b>推理务必纯 bf16，不要 4-bit 量化</b>；克隆一律走 <code>generate_voice_clone(x_vector_only_mode=True)</code>（纯声纹，不吃 ref_text 噪声），ICL 模式需极准确的 ref_text，慎用。<br>
<b>结论：要用袋鼠音色念任意文字，走 C 方案（base + kangaroo 锚点）。若仍要 LoRA 微调，必须先提供准确中文标签（非 whisper 转写）再重训。</b>
</div></body></html>'''
open(os.path.join(d, "ab_test_v3.html"), "w", encoding="utf-8").write(html)
print("written", os.path.join(d, "ab_test_v3.html"), round(os.path.getsize(os.path.join(d, "ab_test_v3.html"))/1024, 1), "KB")

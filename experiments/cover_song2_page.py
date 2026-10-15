# -*- coding: utf-8 -*-
"""生成《唯一》翻唱的试听页（音频内嵌，双击即听）。

口径：每条都是**同一段 45 秒**（85–130s，歌声最密段）、**同一对分离轨**、
**同一个 seed**，只有"哪一档变调 / 有没有伴奏"不同 —— 所以听到的差异就是那一项。

可测性：页面生成是纯函数 `render_html(base)`（喂一个目录就吐 HTML），写盘只在 `main()`。
守卫测试拿临时目录放几个假 mp3，就能验证「每个文件变成一个可播条 / 文本被转义 /
暴露了播放探针」，不必真跑 45 秒音频。
"""
from __future__ import annotations

import base64
import html
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

BASE = pathlib.Path(__file__).resolve().parent.parent / "media/covers/song2/mypitch"

GROUPS = [
    ("① 原唱这一段（对照，没换声）", [
        ("orig_vocal.mp3", "原唱人声（demucs 分离）", "这是 A/B 的『原』——听着它比。"),
    ]),
    ("② 换成我的声音 · 保原调（pitch 0）", [
        ("vocal_p+00.mp3", "只换音色（无伴奏）", "听音色/韵律最清楚。音高与原唱一致。"),
        ("full_p+00.mp3", "人声 + 伴奏（成品）", "整首翻唱的样子。"),
    ]),
    ("③ 换成我的声音 · 降八度（pitch −12）", [
        ("vocal_p-12.mp3", "只换音色（无伴奏）", "建议档：我的说话音域 ~108Hz，原唱 275Hz。"),
        ("full_p-12.mp3", "人声 + 伴奏（成品）", "整首翻唱的样子。"),
    ]),
]

#: 暴露给无头走查的播放探针（与 tools/listen_page.py 的 window.__vmAudio 同口径）
PROBE_HOOK = (
    "window.__vmClips = () => document.querySelectorAll('[data-src]').length;\n"
    "window.__vmAudio = () => (cur ? {t: cur.currentTime, paused: cur.paused, "
    "dur: cur.duration} : null);"
)


def data_uri(p: pathlib.Path) -> str:
    return "data:audio/mpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def render_html(base: pathlib.Path, groups=GROUPS) -> str:
    """把 `base` 下 groups 里实际存在的 mp3 编成内嵌试听页 HTML（不存在的条目跳过）。

    `groups` 可注入（守卫用它喂一个名字里带 `<&>` 的条目，验证转义真的生效）。
    """
    blocks = []
    for title, items in groups:
        rows = []
        for fn, name, note in items:
            p = base / fn
            if not p.exists():
                continue
            rows.append(
                f'<div class="item"><div class="row">'
                f'<button onclick="p(this)" data-src="{data_uri(p)}">▶ 播放</button>'
                f'<b>{html.escape(name)}</b></div>'
                f'<div class="note">{html.escape(note)}</div></div>'
            )
        blocks.append(f'<h2>{html.escape(title)}</h2><div class="list">{"".join(rows)}</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>《唯一》· 换成我的声音</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.6 system-ui, "Microsoft YaHei", sans-serif; max-width: 780px;
        margin: 0 auto; padding: 18px 16px 60px; }}
 h1 {{ font-size: 19px; margin: 0 0 6px; }}
 h2 {{ font-size: 15px; margin: 22px 0 8px; color: #2b6cb0; }}
 .sub {{ color: #666; font-size: 13px; white-space: pre-wrap; margin-bottom: 10px; }}
 .item {{ border: 1px solid #d8d8d8; border-radius: 10px; padding: 9px 11px; margin-bottom: 7px; }}
 .item.playing {{ border-color: #2b6cb0; box-shadow: 0 0 0 2px #2b6cb033; }}
 .row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
 button {{ font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid #bbb;
           background: #fff; cursor: pointer; }}
 .note {{ font-size: 13px; color: #555; margin-top: 4px; }}
</style></head><body>
<h1>《唯一》· 换成我的声音</h1>
<div class="sub">素材：告五人《唯一》（你自己 09-27 从 QQ 音乐扒的那首），271 秒
模型：<b>myvoice_tilt_t03</b>（本轮「训练集预加重」版 —— 高频比原版亮 4~6dB）
片段：第 85–130 秒（整首歌歌声最密的 45 秒）
口径：同一对分离轨、同一个 seed、同一段，<b>只有变调和有没有伴奏不同</b>

你的参考音中位 108.0Hz · 原唱人声中位 275.5Hz（p90 415Hz，最高 1188Hz）
⇒ 原唱比你说话音域高约 16 个半音，所以有「保原调 / 降八度」两版让你挑。

★ 逐帧核验过变调真的落地了：−12 档实测 −12.00 半音、0 档实测 +0.00 半音。</div>
{"".join(blocks)}
<script>
let cur = null;
function p(btn) {{
  const was = cur && !cur.paused && cur.dataset.src === btn.dataset.src;
  if (cur) {{ cur.pause(); document.querySelectorAll('.item.playing').forEach(e => e.classList.remove('playing')); }}
  if (was) {{ cur = null; return; }}
  const a = new Audio(btn.dataset.src);
  cur = a; a.dataset.src = btn.dataset.src;
  btn.closest('.item').classList.add('playing');
  a.play().catch(e => alert('浏览器拦了播放，再点一次：' + e.message));
  a.onended = () => {{ btn.closest('.item').classList.remove('playing'); }};
}}
{PROBE_HOOK}
</script></body></html>
"""


def main() -> int:
    out = BASE / "listen.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = render_html(BASE)
    n = doc.count("data-src=")
    out.write_text(doc, encoding="utf-8")
    print(f"→ {out}（{out.stat().st_size/1048576:.1f} MB，音频已内嵌）")
    print(f"  片段数：{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

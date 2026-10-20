"""生成「带宽 vs 音色解耦」的试听页。

五条一次性 RMS 对齐后编码，避免"谁更响"被误听成"谁更亮"。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_seedvc_probe as P  # noqa: E402

OUT = P.OUT
CLIPS = OUT / "clips2"
TARGET_RMS_DB = -20.0

#: (文件名, 标题, 副标题, 标签, CSS 类)
CLIPS_SPEC = [
    (
        "c_tail_source.wav",
        "源人声（赵雷，分离轨）",
        "44.1k · 8–12k −52.5",
        ["上限参照：真人这一段的高频"],
        "",
    ),
    (
        "c_tail_seedvc.wav",
        "① 你的参照（22k 原始）",
        "8–12k <b>−93.1</b> · CAM++ <b>0.428</b>",
        ["音色对，但没有高频"],
        "hi",
    ),
    (
        "e_你的参照BWE.wav",
        "② 你的参照先 BWE 再喂 Seed-VC",
        "8–12k <b>−68.1</b> · CAM++ <b>0.380</b>",
        ["音色保住了，高频回来了 +25.0dB", "但整体还偏暗 15.6dB"],
        "good",
    ),
    (
        "g_e_补高频.wav",
        "③ ②再补匹配 EQ",
        "8–12k <b>−54.2</b> · CAM++ <b>0.380</b>",
        ["高频到源的水准（源 −52.5）", "跟随性没退化：slope 0.46、气口 −21.4"],
        "good",
    ),
    (
        "c_tail_seedvc_fullband.wav",
        "④ 换别人的全带宽参照（上轮的 d）",
        "8–12k <b>−60.5</b> · CAM++ <b>0.059</b>",
        ["高频好，但<b>音色不是你了</b>（CAM++ 0.059）"],
        "bad",
    ),
]


def _rms_norm(y: np.ndarray, sr: int) -> np.ndarray:
    n, hop = int(0.064 * sr), int(0.010 * sr)
    lo = np.array(
        [
            np.sqrt(np.mean(np.asarray(y[s : s + n], dtype=np.float64) ** 2))
            for s in range(0, len(y) - n, hop)
        ]
    )
    act = lo[lo > np.percentile(lo, 50)]
    cur = 20 * np.log10(max(float(np.median(act)), 1e-12))
    return (np.asarray(y, dtype=np.float64) * 10 ** ((TARGET_RMS_DB - cur) / 20)).astype(np.float32)


def main() -> int:
    CLIPS.mkdir(parents=True, exist_ok=True)
    blocks = []
    for i, (fname, title, meta, tags, cls) in enumerate(CLIPS_SPEC):
        src = OUT / fname
        if not src.exists():
            raise SystemExit(f"缺产物：{src}")
        y, sr = P.M.read(src)
        wav = CLIPS / f"{i}.wav"
        sf.write(str(wav), _rms_norm(y, sr), sr, subtype="PCM_16")
        mp3 = CLIPS / f"{i}.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(wav),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(mp3),
            ],
            check=True,
        )
        tag_html = "".join(
            f'<span class="tag{" bad" if cls == "bad" else (" good" if cls == "good" else "")}">{t}</span>'
            for t in tags
        )
        blocks.append(f"""  <div class="clip">
    <div class="h"><span class="t">{title}</span><span class="m">{meta}</span></div>
    <audio controls preload="none" src="clips2/{i}.mp3"></audio>
    <div class="tags">{tag_html}</div>
  </div>""")
    body = "\n".join(blocks)
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>带宽和音色能不能拆开 — 参照音 BWE 实验</title>
<style>
  :root{{--bg:#0e1014;--card:#161a20;--card2:#1c2129;--line:#262c36;
        --tx:#e7eaf0;--dim:#8b94a3;--red:#ff5f56;--green:#3ecf8e;--amber:#f0b429;--blue:#5aa9ff}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--tx);
       font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:980px;margin:0 auto;padding:40px 22px 80px}}
  h1{{font-size:25px;margin:0 0 6px;letter-spacing:-.3px}}
  .sub{{color:var(--dim);font-size:13.5px;margin-bottom:26px}}
  h2{{font-size:17px;margin:36px 0 12px;padding-left:10px;border-left:3px solid var(--blue)}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}}
  .fix{{border-left:3px solid var(--green)}}
  .lead{{font-size:15.5px;line-height:1.75}}
  b.hl{{color:var(--red)}} b.ok{{color:var(--green)}} b.warn{{color:var(--amber)}}
  code{{background:#0b0d11;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
       font-size:12.5px;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px;margin:10px 0;
        font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace}}
  th,td{{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line)}}
  th:first-child,td:first-child{{text-align:left;font-family:inherit}}
  thead th{{color:var(--dim);font-weight:500;font-size:12px}}
  tr.good td{{background:rgba(62,207,142,.07)}}
  .clip{{background:var(--card2);border:1px solid var(--line);border-radius:9px;
        padding:13px 16px;margin:11px 0}}
  .clip .h{{display:flex;justify-content:space-between;align-items:baseline;
           margin-bottom:9px;flex-wrap:wrap;gap:6px}}
  .clip .h .t{{font-weight:600;font-size:14px}}
  .clip .h .m{{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}}
  audio{{width:100%;height:34px;filter:invert(.92) hue-rotate(180deg)}}
  .tags{{display:flex;gap:7px;flex-wrap:wrap;margin:6px 0 0}}
  .tag{{font-size:11.5px;color:var(--dim);border:1px solid var(--line);border-radius:20px;padding:1px 9px}}
  .tag.bad{{color:var(--red);border-color:rgba(255,95,86,.35)}}
  .tag.good{{color:var(--green);border-color:rgba(62,207,142,.35)}}
  ol{{padding-left:20px}} li{{margin:5px 0}}
  .foot{{color:var(--dim);font-size:12px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
  <h1>带宽和音色，能不能拆开？</h1>
  <div class="sub">《成都》285–305s（320Hz 高音区）· 同一段源、同一个 Seed-VC、30 步扩散 · 五条 RMS 对齐后编码</div>

  <div class="card fix"><div class="lead">
    <b class="ok">能。</b>上一轮发现"想要高频就得换人"——因为参照音同时决定了音色和带宽。
    这一轮把那两件事拆开了：<b>先用 BWE 把你自己的 22.05k 参照音补到 44.1k</b>，音色不变、带宽上去，
    再喂给 Seed-VC。<br><br>
    结果：8–12k 从 <b class="hl">−93.1 回到 −54.2</b>（源 −52.5），
    而声纹相似度 <b class="ok">CAM++ 0.380</b>（你自己的参照是 0.428，而"别人的全带宽"只有 <b class="hl">0.059</b>）。<br>
    <span style="color:var(--dim);font-size:13.5px">也就是说：<b>带宽是参照音的属性，不是音色的属性。</b>
    不用重录 30 分钟语料，也不用换成别人的声音。</span>
  </div></div>

  <h2>一、按这个顺序听</h2>
{body}

  <h2>二、数据</h2>
  <table>
    <thead><tr><th>版本</th><th>6–8k</th><th>8–12k</th><th>12–16k</th><th>r_env</th><th>slope</th><th>气口掉</th><th>CAM++</th></tr></thead>
    <tbody>
      <tr><td>源人声</td><td>−49.7</td><td>−52.5</td><td>−53.8</td><td>0.83</td><td>0.61</td><td>−18.5</td><td>—</td></tr>
      <tr><td>① 你的 22k 参照</td><td>−64.4</td><td>−93.1</td><td>−93.5</td><td>0.25</td><td>0.04</td><td>−0.1</td><td>0.428</td></tr>
      <tr class="good"><td>② 你的参照 BWE</td><td>−62.6</td><td>−68.1</td><td>−62.2</td><td>0.83</td><td>0.46</td><td>−21.7</td><td><b>0.380</b></td></tr>
      <tr class="good"><td>③ ②+匹配 EQ</td><td>−48.2</td><td><b>−54.2</b></td><td>−53.5</td><td>0.83</td><td>0.46</td><td>−21.4</td><td><b>0.380</b></td></tr>
      <tr><td>④ 别人的全带宽</td><td>−57.9</td><td>−60.5</td><td>−63.6</td><td>0.90</td><td>0.83</td><td>−28.1</td><td><b class="hl">0.059</b></td></tr>
    </tbody>
  </table>
  <div class="card"><div class="lead">
    三个读数：
    <ol>
      <li><b>③ 的高频是"内容"不是"底噪"。</b>补电平前后 <code>slope</code> 都是 0.46、气口都是 −21.5 左右 ——
          线性 EQ 只改电平，改不了"跟不跟嗓音"。对照 A 臂（产品链 <code>treble+6dB@7k</code> 抬起来的）
          slope 只有 0.11，那才是底噪。**抬噪声底不会让 slope 变好看，抬内容才会保持。**</li>
      <li><b>④ 的 CAM++ 0.059 才是问题。</b>上一轮我给的那个"高频好"的版本，其实是把别人的音色搬过来了 ——
          你听着不像自己是对的，不是错觉。</li>
      <li><b>③ 还差一点点</b>：slope 0.46 vs 源 0.61、气口 −21.4 vs −18.5。高频跟随性比 ④ 弱一档，
          因为 BWE 补出来的参照音本身没有源那么"活"。但音色是满的，这笔账划算。</li>
    </ol>
  </div></div>

  <h2>三、听的时候帮我判这三条</h2>
  <ol>
    <li><b>③ 和 ④ 比：哪个更像你？</b>数据说 ③（0.380 vs 0.059），但声纹指标对音色的敏感度有限，你耳朵为准。</li>
    <li><b>③ 的高频有没有"嘶"或"沙"？</b>数据说没有（气口掉 21.4dB、slope 0.46），如果有，这把尺子又漏了东西。</li>
    <li><b>③ 的 5–6k 还差 5.7dB</b>（补后 −51.7 vs 源 −46.0），因为增益被夹在 +18dB 上限。
        你听听这里是不是还有点"闷"，如果是，我把上限放开再补一档。</li>
  </ol>

  <div class="foot">
    参照音 BWE：<code>experiments/cover_seedvc_refbwe.py</code>（22.05k → 16k → UniverSR 48k → 44.1k）<br>
    匹配 EQ：<code>experiments/cover_hf_matcher.py</code>（按频带差算增益，对数频率插值，整段零相位 FFT）<br>
    判据：<code>experiments/cover_hf_tracking.py</code>（m1v 的三条：r_env / slope / 气口掉）<br>
    CAM++：<code>cover_metrics.secs()</code>，对用户原始 22.05k 参照算余弦，与其他臂同一把尺子。
  </div>
</div></body></html>
"""
    (OUT / "refbwe.html").write_text(page, encoding="utf-8")
    print(f"页面：{OUT / 'refbwe.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

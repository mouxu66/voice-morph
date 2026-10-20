"""「像童声」诊断页：音区 vs 音色。

沿用 refbwe 页的结构（RMS 对齐后编码、mp3 走 clips/ 相对引用、不内嵌 base64），
但结论完全换掉：这一页要说的是**源没选错人，选错了音区**。
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

from cover_refbwe_page import _rms_norm  # noqa: E402

CLIPS = "clips"
TARGET_RMS_DB = -20.0
#: 旧候选 vs 官方 MV 分离轨的 CAM++（实测 2026-10-20，见 cover_register.py 模块注释）
CAM_VS_OFFICIAL = 0.9866


def _encode(src: Path, dst_dir: Path, idx: int) -> None:
    import numpy as np

    y, sr = sf.read(str(src), dtype="float32", always_2d=True)
    y = _rms_norm(y.mean(axis=1).astype(np.float32), sr)
    wav = dst_dir / f"{idx}.wav"
    sf.write(str(wav), np.asarray(y), sr, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-b:a", "192k", str(dst_dir / f"{idx}.mp3")],
        check=True,
    )


def _clip(idx: int, title: str, meta: str, tags: list[str], cls: str) -> str:
    tag_html = "".join(
        f'<span class="tag{" bad" if cls == "bad" else (" good" if cls == "good" else "")}">{t}</span>'
        for t in tags
    )
    return f"""  <div class="clip">
    <div class="h"><span class="t">{title}</span><span class="m">{meta}</span></div>
    <audio controls preload="none" src="{CLIPS}/{idx}.mp3"></audio>
    <div class="tags">{tag_html}</div>
  </div>"""


def _row(r: dict) -> str:
    def f(v, fmt):
        return "—" if v is None else format(v, fmt)

    # 行底色直接跟手臂类别走：A4 是“对照/音色跑掉”，不能因为“good”以外的标签就洗成绿
    row_cls = {"good": "good", "bad": "bad", "warn": "warn"}.get(str(r.get("cls")), "")
    cls = f' class="{row_cls}"' if row_cls else ""
    # data-l 是窄屏堆叠布局的字段名（≤560px 时表头不显示，靠它给每个值标名）
    return (f"<tr{cls}><td class=\"nm\" data-l=\"版本\">{r['name']}</td>"
            f"<td data-l=\"基频\">{f(r.get('f0'), '.0f')}</td>"
            f"<td data-l=\"8–12k\">{f(r.get('hf_db'), '.1f')}</td>"
            f"<td data-l=\"r_env\">{f(r.get('r_env'), '.2f')}</td>"
            f"<td data-l=\"slope\">{f(r.get('slope'), '.2f')}</td>"
            f"<td data-l=\"气口掉\">{f(r.get('gap_drop'), '.1f')}</td>"
            f"<td data-l=\"CAM++\">{f(r.get('secs_cos'), '.3f')}</td>"
            f"<td data-l=\"判读\">{r.get('verdict', '—')}</td></tr>")


def build(out: Path, meta: dict) -> Path:
    clips = out / CLIPS
    clips.mkdir(parents=True, exist_ok=True)

    src = Path(meta["src"])
    rows = meta["rows"]
    shift = int(meta["shift"])
    idx = 0
    blocks = []
    _encode(src, clips, idx)
    blocks.append(_clip(idx, "源人声（赵雷官方 MV，分离轨，原调）",
                        f"中位 <b>{meta['src_f0']:.0f}Hz</b> · 8–12k {rows[0]['hf_db']:.1f}",
                        ["这是赵雷本人（CAM++ 0.987 对官方 MV）"], ""))
    idx += 1
    for r in rows[1:]:
        _encode(out / f"{r['name']}.wav", clips, idx)
        tags = {
            "bad": ["音区 +19 半音 → 童声/电音的来源"],
            "good": [f"音区对上了（{r['f0']:.0f}Hz）", "音色是你的"],
            "warn": ["音色跑掉（对照）"],
        }.get(r.get("cls", ""), [])
        cos = r["secs_cos"]
        meta_txt = (f"基频 <b>{r['f0']:.0f}Hz</b> · 8–12k <b>{r['hf_db']:.1f}</b> · "
                    f"CAM++ <b>{cos:.3f}</b>" if isinstance(cos, float) else
                    f"基频 <b>{r['f0']:.0f}Hz</b> · 8–12k <b>{r['hf_db']:.1f}</b>")
        blocks.append(_clip(idx, r["title"], meta_txt, tags, r.get("cls", "")))
        idx += 1

    body = "\n".join(blocks)
    table = "\n".join(_row(r) for r in rows)

    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>像童声的是音区，不是源</title>
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
  .warn{{border-left:3px solid var(--amber)}}
  .lead{{font-size:15.5px;line-height:1.75}}
  b.hl{{color:var(--red)}} b.ok{{color:var(--green)}} b.warn{{color:var(--amber)}}
  code{{background:#0b0d11;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
       font-size:12.5px;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace}}
  .tablewrap{{overflow-x:auto;margin:10px 0}}
  table{{width:100%;min-width:820px;border-collapse:collapse;font-size:13px;
        font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace}}
  th,td{{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}}
  th:first-child,td:first-child{{text-align:left;font-family:inherit}}
  td:last-child{{white-space:normal;min-width:120px}}
  /* 窄面板（预览侧栏 ~310–560px）：表格改成一行一卡，字段名走 data-l。
     固定横滚在这么窄的栏里等于"只能看到两列"，用户不会去拖。 */
  @media (max-width:560px){{
    .tablewrap{{overflow:visible}}
    table{{min-width:0;display:block;font-size:12.5px}}
    thead{{display:none}}
    tbody,tr,td{{display:block}}
    tr{{border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:7px 11px}}
    tr.good{{border-color:rgba(62,207,142,.35)}}
    tr.bad{{border-color:rgba(255,95,86,.35)}}
    tr.warn{{border-color:rgba(240,180,41,.35)}}
    tr.good td,tr.bad td,tr.warn td{{background:none}}
    td{{border:0;padding:1.5px 0;display:flex;justify-content:space-between;
        gap:14px;white-space:normal;text-align:right}}
    td::before{{content:attr(data-l);color:var(--dim);font-size:11px;font-family:inherit}}
    td.nm{{font-weight:600;margin-bottom:3px}}
    td.nm::before{{display:none}}
  }}
  th:last-child,td:last-child{{text-align:left;font-family:inherit;font-size:12px;color:var(--dim)}}
  thead th{{color:var(--dim);font-weight:500;font-size:11.5px}}
  tr.good td{{background:rgba(62,207,142,.07)}}
  tr.bad td{{background:rgba(255,95,86,.06)}}
  tr.warn td{{background:rgba(240,180,41,.06)}}
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
  ol{{padding-left:20px}} li{{margin:6px 0}}
  .foot{{color:var(--dim);font-size:12px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
  <h1>像童声的是「音区」，不是「源」</h1>
  <div class="sub">《成都》最后一段副歌 · 同一段源、同一个 Seed-VC、30 步 · 六条 RMS 对齐后编码</div>

  <div class="card warn"><div class="lead">
    <b class="warn">先纠正一件事：源确实是赵雷，不是童声。</b><br>
    旧候选与官方 MV（太合音乐）分离轨的 <b>CAM++ = {CAM_VS_OFFICIAL:.4f}</b> —— 同一个人、几乎同一版录音；
    长时倒谱包络的 F1/F2 也几乎一致（453/1109 vs 438/1109Hz）。童声翻唱不可能过 0.5。<br><br>
    <b class="hl">但你的耳朵没错：出来的东西确实像童声。</b>原因不是源换过人，是
    <b>我把最不适合你的那 20 秒挑了出来</b> —— 最后一段副歌的最高潮，
    中位音高 <b>{meta['src_f0']:.0f}Hz</b>；而你自己的参照音中位是
    <b>{meta['ref_median']:.0f}Hz</b>（p10 102 / p90 {meta['ref_p90']:.0f}）。
    Seed-VC 开着 <code>--f0-condition True</code>，<b>音高只跟源走</b> ——
    于是它用你的音色去唱一个 <b>{meta['src_f0'] / meta['ref_median']:.2f} 倍基频</b>
    （{12 * math.log2(meta['src_f0'] / meta['ref_median']):+.0f} 半音）的假声，
    那就是"童声"，也就是"夹着嗓子"，扩散模型在这个音区还会明显失真成"电音"。
  </div></div>

  <div class="card fix"><div class="lead">
    <b class="ok">修法：把源降到你的音区，音色一点都不动。</b>
    这一段降 <b>{abs(shift)} 半音</b>（{meta['src_f0']:.0f}Hz → {meta['src_f0'] * 2 ** (shift / 12):.0f}Hz），
    落到你自己 p90 的位置 —— 唱得上去、又不挤。<br>
    <span style="color:var(--dim);font-size:13.5px">
    这不是"换个伴奏调"，也不是重录：<b>音区（源决定的）和音色（参照决定的）本来就是两个旋钮</b>。
    上一页证明了带宽和音色可以拆开；这一页证明音区和音色也可以。</span>
  </div></div>

  <h2>一、按这个顺序听</h2>
{body}

  <h2>二、数据</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>版本</th><th>基频</th><th>8–12k</th><th>r_env</th><th>slope</th><th>气口掉</th><th>CAM++</th><th>判读</th></tr></thead>
    <tbody>
{table}
    </tbody>
  </table></div>
  <div class="card"><div class="lead">
    <ol>
      <li><b>先听 A0 和 A1 的差别。</b>两条只差一个 <code>--semi-tone-shift</code>，
          参照音、源、步数全一样。A0 就是你上一页听到的那条（{rows[1]["f0"]:.0f}Hz）。
          <b>如果 A1 听起来明显"像你"，那"童声"这件事就到此为止。</b></li>
      <li><b>A1 vs A2/A3：高频值不值这 0.08 的 CAM++。</b>BWE 把参照音补到 44.1k 再喂，
          8–12k 会往上走（−110 → −77)；代价是音色相似度掉 0.08（0.672 → 0.594）。</li>
      <li><b>A2 和 A3 的差别只有一层匹配 EQ</b>（上限 +12dB，不是上一轮的 +18dB ——
          在这段 +18 会把 slope 从 0.38 压到 0.29，正好掍成“噪声底”）。</li>
      <li><b>A4 是对照，不是候选。</b>别人的全带宽参照，高频最好、音色跑掉 —— 上一轮的结论，别选它。</li>
    </ol>
  </div></div>

  <h2>三、听的时候帮我判这三条</h2>
  <ol>
    <li><b>A1 还像不像童声？</b>（数据：基频 {rows[1]["f0"]:.0f}→{rows[2]["f0"]:.0f}Hz）</li>
    <li><b>A1 的电音还剩多少？</b>如果降调后还明显电，那问题不在音区，得回头查扩散步数/参照音质量。</li>
    <li><b>A1 和 A0 哪个"闷"？</b>降调后高频会跟着走低（谐波位置整体下移），如果 A1 明显闷，
        就说明需要把 BWE 那层（A2/A3）一起上，而不是只降调。</li>
  </ol>

  <div class="foot">
    定调：<code>experiments/cover_register.py</code>（窗口按音区自动挑，目标 = 你参照音的 p90）<br>
    降调：Seed-VC 的 <code>--semi-tone-shift</code>（{shift:+d}），不重新合成、不改音色<br>
    BWE / 匹配 EQ：<code>experiments/cover_seedvc_refbwe.py</code> / <code>cover_hf_matcher.py</code><br>
    源：官方 MV <code>youtube.com/watch?v=EPyFeQcDSUY</code>（太合音乐），demucs htdemucs 分离
  </div>
</div></body></html>
"""
    dst = out / "register.html"
    dst.write_text(page, encoding="utf-8")
    print(f"\n页面：{dst}")
    return dst


if __name__ == "__main__":
    raise SystemExit("由 cover_register.py 调用")

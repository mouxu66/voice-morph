"""「不像赵雷」诊断页：问题出在**挑窗**，不在源、也不在音区。

沿用 refbwe 页的结构（RMS 对齐后编码、mp3 走 clips/ 相对引用、不内嵌 base64）。

这一页替掉的是 m1v 那版结论（"源就是赵雷，错的是音区 −15"）。
那版的核心证据 CAM++ 0.9866 有两个毛病，都必须在页面上写清楚：
  ① 它只证明"两份下载是同一条录音"，**不证明是谁**（我自己把一个视频标成"官方 MV"
     当金标准，再拿它和被怀疑的文件互相印证 = 循环论证）；
  ② 它是在**整曲**上量的，而我喂给模型的那 20 秒根本不在这段里 —— 量错了地方。
真正的根因见 `cover_register.pick_window` 的注释：旧窗口落在歌曲结束后 50 多秒的
衰减尾巴上（−31 ~ −36dBFS），pyin 在噪声帧上吐出 330–345Hz 的八度跳，
于是"中位音高最高"的窗口正好选中了一段**没人唱**的地方。
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
    src_f0 = float(meta["src_f0"])
    src_db = float(meta.get("src_db", float("nan")))
    ref_med = float(meta["ref_median"])
    ref_p90 = float(meta["ref_p90"])
    start = float(meta.get("start", 0.0))
    dur = float(meta.get("dur", 20.0))
    win_vs_loud = meta.get("win_vs_loud")
    loud_start = float(meta.get("loud_start", 0.0))
    self_check = (
        f"自检：新窗 vs 全曲最响段（{loud_start:.0f}s）的 CAM++ = <b>{win_vs_loud:.3f}</b>"
        f"（同一个人；不同成男基线 0.39–0.47）" if isinstance(win_vs_loud, float) else ""
    )
    # A0 = 原调：比参照音高多少半音，这个数就是"你被顶到哪儿"
    gap_st = 12.0 * math.log2(src_f0 / ref_med)

    idx = 0
    blocks = []
    _encode(src, clips, idx)
    blocks.append(_clip(idx, "源人声（正段落，原调）",
                        f"{start:.0f}–{start + dur:.0f}s · 中位 <b>{src_f0:.0f}Hz</b> · "
                        f"电平 {src_db:.1f}dBFS · 8–12k {rows[0]['hf_db']:.1f}",
                        ["这次是真的在唱（不是尾巴）"], ""))
    idx += 1
    for r in rows[1:]:
        _encode(out / f"{r['name']}.wav", clips, idx)
        tags = {
            "bad": [f"原调：比你参照高 {gap_st:+.0f} 半音 → 上一页就是这么来的"],
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
<title>「不像赵雷」是我挑错了那 20 秒</title>
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
  <h1>「不像赵雷」是我挑错了那 20 秒</h1>
  <div class="sub">曲目 5:10 就唱完了，视频尾巴还有 50 多秒衰减 —— 我上一页挑的正是那段 ·
    同一个 Seed-VC、30 步 · 六条 RMS 对齐后编码</div>

  <div class="card warn"><div class="lead">
    <b class="warn">先撤回上一页那句「源就是赵雷」。</b>它靠的是 CAM++ 0.9866，而那个数有两个毛病：<br>
    ① 它只证明<b>两份下载是同一条录音</b>，不证明<b>是谁</b> —— 我把自己标成"官方 MV"的视频
       当金标准，再拿它和被怀疑的文件互相印证，是<b class="hl">循环论证</b>；<br>
    ② 它是在<b>整曲</b>上量的，而我喂给模型的那 20 秒<b class="hl">根本不在这段里</b> —— 量错了地方。<br><br>
    <b class="hl">你的耳朵是对的：上一页的"源"不是一个人在唱。</b>
    这首歌正段落在 <b>{src_f0:.0f}Hz</b>、电平 <b>{src_db:.1f}dBFS</b>；
    而视频在 5:10 左右就唱完了，往后 50 多秒是衰减，
    demucs 人声轨只剩 <b>−31 ~ −36dBFS</b> —— pyin 在这些噪声帧上吐出 <b>330–345Hz</b> 的八度跳。
    旧窗口（313.5–333.5s / 345Hz）就落在这里。所以：<b>素材是一段没人唱的地方</b>，
    Seed-VC 拿近静音去合成，听到的"童声 + 电"其实是噪声被放大后的产物。
  </div></div>

  <div class="card fix"><div class="lead">
    <b class="ok">段内立证（同一份文件，不用知道谁是谁）：</b>
    正文段（180–200s）中位 <b>172Hz</b>、尾巴段（275–295s）<b>339Hz</b>，
    两段的 CAM++ 只有 <b>0.18–0.30</b> —— 比"两个不同成男"的 0.39–0.47 <b>还低</b>。
    也就是说尾巴那段连"另一个人"都算不上，它是残响。<br><br>
    <b class="ok">修法：挑窗加电平门限</b>（窗口里最安静的 20% 帧必须还在全场响帧 20dB 以内）。
    现在挑到 <b>{start:.0f}–{start + dur:.0f}s</b>、<b>{src_f0:.0f}Hz</b>、<b>{src_db:.1f}dBFS</b>；
    只需要降 <b>{abs(shift)} 半音</b>（{src_f0:.0f}Hz → {src_f0 * 2 ** (shift / 12):.0f}Hz，落在你 p90
    {ref_p90:.0f}Hz 上），不再是上一页那个 −15 的极端移调。<br>
    {self_check}
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
      <li><b>先听"源"这一条。</b>它应该是成男本色演唱（{src_f0:.0f}Hz），
          不是上一页那个 345Hz 的尖声。<b>如果你还是觉得不像，就直说 ——
          那就说明这条下载本身就不对，该换素材，而不是继续调参数。</b></li>
      <li><b>再听 A0 vs A1。</b>两条只差一个 <code>--semi-tone-shift</code>（0 vs {shift:+d}），
          参照、源、步数全一样。A0 仍比你参照高 {gap_st:+.0f} 半音。<b>如果 A1 明显没电了，
          功率就全在"音区"这一个旋钮上。</b></li>
      <li><b>A1 vs A2/A3</b>：BWE 把参照音补到 44.1k 再喂，高频会往上走，代价是音色相似度小掉一点。</li>
      <li><b>A4 是对照，不是候选。</b>别人的全带宽参照，高频最好、音色跑掉。</li>
    </ol>
  </div></div>

  <h2>三、听的时候帮我判这三条</h2>
  <ol>
    <li><b>"源"这条还像不像童声/女声？</b>（数据：{src_f0:.0f}Hz，{src_db:.1f}dBFS）</li>
    <li><b>A1 的电还剩多少？</b>如果降 {abs(shift)} 半音后还明显电，那就不是音区问题，
        要回头查扩散步数与参照音质量。</li>
    <li><b>A1 和 A0 哪个闷？</b>如果 A1 明显闷，就说明需要把 BWE 那层（A2/A3）一起上。</li>
  </ol>

  <div class="foot">
    挑窗 + 定调：<code>experiments/cover_register.py</code>
    （电平门限 + 按音区自动挑，目标 = 你参照音的 p90）<br>
    身份核验：<code>experiments/cover_identity_check.py</code>（逐 30s 音高表 + CAM++ 矩阵）<br>
    降调：Seed-VC 的 <code>--semi-tone-shift</code>（{shift:+d}），不重新合成、不改音色<br>
    BWE / 匹配 EQ：<code>experiments/cover_seedvc_refbwe.py</code> / <code>cover_hf_matcher.py</code><br>
    源：demucs htdemucs 分离轨，窗口 {start:.1f}–{start + dur:.1f}s
  </div>
</div></body></html>
"""
    dst = out / "register.html"
    dst.write_text(page, encoding="utf-8")
    print(f"\n页面：{dst}")
    return dst


if __name__ == "__main__":
    raise SystemExit("由 cover_register.py 调用")

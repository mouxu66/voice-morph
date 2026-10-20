"""「根本不像我的声音」：同一段源，RVC（训过的音色）vs Seed-VC（9 秒零样本）。

用户 2026-10-20 深夜的第三次裁决
    「我问了很多人，他说根本都不像我的声音哦」

这一条和 m1v / m1x 的坑不是同一类，它更接近"选错了工具"：

    此前所有臂的"目标音色"都来自 **Seed-VC 零样本**，参照音是
    `media/voicebank/myvoice_supp2026/reference.wav` —— **8.88 秒、22.05k、
    PPT 演讲的现场收音**（`meta.json`: kind=rvc_model, sources=ppt12_0053.wav）。
    它给的其实是"那支麦 + 那间会议室 + 念稿时的音色"。

    而项目里**本来就有训过的音色**：`D:/RVC/logs/myvoice_*/G_2333333.pth` +
    index。其中 `myvoice_singdom` 就是"唱歌域过采样重训"（m1p）的产物，
    `myvoice_ppt12_full` 是 12.23 分钟说话素材训的现役音色。
    RVC 只换音色、照抄 F0 曲线 —— 这正是翻唱该用的东西。

    所以这一轮要分开两件事：
      ① 模型路线（RVC 训过 vs Seed-VC 零样本）；
      ② 参照音质量（8.9 秒 PPT 收音 vs 他的真唱）。
    如果 RVC 那两条明显更"像他"，那前面几轮的结论（-15/-5 半音、BWE、匹配 EQ）
    全都是在**一个注定不像的参照音**上做微调 —— 那些数字不算错，但修的不是这个问题。

★ 判断方式换成盲听 + 锚点
    把**他本人的真唱**单独放在最上面当锚，下面 5 条不给名字、不给指标，
    顺序用固定种子打乱（映射只写进 `voice_ab.json`，不进 HTML）。
    「像不像我」本来就是相对判断：没有锚点，听的人只能拿记忆里的"他说话"去比，
    而唱歌和说话不是一个音区 —— 这也是"很多人说不像"的一部分原因。

用法
    .venv/Scripts/python.exe experiments/cover_voice_ab.py            # 转换 + 度量 + 页面
    .venv/Scripts/python.exe experiments/cover_voice_ab.py --measure  # 只度量和出页面
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
sys.path.insert(0, str(ROOT / "m2_server"))

import cover_metrics as MET  # noqa: E402
import cover_register as REG  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402
from cover_refbwe_page import _rms_norm  # noqa: E402

OUT = ROOT / "media" / "covers" / "midsong_20261018" / "voice_ab"
SRC = ROOT / "media" / "covers" / "midsong_20261018" / "register" / "src.wav"
REG_DIR = ROOT / "media" / "covers" / "midsong_20261018" / "register"
SPEECH_REF = ROOT / "media" / "voicebank" / "myvoice_supp2026" / "reference.wav"
#: 他本人的真唱（多段里最长的一段；锚点就用它）
MY_SING = ROOT / "media" / "raw_recordings" / "2026-10-supp" / "sing" / "唱歌.m4a"

#: (key, RVC 实验名, 变调半音) —— 变调 0 表示照抄源的 F0 曲线
RVC_ARMS = [
    ("singdom_p0", "myvoice_singdom", 0),
    ("ppt12full_p0", "myvoice_ppt12_full", 0),
    ("singdom_m5", "myvoice_singdom", -5),
]
#: 上一轮 Seed-VC 的臂直接拿来同台比（不再重跑模型）
SEEDVC_ARMS = [
    ("seedvc_A1", REG_DIR / "A1_降调_你的参照.wav"),
    ("seedvc_A2", REG_DIR / "A2_降调_你的参照BWE.wav"),
]

SEED = 20261020
#: 锚点窗口：目标音高（源那段的中位 195Hz），用来挑"同一音区"的真唱，避免拿低音段当真唱锚
ANCHOR_TARGET_F0 = 195.0
ANCHOR_DUR = 20.0


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float32), int(sr)


def decode(src: Path, dst: Path) -> Path:
    """任意容器 → 44.1k 单声道 wav（RVC 与度量都只吃 wav）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-ar", "44100", "-ac", "1", str(dst)],
        check=True, timeout=600,
    )
    return dst


def pick_anchor(y: np.ndarray, sr: int, dur: float = ANCHOR_DUR,
                target: float = ANCHOR_TARGET_F0,
                scan_from: float = 0.0) -> tuple[float, float]:
    """在他自己的真唱里挑一段「音区接近源那段」的窗口，返回 (起点, 该窗中位 f0)。

    不挑"最高"也不挑"最响"，挑**音区最接近**：锚点必须在同一个音区，
    否则听的人会拿低八度的真唱去比 195Hz 的成品，"不像"是先写好的结论。
    """
    f0, fps = REG.f0_curve(y, sr)
    rms = REG.rms_curve(y, sr)
    n = min(len(f0), len(rms))
    f0, rms = f0[:n], rms[:n]
    win, step = int(dur * fps), max(1, int(0.5 * fps))
    fdb = 20.0 * np.log10(rms + 1e-12)
    gate = float(np.percentile(fdb[int(scan_from * fps):], 90)) - 20.0
    best, best_cost = float(scan_from), 1e9
    for i in range(int(scan_from * fps), max(1, n - win), step):
        if float(np.percentile(fdb[i:i + win], 20)) < gate:
            continue
        v = f0[i:i + win]
        v = v[np.isfinite(v) & (v > 70) & (v < 480)]
        if v.size < win * 0.4:
            continue
        med = float(np.median(v))
        cost = abs(12.0 * np.log2(max(med, 1.0) / target))   # 半音距离
        if cost < best_cost:
            best, best_cost = i / fps, cost
    return best, target if best_cost > 1e8 else float(best_cost)


def rvc_arm(key: str, voice: str, pitch: int, src: Path) -> Path:
    """跑一条 RVC。已存在且比源新就复用（一次 ~60s，别重复跑）。"""
    dst = OUT / f"{key}.wav"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    import rvc_convert

    got = Path(rvc_convert.rvc_convert(src, voice, pitch=pitch, index_rate=0.5))
    shutil.copy2(got, dst)
    return dst


def measure(path: Path, anchor: Path) -> dict:
    y, sr = read(path)
    row = {
        "f0": REG.median_f0(path),
        "hf_db": round(float(P.analyze(y, sr)["bands"]["8000-12000"]), 1),
        # ★ 这一列才是人耳问的那件事：像不像**他**（锚 = 他的真唱）
        "cam_anchor": MET.secs(path, anchor),
        # 旧口径留一列，方便看出"对 8.9 秒 PPT 收音"和"对他的真唱"能不能拉开
        "cam_speech_ref": MET.secs(path, SPEECH_REF),
    }
    return row


def _encode(src: Path, dst_dir: Path, idx: int) -> None:
    y, sr = read(src)
    y = _rms_norm(y, sr)
    wav = dst_dir / f"{idx}.wav"
    sf.write(str(wav), np.asarray(y), sr, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-b:a", "192k", str(dst_dir / f"{idx}.mp3")],
        check=True, timeout=300,
    )


LABELS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]


def build_page(rows: list[dict], anchor: Path, key: dict, out: Path | None = None) -> Path:
    """出盲听页。`out` 可传临时目录（测试用）—— 否则测试会盖掉真页面。"""
    out = out or OUT
    clips = out / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    _encode(anchor, clips, 0)

    blocks = []
    for i, r in enumerate(rows, start=1):
        _encode(out / f"{r['key']}.wav", clips, i)
        blocks.append(f"""  <div class="clip">
    <div class="h"><span class="t">{LABELS[i - 1]}</span></div>
    <audio controls preload="none" src="clips/{i}.mp3"></audio>
  </div>""")

    table = "\n".join(
        f"<tr><td class='nm'>{LABELS[i]}</td><td>{r['f0']:.0f}</td>"
        f"<td>{r['hf_db']:.1f}</td><td>{_fmt(r['cam_anchor'])}</td>"
        f"<td>{_fmt(r['cam_speech_ref'])}</td></tr>"
        for i, r in enumerate(rows)
    )

    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>哪一条像我把这首歌唱的</title>
<style>
  :root{{--bg:#0e1014;--card:#161a20;--card2:#1c2129;--line:#262c36;
        --tx:#e7eaf0;--dim:#8b94a3;--blue:#5aa9ff;--green:#3ecf8e}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--tx);
       font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:900px;margin:0 auto;padding:38px 20px 70px}}
  h1{{font-size:24px;margin:0 0 6px;letter-spacing:-.3px}}
  .sub{{color:var(--dim);font-size:13.5px;margin-bottom:22px}}
  h2{{font-size:17px;margin:32px 0 10px;padding-left:10px;border-left:3px solid var(--blue)}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}}
  .lead{{font-size:15px;line-height:1.72}}
  code{{background:#0b0d11;border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:12.5px}}
  .anchor{{border-left:3px solid var(--green)}}
  .clip{{background:var(--card2);border:1px solid var(--line);border-radius:9px;padding:12px 15px;margin:10px 0}}
  .clip .h{{margin-bottom:8px;font-weight:600;font-size:15px}}
  audio{{width:100%;height:34px;filter:invert(.92) hue-rotate(180deg)}}
  table{{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;
        font-family:ui-monospace,Consolas,monospace}}
  th,td{{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line)}}
  th:first-child,td:first-child{{text-align:left}}
  thead th{{color:var(--dim);font-weight:500;font-size:11.5px}}
  .foot{{color:var(--dim);font-size:12px;margin-top:30px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body><div class="wrap">
  <h1>哪一条像「我」把这首歌唱的</h1>
  <div class="sub">同一段源（{key['src']['window']}，中位 {key['src']['f0']:.0f}Hz）· 五条盲听 · 顺序已打乱</div>

  <div class="card anchor"><div class="lead">
    <b>先听这条锚：这是我本人在唱歌。</b>（不是翻唱成品，也不是合成的）<br>
    <audio controls preload="none" src="clips/0.mp3"></audio><br>
    <span style="color:var(--dim);font-size:13px">
    拿它当尺子，再往下听甲乙丙丁戊。<b>不要拿你记忆里我说话的声音去比</b> ——
    唱歌和说话不是一个音区，这也是"很多人说不像"的一半原因。</span>
  </div></div>

  <h2>盲听五条（顺序打乱，我不知道你会先听哪条）</h2>
{"".join(blocks)}

  <div class="card"><div class="lead">
    <b>请回答两件事</b><br>
    ① 哪几条<b>最像锚点那个人</b>？（可以选多条，也可以说"都不像"）<br>
    ② 哪几条<b>最不像</b>，最不像的是哪里不像：音色 / 音区 / 咬字 / 电。
  </div></div>

  <h2>数据（听完再看）</h2>
  <table>
    <thead><tr><th>条目</th><th>基频</th><th>8–12k</th>
      <th>像锚点(CAM++)</th><th>像 8.9s 收音(CAM++)</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
  <div class="card"><div class="lead">
    两列 CAM++ 是这件事的关键对照：<b>「像他的真唱」与「像那段 PPT 收音」如果排序相反</b>，
    就说明前几轮一直在优化一个错误的相似度。CAM++ 是语音尺子、在歌声上会饱和
    （不同成男天生就有 0.39–0.47），所以它只用于排序，绝对值别读。
  </div></div>

  <div class="foot">产物：<code>experiments/cover_voice_ab.py</code> ·
    盲听映射：<code>voice_ab.json</code>（<b>页面源码里不出现</b> —— 否则按 F12 就揭宝了）</div>
</div>
</body></html>
"""
    dst = out / "voice_ab.html"
    dst.write_text(page, encoding="utf-8")
    return dst


def _fmt(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过 RVC 转换，只度量 + 出页面")
    ap.add_argument("--page-only", action="store_true",
                    help="按已有 voice_ab.json 重出页面（改文案/样式时不重跑 CAM++）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.page_only:
        meta = json.loads((OUT / "voice_ab.json").read_text(encoding="utf-8"))
        rows = [dict(meta["rows"][meta["map"][lb]], key=meta["map"][lb])
                for lb in LABELS if lb in meta["map"]]
        page = build_page(rows, OUT / "anchor_my_sing.wav", meta, OUT)
        print(f"页面：{page}")
        return 0
    if not SRC.exists():
        raise SystemExit(f"缺源切片：{SRC}（先跑 experiments/cover_register.py）")

    # ── ① 锚点：他本人的真唱，音区最接近源那段的那 20 秒 ────────────────
    sing_wav = decode(MY_SING, OUT / "_my_sing_44k.wav")
    sy, ssr = read(sing_wav)
    start, _cost = pick_anchor(sy, ssr)
    anchor = OUT / "anchor_my_sing.wav"
    if not anchor.exists() or anchor.stat().st_mtime < sing_wav.stat().st_mtime:
        a, b = int(start * ssr), int((start + ANCHOR_DUR) * ssr)
        sf.write(str(anchor), sy[a:b], ssr, subtype="PCM_16")
    print(f"锚点：我的真唱 {start:.1f}–{start + ANCHOR_DUR:.1f}s  "
          f"中位 {REG.median_f0(anchor)}Hz")

    # ── ② RVC 臂 ──────────────────────────────────────────────────────
    for key, voice, pitch in RVC_ARMS:
        if args.measure and (OUT / f"{key}.wav").exists():
            continue
        print(f"  · RVC {voice} pitch {pitch:+d} …", end="", flush=True)
        rvc_arm(key, voice, pitch, SRC)
        print(" ok")
    try:
        import rvc_convert

        rvc_convert.stop_worker()
    except Exception:  # noqa: BLE001 —— worker 没起也不要紧
        pass

    # ── ③ Seed-VC 臂复制进来（同台比，不重跑模型）────────────────────────
    for key, src in SEEDVC_ARMS:
        dst = OUT / f"{key}.wav"
        if src.exists() and (not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime):
            shutil.copy2(src, dst)

    keys = [k for k, _v, _p in RVC_ARMS] + [k for k, _p in SEEDVC_ARMS if (OUT / f"{k}.wav").exists()]

    # ── ④ 度量 ────────────────────────────────────────────────────────
    rows = []
    for k in keys:
        r = measure(OUT / f"{k}.wav", anchor)
        r["key"] = k
        rows.append(r)
        print(f"{k:<16}{r['f0']:>6.0f}Hz  hf {r['hf_db']:>7.1f}  "
              f"像锚 {_fmt(r['cam_anchor'])}  像收音 {_fmt(r['cam_speech_ref'])}")

    # ── ⑤ 打乱顺序出页面（映射只进 json）────────────────────────────────
    order = list(range(len(rows)))
    random.Random(SEED).shuffle(order)
    shuffled = [rows[i] for i in order]
    key = {
        "src": {"window": f"{REG.SEG_DUR:.0f}s 切片", "f0": REG.median_f0(SRC)},
        "seed": SEED,
        "map": {LABELS[i]: r["key"] for i, r in enumerate(shuffled)},
        "rows": {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in rows},
    }
    page = build_page(shuffled, anchor, key, OUT)
    (OUT / "voice_ab.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(f"\n页面：{page}\n映射：{OUT / 'voice_ab.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

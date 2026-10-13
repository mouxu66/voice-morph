# -*- coding: utf-8 -*-
"""听测页生成器：把发放包变成「点一下就能打分」的离线网页。

为什么需要这个模块
    发放包已经把盲法补上了，但**收集端还是手工的**：听者要照着 `playlist.txt`
    一个一个点开 wav、再在 CSV 里手打 48 行数字。这个环节的失败方式很隐蔽 ——
    听者会**错行**（听第 N 条打了第 N-1 行的分）、会漏填、会把 5 打成 6，
    而 `merge-listen` 只能告诉你"有非法值"，说不清是哪一条听错了。

    所以把「播放 + 打分 + 导出」三件事放进同一个页面：**当前在放哪一条，
    就只能给哪一条打分**，错行在结构上不可能发生。

★ 调研到的一条硬约束（决定了页面长什么样）
    浏览器的自动播放策略要求**先有用户手势**才允许出声
    （Chrome autoplay policy / MDN Web Audio best practices）——
    所以页面**不自动连播**：每条一个播放按钮，点一下放一条。
    这同时符合评测习惯：听者必须主动决定"这条听完了"。

★ 两条研究口径，写进页面里，不许悄悄改
    1. **不做响度归一化**：音频是逐字节内嵌的（`--embed` 模式会对 sha256 逐条核对），
       页面不以任何方式改音量。给刺激做归一化会改变被测对象本身，
       客观指标与主观分就对不上了。
    2. **同一副耳机、音量固定**：页面只提示，不做校验 —— 但提示必须写在
       听者看得见的地方（首页第一条），否则这条要求现场就丢了。

★ 与 `web/public/listen.html` 的区别（别搞混）
    那个是**产品向**的盲听（2–10 条，扫码排名，试用音间选音色）。
    这个是**研究向**的：48 条固定顺序、每人都听同一套、导出 `blind_id,mos,note`
    直接喂 `merge-listen`。两者共用"盲号"这个思想，但目标与规模都不同。

用法
    python tools/listen_page.py --exp pilotC --pack D:/听测包 --out D:/听测包
    python tools/listen_page.py --exp pilotC --pack D:/听测包 --out D:/听测包 --embed

产出（`<out>/` 下）
    听测页.html      双击即用（`--embed` 时音频内嵌为单文件，可整体转发给听者）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import voicebank_pack as vp  # noqa: E402  （复用泄题判据与评分表列定义）

#: 页面文件名（中文名对听者更友好；同时也是"不覆盖"守卫的判据）
PAGE_FILE = "听测页.html"

#: 进度存在 localStorage 的键前缀 —— 听者中途被打断也能接着打
STORE_PREFIX = "vm-listen-"

#: MOS 锚点（与发放包共用一份，别在两处各写一套会漂移的描述）
MOS_ANCHORS = vp.MOS_ANCHORS


def _read_playlist(pack: Path) -> list[dict]:
    """从发放包里读出「盲号 → 音频」的固定顺序。"""
    pl = pack / vp.PLAYLIST_FILE
    if not pl.exists():
        raise FileNotFoundError(f"没有播放清单: {pl}（先跑 voicebank_pack）")
    items: list[dict] = []
    for raw in pl.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        bid, _, rel = raw.partition("\t")
        bid, rel = bid.strip(), rel.strip()
        if bid and rel:
            items.append({"blind_id": bid, "rel": rel})
    return items


def _data_uri(path: Path) -> str:
    b = path.read_bytes()
    return "data:audio/wav;base64," + base64.b64encode(b).decode("ascii")


def _page_html(items: list[dict], *, sid: str, embed: bool) -> str:
    """生成整页。所有会进 HTML 的东西都在这里过一遍 html.escape。"""
    rows = []
    for i, it in enumerate(items, 1):
        src = it["source"] if embed else it["rel"]
        rows.append({"n": i, "id": it["blind_id"], "src": src})
    anchors = "".join(f"<li>{html.escape(a)}</li>" for a in MOS_ANCHORS)
    data = json.dumps(rows, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>盲听听测 · {len(items)} 条</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 16px 14px 96px; font: 16px/1.6 system-ui, "Microsoft YaHei", sans-serif;
         max-width: 720px; margin-inline: auto; }}
  h1 {{ font-size: 19px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
  .item {{ border: 1px solid #d5d5d5; border-radius: 12px; padding: 12px; margin-bottom: 10px; }}
  .item.done {{ border-color: #2e9d5b; background: #f3fbf6; }}
  .item.now {{ border-color: #2b6cb0; box-shadow: 0 0 0 2px #2b6cb033; }}
  .row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .bid {{ font: 600 15px/1 ui-monospace, monospace; min-width: 74px; }}
  button {{ font: inherit; padding: 9px 14px; border-radius: 9px; border: 1px solid #bbb;
            background: #fff; cursor: pointer; }}
  button:active {{ transform: translateY(1px); }}
  .play {{ min-width: 84px; }}
  .pick {{ display: flex; gap: 6px; }}
  .pick button {{ width: 42px; padding: 9px 0; text-align: center; font-weight: 600; }}
  .pick button.on {{ background: #2b6cb0; border-color: #2b6cb0; color: #fff; }}
  .note {{ width: 100%; margin-top: 8px; padding: 7px 9px; border: 1px solid #ccc;
           border-radius: 8px; font: inherit; }}
  footer {{ position: fixed; inset: auto 0 0 0; padding: 12px 14px; background: #fffffff2;
            border-top: 1px solid #ddd; display: flex; gap: 10px; align-items: center;
            backdrop-filter: blur(6px); }}
  #bar {{ flex: 1; font-size: 14px; color: #444; }}
  #exp {{ background: #2b6cb0; border-color: #2b6cb0; color: #fff; font-weight: 600; }}
  details {{ margin-bottom: 14px; font-size: 14px; }}
</style>
</head>
<body>
<h1>盲听听测 · {len(items)} 条</h1>
<div class="sub">按顺序听、逐条打分。进度会自动保存，中途可以关掉页面再回来。</div>

<details open>
  <summary>打分口径与三条要求（先看一眼）</summary>
  <ul>{anchors}</ul>
  <p><b>三条要求</b>：① 戴耳机、音量固定，中途别调；② 独立评、不讨论；
  ③ 凭第一感觉，别反复回听同一条。</p>
  <p>这里<b>没有标准答案</b>，也不需要猜哪条是真人 —— 只评「自然不自然」。</p>
</details>

<div id="list"></div>

<footer>
  <span id="bar"></span>
  <button id="exp">导出评分表</button>
</footer>

<script>
const ITEMS = {data};
const STORE = "{STORE_PREFIX}{sid}";
let st = JSON.parse(localStorage.getItem(STORE) || "{{}}");
let player = null, playing = -1;

function save() {{ localStorage.setItem(STORE, JSON.stringify(st)); render(); }}

function play(i) {{
  if (player) {{ player.pause(); player = null; }}
  if (playing >= 0) document.querySelector(`.item[data-i="${{playing}}"]`)?.classList.remove("now");
  player = new Audio(ITEMS[i].src);
  playing = i;
  document.querySelector(`.item[data-i="${{i}}"]`)?.classList.add("now");
  player.play().catch(e => alert("浏览器拦了播放，请再点一次播放按钮：" + e.message));
  player.onended = () => {{ player = null; playing = -1; render(); }};
}}

function setMos(i, v) {{
  st[ITEMS[i].id] = st[ITEMS[i].id] || {{}};
  st[ITEMS[i].id].mos = v;
  save();
}}

function setNote(i, v) {{
  st[ITEMS[i].id] = st[ITEMS[i].id] || {{}};
  st[ITEMS[i].id].note = v;
  localStorage.setItem(STORE, JSON.stringify(st));
}}

function render() {{
  const box = document.getElementById("list");
  box.innerHTML = "";
  ITEMS.forEach((it, i) => {{
    const rec = st[it.id] || {{}};
    const el = document.createElement("div");
    el.className = "item" + (rec.mos ? " done" : "");
    el.dataset.i = i;
    const picks = [1,2,3,4,5].map(v =>
      `<button class="${{rec.mos === v ? "on" : ""}}" data-mos="${{v}}">${{v}}</button>`).join("");
    el.innerHTML = `<div class="row">
      <span class="bid">${{it.id}}</span>
      <button class="play">▶ 播放</button>
      <span class="pick">${{picks}}</span>
    </div>
    <input class="note" placeholder="备注（可留空）" value="${{(rec.note || "").replace(/"/g, "&quot;")}}">`;
    el.querySelector(".play").onclick = () => play(i);
    el.querySelectorAll("[data-mos]").forEach(b => b.onclick = () => setMos(i, +b.dataset.mos));
    el.querySelector(".note").oninput = e => setNote(i, e.target.value);
    box.appendChild(el);
  }});
  const done = ITEMS.filter(it => (st[it.id] || {{}}).mos).length;
  document.getElementById("bar").textContent = `已打 ${{done}} / ${{ITEMS.length}} 条`;
}}

function csv() {{
  const esc = v => (v == null ? "" : String(v)).replace(/[",\\n\\r]/g, c => " " ).trim();
  let out = "blind_id,mos,note\\n";
  ITEMS.forEach(it => {{
    const rec = st[it.id] || {{}};
    out += `${{it.id}},${{rec.mos || ""}},${{esc(rec.note)}}\\n`;
  }});
  return out;
}}

// 供自动化测试读取（不影响听者使用）
// 播放钩子：自动播放策略要求真实用户手势，所以只能"点一下再看 currentTime" ——
// 没有这个钩子就无法在浏览器里证明"音频真的在响"，而不是只渲染了 48 个按钮
window.__vmAudio = () => (player ? {{t: player.currentTime, paused: player.paused}} : null);
window.__vmExport = csv;
window.__vmStats = () => ({{
  n: ITEMS.length,
  done: ITEMS.filter(it => (st[it.id] || {{}}).mos).length,
  missing: ITEMS.filter(it => !(st[it.id] || {{}}).mos).map(it => it.id),
}});

document.getElementById("exp").onclick = () => {{
  const s = window.__vmStats();
  if (s.done < s.n) {{
    if (!confirm(`还有 ${{s.n - s.done}} 条没打分，确定要导出吗？（先导出也可以，回来接着打）`)) return;
  }}
  const blob = new Blob([csv()], {{type: "text/csv;charset=utf-8"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "listener.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}};

render();
</script>
</body>
</html>
"""


def build(exp: str, *, pack: Path, out: Path, root: Path | None = None,
          embed: bool = False, force: bool = False) -> dict:
    """把发放包打成听测页。`embed=True` 时音频逐字节内嵌成单文件。"""
    pack, out = Path(pack), Path(out)
    res: dict = {"exp": exp, "pack": str(pack), "out": str(out), "embed": embed,
                 "error": None, "error_stage": None, "notes": [], "warnings": [],
                 "n_items": 0, "clips": 0, "leaks": []}

    dest = out / PAGE_FILE
    if dest.exists() and not force:
        res["error"] = (f"听测页已存在: {dest} —— 覆盖会刷新听者的进度键吗？"
                        f"不会，但会换掉他们手上那一份。确认要重做就加 --force")
        res["error_stage"] = "目标文件"
        return res

    try:
        items = _read_playlist(pack)
    except FileNotFoundError as e:
        res["error"] = str(e)
        res["error_stage"] = "播放清单"
        return res

    if not items:
        res["error"] = f"播放清单里没有条目: {pack / vp.PLAYLIST_FILE}"
        res["error_stage"] = "播放清单"
        return res

    seen: set[str] = set()
    dupes = sorted({it["blind_id"] for it in items
                    if it["blind_id"] in seen or seen.add(it["blind_id"])})
    if dupes:
        res["error"] = f"播放清单里有重复盲号: {dupes[:3]}（会给同一条打两次分）"
        res["error_stage"] = "播放清单"
        return res

    # ★ 先全部核一遍再生成：缺一条音就整体拒绝（听者点到没声音会以为自己操作错了）
    missing = [it for it in items if not (pack / it["rel"]).is_file()]
    if missing:
        res["error"] = (f"{len(missing)}/{len(items)} 条音频不在包里"
                        f"（如 {missing[0]['blind_id']} → {missing[0]['rel']}）")
        res["error_stage"] = "音频"
        return res
    res["clips"] = len(items)

    for it in items:
        p = pack / it["rel"]
        it["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        if embed:
            it["source"] = _data_uri(p)

    sid = hashlib.sha256(
        "".join(it["sha256"] for it in items).encode("ascii")).hexdigest()[:12]
    res["sid"] = sid

    page = _page_html(items, sid=sid, embed=embed)

    # ---- 泄漏守卫：与研究材料的判据同一套（含实验名不算泄漏的边界） ----
    if root is None:
        # ★ 不传 root = 没有揭盲表可对账 = 这一轮**没有做**身份扫描。
        #   必须说出来：网页版泄题比 CSV 危险（听者按 F12 就能看全）。
        #   曾经这里直接跳过且一声不响 —— 测试逼出来的（2026-10-10）。
        res["warnings"].append(
            "未传 --root → 本轮未做身份泄漏扫描；发布给听者前请带上 "
            "--root <实验根> 重跑一次")
    else:
        key_path = Path(root) / exp / "listen" / "key.csv"
        if key_path.exists():
            # ★ 判据与发放包**同一套**（`voicebank_pack._identity_tokens`）：
            #   只取 speaker / 时长档 / 条件三列，刻意不含整条样本路径 ——
            #   路径里带实验名，而实验名就得写在页面标题里。
            tokens = vp._identity_tokens(vp._read_key(key_path))
            low = page.lower()
            hits = [{"file": PAGE_FILE, "token": t} for t in sorted(tokens)
                    if t.lower() in low]
            if hits:
                res["error"] = (f"听测页里出现了身份信息（{len(hits)} 处，如 \""
                                f"{hits[0]['token']}\"）—— 网页版盲听一旦泄题，"
                                f"听者在浏览器里就能看见")
                res["error_stage"] = "泄漏守卫"
                res["leaks"] = hits
                return res
        else:
            res["warnings"].append(f"没找到揭盲表 {key_path}，本次未做身份泄漏扫描")

    out.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    res["n_items"] = len(items)
    res["page"] = str(dest)
    res["bytes"] = dest.stat().st_size
    res["ok"] = True
    if embed:
        res["warnings"].append(
            f"单文件 {res['bytes'] / 1048576:.1f} MB（音频已内嵌）—— "
            f"可直接转发给听者，双击即用、无需装任何东西")
    else:
        res["notes"].append(
            f"页面引用同目录的 {vp.CLIPS_DIR}/ —— 要把页面**和 clips 文件夹一起**拷贝，"
            f"单独发 HTML 会没有声音")
    res["warnings"].append(
        "页面按时长逐条播放，**不做响度归一化**（归一化会改变被测刺激本身）；"
        "请提醒听者全程同一副耳机、同一音量")
    return res


def main() -> int:
    # 重定向到管道时 Python 按 cp936 写 stdout —— 中文会变乱码，这里统一钉成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="把发放包打成离线听测页（点一下就能打分）")
    ap.add_argument("--exp", required=True, help="实验名（用于身份泄漏扫描）")
    ap.add_argument("--root", default=None, help="实验根目录（默认 outputs/voicebank）")
    ap.add_argument("--pack", required=True, help="发放包目录（含 clips/ 与 playlist.txt）")
    ap.add_argument("--out", required=True, help="听测页落盘目录")
    ap.add_argument("--embed", action="store_true",
                    help="音频内嵌为单文件（约 10+MB，但可整体转发、双击即用）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的听测页")
    args = ap.parse_args()

    res = build(args.exp, pack=Path(args.pack), out=Path(args.out), embed=args.embed,
                root=Path(args.root) if args.root else None, force=args.force)
    if res.get("error"):
        print(f"[page] 失败（{res.get('error_stage')}）: {res['error']}")
        for lk in res.get("leaks", [])[:5]:
            print(f"[page]   泄漏: {lk['file']} ← 「{lk['token']}」")
        return 1
    for n in res.get("notes", []):
        print(f"[page] 注意: {n}")
    for w in res.get("warnings", []):
        print(f"[page] 警告: {w}")
    print(f"[page] {res['n_items']} 条 → {res['page']}")
    print(f"[page] 进度键: {STORE_PREFIX}{res.get('sid')}（换一批音频会换键，旧进度不串味）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

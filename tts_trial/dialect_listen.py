"""把多次方言探针的结果拼成一张对照试听页（纯本地 HTML，无依赖）。

用途：同一批文本、不同发声方式（如 eric=成都话 vs uncle_fu=普通话），
并排放在一行里，点一下就能切换，直接判断「口音有没有真的变」。

用法：
    python -u dialect_listen.py --tags eric_chengdu zh_control \
        --titles 成都话-内置音色 普通话-对照 \
        --out D:/变声/media/dialect_probe/listen.html
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(r"D:/变声/media/dialect_probe")

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>方言探针对照</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 24px 20px 60px; background: #f6f7f9; color: #1b1f24;
         font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }}
  h1 {{ font-size: 19px; margin: 0 0 6px; }}
  .sub {{ color: #6b7280; font-size: 13px; margin-bottom: 22px; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
           padding: 14px 16px; margin-bottom: 12px; }}
  .text {{ font-size: 15px; font-weight: 600; margin-bottom: 10px; }}
  .row {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .v {{ flex: 1 1 240px; background: #fafbfc; border: 1px solid #eceef1;
        border-radius: 8px; padding: 8px 10px; }}
  .name {{ font-size: 12px; color: #6b7280; margin-bottom: 4px; }}
  audio {{ width: 100%; height: 34px; }}
  .meta {{ font-size: 11px; color: #9aa1ab; margin-top: 4px; }}
</style>
</head>
<body>
<h1>方言探针对照</h1>
<div class="sub">{subtitle}</div>
{cards}
</body>
</html>
"""

CARD = """<div class="card">
  <div class="text">{idx}. {text}</div>
  <div class="row">{players}</div>
</div>"""

PLAYER = """<div class="v">
    <div class="name">{title}</div>
    <audio controls preload="none" src="{src}"></audio>
    <div class="meta">{meta}</div>
  </div>"""


def load(tag: str) -> dict:
    meta_path = ROOT / tag / "manifest.json"
    if not meta_path.exists():
        raise SystemExit(f"缺少清单：{meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["_items"] = {it["key"]: it for it in meta["items"]}
    return meta


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", required=True, help="产物目录名，顺序即展示顺序")
    ap.add_argument("--titles", nargs="*", default=None, help="每列的标题，缺省用 tag")
    ap.add_argument("--out", default=str(ROOT / "listen.html"))
    args = ap.parse_args()

    metas = [load(t) for t in args.tags]
    titles = args.titles or args.tags
    if len(titles) != len(args.tags):
        raise SystemExit("--titles 数量要与 --tags 一致")

    keys = list(metas[0]["_items"].keys())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = out.parent.resolve()

    def src_of(p: str) -> str:
        """尽量用相对路径 —— 预览器按 http 打开时 file:// 会被拦掉。"""
        try:
            return Path(p).resolve().relative_to(base).as_posix()
        except ValueError:
            return "file:///" + str(Path(p).resolve()).replace("\\", "/")

    cards = []
    for i, key in enumerate(keys, 1):
        text = metas[0]["_items"][key]["text"]
        players = []
        for m, title in zip(metas, titles, strict=False):
            it = m["_items"].get(key)
            if not it:
                continue
            players.append(PLAYER.format(
                title=title, src=src_of(it["file"]), meta=f'{it["dur"]:.2f}s'))
        cards.append(CARD.format(idx=i, text=text, players="".join(players)))

    desc = " / ".join(
        f'{t}（{m.get("mode")}'
        + (f'，speaker={m["speaker"]}' if m.get("speaker") else "")
        + "）"
        for m, t in zip(metas, titles, strict=False))
    out.write_text(
        PAGE.format(subtitle=desc, cards="\n".join(cards)), encoding="utf-8")
    print(f"已生成 {out}（{len(cards)} 条 × {len(metas)} 列）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

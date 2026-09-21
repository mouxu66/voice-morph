"""市场精选配图留白归一化（把 30 张图的"图形占画布比例"统一）。

问题（2026-09-21 用户截图报障）
---------------------------------------------------------------
30 张配图都是方形画布，但**图形占画布的比例从 53% 到 94% 不等**。
前端在两个地方满铺渲染：

    web/src/pages/Home/HomePage.tsx:68   h-12 w-12  rounded-xl  (48px，半径 12px = 25%)
    web/src/pages/VoiceMarket/VoiceMarketPage.tsx:399  h-20 w-20 rounded-xl (80px，半径 15%)

`object-cover` 在方画布→方盒子里等于不裁切，于是占比 94% 的图视觉上"顶到圆角边"
（像从容器里溢出），占比 53% 的图又显得小一圈 —— 一排卡片看着参差不齐。

几何依据（为什么 target 默认 0.84）
---------------------------------------------------------------
48×48 盒、圆角半径 r=12px。居中正方形的角点 (24-s, 24-s) 要落在圆角内：
s ≤ 12 时恒成立；s > 12 时需 2(12-s)² ≤ 144 → s ≤ 12+√72 ≈ 20.49。
即边长 ≤ 40.97px = 画布 85.4%。所以"最长边 ≤ 画布 85%"就不会碰到圆角，
默认取 0.84 留 1.4% 余量。

做法（**只动有真透明通道的图**）
---------------------------------------------------------------
1. alpha > --alpha-threshold 求内容 bbox
2. 裁到 bbox
3. 等比缩放，使**最长边 = 画布 × --target**
4. 居中贴回**原尺寸**的透明画布（尺寸/长宽比不变，其他消费方不受影响）

不动的图
---------------------------------------------------------------
不透明满幅图（alpha 恒 255，本库 5 张 512×512 的 RGB 插画：
katoong_lanyangyang / katoong_manbo / lanyangyang / sunwukong / paidaxing）。
它们本来就是满幅设计，裁 bbox 会得到整张画布、归一化等于放大到爆边。
脚本识别后**跳过并单独报告**，不静默处理。

用法
---------------------------------------------------------------
    python tools/normalize_market_imgs.py --dry-run          # 只报告，不写任何文件
    python tools/normalize_market_imgs.py --sheet cmp.png    # 另出前后对照图
    python tools/normalize_market_imgs.py                    # 就地改写 assets
    python tools/normalize_market_imgs.py --out D:/tmp/norm  # 写到别处

改完 assets 后**必须**跑 `python tools/market_imgs_push.py --repo mouxu66/voice-market-assets`
把新图推到远程图库并换 revision —— 因为 `market_images.local_image_path()` 是
**先查 `outputs/market/imgs_cache/` 再查打包图**，只改打包图对已同步过的用户不生效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "m2_server" / "assets" / "market_imgs"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

DEFAULT_TARGET = 0.84
DEFAULT_ALPHA_THRESHOLD = 8
# 归一化容差：已在目标附近的图不再缩放。
# 两个作用：① 避免 LANCZOS 抗锯齿把 bbox 撑大 1~2px 后被反复微缩（收敛到不动点）；
# ② 保证"跑第二遍字节不变"，即幂等。2% 的尺寸差在 48px 盒子里肉眼不可见。
_TOLERANCE_RATIO = 0.02

# 判定"已居中"时允许的像素偏差。
# 必须是**非零**的：LANCZOS 缩放会在内容边缘留下 alpha 渐变的抗锯齿像素，
# 实测会让 bbox 比实际粘贴框外扩 1~2px。用精确相等判定的话，
# 归一化过的图永远判不出"已合规"，于是每跑一遍都重采样一次 —— 实测 `diyin.png`
# 就是这样不收敛的（2026-09-21）。2px / 618px = 0.3%，肉眼不可见。
_CENTER_TOLERANCE_PX = 2


def _tolerance(want: int) -> int:
    return max(2, round(want * _TOLERANCE_RATIO))


def has_transparency(im: Image.Image, threshold: int = DEFAULT_ALPHA_THRESHOLD) -> bool:
    """是否存在（近）全透明像素。不透明满幅图的 alpha 恒 255 → False。"""
    return im.convert("RGBA").getchannel("A").getextrema()[0] <= threshold


def content_bbox(im: Image.Image, threshold: int = DEFAULT_ALPHA_THRESHOLD):
    """内容 bbox（alpha > threshold）；全透明返回 None。"""
    a = im.convert("RGBA").getchannel("A")
    return a.point(lambda v: 255 if v > threshold else 0).getbbox()


def normalize_image(
    im: Image.Image,
    target: float = DEFAULT_TARGET,
    threshold: int = DEFAULT_ALPHA_THRESHOLD,
) -> tuple[Image.Image, dict]:
    """裁到内容 bbox → 等比缩放到最长边 = 画布×target → 居中贴回原尺寸透明画布。

    返回 (新图 RGBA, meta)。meta["action"] ∈ {"normalized", "skip"}。

    "skip" 有两种 reason：
      - `fully-transparent`：没有内容，无从归一化
      - `already-normalized`：**已经合规且已居中** → 调用方必须**不要写文件**。

    为什么要有 `already-normalized` 这个分支：存盘时为了压体积会把 PNG 量化回调色板
    （见 `encode_png`），量化是**不可逆**的 —— 读回来再量化一次会得到略有差异的字节。
    若不识别"已经合规"，反复跑本工具会让文件被无意义地重写、体积逐次微漂
    （2026-09-21 实测：第二遍 280,047 → 278,531 字节）。
    识别后第二遍起是**真 no-op**，文件字节不变。
    """
    canvas = im.size
    rgba = im.convert("RGBA")
    bb = content_bbox(rgba, threshold)
    if bb is None:
        return rgba, {"action": "skip", "reason": "fully-transparent", "ratio_before": None}

    cw, ch = bb[2] - bb[0], bb[3] - bb[1]
    longest = max(cw, ch)
    want = round(max(canvas) * target)
    ratio_before = round(longest / max(canvas), 4)

    if (
        abs(bb[0] - (canvas[0] - cw) // 2) <= _CENTER_TOLERANCE_PX
        and abs(bb[1] - (canvas[1] - ch) // 2) <= _CENTER_TOLERANCE_PX
        and abs(longest - want) <= _tolerance(want)
    ):
        return rgba, {
            "action": "skip",
            "reason": "already-normalized",
            "ratio_before": ratio_before,
            "ratio_after": ratio_before,
            "resized": False,
        }

    meta = {
        "action": "normalized",
        "canvas": canvas,
        "bbox_before": bb,
        "ratio_before": ratio_before,
        "resized": False,
    }

    if abs(longest - want) <= _tolerance(want):
        piece = rgba.crop(bb)  # 已在目标附近：只做居中，不重采样
    else:
        scale = want / longest
        piece = rgba.crop(bb).resize(
            (max(1, round(cw * scale)), max(1, round(ch * scale))), Image.LANCZOS
        )
        meta["resized"] = True

    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.paste(piece, ((canvas[0] - piece.width) // 2, (canvas[1] - piece.height) // 2))
    meta["size_after"] = (piece.width, piece.height)
    meta["ratio_after"] = round(max(piece.size) / max(canvas), 4)
    return out, meta


def encode_png(im: Image.Image) -> bytes:
    """编码 PNG：RGBA 与"量化回调色板"两种取小者。

    原图多为 P 模式 + transparency；这些是平涂插画，量化回 P 基本无损。
    取小者是为了不为了省几十 KB 而引入色带（量化偶尔反而更大）。
    """
    import io

    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    best = buf.getvalue()
    try:
        q = io.BytesIO()
        im.quantize(colors=256, method=Image.FASTOCTREE).save(q, "PNG", optimize=True)
        if q.tell() < len(best):
            best = q.getvalue()
    except Exception:
        pass
    return best


def save_png(im: Image.Image, dest: Path) -> int:
    """存 PNG，返回字节数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = encode_png(im)
    dest.write_bytes(data)
    return len(data)


def _rounded(im: Image.Image, box: int, radius_ratio: float) -> Image.Image:
    """把图画进 box×box 的圆角盒（模拟前端 rounded-xl 的裁切）。"""
    scaled = im.convert("RGBA").resize((box, box), Image.LANCZOS)
    mask = Image.new("L", (box, box), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, box - 1, box - 1), radius=int(box * radius_ratio), fill=255
    )
    out = Image.new("RGBA", (box, box), (255, 255, 255, 0))
    out.paste(scaled, (0, 0), mask)
    return out


def build_sheet(pairs: list[tuple[str, Image.Image, Image.Image]], dest: Path) -> None:
    """前后对照图：每行 [原图 / 归一化后]，按首页 48px 盒的圆角比例裁切。"""
    cell, pad, label_w = 96, 8, 150
    cols = 2
    row_h = cell + pad
    W = label_w + cols * (cell + pad) + pad
    H = pad + len(pairs) * row_h
    sheet = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad // 2), "原图 / 归一化后（模拟 48px rounded-xl 盒）", fill=(0, 0, 0, 255))
    for i, (name, before, after) in enumerate(pairs):
        y = pad + (i + 1) * row_h
        d.text((pad, y + cell // 2), name, fill=(0, 0, 0, 255))
        for j, img in enumerate((before, after)):
            x = label_w + j * (cell + pad)
            sheet.paste(_rounded(img, cell, 0.25), (x, y))
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(dest, "PNG")


def audit_dir(
    src: Path,
    target: float = DEFAULT_TARGET,
    threshold: int = DEFAULT_ALPHA_THRESHOLD,
) -> list[dict]:
    """体检：返回不合规条目（空列表 = 全部合规）。

    判据与 `normalize_image` 同一份语义：带透明通道的图，内容最长边占画布比例
    必须落在 target ± _TOLERANCE_RATIO 内；不透明满幅图不参与（设计如此，见模块头）。

    **`--audit` 与测试门禁共用本函数** —— 这样"注入一张没归一化的图，门禁会不会红"
    才真的证明了门禁有效，而不是两边各写一套判据、一起错还能自洽地绿。
    """
    problems: list[dict] = []
    for f in iter_sources(src):
        im = Image.open(f)
        if not has_transparency(im, threshold):
            continue
        bb = content_bbox(im, threshold)
        if bb is None:
            problems.append({"name": f.stem, "reason": "fully-transparent"})
            continue
        ratio = max(bb[2] - bb[0], bb[3] - bb[1]) / max(im.size)
        if abs(ratio - target) > _TOLERANCE_RATIO:
            problems.append({
                "name": f.stem,
                "reason": "ratio-out-of-range",
                "ratio": round(ratio, 4),
                "want": target,
            })
    return problems


def iter_sources(src: Path):
    for f in sorted(src.iterdir()):
        if f.is_file() and f.suffix.lower() in IMG_EXTS:
            yield f


def run(
    src: Path,
    out_dir: Path | None,
    target: float,
    threshold: int,
    dry_run: bool,
    sheet: Path | None,
) -> int:
    files = list(iter_sources(src))
    if not files:
        print(f"没有配图：{src}", file=sys.stderr)
        return 1

    pairs: list[tuple[str, Image.Image, Image.Image]] = []
    normalized = skipped_opaque = skipped_empty = skipped_ok = 0
    bytes_before = bytes_after = 0

    print(f"{'voice_id':26s} {'画布':11s} {'占比前':>7s} {'占比后':>7s} {'缩放':>5s} {'字节':>18s}")
    print("-" * 82)
    for f in files:
        im = Image.open(f)
        n_before = f.stat().st_size
        bytes_before += n_before

        if not has_transparency(im, threshold):
            skipped_opaque += 1
            print(f"{f.stem:26s} {im.size[0]}x{im.size[1]:<6d} {'满幅(不透明)':>7s} {'—':>7s} {'—':>5s} {n_before:>18,d}")
            continue

        new, meta = normalize_image(im, target, threshold)
        if meta["action"] == "skip":
            if meta["reason"] == "already-normalized":
                skipped_ok += 1
                print(f"{f.stem:26s} {im.size[0]}x{im.size[1]:<6d} "
                      f"{meta['ratio_before']*100:6.1f}% {meta['ratio_after']*100:6.1f}% "
                      f"{'已合规':>5s} {n_before:>18,d}  不写")
            else:
                skipped_empty += 1
                print(f"{f.stem:26s} {im.size[0]}x{im.size[1]:<6d} {'全透明':>7s} {'—':>7s} {'—':>5s} {n_before:>18,d}")
            continue

        dest = (out_dir / f.name) if out_dir else f
        n_after = len(encode_png(new)) if dry_run else save_png(new, dest)
        bytes_after += n_after
        normalized += 1
        mark = "*" if meta["resized"] else " "
        print(
            f"{f.stem:26s} {im.size[0]}x{im.size[1]:<6d} "
            f"{meta['ratio_before']*100:6.1f}% {meta['ratio_after']*100:6.1f}%{mark} "
            f"{n_before:>8,d}→{n_after:>8,d}"
        )
        if sheet:
            pairs.append((f.stem, im.copy(), new.copy()))

    print("-" * 82)
    print(f"归一化 {normalized} 张 · 已合规不写 {skipped_ok} 张 · "
          f"跳过（不透明满幅）{skipped_opaque} 张 · 跳过（全透明）{skipped_empty} 张")
    if normalized:
        print(f"体积 {bytes_before:,d} → {bytes_after:,d} 字节（跳过的不计）")
    else:
        print(f"未写任何文件，体积不变（共 {bytes_before:,d} 字节）")
    if dry_run:
        print("--dry-run：未写入任何文件")
    if sheet and pairs:
        build_sheet(pairs, sheet)
        print(f"对照图：{sheet}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="市场配图留白归一化")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="配图目录（默认打包 assets）")
    ap.add_argument("--out", default=None, help="输出目录（默认就地改写 --src）")
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET,
                    help=f"内容最长边占画布比例（默认 {DEFAULT_TARGET}，上限约 0.85 否则碰圆角）")
    ap.add_argument("--alpha-threshold", type=int, default=DEFAULT_ALPHA_THRESHOLD,
                    help=f"判定为内容的 alpha 下限（默认 {DEFAULT_ALPHA_THRESHOLD}）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--audit", action="store_true",
                    help="只体检不修改：列出占比不合规的图，有不合规则退出码 1")
    ap.add_argument("--sheet", default=None, help="另出一张前后对照图到该路径")
    args = ap.parse_args()

    if not (0 < args.target <= 0.95):
        print("--target 应在 (0, 0.95]；超过 0.85 会碰圆角", file=sys.stderr)
        return 2

    if args.audit:
        problems = audit_dir(Path(args.src), args.target, args.alpha_threshold)
        if not problems:
            n = sum(1 for _ in iter_sources(Path(args.src)))
            print(f"[OK] {args.src}：{n} 张配图留白全部合规（target={args.target}）")
            return 0
        print(f"[FAIL] {len(problems)} 张配图留白不合规（target={args.target}）：", file=sys.stderr)
        for p in problems:
            print(f"  {p['name']:26s} {p['reason']} ratio={p.get('ratio')}", file=sys.stderr)
        print("修复：python tools/normalize_market_imgs.py", file=sys.stderr)
        return 1

    return run(
        Path(args.src),
        Path(args.out) if args.out else None,
        args.target,
        args.alpha_threshold,
        args.dry_run,
        Path(args.sheet) if args.sheet else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())

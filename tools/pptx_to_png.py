"""PPTX → 逐页 PNG（讲课视频的"画面"那一半，2026-10-08）。

为什么有这个模块
    `tools/lecture_video.py` 会把「帧序列 + 旁白音轨」拼成 mp4，但它**不生成画面**。
    而「讲稿录课」（`m2_server/course_api.py`）只解析 PPT 备注页的文字、**从不渲染
    幻灯片**，`outputs/lecture_demo/render_slides.py` 那份画面又是照着某一份讲稿
    硬编码手画的板书（标题/BULLETS/段映射全写死）。于是"用自己的课件出片"这条路
    在仓库里是空的 —— 本模块补的就是这一步：真 PPTX → `slides/page_%03d.png`，
    文件名与 `lecture_video.py --pages` 期望的完全一致，接上就是一整条链路：

        pptx ──parse_pptx(备注文字)──▶ 逐页 wav ─┐
          └──pptx_to_png(画面)─────▶ slides/ ───┴─▶ lecture_video --pages ─▶ mp4

★ 页号必须与 course_api 对齐（本模块最容易错、且**错了不报错**的地方）
    `course_api.parse_pptx` 用 `ppt/slides/slideN.xml` 的 **N** 当页号（备注也是按 N
    经 rels 映射的）。而 PowerPoint 导出 PNG 是按**展示顺序**（`ppt/presentation.xml`
    的 `p:sldIdLst`）。这两者在"插入过/调整过页序"的 pptx 上**不一样** ——
    照导出顺序命名，就会出现「第 3 页的画面配第 2 页的旁白」这种静默错位，
    成片看着完全正常。所以这里先把展示顺序解出来（零依赖：zip + 正则，与
    `parse_pptx` 同一套），再按 `展示顺序[i] → N` 命名，并断言 `Slides.Count`
    与解析出的顺序长度一致（对不上就报错，绝不猜）。

依赖与边界（为什么是 PowerPoint COM）
    · 本机实测：LibreOffice **没装**；PowerPoint 装了（Office 16），COM 导出
      1920×1080 PNG 可行（`outputs/_pptx_render_probe.py`，启动 ~14.5s、每页 ~0.4s）。
    · `comtypes` 只声明在 `requirements-dev.txt`（开发/工具侧），**运行时后端不需要它**
      —— 本模块只在真要导出的那一瞬间 import，缺了报一句人话。
    · **没有 Office 的机器走不通这条路**：本模块给明确报错与替代方向，不静默降级
      （静默降级 = 用户以为出了片，其实画面是别的）。插件若要用它，`extras.external`
      里必须声明"外部程序：Microsoft PowerPoint"。
    · ★ **绝不关掉用户正在用的 PowerPoint**：COM 的 `CreateObject` 会附着到已在运行的
      实例上；若不管三七二十一 `Quit()`，用户手上来不及保存的演示文稿就没了。
      所以先查 POWERPNT.EXE 的 PID：**本来就开着就只关自己那一份**，不 Quit。

用法
    python tools/pptx_to_png.py 课件.pptx --out-dir outputs/course_1/slides
    python tools/pptx_to_png.py 课件.pptx --out-dir DIR --width 1280 --height 720
    python tools/pptx_to_png.py 课件.pptx --out-dir DIR --dry-run     # 只看会导出哪些页
    python tools/pptx_to_png.py 课件.pptx --out-dir DIR --force       # 重导已有页

产物：`<out-dir>/page_%03d.png` + `<out-dir>/slides.json`（凭据：源文件 sha256、
引擎版本、展示顺序映射、逐页尺寸与字节数 —— 与 voicebank_synth 的 provenance 同一动机）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 导出尺寸（默认 1080p；宽高都必须是偶数 —— yuv420p 编不出来奇数）
DEFAULT_W, DEFAULT_H = 1920, 1080

_PRES_SLDID = re.compile(r"<p:sldIdLst>(.*?)</p:sldIdLst>", re.S)
_PRES_RID = re.compile(r'r:id="([^"]+)"')
_REL = re.compile(r'Id="([^"]+)"[^>]*Target="([^"]+)"')
_SLIDE_PART = re.compile(r"slide(\d+)\.xml$")


class PptxError(RuntimeError):
    """业务错（报错文案直接给用户看）。"""


# ---------------- 展示顺序（零依赖，纯 XML） ----------------


def slide_part_order(pptx: Path) -> list[int]:
    """pptx → **展示顺序**上的 `slideN.xml` 序号列表（course_api 的页号口径）。

    为什么不能用 `sorted(slideN)`：那是**文件序号**（创建顺序），用户拖动调整过
    页序后与展示顺序不一致 —— 用错就会出现"画面配错旁白"的静默错位。
    """
    try:
        with zipfile.ZipFile(pptx) as z:
            names = set(z.namelist())
            pres = z.read("ppt/presentation.xml").decode("utf-8", "replace")
            rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise PptxError(f"这不是有效的 .pptx（zip 结构打不开或缺部件）：{pptx.name}") from e

    m = _PRES_SLDID.search(pres)
    if m is None:
        raise PptxError(f"{pptx.name} 的 presentation.xml 里没有 <p:sldIdLst>（读不出页序）")
    rid2target = dict(_REL.findall(rels))
    order: list[int] = []
    for rid in _PRES_RID.findall(m.group(1)):
        target = rid2target.get(rid, "")
        hit = _SLIDE_PART.search(target)
        if hit is None:
            raise PptxError(f"{pptx.name}：{rid} → {target!r} 不是幻灯片部件，页序解析失败")
        n = int(hit.group(1))
        if f"ppt/slides/slide{n}.xml" not in names:
            raise PptxError(f"{pptx.name}：页序指向的 ppt/slides/slide{n}.xml 不在包里")
        order.append(n)
    if not order:
        raise PptxError(f"{pptx.name} 里没有幻灯片")
    return order


# ---------------- PowerPoint 引擎 ----------------


def powerpoint_exe() -> str | None:
    """找 POWERPNT.EXE（注册表 App Paths 优先，再扫常见安装目录）。找不到返回 None。"""
    try:
        import winreg  # noqa: PLC0415 —— 只在 Windows 上有

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\POWERPNT.EXE",
        ) as k:
            val = winreg.QueryValueEx(k, "")[0]
            if val and Path(val).is_file():
                return str(val)
    except Exception:  # noqa: BLE001 —— 没装/无权限都走下面的兜底
        pass
    for base in (r"C:\Program Files\Microsoft Office", r"C:\Program Files (x86)\Microsoft Office"):
        if not Path(base).is_dir():
            continue
        for hit in sorted(Path(base).glob("root/Office*/POWERPNT.EXE"), reverse=True):
            return str(hit)
    return None


def powerpoint_pids() -> list[int]:
    """当前运行中的 POWERPNT.EXE PID 列表（判"是不是用户本来就开着"）。

    PowerShell 输出编码必须显式指定 utf-8 + errors=replace：中文 Windows 的控制台
    是 GBK，`text=True` 按 locale 解会在 UTF-8 模式下把 stdout 变成 None，
    随后 `.splitlines()` 抛异常被 except 吞掉 → 静默返回"没在跑"（速查表 53）。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process POWERPNT -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        ).stdout or ""
        return [int(ln.strip()) for ln in out.splitlines() if ln.strip().isdigit()]
    except Exception:  # noqa: BLE001
        return []


class ComEngine:
    """真的用 PowerPoint COM 导出的引擎（惰性 import comtypes）。"""

    def __init__(self) -> None:
        self.app = None
        self.pres = None
        self.preexisting = powerpoint_pids()  # ★ 起 COM 之前先记：决定要不要 Quit
        self.pptx_path: Path | None = None

    def open(self, pptx: Path) -> int:
        try:
            import comtypes.client as cc  # noqa: PLC0415
        except ImportError as e:  # pragma: no cover —— 开发机都有（requirements-dev）
            raise PptxError(
                "缺 comtypes（进 PowerPoint 的 COM 要用它）：pip install comtypes"
            ) from e
        exe = powerpoint_exe()
        if exe is None:
            raise PptxError(
                "本机找不到 Microsoft PowerPoint —— 这条路（真课件画面）走不通。"
                "替代：① 先在装了 Office 的机器上导出 PNG 再拿过来；"
                "② 用按讲稿生成的要点卡画面（不需要 Office）"
            )
        try:
            self.app = cc.CreateObject("PowerPoint.Application")
            # ReadOnly + WithWindow=False：不动用户的文件、不弹窗
            # （PowerPoint 不允许 Application.Visible=False，用这个参数代替）
            # ⚠️ 必须传**绝对路径**：COM 解析相对路径用的是 PowerPoint 自己的工作目录，
            # 不是我们的 CWD —— 传相对的会报 `-2147024893 系统找不到指定的路径`
            # （本项目踩过一次，速查表 79 同一族：绝对路径）。
            self.pres = self.app.Presentations.Open(
                str(pptx.resolve()), ReadOnly=True, WithWindow=False)
        except Exception as e:  # noqa: BLE001 —— COM 的异常类型五花八门，一律转人话
            # ★ 失败也要把**我们刚拉起来的**那个实例收掉，否则留下一个隐形 POWERPNT.EXE
            self.close()
            raise PptxError(f"PowerPoint 打不开这个文件：{pptx.name}（{type(e).__name__}: {e}）") from e
        self.pptx_path = pptx
        return int(self.pres.Slides.Count)

    def version(self) -> str:
        try:
            return f"PowerPoint {self.app.Version}"
        except Exception:  # noqa: BLE001
            return "PowerPoint (版本未知)"

    def export_slide(self, index: int, out: Path, width: int, height: int) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 同样必须绝对路径（COM 的相对路径基准不是我们的 CWD）
            self.pres.Slides(index).Export(str(out.resolve()), "PNG", width, height)
        except Exception as e:  # noqa: BLE001
            raise PptxError(f"导出第 {index} 张幻灯片失败（{type(e).__name__}: {e}）") from e

    def close(self) -> None:
        for step, fn in (("关闭演示文稿", lambda: self.pres and self.pres.Close()),
                         ("退出 PowerPoint", lambda: self.app and self.app.Quit())):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {step}失败（不影响已导出的图）：{type(e).__name__}: {e}",
                      file=sys.stderr)
        self.pres = self.app = None
        if self.preexisting:
            # 用户本来就开着 PowerPoint —— 附着上去的实例不许我们 Quit，这里只做说明：
            # 不要用 taskkill 去"清理"，那是删用户的活儿。
            print(f"[note] 导出前 PowerPoint 已在运行（PID {self.preexisting}）——"
                  f" 本工具不会结束你的 PowerPoint")
        else:
            left = powerpoint_pids()
            if left:
                # 我们起的实例没退干净：报出来让人决定，**不替用户 taskkill**
                print(f"[warn] PowerPoint 可能还留着进程 {left}（本工具不会替你强杀）",
                      file=sys.stderr)

    @property
    def started_by_us(self) -> bool:
        return not self.preexisting


# ---------------- 导出编排（可注入引擎 → 无 Office 也能测） ----------------


def _png_ok(path: Path, width: int, height: int) -> bool:
    """已存在的 PNG 是否**可用**（半截文件/尺寸不对都要重导）。

    ⚠️ 只看"文件在不在"是不够的：导出被打断会留下打不开的半截 PNG，
    而它会被当成"这页好了"直接跳过 —— 成片里那一页就是黑屏/花屏。
    ⚠️ 也**别用文件大小的魔法阈值**（曾写 `size < 1024`：纯色小图合法却只有几百字节，
    会被误判成坏文件而反复重导）。判据是"能不能真解码 + 尺寸对不对"：
    `Image.load()` 会把数据解一遍，被截断的 PNG 在这一步才暴露。
    """
    if not path.is_file():
        return False
    try:
        from PIL import Image

        with Image.open(path) as im:
            if im.size != (width, height):
                return False
            im.load()  # 真解码一遍
        return True
    except Exception:  # noqa: BLE001
        return False


def plan_exports(order: list[int], out_dir: Path, width: int, height: int,
                 force: bool) -> tuple[list[tuple[int, Path]], list[tuple[int, Path]]]:
    """→ (要导出的 [(第i张, 目标路径)], 复用的)。第 i 张 = 展示顺序里的位置。"""
    todo, reuse = [], []
    for i, _n in enumerate(order, start=1):
        out = out_dir / f"page_{order[i - 1]:03d}.png"
        (reuse if (not force and _png_ok(out, width, height)) else todo).append((i, out))
    return todo, reuse


def export_pptx_to_png(
    pptx: Path, out_dir: Path, *, width: int = DEFAULT_W, height: int = DEFAULT_H,
    force: bool = False, dry_run: bool = False, engine=None
) -> dict:
    """导出逐页 PNG。返回凭据 dict（调用方负责落盘）。"""
    if not pptx.is_file():
        raise PptxError(f"找不到文件：{pptx}")
    if width % 2 or height % 2:
        raise PptxError(f"宽高必须是偶数（yuv420p 编不出来）：{width}×{height}")
    order = slide_part_order(pptx)
    out_dir.mkdir(parents=True, exist_ok=True)
    todo, reuse = plan_exports(order, out_dir, width, height, force)

    info = {
        "tool": "tools/pptx_to_png.py",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pptx": str(pptx),
        "pptx_sha256": hashlib.sha256(pptx.read_bytes()).hexdigest(),
        "size": f"{width}×{height}",
        "slide_order": order,  # 展示顺序[i] = slideN.xml 的 N（与 course_api 的页号同口径）
        "planned": [{"slide_index": i, "file": p.name} for i, p in todo],
        "reused": [{"slide_index": i, "file": p.name} for i, p in reuse],
    }
    if dry_run:
        info["dry_run"] = True
        print(f"[dry-run] {pptx.name}: {len(order)} 页，展示顺序→文件序号 {order}")
        print(f"[dry-run] 要导出 {len(todo)} 页、复用 {len(reuse)} 页 → {out_dir}")
        for i, p in todo:
            print(f"[dry-run]   {p.name}  ← 第 {i} 张")
        return info

    eng = engine or ComEngine()
    # 引擎可注入（单测用假引擎，不需要装 Office）：无论真假都走 open → 校验 → 导出 → close
    # 这一条路径 —— 于是"页数对不上要拒绝"和"失败也要收尾"这两条都能被测到。
    try:
        count = eng.open(pptx)
        if count != len(order):
            # 顺序与页数对不上 = 映射不可信；继续导就是"画面配错旁白"
            raise PptxError(
                f"PowerPoint 说有 {count} 张，而解析出的页序有 {len(order)} 个 —— "
                f"页序映射不可信，拒绝导出（宁可不做，也不出错位的成片）"
            )
        info["engine"] = eng.version()
        print(f"[{info['engine']}] {pptx.name}: {count} 页，导出 {len(todo)} / 复用 {len(reuse)}")
        for i, out in todo:
            eng.export_slide(i, out, width, height)
            if out.is_file():
                print(f"  第 {i} 张 → {out.name}  ({out.stat().st_size // 1024} KB)")
    finally:
        eng.close()  # ★ 失败路径也要收尾（否则留下隐形 POWERPNT.EXE）

    # 导出后逐张复核：不许把"没导出来"当成功
    bad = [p.name for _i, p in todo if not _png_ok(p, width, height)]
    if bad:
        raise PptxError(f"这些页没导出成功（文件打不开或尺寸不对）：{'、'.join(bad[:6])}")
    info["pages"] = [
        {"file": f"page_{n:03d}.png", "slide_part": f"ppt/slides/slide{n}.xml"}
        for n in order
    ]
    return info


def main(argv: list[str] | None = None) -> int:
    # 重定向到管道时 Python 按 cp936 写 stdout —— 中文会变乱码，这里统一钉成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="PPTX → 逐页 PNG（讲课视频的画面，文件名对齐 course 页号）")
    ap.add_argument("pptx", help="源 .pptx")
    ap.add_argument("--out-dir", required=True, help="输出目录（page_%03d.png + slides.json）")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--force", action="store_true", help="重导已存在的页")
    ap.add_argument("--dry-run", action="store_true", help="只打印会导出哪些页")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    try:
        info = export_pptx_to_png(Path(args.pptx), out_dir, width=args.width,
                                  height=args.height, force=args.force, dry_run=args.dry_run)
        if not args.dry_run:
            (out_dir / "slides.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[ok] {len(info['pages'])} 页 → {out_dir}"
                  f"（凭据 {out_dir / 'slides.json'}）；"
                  f"接着跑 tools/lecture_video.py --pages manifest.json --slides {out_dir}")
        return 0
    except PptxError as e:
        print(f"[失败] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

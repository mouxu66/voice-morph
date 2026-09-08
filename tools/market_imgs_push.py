"""把市场精选配图推送到 GitHub 图库仓库（远程图库同步的云端侧）。

前置（一次性，本机终端）：
  1. GitHub 网页上新建 **public** 空仓库（如 mouxu66/voice-market-assets）
     —— 必须 public，jsdelivr CDN 才能加速。
  2. 本机 git 已能推 GitHub（credential manager 登录过，推过任意 repo 即可）。

用法：
  python tools/market_imgs_push.py --repo mouxu66/voice-market-assets

之后桌面端在 D:/变声/.env 加一行 VM_MARKET_IMG_REPO=mouxu66/voice-market-assets，
重启桌面端即自动同步；以后换图 = 改 assets 里的图后重跑本脚本（revision 自动更新，
客户端 6h 内拉新，jsdelivr 分支缓存最多延迟 ~12h）。
"""
import argparse
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "m2_server" / "assets" / "market_imgs"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def run(cmd: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"FAIL {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="GitHub 仓库 user/name（public）")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--source", default=str(SRC), help="配图目录（默认打包 assets）")
    args = ap.parse_args()

    src = Path(args.source)
    files = {f.stem.lower(): f.name for f in sorted(src.iterdir())
             if f.is_file() and f.suffix.lower() in IMG_EXTS}
    if not files:
        raise SystemExit(f"no images in {src}")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "repo"
        run(["git", "clone", "--depth", "1",
             f"https://github.com/{args.repo}.git", str(work)])  # 空 repo 也 OK
        imgs = work / "imgs"
        imgs.mkdir(exist_ok=True)
        for vid, name in files.items():
            shutil.copy2(src / name, imgs / name)
        revision = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (work / "images.json").write_text(
            json.dumps({"revision": revision, "imgs": {v: f"imgs/{n}" for v, n in files.items()}},
                       ensure_ascii=False, indent=2),
            "utf-8")
        (work / "README.md").write_text(
            f"# voice-market-assets\n\n"
            f"变声工坊·音色市场精选配图库（revision {revision}，{len(files)} 张）。\n\n"
            f"同步通道：`cdn.jsdelivr.net/gh/{args.repo}@main/images.json`\n"
            f"角色形象图版权归原权利方，仅供个人学习研究；OpenMoji 图标 CC BY-SA 4.0。\n",
            "utf-8")
        run(["git", "add", "-A"], cwd=work)
        run(["git", "-c", "user.name=voice-morph", "-c", "user.email=dev@local",
             "commit", "-m", f"market imgs {revision}"], cwd=work)
        run(["git", "push", "origin", f"HEAD:{args.branch}"], cwd=work)
    print(f"OK: {len(files)} imgs pushed, revision={revision}")
    print(f"下一步：在 D:/变声/.env 加  VM_MARKET_IMG_REPO={args.repo}  然后重启桌面端")


if __name__ == "__main__":
    main()

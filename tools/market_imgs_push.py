"""把市场精选配图推送到 GitHub 图库仓库（远程图库同步的云端侧）。

前置（一次性）：
  1. GitHub 上有 **public** 图库仓库（如 mouxu66/voice-market-assets）——必须 public，
     jsdelivr CDN 才能加速。本机已建好；换号时用 API 或网页重建即可。
  2. 本机 git 已能推 GitHub（credential manager 登录过，推过任意 repo 即可）。

用法：
  python tools/market_imgs_push.py --repo mouxu66/voice-market-assets

网络不通时（2026-09-22 实测）：某些网络下 `github.com` 的 **DNS 解析结果**会被
阻断，而 GitHub 的常规 IP 是通的 —— 表现为 `git clone` 报
`Failed to connect to github.com:443 after N ms`，但同一个域名换 IP 就 HTTP 200。
此时用 `--git-resolve` 把连接钉到能通的 IP（主机名仍是 github.com，证书照常校验）：

  # 先找能通的 IP（任选其一成功即可）
  for ip in 140.82.113.4 140.82.121.4 140.82.112.4; do
    timeout 25 git -c http.curloptResolve="github.com:443:$ip" \
      ls-remote --heads https://github.com/mouxu66/voice-market-assets.git && break
  done
  python tools/market_imgs_push.py --repo mouxu66/voice-market-assets --git-resolve 140.82.113.4

（`http.curloptResolve` 需要 Git ≥ 2.44；本机 2.55 实测可用。）

图库地址已内置为产品默认值（market_images._REPO），.env 无需配置；
以后换图 = 改 assets 里的图后重跑本脚本（revision 自动更新，
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
    ap.add_argument("--git-resolve", default=None, metavar="IP",
                    help="把 github.com:443 钉到该 IP（绕开被阻断的 DNS 结果，见文件头）")
    args = ap.parse_args()

    src = Path(args.source)
    files = {f.stem.lower(): f.name for f in sorted(src.iterdir())
             if f.is_file() and f.suffix.lower() in IMG_EXTS}
    if not files:
        raise SystemExit(f"no images in {src}")

    # -c 选项要加在每个 git 调用前（clone / push 都要）
    pre = (["-c", f"http.curloptResolve=github.com:443:{args.git_resolve}"]
           if args.git_resolve else [])

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "repo"
        run(["git", *pre, "clone", "--depth", "1",
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
            f"配图均为本仓库自生成的原创插画（MIT）；OpenMoji 图标为 CC BY-SA 4.0。\n",
            "utf-8")
        run(["git", "add", "-A"], cwd=work)
        run(["git", "-c", "user.name=voice-morph", "-c", "user.email=dev@local",
             "commit", "-m", f"market imgs {revision}"], cwd=work)
        run(["git", *pre, "push", "origin", f"HEAD:{args.branch}"], cwd=work)
    print(f"OK: {len(files)} imgs pushed, revision={revision}")
    print("客户端刷新市场即自动拉新（TTL 6h；jsdelivr 分支缓存最多延迟 ~12h）")


if __name__ == "__main__":
    main()

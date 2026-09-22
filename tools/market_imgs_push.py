"""把市场精选配图推送到 GitHub 图库仓库（远程图库同步的云端侧）。

前置（一次性）：
  1. GitHub 上有 **public** 图库仓库（如 mouxu66/voice-market-assets）——必须 public，
     jsdelivr CDN 才能加速。本机已建好；换号时用 API 或网页重建即可。
  2. 本机 git 已能推 GitHub（credential manager 登录过，推过任意 repo 即可）。

用法：
  python tools/market_imgs_push.py --repo mouxu66/voice-market-assets

**镜像语义（2026-09-22 起）**：本脚本让远程 `imgs/` 与打包 assets **完全一致**
—— 既拷入新的，也**删掉 assets 里没有的**。此前只拷入不删除，于是远程仓库里的
死文件会**永久滞留**（每次推送 `git add -A` 都把它原样带回去，且因为不在
`images.json` 清单里，永远不会被任何客户端下载到、也就永远没人发现）。
实测代价：`imgs/manbo.png`（414KB）就是这么留下的 —— 早期命名遗留，
清单里早已只有 `katoong_manbo`，它却在仓库里白占了几个月。
想只增不删时加 `--no-prune`（例如临时推一批图、暂不动旧文件）。

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


def scan_images(src: Path) -> dict[str, str]:
    """扫描配图目录 → {voice_id: 文件名}。voice_id 取 stem 小写。"""
    out: dict[str, str] = {}
    for f in sorted(src.iterdir()):
        if f.is_file() and f.suffix.lower() in IMG_EXTS:
            out[f.stem.lower()] = f.name
    return out


def plan_mirror(desired: dict[str, str], existing: list[str]) -> list[str]:
    """算出 `imgs/` 里**应删除**的文件名，使它与 desired 完全一致。

    按**文件名**（而非 voice_id）比对，这样同一音色换扩展名
    （`a.png` → `a.jpg`）时，旧扩展名那份也会被删掉 —— 否则本地会同时存在
    两份，而 `market_images._find_local()` 按 `_IMG_EXTS` 顺序取（png 在前），
    可能一直取到那张旧的。

    只处理**图片扩展名**：`imgs/` 下若有 `.gitkeep` 等非配图文件则不动它
    （本工具的职责是镜像配图，不是清空目录）。

    返回排序后的列表（便于稳定输出与断言）。
    """
    keep = set(desired.values())
    return sorted(
        n for n in existing
        if n not in keep and Path(n).suffix.lower() in IMG_EXTS
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="GitHub 仓库 user/name（public）")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--source", default=str(SRC), help="配图目录（默认打包 assets）")
    ap.add_argument("--git-resolve", default=None, metavar="IP",
                    help="把 github.com:443 钉到该 IP（绕开被阻断的 DNS 结果，见文件头）")
    ap.add_argument("--no-prune", action="store_true",
                    help="只增不删：保留远程 imgs/ 里 assets 已没有的文件（默认镜像删除）")
    args = ap.parse_args()

    src = Path(args.source)
    files = scan_images(src)
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

        # 镜像清理：删掉远程有、assets 已没有的文件。
        # 少了这步，仓库里的死文件会被每次推送原样带回（见文件头「镜像语义」）。
        existing = [f.name for f in imgs.iterdir() if f.is_file()]
        stale = plan_mirror(files, existing)
        if args.no_prune:
            if stale:
                print(f"  ! --no-prune：保留 {len(stale)} 个远程多余文件 {stale}")
        else:
            for name in stale:
                (imgs / name).unlink()
                print(f"  - 删除远程多余文件 imgs/{name}")

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
    print(f"OK: {len(files)} imgs pushed, revision={revision}"
          + (f", pruned={len(stale)}" if stale and not args.no_prune else ""))
    print("客户端刷新市场即自动拉新（TTL 6h；jsdelivr 分支缓存最多延迟 ~12h）")


if __name__ == "__main__":
    main()

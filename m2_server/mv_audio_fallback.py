"""QQ 音乐会员曲的**兜底取音**：从官方 MV 里抽音轨。

为什么单独一个模块
------------------
`ytdlp_fetch` 的模块注释里写死了一条性质：**本仓库没有一行绕 DRM 的代码**。
那个性质是刻意维持的（比赛/开源脱敏时这块天然干净），所以 MV 这条路不能塞进
`ytdlp_fetch` —— 它是**另一条性质不同的链路**，单独成文件、单独一个开关、
在文档里单独说清边界。

它怎么工作（2026-09-27 实测通过，全匿名，无需登录态）
--------------------------------------------------
用户发现的关键事实：**QQ 音乐的 MV 网页一直是免费能看的**。顺着这条线实测下来：
歌曲音频接口（`vkey.CgiGetVkey`）对会员曲返回 `purl` 空 + `fnameHitCa`
（Copyright authority 不通过），但 **MV 的播放页内部吐的是明文 MPEG-TS 分片**，
不校验登录态、不加密。

    ① `music.pf_song_detail_svr.get_song_detail` 拿歌曲**关联的 vid**
    ② 开浏览器上下文打开 `y.qq.com/n/ryqq/mv/<vid>`
    ③ 监听响应，截 .m3u8 索引 + 全部 .ts 分片
    ④ 按序合并 → 明文 MPEG-TS（实测《唯一》36MB / 720p / 271s）
    ⑤ ffmpeg `-vn -acodec copy` → AAC/m4a

★ 两个实测踩到的坑，改代码时别踩回去：

    · **直链是一次性签名路径**。每次刷新 m3u8 的前缀都不同
      （`15541F1F…` / `0A9C88EC…` / `5B6B0D92…`），拖到下一页就 403。
      所以必须**抓到就立刻下**，不能先存 URL 再排队。
    · **分片 URL 完全相同**。用 `Map<url, body>` 收会被覆盖成 1 片
      （实测踩过，合并出来只有 387KB）。必须用**数组按到达顺序**收。

音质代价（必须告诉用户，不能含糊）
--------------------------------
MV 音轨 ≠ 唱片母带。实测《唯一》MV 抽出来是 **192 kbps AAC**，
而正版音源是 FLAC 无损（~1000 kbps），差 5 倍。且 MV 音轨混了影像的声音设计
（环境声、气口），可能带画面声。**做粗料够用，做音源不够。**

边界（这几条是刻意的，不是漏了）
------------------------------
· **默认关闭**（`VM_MV_FALLBACK` 未设 = 关）。理由与 `VM_YTDLP_COOKIES` 一致：
  这是"绕过平台会员授权"的取音路径，用户自己开、自己承担使用边界。
· **只对 QQ 音乐生效**。别家没有等价的 MV 免费通路（B 站另有官方 extractor）。
· **只在 yt-dlp 失败之后才走**。它是兜底，不是首选 —— 首选永远是正版音源。
· 产物**落会话目录**、遵循"随用随删"，与 `ytdlp_fetch` 同一口径。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import session_out

#: 开关。不设 = 关闭（默认）。见模块注释「边界」。
ENV_ENABLE = "VM_MV_FALLBACK"

#: 让 Node 侧找到 `playwright-core`。默认猜 `web/node_modules` —— 那是本仓唯一
#: 装了它的地方，且 `web/` 与 `m2_server/` 是兄弟目录。
ENV_PW_CORE = "VM_PLAYWRIGHT_CORE"

#: Node 可执行文件。找不到就走 PATH 里的 `node`。
ENV_NODE = "VM_NODE"

#: 单曲 MV 抓取的墙钟上限。实测《唯一》271s / 36MB 在本机约 40~70s 抓完
#: （27 片，每片要等浏览器真的请求它）。给 300s 留堆量。
_TIMEOUT_S = 300

#: QQ 音乐 songmid 形态：14 位 base62。字符集本身就是安全性质（拼进 URL 用）。
_SONGMID_RE = re.compile(r"^[0-9A-Za-z_-]{3,64}$")

#: 从 song_detail 响应里抠 vid。vid 形态如 `r0035thc5pb`（base62，以 r 开头居多，
#: 但不写死前缀 —— 别家 vid 形态会变）。
_VID_RE = re.compile(r"^[0-9A-Za-z]{6,32}$")


class MvFallbackError(RuntimeError):
    """给用户看的错误（消息即文案）。"""


def enabled() -> bool:
    """MV 兜底是否开着。默认关。"""
    return os.environ.get(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")


def status() -> dict:
    """给前端/`probe` 看的当前状态（不触发任何抓取）。"""
    return {
        "enabled": enabled(),
        "env": ENV_ENABLE,
        "playwright_core": str(_playwright_core() or ""),
        "node": shutil.which("node") or os.environ.get(ENV_NODE, ""),
        "detail": (
            "已开启：yt-dlp 拿不到音频时会尝试从 QQ 音乐官方 MV 抽音轨"
            "（音质为 MV 抽轨，192kbps 量级，非母带）"
            if enabled()
            else f"未开启。设环境变量 {ENV_ENABLE}=1 后重启后端即可启用。"
        ),
    }


# --------------------------------------------------------------------------
# 外部依赖定位
# --------------------------------------------------------------------------


def _playwright_core() -> Path | None:
    """找 `playwright-core` 模块目录。"""
    env = os.environ.get(ENV_PW_CORE, "").strip()
    if env:
        p = Path(env)
        if (p / "package.json").exists():
            return p
        # 也接受"指到 web 目录"的写法
        cand = p / "node_modules" / "playwright-core"
        if (cand / "package.json").exists():
            return cand
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent):
        cand = base / "web" / "node_modules" / "playwright-core"
        if (cand / "package.json").exists():
            return cand
    return None


def _node_exe() -> str | None:
    env = os.environ.get(ENV_NODE, "").strip()
    if env and Path(env).exists():
        return env
    return shutil.which("node")


def _ffmpeg() -> str | None:
    """找 ffmpeg。`ytdlp_fetch` 那边已经证明本机会有，这里只做一份自己的探测。"""
    env = os.environ.get("VM_FFMPEG", "").strip()
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Windows 上常见位置（winget 装的 Gyan build）
    for pat in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
        Path("C:/ffmpeg/bin"),
    ):
        if pat.exists():
            hits = list(pat.glob("**/bin/ffmpeg.exe"))[:1]
            if hits:
                return str(hits[0])
    return None


# --------------------------------------------------------------------------
# 第 ① 步：用 songmid 换 vid（纯 HTTP，不需要浏览器）
# --------------------------------------------------------------------------


def _musicu(module: str, method: str, param: dict, timeout_s: int = 15) -> dict:
    """匿名调一次 `musicu.fcg`。只用于**拿 vid**，不碰音频接口。"""
    body = {
        "comm": {
            "ct": 24, "cv": 0, "v": "1.0.0", "platform": "yqq.json",
            "uin": "0", "g_tk_new_20200303": "5381", "g_tk": "5381",
            "format": "json", "inCharset": "utf-8", "outCharset": "utf-8", "notice": 0,
        },
        module: {"module": module, "method": method, "param": param},
    }
    r = requests.post(
        "https://u.y.qq.com/cgi-bin/musicu.fcg",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Referer": "https://y.qq.com/",
            "Origin": "https://y.qq.com",
            "Content-Type": "application/json;charset=UTF-8",
        },
        timeout=timeout_s,
    )
    return r.json()


def find_vid(songmid: str, timeout_s: int = 15) -> str | None:
    """歌曲 mid → 关联 MV 的 vid。没有关联 MV 返回 `None`。

    实测：《唯一》`0023jgxa0Ym5yo` → `r0035thc5pb`（在 `song_detail` 响应里
    以 `"vid":"…"` 出现，嵌套层级不固定，所以直接对序列化后的 JSON 扫正则 ——
    比逐层抠耐改版）。
    """
    if not _SONGMID_RE.match(songmid or ""):
        return None
    try:
        d = _musicu("music.pf_song_detail_svr", "get_song_detail", {"song_mid": songmid}, timeout_s)
    except Exception:
        return None
    blob = json.dumps(d, ensure_ascii=False)
    for m in re.finditer(r'"vid"\s*:\s*"([^"]+)"', blob):
        v = m.group(1)
        if _VID_RE.match(v):
            return v
    return None


# --------------------------------------------------------------------------
# 第 ②~④ 步：浏览器抓 MV 全部分片
# --------------------------------------------------------------------------

#: 注入给 Node 的抓取脚本。**内联**而不是额外落一个 .js 文件 —— 少一个要同步维护
#: 的产物，且它的存在只服务于这一个函数。
#:
#: ★ 核心难点是「怎么知道抓全了」。分片是浏览器按播放进度**惰性**拉的，只播开头
#:   就只拿到开头几个。而且分片 URL 完全相同（`qmmv_xxx.f9934.ts`），
#:   **不能用 URL 去重**，只能按到达顺序累积。
#:   做法：先读 m3u8 数出 `#EXTINF` 行数 = 该有的分片数，然后扫完整条时间轴，
#:   直到拿到足够的分片或时间轴走完。**不完整就报 `incomplete` 让上层失败**——
#:   给用户一首被截断的歌，比明确告诉他"没抓全"糟得多。
_GRAB_JS = r"""
const fs = require('fs');
const path = require('path');
const OUT = process.argv[2];
const VID = process.argv[3];
const PW = process.argv[4];

const { chromium } = require(PW);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const segs = [];        // ★ 数组，不是 Map —— 分片 URL 完全相同，Map 会被覆盖
  let want = 0;           // m3u8 里声明的分片数（0 = 还没拿到索引）

  const browser = await chromium.launch({
    channel: 'msedge',
    headless: true,
    args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'],
  });
  let ctx;
  try {
    ctx = await browser.newContext({
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
        '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
      viewport: { width: 1440, height: 900 },
    });
  } catch (e) {
    // msedge 通道不可用时退回自带 chromium
    ctx = await (await chromium.launch({ headless: true })).newContext();
  }

  const page = await ctx.newPage();
  page.on('response', async (res) => {
    const u = res.url();
    if (!/\.(ts|m3u8)(\?|$)/i.test(u)) return;
    try {
      const buf = Buffer.from(await res.body());
      if (/\.m3u8(\?|$)/i.test(u)) {
        fs.writeFileSync(path.join(OUT, 'index.m3u8'), buf);
        if (!want) {
          const txt = buf.toString('utf8');
          const n = (txt.match(/#EXTINF/g) || []).length;
          if (n > 0) want = n;
        }
      } else {
        segs.push(buf);
      }
    } catch {}
  });

  const url = `https://y.qq.com/n/ryqq/mv/${VID}`;
  try { await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 }); } catch {}
  await sleep(3500);

  const v = await page.$('video');
  if (v) await v.click({ timeout: 3000 }).catch(() => {});
  await page.evaluate(() => {
    const el = document.querySelector('video');
    if (el) { el.muted = true; el.play().catch(() => {}); }
  }).catch(() => {});
  await sleep(5000);

  const dur = await page.evaluate(() => {
    const el = document.querySelector('video');
    return el && isFinite(el.duration) ? el.duration : 0;
  }).catch(() => 0);

  // 扫完整条时间轴。步长 8s（原来 12s 太粗，会跳过分片）；
  // 每轮之间给浏览器 1.2s 去把该位置的分片拉回来。
  if (dur > 0) {
    for (let t = 0; t < dur + 8; t += 8) {
      await page.evaluate((tt) => {
        const el = document.querySelector('video');
        if (el) el.currentTime = tt;
      }, t).catch(() => {});
      await sleep(1200);
    }
    // 收尾：多等一会儿，把最后几片读完
    await sleep(4000);
  }

  await browser.close();

  if (!segs.length) {
    console.log(JSON.stringify({ ok: false, err: 'no-segments', dur, want }));
    process.exit(0);
  }

  const merged = Buffer.concat(segs);
  const p = path.join(OUT, 'mv.ts');
  fs.writeFileSync(p, merged);

  // ★ 完整性判定：拿到了声明的片数才算全。没有索引（want=0）时只看有没有内容。
  const complete = want === 0 ? true : segs.length >= want;
  console.log(JSON.stringify({
    ok: true,
    complete,
    segments: segs.length,
    want,
    bytes: merged.length,
    duration: dur,
    ts: p,
  }));
})();
"""


def _grab_mv(vid: str, out_dir: Path, timeout_s: int = _TIMEOUT_S) -> dict:
    """跑一次浏览器抓取，把分片合并成 `out_dir/mv.ts`。返回抓取统计。"""
    node = _node_exe()
    if not node:
        raise MvFallbackError("没找到 node，MV 兜底用不了（它靠 playwright 驱动浏览器）。")

    pw = _playwright_core()
    if pw is None:
        raise MvFallbackError(
            "没找到 playwright-core。设 " + ENV_PW_CORE + " 指向 `web/node_modules/playwright-core`，"
            "或先在该目录 `npm i playwright-core`。"
        )

    script = out_dir / "_grab.cjs"
    script.write_text(_GRAB_JS, encoding="utf-8")

    cmd = [node, str(script), str(out_dir), vid, str(pw)]
    try:
        proc = subprocess.run(  # noqa: S603 —— exe 来自本地探测，参数由本模块拼出
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise MvFallbackError(
            f"抓 MV 超过 {timeout_s}s 还没完成，已中止。网络慢或该 MV 分片特别多时会出现。"
        ) from exc

    tail = (proc.stdout or "").strip().splitlines()
    payload = None
    for line in reversed(tail):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue

    if payload is None:
        msg = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise MvFallbackError(f"抓 MV 失败（node 没给出结果）。原始输出：{msg or '空'}")
    if not payload.get("ok"):
        err = payload.get("err", "unknown")
        if err == "no-segments":
            raise MvFallbackError(
                "打开了 MV 页但没抓到任何分片 —— 该视频可能已下架、地区受限，"
                "或页面结构改版。"
            )
        raise MvFallbackError(f"抓 MV 失败：{err}")

    # ★ 抓全了才算成功。分片是惰性加载的，只播开头就只拿到开头几个 ——
    # 拿一份**被截断**的歌给用户，比明确失败糟得多（他可能听不出少了尾部，
    # 拿去跑翻唱才发现）。所以这里宁可失败。
    if not payload.get("complete", True):
        raise MvFallbackError(
            f"MV 分片没抓全（拿到 {payload.get('segments', 0)}/{payload.get('want', '?')} 片），"
            "可能是网络慢或视频较长。稍后重试；若一直不行，换一首或换来源。"
        )
    return payload


# --------------------------------------------------------------------------
# 第 ⑤ 步：从 ts 抽音轨
# --------------------------------------------------------------------------


def _extract_audio(ts_path: Path, out_path: Path, timeout_s: int = 120) -> None:
    """`ffmpeg -vn -acodec copy` —— 不重编码，直接剥出 AAC 音轨。"""
    ff = _ffmpeg()
    if not ff:
        raise MvFallbackError("没找到 ffmpeg，抽不了音轨。装好 ffmpeg 或用 VM_FFMPEG 指向它。")

    cmd = [
        ff, "-y", "-v", "error",
        "-i", str(ts_path),
        "-vn", "-acodec", "copy",
        str(out_path),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 —— exe 来自本地探测
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise MvFallbackError("ffmpeg 抽音轨超时。") from exc
    if proc.returncode != 0 or not out_path.exists():
        raise MvFallbackError(f"ffmpeg 抽音轨失败：{(proc.stderr or '').strip()[-300:]}")


def _probe_duration_s(path: Path) -> float:
    """读音频时长（秒）。读不出来给 `0` —— 调用方把它当"无法校验"，不是失败。

    优先 `soundfile`（只读文件头，快且不依赖 ffmpeg 进程）；
    当前仓库已经因 `ytdlp_api._duration_of` 依赖它，所以这不是新增依赖。
    m4a 若 soundfile 读不了（取决于 libsndfile 版本），退回 ffprobe。
    """
    try:
        import soundfile as sf

        d = float(sf.info(str(path)).duration)
        if d > 0:
            return round(d, 2)
    except Exception:  # noqa: BLE001 —— 见 docstring：读不到不叫失败
        pass

    ff = _ffmpeg()
    if not ff:
        return 0.0
    probe = str(Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    if not Path(probe).exists():
        return 0.0
    try:
        proc = subprocess.run(  # noqa: S603 —— exe 来自本地探测
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return round(float((proc.stdout or "").strip() or 0), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def _unlink_quietly(p: Path) -> bool:
    """删一个文件，返回"删干净了没"。与 `ytdlp_fetch._unlink_quietly` 同款。"""
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 对外主入口
# --------------------------------------------------------------------------


def fetch_from_mv(songmid: str, timeout_s: int = _TIMEOUT_S) -> dict:
    """会员曲兜底：`songmid` → MV 音轨，落会话目录，返回与 `ytdlp_fetch.fetch` 同形。

    **调用方必须先确认 `enabled()`**（本函数也会自查一次，双保险）。
    抛 `MvFallbackError`，消息是给用户看的人话。
    """
    if not enabled():
        raise MvFallbackError(
            f"MV 兜底没开启。要用请设环境变量 {ENV_ENABLE}=1 后重启后端。"
        )
    songmid = (songmid or "").strip()
    if not _SONGMID_RE.match(songmid):
        raise MvFallbackError("这条链接里没抠出 QQ 音乐歌曲 ID，MV 兜底用不了。")

    vid = find_vid(songmid)
    if not vid:
        raise MvFallbackError(
            "这首歌在 QQ 音乐没有关联的官方 MV，兜底取音走不通。换一首或换来源。"
        )

    out_dir = session_out.session_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"ytdlp_mv_{int(time.time())}"

    # 中间产物扔系统 TEMP（.ts 有几十 MB，不该在会话目录里留下痕迹）
    with tempfile.TemporaryDirectory(prefix="vm_mv_") as tmp:
        tmp_dir = Path(tmp)
        stat = _grab_mv(vid, tmp_dir, timeout_s)
        ts_path = Path(stat["ts"])
        if not ts_path.exists():
            raise MvFallbackError("抓到了 MV 但没有可用的合并文件。")

        dst = out_dir / f"{stem}.m4a"
        _extract_audio(ts_path, dst)

    if not dst.exists() or dst.stat().st_size <= 0:
        raise MvFallbackError("MV 音轨抽出来是空的。")

    # ★ 抽完再对一次时长。`_grab_mv` 已经用"片数对不对"把关了，这里是**第二道**：
    # 片数对了但某一刀切歪（或 ffmpeg 只解出前半段）时，产物仍是截断的。
    # 容忍 3 秒 —— MV 的音频流比视频流略短（实测 271.17 vs 271.24）是正常的。
    mv_dur = float(stat.get("duration") or 0)
    got_dur = _probe_duration_s(dst)
    if mv_dur > 0 and got_dur > 0 and got_dur < mv_dur - 3:
        _unlink_quietly(dst)
        raise MvFallbackError(
            f"抽出来的音轨只有 {got_dur:.0f}s，MV 本身是 {mv_dur:.0f}s —— "
            "没取完整，已丢弃。稍后重试。"
        )

    size = dst.stat().st_size
    return {
        "name": dst.name,
        "path": str(dst),
        "url": f"/media/session/{dst.name}",
        "suffix": ".m4a",
        "bytes": size,
        "content_type": "audio/mp4",
        "site": "QQ音乐（MV 抽轨）",
        "source_url": f"https://y.qq.com/n/ryqq/mv/{vid}",
        "mv_vid": vid,
        # ★ 让上游能把这句原样透到界面：用户必须知道音质打折了。
        "quality_note": (
            f"音质：MV 抽轨（{stat['segments']} 片合并，约 192kbps AAC），"
            "非正版母带，且混有影像声音。做粗料够用，做音源不够。"
        ),
        "duration_hint_s": round(got_dur or mv_dur, 1),
    }


def _main(argv: list[str]) -> int:
    """排查用：`python m2_server/mv_audio_fallback.py status|vid|fetch <songmid>`。"""
    if not argv or argv[0] == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "vid" and len(argv) > 1:
        v = find_vid(argv[1])
        print(json.dumps({"songmid": argv[1], "vid": v}, ensure_ascii=False, indent=2))
        return 0 if v else 1
    if argv[0] == "fetch" and len(argv) > 1:
        try:
            print(json.dumps(fetch_from_mv(argv[1]), ensure_ascii=False, indent=2))
        except MvFallbackError as e:
            print(f"错误：{e}")
            return 1
        return 0
    print("用法：mv_audio_fallback.py [status | vid <songmid> | fetch <songmid>]")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

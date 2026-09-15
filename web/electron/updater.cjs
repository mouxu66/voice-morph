// 自动更新：检查 → 下载 → 校验 → 安装（主进程模块，由 main.cjs 挂载 IPC）。
//
// 设计要点：
//   1. 托管无关：更新源就是一个静态清单 latest.json，放 GitHub Releases、对象存储、
//      网盘直链、甚至局域网共享目录都行 —— 地址见下面的 DEFAULT_MANIFEST_URL，
//      运行时可用环境变量 VM_UPDATE_URL 覆盖。
//   2. 零新依赖：只用 Node 内置 http/https/fs/crypto，不需要 electron-updater，
//      也就不用联网装包、不用管签名工具链。
//   3. 默认指向 GitHub Releases（2026-09-14 起）。别名形如
//      `/releases/latest/download/latest.json` —— 永远指向最新 Release，发版不用改代码。
//      想要完全离线的机器可显式关掉：`VM_UPDATE_URL=off`（见 DISABLED_VALUES）。
//   4. 安全：清单里必须给 sha256，下载完校验通过才允许安装，防止下载损坏或被劫持。
//
// 清单 latest.json 格式：
//   {
//     "version": "0.3.0",
//     "notes":    "### 新增\n- 自动音高建议\n- 降噪强度三档\n### 修复\n- 弱人声被压断",
//     "pub_date": "2026-09-02T22:00:00Z",
//     "url":      "https://.../变声工坊-0.3.0-Setup.exe",
//     "sha256":   "小写 64 位十六进制",
//     "size":     123456789,        // 可选，用于展示体积
//     "mandatory": false            // 可选，true 时前端不给"跳过此版本"
//   }
const { app } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const http = require("http");
const https = require("https");

const REQ_TIMEOUT = 15000;
const MAX_REDIRECTS = 5;

// 路径都延迟计算：app.getPath 在 app ready 之前调用不安全
function updateDir() {
  return path.join(app.getPath("userData"), "updates");
}
function skipFile() {
  return path.join(app.getPath("userData"), "update-skip.json");
}

/**
 * 「已把安装包交给安装程序」的标记文件。
 * 它的唯一用途是让**下一次启动**知道：updates/ 里那些缓存包已经完成使命、可以删了。
 * 为什么不在 installUpdate 里直接删：安装包正被 NSIS 进程占用，Windows 不允许删除
 * （删了还会让安装中途失败）；而安装成功后应用已经退出，只能由新版本启动时来收尾。
 */
function pendingInstallFile() {
  return path.join(app.getPath("userData"), "update-install-pending.json");
}

/**
 * 更新检查结果落盘路径。
 * 默认写到 userData/update-check-result.json（供测试断言「收到更新提示」）。
 * 测试钩子 VM_UPDATE_TEST_AUTO 场景下，允许用 VM_UPDATE_TEST_RESULT 环境变量
 * 覆盖到一个脚本可控的路径（如 C:\vm_e2e），避免测试去猜 userData 真实位置。
 * 设了该变量时不需要 electron app 实例，便于纯 Node 单测。
 */
function updateCheckResultPath() {
  if (process.env.VM_UPDATE_TEST_RESULT) return String(process.env.VM_UPDATE_TEST_RESULT);
  return path.join(app.getPath("userData"), "update-check-result.json");
}

/** 当前版本（取自 package.json 的 version 字段） */
function currentVersion() {
  return app.getVersion();
}

// 更新源地址：默认走 GitHub Releases 的「最新版永久别名」。
//
// 为什么用 `/releases/latest/download/<asset>` 而不是指向某个具体 tag：
// 这条路径由 GitHub 动态解析到**最新**那个 Release 的同名 asset，所以发新版时
// 只要把 latest.json 和安装包挂到新 Release 上，客户端不用改任何配置就能拿到。
// 发版流程见 docs/release-sop.md（scripts\release.ps1 -Publish 一条命令完成）。
const DEFAULT_MANIFEST_URL =
  "https://github.com/mouxu66/voice-morph/releases/latest/download/latest.json";

// 显式关闭更新（回到"纯本地、零网络请求"）：VM_UPDATE_URL 设成这些值之一即可。
// 之所以需要它：默认源是非空的，光把环境变量留空已无法表达"我要离线"。
const DISABLED_VALUES = new Set(["0", "off", "none", "false", "disable", "disabled"]);

/** 更新源地址；返回空串 = 不检查更新（纯本地，不发任何网络请求） */
function manifestUrl() {
  const raw = String(process.env.VM_UPDATE_URL || DEFAULT_MANIFEST_URL || "").trim();
  if (DISABLED_VALUES.has(raw.toLowerCase())) return "";
  return raw;
}

/** 简易语义化版本比较：按数字段逐段比，忽略前缀 v 与后缀（如 -beta） */
function compareVersion(a, b) {
  const norm = (v) => String(v || "0").trim().replace(/^v/i, "").split("-")[0];
  const pa = norm(a).split(".").map((x) => parseInt(x, 10) || 0);
  const pb = norm(b).split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d !== 0) return d > 0 ? 1 : -1;
  }
  return 0;
}

/**
 * 发一个 GET 请求，自动跟随重定向（GitHub 的资源地址会 302 到 CDN）。
 * 返回 Promise<{ status, headers, buffer }>，任何异常都 reject。
 */
function request(urlStr, redirects = 0) {
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(urlStr);
    } catch {
      reject(new Error(`无效的更新地址：${urlStr}`));
      return;
    }
    const mod = url.protocol === "http:" ? http : https;
    const req = mod.get(
      url,
      {
        timeout: REQ_TIMEOUT,
        headers: { "User-Agent": "VoiceMorphStudio-Updater", Accept: "*/*" },
      },
      (res) => {
        const code = res.statusCode || 0;
        // 重定向：换地址重来
        if (code >= 300 && code < 400 && res.headers.location) {
          res.resume();
          if (redirects >= MAX_REDIRECTS) {
            reject(new Error("更新地址重定向次数过多"));
            return;
          }
          const next = new URL(res.headers.location, url).toString();
          request(next, redirects + 1).then(resolve, reject);
          return;
        }
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () =>
          resolve({ status: code, headers: res.headers, buffer: Buffer.concat(chunks) }));
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("请求更新信息超时"));
    });
  });
}

function loadSkipped() {
  try {
    return JSON.parse(fs.readFileSync(skipFile(), "utf-8")) || {};
  } catch {
    return {};
  }
}
function saveSkipped(obj) {
  try {
    fs.writeFileSync(skipFile(), JSON.stringify(obj), "utf-8");
  } catch {
    /* 记不住就算了，不影响主流程 */
  }
}

/** 算文件的 sha256（大文件流式读，不占内存） */
function sha256OfFile(file) {
  const h = crypto.createHash("sha256");
  const fd = fs.openSync(file, "r");
  const buf = Buffer.alloc(1024 * 1024);
  try {
    let n = fs.readSync(fd, buf, 0, buf.length, null);
    while (n > 0) {
      h.update(buf.subarray(0, n));
      n = fs.readSync(fd, buf, 0, buf.length, null);
    }
  } finally {
    fs.closeSync(fd);
  }
  return h.digest("hex");
}

/** 已下载好的安装包路径（存在则说明可直接安装，不用重下） */
function downloadedFile(manifest) {
  if (!manifest || !manifest.url) return null;
  const name = decodeURIComponent(String(manifest.url).split("?")[0].split("/").pop() || "");
  if (!name || !/\.exe$/i.test(name)) return null;
  const p = path.join(updateDir(), name);
  return fs.existsSync(p) ? p : null;
}

/** updates/ 下的文件列表（只列文件，忽略子目录） */
function listCached() {
  const dir = updateDir();
  try {
    return fs.readdirSync(dir)
      .map((n) => path.join(dir, n))
      .filter((p) => {
        try { return fs.statSync(p).isFile(); } catch { return false; }
      });
  } catch {
    return [];   // 目录还不存在
  }
}

/**
 * 清理 updates/ 里的下载缓存。调用时机 = 应用启动（见 update-ipc.cjs 的 registerUpdateIpc）。
 *
 * 规则（保守，绝不误删还没安装的包）：
 *   · 存在「待安装标记」→ 说明这份缓存已经交给安装程序了 → 清空整个目录 + 标记。
 *   · 没有标记 → 只清 `.part` 残片（下载中断留下的），其余保留，仍可复用缓存。
 * 删除失败（安装程序还占着文件）不算错：标记留着，下次启动再试。
 */
function sweepDownloadedPackages() {
  const removed = [];
  const failed = [];
  let handedOff = false;
  try {
    handedOff = fs.existsSync(pendingInstallFile());
  } catch { /* 读不到标记就当没有 */ }

  for (const p of listCached()) {
    const isPart = /\.part$/i.test(p);
    if (!handedOff && !isPart) continue;
    try {
      fs.unlinkSync(p);
      removed.push(path.basename(p));
    } catch {
      failed.push(path.basename(p));
    }
  }
  // 只有确认清干净了才摘掉标记，否则下次启动接着收拾
  if (handedOff && failed.length === 0) {
    try { fs.unlinkSync(pendingInstallFile()); } catch { /* 下次再来 */ }
  }
  return { removed, failed, handedOff };
}

/**
 * 检查更新。
 * 返回 { ok, configured, hasUpdate, current, latest, reason, hint }
 *  - configured=false：更新源被显式关闭（VM_UPDATE_URL=off），不算错误
 *  - hasUpdate：有新版本且未被"跳过此版本"
 *  - hint：reason 的可操作补充（能说清"接下来该做什么"时才有值）
 *
 * 关于 reason/hint 的分工（2026-09-16 补）：此前一切失败都只给 reason 一句
 * 「更新源返回 HTTP 404」，用户完全分不清是"还没发版"还是"网络断了"——
 * 而这两种情况的应对完全不同（等发版 vs 查网络/代理）。下面把可判定的
 * 情况拆开说清楚，判不出来的老实说"无法确定"。
 */
async function checkForUpdates() {
  const current = currentVersion();
  const url = manifestUrl();
  if (!url) {
    return {
      ok: true, configured: false, hasUpdate: false, current, latest: null,
      reason: "更新源已关闭（VM_UPDATE_URL=off），当前为纯本地模式",
      hint: "",
    };
  }
  const isGithubAlias = /github\.com\/.+\/releases\/latest\/download\//i.test(url);
  let res;
  try {
    res = await request(url);
  } catch (e) {
    return {
      ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: `无法连接更新源：${e.message}`,
      hint: "检查网络是否可用；若在用代理/VPN，确认它能访问 github.com（本应用不走系统代理时需要手动放行）。",
    };
  }
  if (res.status !== 200) {
    // GitHub 的 releases/latest 别名在「一个 Release 都没发过」时返回 404。
    // 这是当前（0.2.3 只在本机打过、从未发布）最可能的原因，直接说明白。
    if (res.status === 404 && isGithubAlias) {
      return {
        ok: false, configured: true, hasUpdate: false, current, latest: null,
        reason: "更新源还没有可用的版本（该 Release 尚未发布）",
        hint: "说明开发方还没发布过正式版本，当前已是最新。等发了 Release 再点检查即可。",
      };
    }
    if (res.status === 403 || res.status === 429) {
      return {
        ok: false, configured: true, hasUpdate: false, current, latest: null,
        reason: `更新源拒绝了请求（HTTP ${res.status}，通常是请求过于频繁）`,
        hint: "稍等几分钟再试。",
      };
    }
    return {
      ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: `更新源返回 HTTP ${res.status}`,
      hint: res.status >= 500
        ? "更新服务器暂时不可用，稍后再试。"
        : "确认更新地址是否正确（设置里的更新源，或环境变量 VM_UPDATE_URL）。",
    };
  }
  let m;
  try {
    m = JSON.parse(res.buffer.toString("utf-8"));
  } catch {
    return {
      ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: "更新清单不是合法 JSON",
      hint: "更新源返回了内容但格式不对，可能是被网络劫持或有登录页拦截，检查该地址在浏览器里能否直接打开。",
    };
  }
  const latest = {
    version: String(m.version || ""),
    notes: String(m.notes || m.changelog || ""),
    pub_date: String(m.pub_date || m.pubDate || ""),
    url: String(m.url || ""),
    sha256: String(m.sha256 || "").toLowerCase(),
    size: Number(m.size) || 0,
    mandatory: Boolean(m.mandatory),
  };
  if (!latest.version || !latest.url) {
    return {
      ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: "更新清单缺少 version 或 url 字段",
      hint: "更新清单文件不完整，请联系开发方或检查自建源的发布脚本。",
    };
  }
  if (compareVersion(latest.version, current) <= 0) {
    return { ok: true, configured: true, hasUpdate: false, current, latest,
      reason: "已是最新版本", hint: "" };
  }
  const skipped = loadSkipped();
  if (!latest.mandatory && skipped.latest === latest.version) {
    return { ok: true, configured: true, hasUpdate: false, current, latest,
      reason: `已跳过版本 ${latest.version}`, hint: "" };
  }
  return { ok: true, configured: true, hasUpdate: true, current, latest, reason: "", hint: "" };
}

/**
 * 下载安装包到 userData/updates/。
 * onProgress(pct, { received, total }) 用于把进度推给渲染层。
 * 返回 { ok, file, reason }
 */
function downloadUpdate(manifest, onProgress) {
  return new Promise((resolve) => {
    const url = String((manifest && manifest.url) || "");
    if (!url) {
      resolve({ ok: false, file: "", reason: "清单里没有下载地址" });
      return;
    }
    // 缓存复用必须重新校验：同一文件名但内容换过（重新上传过安装包）时要重下，
    // 否则会拿旧包装新版，装完版本号不变、更新死循环。
    const cached = downloadedFile(manifest);
    const want = String((manifest && manifest.sha256) || "").toLowerCase();
    if (cached && want) {
      let digest = "";
      try {
        digest = sha256OfFile(cached);
      } catch { /* 读不了就当没缓存 */ }
      if (digest === want) {
        resolve({ ok: true, file: cached, cached: true, reason: "" });
        return;
      }
      try {
        fs.unlinkSync(cached);   // 内容对不上，删掉重下
      } catch { /* ignore */ }
    }
    let target;
    try {
      // 相对 url 基于清单地址（VM_UPDATE_URL）解析；绝对 url 忽略 base。
      // 没配 VM_UPDATE_URL 时不传 base，避免 `new URL(absolute, "")` 抛错。
      const base = manifestUrl();
      target = base ? new URL(url, base) : new URL(url);
    } catch {
      resolve({ ok: false, file: "", reason: `无效下载地址：${url}` });
      return;
    }
    const mod = target.protocol === "http:" ? http : https;
    const name = decodeURIComponent(target.pathname.split("/").pop() || "update.exe");
    const dir = updateDir();
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      /* 建目录失败会在下面写文件时暴露 */
    }
    const tmpPath = path.join(dir, `${name}.part`);
    const finalPath = path.join(dir, name);

    const fail = (reason) => {
      try {
        if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
      } catch { /* 清理失败无所谓 */ }
      resolve({ ok: false, file: "", reason });
    };

    const run = (urlObj, redirects) => {
      const req = mod.get(
        urlObj,
        { timeout: 30000, headers: { "User-Agent": "VoiceMorphStudio-Updater" } },
        (res) => {
          const code = res.statusCode || 0;
          if (code >= 300 && code < 400 && res.headers.location) {
            res.resume();
            if (redirects >= MAX_REDIRECTS) { fail("下载地址重定向次数过多"); return; }
            run(new URL(res.headers.location, urlObj), redirects + 1);
            return;
          }
          if (code !== 200) { res.resume(); fail(`下载失败：HTTP ${code}`); return; }

          const total = Number(res.headers["content-length"]) || 0;
          let received = 0;
          const hash = crypto.createHash("sha256");
          const ws = fs.createWriteStream(tmpPath);
          res.on("data", (c) => {
            received += c.length;
            hash.update(c);
            if (onProgress && total) {
              onProgress(Math.min(100, Math.round((received / total) * 100)), { received, total });
            }
          });
          ws.on("error", () => fail("写入安装包失败（磁盘空间不足？）"));
          res.on("error", () => fail("下载中断"));
          res.on("end", () => {
            ws.end(() => {
              const digest = hash.digest("hex");
              const want = String((manifest && manifest.sha256) || "").toLowerCase();
              // sha256 是防损坏/防劫持的关键：清单没给就明确拒绝装，绝不"先装了再说"
              if (!want) { fail("清单未提供 sha256，已拒绝安装（安全策略）"); return; }
              if (digest !== want) {
                fail(`安装包校验失败：期望 ${want.slice(0, 12)}… 实际 ${digest.slice(0, 12)}…`);
                return;
              }
              try {
                fs.renameSync(tmpPath, finalPath);
              } catch {
                fail("安装包保存失败");
                return;
              }
              resolve({ ok: true, file: finalPath, reason: "" });
            });
          });
          res.pipe(ws);
        },
      );
      req.on("error", (e) => fail(`下载失败：${e.message}`));
      req.on("timeout", () => { req.destroy(); fail("下载超时"); });
    };
    run(target, 0);
  });
}

/**
 * 安装参数。默认无参（弹 NSIS 向导，由用户点「立即更新」）；
 * 测试钩子 VM_UPDATE_TEST_AUTO 开启时传 /S 静默安装，让干净 VM 里无人值守
 * e2e 能自动完成「下载→安装」整条链，不需要人去点 NSIS 向导。默认关闭。
 */
function installArgs() {
  return process.env.VM_UPDATE_TEST_AUTO ? ["/S"] : [];
}

/**
 * 安装并退出：NSIS 安装包 detached 拉起（脱离本进程，退出后仍然存活），
 * 然后立即 quit 让安装程序能覆盖文件。
 */
function installUpdate(file) {
  const p = String(file || "");
  if (!p || !fs.existsSync(p)) return { ok: false, reason: "安装包不存在，请重新下载" };
  // 先落标记再拉起安装程序：标记 = "这份缓存已交出去过"，下次启动据此清理（约 100MB）。
  // 写失败也不影响安装，只是缓存会多留一份，不值得因此中断更新。
  try {
    fs.writeFileSync(
      pendingInstallFile(),
      JSON.stringify({ file: p, at: new Date().toISOString() }),
      "utf-8",
    );
  } catch { /* ignore */ }
  try {
    spawn(p, installArgs(), { detached: true, stdio: "ignore" }).unref();
  } catch (e) {
    return { ok: false, reason: `启动安装程序失败：${e.message}` };
  }
  // 给安装程序一点拉起时间，再退出本体（否则可能被杀掉）
  setTimeout(() => app.quit(), 800);
  return { ok: true, reason: "" };
}

/** 记下"跳过此版本"，之后不再自动弹（手动点检查更新仍能看到） */
function skipVersion(version) {
  const s = loadSkipped();
  s.latest = String(version || "");
  s.at = new Date().toISOString();
  saveSkipped(s);
  return { ok: true };
}

module.exports = {
  currentVersion,
  manifestUrl,
  compareVersion,
  checkForUpdates,
  downloadUpdate,
  installUpdate,
  installArgs,
  skipVersion,
  downloadedFile,
  sweepDownloadedPackages,
  updateDir,
  pendingInstallFile,
  updateCheckResultPath,
};

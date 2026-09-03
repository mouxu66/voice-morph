// 自动更新：检查 → 下载 → 校验 → 安装（主进程模块，由 main.cjs 挂载 IPC）。
//
// 设计要点：
//   1. 托管无关：更新源就是一个静态清单 latest.json，放 GitHub Releases、对象存储、
//      网盘直链、甚至局域网共享目录都行 —— 地址由环境变量 VM_UPDATE_URL 指定。
//   2. 零新依赖：只用 Node 内置 http/https/fs/crypto，不需要 electron-updater，
//      也就不用联网装包、不用管签名工具链。
//   3. 纯本地默认：没配 VM_UPDATE_URL 就不发任何网络请求（默认行为完全离线），
//      检查更新按钮会明确提示"未配置更新源"。
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

/** 当前版本（取自 package.json 的 version 字段） */
function currentVersion() {
  return app.getVersion();
}

// 更新源地址：发新版前填一次即可（留空 = 纯本地，不检查更新）。
// 运行时可用环境变量 VM_UPDATE_URL 覆盖（调试 / 内网分发时方便改指向）。
const DEFAULT_MANIFEST_URL = "";

/** 更新源地址；空串 = 未配置，保持纯本地（不发任何网络请求） */
function manifestUrl() {
  return String(process.env.VM_UPDATE_URL || DEFAULT_MANIFEST_URL || "").trim();
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

/**
 * 检查更新。
 * 返回 { ok, configured, hasUpdate, current, latest, reason }
 *  - configured=false：没配更新源（保持离线），不算错误
 *  - hasUpdate：有新版本且未被"跳过此版本"
 */
async function checkForUpdates() {
  const current = currentVersion();
  const url = manifestUrl();
  if (!url) {
    return {
      ok: true, configured: false, hasUpdate: false, current, latest: null,
      reason: "未配置更新源（VM_UPDATE_URL），当前为纯本地模式",
    };
  }
  let res;
  try {
    res = await request(url);
  } catch (e) {
    return { ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: `无法连接更新源：${e.message}` };
  }
  if (res.status !== 200) {
    return { ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: `更新源返回 HTTP ${res.status}` };
  }
  let m;
  try {
    m = JSON.parse(res.buffer.toString("utf-8"));
  } catch {
    return { ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: "更新清单不是合法 JSON" };
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
    return { ok: false, configured: true, hasUpdate: false, current, latest: null,
      reason: "更新清单缺少 version 或 url 字段" };
  }
  if (compareVersion(latest.version, current) <= 0) {
    return { ok: true, configured: true, hasUpdate: false, current, latest,
      reason: "已是最新版本" };
  }
  const skipped = loadSkipped();
  if (!latest.mandatory && skipped.latest === latest.version) {
    return { ok: true, configured: true, hasUpdate: false, current, latest,
      reason: `已跳过版本 ${latest.version}` };
  }
  return { ok: true, configured: true, hasUpdate: true, current, latest, reason: "" };
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
      target = new URL(url);
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
 * 安装并退出：NSIS 安装包 detached 拉起（脱离本进程，退出后仍然存活），
 * 然后立即 quit 让安装程序能覆盖文件。
 */
function installUpdate(file) {
  const p = String(file || "");
  if (!p || !fs.existsSync(p)) return { ok: false, reason: "安装包不存在，请重新下载" };
  try {
    spawn(p, [], { detached: true, stdio: "ignore" }).unref();
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
  skipVersion,
  downloadedFile,
  updateDir,
};

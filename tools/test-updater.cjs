// 更新器专项测试（updater.cjs 全分支，无 UI、无真实安装包）：
//   版本比较 / 未配置源 / 连接失败 / 新版本判定 / 跳过版本 / 强制更新 /
//   下载缓存复用 / 安装包重传重下 / sha256 缺失拒绝 / 校验失败 / 安装包不存在。
//
// 运行：node tools/test-updater.cjs —— 退出码 0 = 通过，非 0 = 失败。
// 说明：用真实本地 loopback http.server（随机端口）当更新源，updater 全程真实请求，
//      不 stub http/https；只桩 electron（app.getPath userData + getVersion）。
const assert = require("node:assert");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

let currentVersion = "0.2.0";
let userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-updater-test-"));

const webRoot = path.join(__dirname, "..", "web");
const electronEntry = require.resolve("electron", { paths: [webRoot] });
require.cache[electronEntry] = {
  id: electronEntry, filename: electronEntry, loaded: true,
  exports: {
    app: {
      getPath: (name) => userDataDir,
      getVersion: () => currentVersion,
      quit: () => {},
    },
  },
};
const updater = require("../web/electron/updater.cjs");

(async function main() {
  let pass = 0, fail = 0;
  /** t() 只登记；main 末尾按登记顺序 await —— 测试共享 served.manifest /
   *  process.env.VM_UPDATE_URL 全局状态，必须串行，不能 Promise.all 并发。 */
  const checks = [];
  function t(name, fn) {
    checks.push({ name, fn });
  }

  // ---------- 版本比较 ----------
  t("compareVersion: 常规递增", () => {
    assert.ok(updater.compareVersion("0.3.0", "0.2.0") > 0);
    assert.ok(updater.compareVersion("0.2.0", "0.3.0") < 0);
    assert.strictEqual(updater.compareVersion("0.2.0", "0.2.0"), 0);
  });
  t("compareVersion: 忽略 v 前缀与 -beta 后缀", () => {
    assert.ok(updater.compareVersion("v1.2.3", "1.2.2") > 0);
    assert.strictEqual(updater.compareVersion("1.2.3-beta", "1.2.3"), 0);
  });
  t("compareVersion: 段数不同按最长对齐", () => {
    assert.ok(updater.compareVersion("1.10", "1.9") > 0);
    assert.strictEqual(updater.compareVersion("1.2", "1.2.0"), 0);
  });

  // ---------- 本地更新源（真实 loopback） ----------
  const served = { manifest: null, exeBytes: Buffer.alloc(0) };
  const server = http.createServer((req, res) => {
    if (req.url === "/latest.json") {
      const body = JSON.stringify(served.manifest);
      res.writeHead(200, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
      res.end(body);
    } else if (req.url === "/setup.exe") {
      res.writeHead(200, { "Content-Length": served.exeBytes.length });
      res.end(served.exeBytes);
    } else {
      res.writeHead(404); res.end();
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const BASE = `http://127.0.0.1:${server.address().port}`;

  function sha(bytes) {
    return crypto.createHash("sha256").update(bytes).digest("hex");
  }
  function makeManifest(overrides = {}) {
    return {
      version: "0.3.0",
      notes: "### 测试\n- 专项覆盖",
      pub_date: new Date().toISOString(),
      url: `${BASE}/setup.exe`,
      sha256: sha(served.exeBytes),
      size: served.exeBytes.length,
      mandatory: false,
      ...overrides,
    };
  }

  const withUrl = (fn) => {
    const prev = process.env.VM_UPDATE_URL;
    process.env.VM_UPDATE_URL = `${BASE}/latest.json`;
    try { return fn(); } finally { if (prev === undefined) delete process.env.VM_UPDATE_URL; else process.env.VM_UPDATE_URL = prev; }
  };
  const resetUserData = () => { userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-updater-test-")); };

  // ---------- checkForUpdates ----------
  t("checkForUpdates: 未配置源 = configured:false（纯本地不请求）", async () => {
    delete process.env.VM_UPDATE_URL;
    const r = await updater.checkForUpdates();
    assert.strictEqual(r.configured, false);
    assert.strictEqual(r.hasUpdate, false);
  });
  t("checkForUpdates: 连接失败 -> ok:false", async () => {
    process.env.VM_UPDATE_URL = "http://127.0.0.1:1/latest.json"; // 必然不可达端口
    const r = await updater.checkForUpdates();
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.hasUpdate, false);
    delete process.env.VM_UPDATE_URL;
  });
  t("checkForUpdates: 清单缺失 version/url -> ok:false", async () => {
    await withUrl(async () => {
      served.manifest = { version: "", url: "", sha256: "x" };
      const r = await updater.checkForUpdates();
      assert.strictEqual(r.ok, false);
      assert.match(r.reason, /缺少 version 或 url/);
    });
  });
  t("checkForUpdates: 新版本判定", async () => {
    await withUrl(async () => {
      served.manifest = makeManifest();
      const r = await updater.checkForUpdates();
      assert.strictEqual(r.ok, true);
      assert.strictEqual(r.hasUpdate, true);
      assert.strictEqual(r.latest.version, "0.3.0");
    });
  });
  t("checkForUpdates: 已是最新 -> hasUpdate:false", async () => {
    await withUrl(async () => {
      served.manifest = makeManifest({ version: "0.2.0" });
      const r = await updater.checkForUpdates();
      assert.strictEqual(r.hasUpdate, false);
    });
  });
  t("checkForUpdates: 跳过版本后不再报", async () => {
    resetUserData();
    await withUrl(async () => {
      served.manifest = makeManifest();
      assert.strictEqual((await updater.checkForUpdates()).hasUpdate, true);
      updater.skipVersion("0.3.0");
      assert.strictEqual((await updater.checkForUpdates()).hasUpdate, false);
    });
  });
  t("checkForUpdates: 强制更新不受跳过影响", async () => {
    resetUserData();
    await withUrl(async () => {
      served.manifest = makeManifest({ mandatory: true });
      updater.skipVersion("0.3.0");
      assert.strictEqual((await updater.checkForUpdates()).hasUpdate, true);
    });
  });

  // ---------- downloadUpdate ----------
  t("downloadUpdate: 清单无 sha256 拒绝下载（安全策略）", async () => {
    served.manifest = makeManifest({ sha256: "" });
    const r = await updater.downloadUpdate(served.manifest, () => {});
    assert.strictEqual(r.ok, false);
    assert.match(r.reason, /sha256/);
  });
  t("downloadUpdate: 正常下载 + 进度回调 + 缓存复用", async () => {
    resetUserData();
    served.exeBytes = crypto.randomBytes(512 * 1024);
    served.manifest = makeManifest();
    let progressCalls = 0;
    const r1 = await updater.downloadUpdate(served.manifest, () => { progressCalls += 1; });
    assert.strictEqual(r1.ok, true, r1.reason);
    assert.ok(fs.existsSync(r1.file));
    assert.ok(progressCalls > 0, "下载应推送进度");
    const r2 = await updater.downloadUpdate(served.manifest, () => {});
    assert.strictEqual(r2.ok, true);
    assert.strictEqual(r2.cached, true, "同名同 sha256 应复用缓存");
  });
  t("downloadUpdate: 安装包重传（同文件名）自动重下", async () => {
    resetUserData();
    served.exeBytes = crypto.randomBytes(128 * 1024);
    served.manifest = makeManifest();                 // 旧内容 A + sha(A)
    const r1 = await updater.downloadUpdate(served.manifest, () => {});
    assert.strictEqual(r1.ok, true);
    served.exeBytes = crypto.randomBytes(128 * 1024); // 服务器换新内容 B
    const fresh = makeManifest();                     // 清单 sha 同步为 sha(B)
    const r2 = await updater.downloadUpdate(fresh, () => {});
    assert.strictEqual(r2.ok, true, r2.reason);
    assert.notStrictEqual(r2.cached, true, "内容换版必须重下而不是复用旧文件");
    assert.strictEqual(sha(fs.readFileSync(r2.file)), fresh.sha256, "重下后磁盘内容必须匹配新 sha256");
  });
  t("downloadUpdate: 下载内容与清单 sha 不符 -> 拒绝", async () => {
    resetUserData();
    served.exeBytes = crypto.randomBytes(64 * 1024);
    served.manifest = makeManifest({ sha256: "a".repeat(64) }); // 假哈希，与内容必然不符
    const r = await updater.downloadUpdate(served.manifest, () => {});
    assert.strictEqual(r.ok, false);
    assert.match(r.reason, /校验失败/);
  });

  // ---------- installUpdate ----------
  t("installUpdate: 安装包不存在 -> ok:false", () => {
    const r = updater.installUpdate(path.join(os.tmpdir(), "vm-missing-setup.exe"));
    assert.strictEqual(r.ok, false);
  });

  // ---------- 收尾 ----------
  for (const { name, fn } of checks) {
    try { await fn(); pass += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { fail += 1; process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); }
  }
  server.close();
  process.stdout.write(`\n[test-updater] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();
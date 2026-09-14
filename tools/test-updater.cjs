// 更新器专项测试（updater.cjs 全分支，无 UI、无真实安装包）：
//   版本比较 / 默认源指向 GitHub / 显式关闭源 / 连接失败 / 新版本判定 / 跳过版本 /
//   强制更新 / 下载缓存复用 / 安装包重传重下 / sha256 缺失拒绝 / 校验失败 /
//   安装包不存在 / 拉起安装程序并落标记 / 安装后缓存清理。
//
// 运行：node tools/test-updater.cjs —— 退出码 0 = 通过，非 0 = 失败。
// 说明：用真实本地 loopback http.server（随机端口）当更新源，updater 全程真实请求，
//      不 stub http/https；只桩 electron（app.getPath userData + getVersion）与
//      child_process.spawn（防止测试真的去跑安装程序）。
const assert = require("node:assert");
const { installElectronStub } = require("./electron-stub.cjs");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

let currentVersion = "0.2.0";
let userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-updater-test-"));

// spawn 桩：installUpdate 会 detached 拉起安装程序 —— 测试里绝不能真跑 exe。
// updater.cjs 顶层写的是 `const { spawn } = require("child_process")`，解构在 require
// 时刻就把值取走了，所以必须在 require updater 之前替换（之后替换对它是无效的）。
const realSpawn = childProcess.spawn;
const spawnCalls = [];
childProcess.spawn = (cmd, args, opts) => {
  spawnCalls.push({ cmd, args, opts });
  return { unref() {} };
};

// 桩装在 require updater.cjs 之前；不依赖本机是否装过 web/node_modules
installElectronStub({
  app: {
    getPath: (name) => userDataDir,
    getVersion: () => currentVersion,
    quit: () => {},
  },
});
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

  // ---------- 更新源地址 ----------
  t("默认更新源：指向 GitHub Releases 的最新版永久别名", () => {
    const prev = process.env.VM_UPDATE_URL;
    try {
      delete process.env.VM_UPDATE_URL;
      assert.strictEqual(
        updater.manifestUrl(),
        "https://github.com/mouxu66/voice-morph/releases/latest/download/latest.json",
      );
    } finally {
      if (prev !== undefined) process.env.VM_UPDATE_URL = prev;
    }
  });
  t("默认更新源：用 latest 别名而非写死 tag（发版无需改代码）", () => {
    const prev = process.env.VM_UPDATE_URL;
    try {
      delete process.env.VM_UPDATE_URL;
      const u = updater.manifestUrl();
      assert.match(u, /^https:\/\//, "必须是 https");
      assert.match(u, /\/releases\/latest\/download\//, "必须走 latest 永久别名");
      assert.match(u, /latest\.json$/, "必须指向清单文件本身");
    } finally {
      if (prev !== undefined) process.env.VM_UPDATE_URL = prev;
    }
  });
  t("显式关闭源：VM_UPDATE_URL=off → 空串（纯本地）", () => {
    const prev = process.env.VM_UPDATE_URL;
    try {
      for (const v of ["off", "OFF", "0", "none", "false", "disabled"]) {
        process.env.VM_UPDATE_URL = v;
        assert.strictEqual(updater.manifestUrl(), "", `${v} 应关闭更新`);
      }
      process.env.VM_UPDATE_URL = "https://example.com/latest.json";
      assert.strictEqual(updater.manifestUrl(), "https://example.com/latest.json");
    } finally {
      if (prev === undefined) delete process.env.VM_UPDATE_URL;
      else process.env.VM_UPDATE_URL = prev;
    }
  });

  // ---------- checkForUpdates ----------
  t("checkForUpdates: 显式关闭源 = configured:false（纯本地不请求）", async () => {
    process.env.VM_UPDATE_URL = "off";
    try {
      const r = await updater.checkForUpdates();
      assert.strictEqual(r.configured, false);
      assert.strictEqual(r.hasUpdate, false);
    } finally {
      delete process.env.VM_UPDATE_URL;
    }
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
  t("downloadUpdate: 相对 url 基于清单地址解析", async () => {
    resetUserData();
    served.exeBytes = crypto.randomBytes(64 * 1024);
    // manifest.url 只给相对文件名；base 来自 VM_UPDATE_URL（= BASE/latest.json）
    await withUrl(async () => {
      const r = await updater.downloadUpdate(makeManifest({ url: "setup.exe" }), () => {});
      assert.strictEqual(r.ok, true, r.reason);
      assert.ok(fs.existsSync(r.file));
    });
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
  t("installUpdate: 拉起安装程序 + 落「待安装」标记", () => {
    resetUserData();
    const exe = path.join(userDataDir, "VoiceMorph-Setup-9.9.9.exe");
    fs.writeFileSync(exe, "fake-installer");
    const before = spawnCalls.length;
    const r = updater.installUpdate(exe);
    assert.strictEqual(r.ok, true, r.reason);
    assert.strictEqual(spawnCalls.length, before + 1, "应拉起安装程序一次");
    assert.strictEqual(spawnCalls[before].cmd, exe);
    assert.deepStrictEqual(spawnCalls[before].args, [], "默认应无参（弹 NSIS 向导）");
    assert.strictEqual(spawnCalls[before].opts.detached, true, "必须 detached：退出后安装程序仍存活");
    assert.ok(fs.existsSync(updater.pendingInstallFile()), "必须落标记，否则缓存永远清不掉");
  });

  // ---------- sweepDownloadedPackages（安装后清缓存） ----------
  function seedCache(names) {
    resetUserData();
    const dir = updater.updateDir();
    fs.mkdirSync(dir, { recursive: true });
    for (const n of names) fs.writeFileSync(path.join(dir, n), "cached");
    return dir;
  }
  t("sweep: 未交接过的下载包保留（还能复用缓存）", () => {
    const dir = seedCache(["VoiceMorph-Setup-9.9.9.exe"]);
    const r = updater.sweepDownloadedPackages();
    assert.deepStrictEqual(r.removed, []);
    assert.strictEqual(r.handedOff, false);
    assert.ok(fs.existsSync(path.join(dir, "VoiceMorph-Setup-9.9.9.exe")),
      "没点过「立即更新」的包不能删，否则用户得重下 100MB");
  });
  t("sweep: 已交接给安装程序 → 下次启动清空缓存与标记", () => {
    const dir = seedCache(["VoiceMorph-Setup-9.9.9.exe"]);
    fs.writeFileSync(updater.pendingInstallFile(), "{}");
    const r = updater.sweepDownloadedPackages();
    assert.deepStrictEqual(r.removed, ["VoiceMorph-Setup-9.9.9.exe"]);
    assert.deepStrictEqual(r.failed, []);
    assert.strictEqual(r.handedOff, true);
    assert.strictEqual(fs.existsSync(path.join(dir, "VoiceMorph-Setup-9.9.9.exe")), false);
    assert.strictEqual(fs.existsSync(updater.pendingInstallFile()), false, "清干净了才摘标记");
  });
  t("sweep: 无标记时也清掉中断下载的 .part 残片", () => {
    const dir = seedCache(["VoiceMorph-Setup-9.9.9.exe.part", "VoiceMorph-Setup-9.9.9.exe"]);
    const r = updater.sweepDownloadedPackages();
    assert.deepStrictEqual(r.removed, ["VoiceMorph-Setup-9.9.9.exe.part"]);
    assert.ok(fs.existsSync(path.join(dir, "VoiceMorph-Setup-9.9.9.exe")), "完整包保留");
  });
  t("sweep: 目录不存在时不炸", () => {
    resetUserData();   // 新 mkdtemp 里还没有 updates/
    const r = updater.sweepDownloadedPackages();
    assert.deepStrictEqual(r.removed, []);
    assert.deepStrictEqual(r.failed, []);
  });
  t("installArgs: 默认无参；VM_UPDATE_TEST_AUTO 开启时带 /S", () => {
    const prev = process.env.VM_UPDATE_TEST_AUTO;
    try {
      delete process.env.VM_UPDATE_TEST_AUTO;
      assert.deepStrictEqual(updater.installArgs(), []);
      process.env.VM_UPDATE_TEST_AUTO = "1";
      assert.deepStrictEqual(updater.installArgs(), ["/S"]);
    } finally {
      if (prev === undefined) delete process.env.VM_UPDATE_TEST_AUTO;
      else process.env.VM_UPDATE_TEST_AUTO = prev;
    }
  });

  // ---------- updateCheckResultPath ----------
  t("updateCheckResultPath: 未设 env 时回退 userData", () => {
    const prev = process.env.VM_UPDATE_TEST_RESULT;
    try {
      delete process.env.VM_UPDATE_TEST_RESULT;
      assert.strictEqual(
        updater.updateCheckResultPath(),
        path.join(userDataDir, "update-check-result.json"),
      );
    } finally {
      if (prev === undefined) delete process.env.VM_UPDATE_TEST_RESULT;
      else process.env.VM_UPDATE_TEST_RESULT = prev;
    }
  });
  t("updateCheckResultPath: 设 VM_UPDATE_TEST_RESULT 时优先返回该路径", () => {
    const prev = process.env.VM_UPDATE_TEST_RESULT;
    try {
      process.env.VM_UPDATE_TEST_RESULT = "C:\\vm_e2e\\update-check-result.json";
      assert.strictEqual(updater.updateCheckResultPath(), "C:\\vm_e2e\\update-check-result.json");
    } finally {
      if (prev === undefined) delete process.env.VM_UPDATE_TEST_RESULT;
      else process.env.VM_UPDATE_TEST_RESULT = prev;
    }
  });

  // ---------- 收尾 ----------
  for (const { name, fn } of checks) {
    try { await fn(); pass += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { fail += 1; process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); }
  }
  server.close();
  childProcess.spawn = realSpawn;
  process.stdout.write(`\n[test-updater] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();
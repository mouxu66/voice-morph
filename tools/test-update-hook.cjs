// 启动静默更新钩子（scheduleStartupUpdateCheck）专项测试：
//   VM_UPDATE_TEST_AUTO 测试钩子的完整触发逻辑 —— 成功路径 / 下载失败 / 检查失败
//   / 无更新 / 未设钩子 五种情况，并断言**结果文件是否写出、写了什么**。
//
// 运行：node tools/test-update-hook.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 为什么值得单测：这段逻辑只在 app.isPackaged=true 且设了 VM_UPDATE_TEST_AUTO 时才跑，
// 本机开发永远走不到；它又是 VM 端到端测试的**唯一观测点**——
// 一旦「下载失败时也静默不写结果文件」，测试只能看到「链路未完成」，无法定性。
const assert = require("node:assert");
const { installElectronStub } = require("./electron-stub.cjs");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");

let pass = 0;
let fail = 0;
function t(name, fn) {
  return (async () => {
    try {
      await fn();
      pass += 1;
      process.stdout.write(`  ✓ ${name}\n`);
    } catch (e) {
      fail += 1;
      process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`);
    }
  })();
}

// 用临时目录当 userData，并把结果文件路径指向临时目录
const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "vm-hook-test-"));
const resultPath = path.join(tmpRoot, "update-check-result.json");

// 桩掉 electron（app.isPackaged / getPath / getVersion）
// 桩装在 require updater/update-ipc 之前；不依赖本机是否装过 web/node_modules
const fakeApp = {
  isPackaged: true,
  getPath: () => tmpRoot,
  getVersion: () => "0.2.0",
  quit: () => {},
};
installElectronStub({ app: fakeApp, ipcMain: { handle: () => {}, on: () => {} } });

const updater = require("../web/electron/updater.cjs");
const { scheduleStartupUpdateCheck } = require("../web/electron/update-ipc.cjs");

const exeBytes = Buffer.from("FAKE-INSTALLER-BYTES-FOR-HOOK-TEST");
const exeSha = crypto.createHash("sha256").update(exeBytes).digest("hex");

let served = null;      // 当前 HTTP 服务返回的清单
let requests = [];      // 记录请求路径，用于断言「是否真的下载了 exe」

async function main() {
  // 本地 loopback http server 当更新源
  const server = http.createServer((req, res) => {
    requests.push(req.url);
    if (req.url === "/latest.json") {
      if (served === null) { res.writeHead(404); res.end("nf"); return; }
      const body = JSON.stringify(served);
      res.writeHead(200, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
      res.end(body);
      return;
    }
    if (req.url === "/VoiceMorph-Setup-0.2.2.exe") {
      const size = Number(served && served.size) || exeBytes.length;
      res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Length": size });
      // 若清单聲称的 size 大于真实字节数，则只发真实字节后直接 end（模拟截断）
      res.end(exeBytes);
      return;
    }
    res.writeHead(404);
    res.end("nf");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;

  const savedEnv = {};
  function setEnv(k, v) {
    if (!(k in savedEnv)) savedEnv[k] = process.env[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }

  function clearResult() {
    try { fs.unlinkSync(resultPath); } catch { /* 不存在即可 */ }
  }
  function readResult() {
    if (!fs.existsSync(resultPath)) return null;
    return JSON.parse(fs.readFileSync(resultPath, "utf-8"));
  }

  // 钩子延迟 12s，测试里用假的 setTimeout 立即执行。
  // 做法：临时把 global.setTimeout 换成立即调用（保留返回值形状）。
  async function runHookImmediately(timeoutMs = 4000) {
    const realSetTimeout = global.setTimeout;
    global.setTimeout = (fn) => {
      // 立即执行钩子回调，但把它推到一个可 await 的 promise 上
      hookPromise = Promise.resolve().then(() => fn());
      return { unref() {}, ref() {} };
    };
    let hookPromise = null;
    try {
      scheduleStartupUpdateCheck(null);
      // 等钩子回调链跑完（含 http 往返）
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        await new Promise((r) => realSetTimeout(r, 50));
        if (hookPromise) {
          await hookPromise.catch(() => {});
          // 再给内部 await 一点时间落盘
          await new Promise((r) => realSetTimeout(r, 150));
          break;
        }
      }
      await new Promise((r) => realSetTimeout(r, 100));
    } finally {
      global.setTimeout = realSetTimeout;
    }
  }

  process.stdout.write("[test-update-hook] VM_UPDATE_TEST_AUTO 触发逻辑\n");

  // --- 场景 0：未设钩子 → 不发请求、不写结果文件 ---
  await t("未设 VM_UPDATE_TEST_AUTO：正常检查但不下载、不写结果文件", async () => {
    clearResult();
    requests = [];
    setEnv("VM_UPDATE_TEST_AUTO", undefined);
    setEnv("VM_UPDATE_URL", `${base}/latest.json`);
    setEnv("VM_UPDATE_TEST_RESULT", resultPath);
    served = {
      version: "0.2.2", url: "VoiceMorph-Setup-0.2.2.exe",
      sha256: exeSha, size: exeBytes.length, notes: "", mandatory: false,
    };
    await runHookImmediately();
    assert.strictEqual(readResult(), null, "未设钩子时不应写结果文件");
    // 检查更新本身是**正常生产行为**（不受钩子影响），故会请求 latest.json；
    // 但**绝不能下载 exe**（那是钩子才做的），这是本用例的核心断言。
    assert.ok(
      !requests.some((u) => String(u).includes(".exe")),
      `未设钩子时不应下载安装包，实际=${JSON.stringify(requests)}`,
    );
  });

  // --- 场景 1：设钩子 + 有更新 + 下载成功 → 写结果文件，stage=download, downloadOk=true ---
  await t("设钩子+有更新+下载成功：写结果文件（downloadOk=true，含 file）", async () => {
    clearResult();
    requests = [];
    setEnv("VM_UPDATE_TEST_AUTO", "1");
    setEnv("VM_UPDATE_URL", `${base}/latest.json`);
    setEnv("VM_UPDATE_TEST_RESULT", resultPath);
    served = {
      version: "0.2.2", url: "VoiceMorph-Setup-0.2.2.exe",
      sha256: exeSha, size: exeBytes.length, notes: "test", mandatory: false,
    };
    await runHookImmediately();
    const r = readResult();
    assert.ok(r, "应写出结果文件");
    assert.strictEqual(r.stage, "download");
    assert.strictEqual(r.downloadOk, true, `downloadOk 应为 true，实际=${JSON.stringify(r)}`);
    assert.ok(r.file && fs.existsSync(r.file), "file 应指向已下载文件");
    assert.strictEqual(r.auto, true);
    // 应请求过 exe（真的下载了）
    assert.ok(
      requests.some((u) => String(u).includes("VoiceMorph-Setup-0.2.2.exe")),
      `应下载 exe，实际请求=${JSON.stringify(requests)}`,
    );
  });

  // --- 场景 2：设钩子 + 有更新 + 下载失败（sha256 不匹配）→ 仍写结果文件，带 error ---
  await t("设钩子+下载失败(sha256 不符)：仍写结果文件（downloadOk=false + error）", async () => {
    clearResult();
    requests = [];
    setEnv("VM_UPDATE_TEST_AUTO", "1");
    setEnv("VM_UPDATE_URL", `${base}/latest.json`);
    setEnv("VM_UPDATE_TEST_RESULT", resultPath);
    served = {
      version: "0.2.2", url: "VoiceMorph-Setup-0.2.2.exe",
      sha256: "a".repeat(64),   // 假哈希，必然不匹配
      size: exeBytes.length, notes: "", mandatory: false,
    };
    await runHookImmediately();
    const r = readResult();
    assert.ok(r, "下载失败时**也必须**写结果文件（这是本次修复的核心）");
    assert.strictEqual(r.downloadOk, false);
    assert.strictEqual(r.stage, "download");
    assert.ok(String(r.error || "").length > 0, "应带 error 字段说明失败原因");
  });

  // --- 场景 3：设钩子 + 更新源连不上 → 写结果文件，stage=check, ok=false ---
  await t("设钩子+更新源连不上：写结果文件（stage=check, ok=false, 带 error）", async () => {
    clearResult();
    requests = [];
    setEnv("VM_UPDATE_TEST_AUTO", "1");
    // 指向一个必然连不上的端口
    setEnv("VM_UPDATE_URL", "http://127.0.0.1:1/latest.json");
    setEnv("VM_UPDATE_TEST_RESULT", resultPath);
    served = null;
    await runHookImmediately();
    const r = readResult();
    assert.ok(r, "检查失败时也应写结果文件");
    assert.strictEqual(r.stage, "check");
    assert.strictEqual(r.ok, false);
    assert.ok(String(r.reason || r.error || "").length > 0, "应说明失败原因");
  });

  // --- 场景 4：设钩子 + 已是最新 → 写结果文件，hasUpdate=false ---
  await t("设钩子+已是最新版本：写结果文件（stage=check, hasUpdate=false）", async () => {
    clearResult();
    requests = [];
    setEnv("VM_UPDATE_TEST_AUTO", "1");
    setEnv("VM_UPDATE_URL", `${base}/latest.json`);
    setEnv("VM_UPDATE_TEST_RESULT", resultPath);
    served = {
      version: "0.2.0", url: "VoiceMorph-Setup-0.2.0.exe",
      sha256: exeSha, size: exeBytes.length, notes: "", mandatory: false,
    };
    await runHookImmediately();
    const r = readResult();
    assert.ok(r, "无更新时也应写结果文件，便于区分「没检测到更新」");
    assert.strictEqual(r.stage, "check");
    assert.strictEqual(r.hasUpdate, false);
  });

  // 还原环境
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  server.close();
  try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch { /* ignore */ }

  process.stdout.write(`\n[test-update-hook] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
  process.stdout.write(`\n[test-update-hook] 运行异常：${e && e.stack}\n`);
  process.exit(1);
});

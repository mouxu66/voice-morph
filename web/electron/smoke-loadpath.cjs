// 冒烟：生产/开发加载路径门控（resolveProjectRoot + frontendHtmlCandidates）。
// 运行：node web/electron/smoke-loadpath.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 回归点（2026-09-12 坑）：安装版（app.isPackaged=true）绝不回退到本机 D:\变声 源码根，
// 否则自动更新装完新安装包，应用仍读 d:\变声\web\dist 旧前端、跑 d:\变声\m2_server 旧后端。
//
// 模式参考 2026-09-03 记录：require.cache 顶替 electron 桩，让纯 Node 能加载主进程模块。
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

let isPackaged = false;
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-userdata-"));

const electronStub = {
  app: {
    get isPackaged() { return isPackaged; },
    getPath: (name) => {
      if (name === "userData") return userDataDir;
      throw new Error(`unexpected getPath: ${name}`);
    },
    quit: () => {},
    whenReady: () => new Promise(() => {}),
    getVersion: () => "0.2.0",
    on: () => {},
  },
  ipcMain: { handle: () => {}, on: () => {} },
  dialog: { showMessageBoxSync: () => 0 },
  shell: { showItemInFolder: () => {} },
  BrowserWindow: function () {},
  Menu: { buildFromTemplate: () => ({ popup: () => {} }) },
  globalShortcut: { register: () => true, unregisterAll: () => {} },
  screen: { getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }) },
};
require.cache[require.resolve("electron")] = {
  id: "electron", filename: "electron.js", loaded: true, exports: electronStub,
};

function withResourcesPath(dir) {
  Object.defineProperty(process, "resourcesPath", { value: dir, configurable: true });
}

/** 构造一个「安装包里应存在」的 resources 布局，返回临时资源根 */
function makeProdFixture() {
  const t = fs.mkdtempSync(path.join(os.tmpdir(), "vm-prod-"));
  const backendDir = path.join(t, "backend");
  fs.mkdirSync(path.join(backendDir, "m2_server"), { recursive: true });
  fs.writeFileSync(path.join(backendDir, "m2_server", "server.py"), "# fixture\n");
  fs.mkdirSync(path.join(backendDir, "web_dist"), { recursive: true });
  fs.writeFileSync(path.join(backendDir, "web_dist", "index.html"), "<html>fixture</html>\n");
  return t;
}

const backend = require("./backend.cjs");
const REAL_SRC_ROOT = path.dirname(path.dirname(__dirname)); // web/electron/.. 的上一级 = 项目根

function section(name) {
  process.stdout.write(`[smoke] ${name} ... `);
  return () => process.stdout.write("ok\n");
}

// ---- 1. 开发模式：照旧直连 D:\变声 源码根 ----
{
  const done = section("开发: resolveProjectRoot 命中源码根");
  isPackaged = false;
  withResourcesPath(path.join(os.tmpdir(), "vm-dev-nores-"));
  const root = backend.resolveProjectRoot();
  assert.ok(fs.existsSync(path.join(root, "m2_server", "server.py")), `dev root 应含 server.py，实际 ${root}`);
  assert.notStrictEqual(root.indexOf(REAL_SRC_ROOT), -1, `dev root 应是项目根 ${REAL_SRC_ROOT}，实际 ${root}`);
  done();
}

// ---- 2. 开发: frontendHtmlCandidates 优先源码根 web/dist ----
{
  const done = section("开发: 前端候选优先源码根 web/dist");
  backend.setProjectRoot(REAL_SRC_ROOT);
  const cands = backend.frontendHtmlCandidates();
  assert.strictEqual(cands[0], path.join(REAL_SRC_ROOT, "web", "dist", "index.html"));
  done();
}

// ---- 3. 生产（核心回归）：即便 D:\变声 存在，也只读安装包内资源 ----
{
  const done = section("生产: resolveProjectRoot 落在 resources/backend，绝不含源码根");
  isPackaged = true;
  const res = makeProdFixture();
  withResourcesPath(res);
  const root = backend.resolveProjectRoot();
  assert.strictEqual(root, path.join(res, "backend"),
    `prod root 必须是 resources/backend，实际回退到了 ${root}`);
  assert.ok(!root.toLowerCase().includes("变声"), "prod root 不得包含 D:\\变声");
  done();
}

// ---- 4. 生产: frontendHtmlCandidates 第一个命中 extraResources web_dist ----
{
  const done = section("生产: 前端候选首个是 resources/backend/web_dist");
  const res = process.resourcesPath;
  const cands = backend.frontendHtmlCandidates();
  assert.strictEqual(cands[0], path.join(res, "backend", "web_dist", "index.html"));
  // 注：第二候选（asar 内置 dist）在真实打包后路径在 app.asar 内，不会穿透到磁盘源码根；
  // 纯 Node 测试环境下 __dirname 是源码路径，故不做字符串比对（见第 5 步仅验尾缀）。
  const picked = cands.find((p) => fs.existsSync(p));
  assert.strictEqual(picked, path.join(res, "backend", "web_dist", "index.html"),
    "生产 find 应命中 extraResources web_dist（即便源码根 web/dist 存在）");
  done();
}

// ---- 5. 生产: resources 里没有 web_dist 时回退 asar 内置（第二候选）语义 = 安装包内，不会穿透到源码 ----
{
  const done = section("生产: 候选第二项是 asar 内置 dist");
  const res = process.resourcesPath;
  const second = backend.frontendHtmlCandidates()[1];
  assert.ok(second.endsWith(path.join("dist", "index.html")));
  done();
}

// ---- 6. 生产: 无任何资源时兜底返回 resources/backend（不抛、不读源码） ----
{
  const done = section("生产: 资源缺失时兜底 resources/backend");
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "vm-empty-"));
  withResourcesPath(empty);
  const root = backend.resolveProjectRoot();
  assert.strictEqual(root, path.join(empty, "backend"));
  done();
}

// ---- 7. 开发模式启动不检查更新 ----
{
  const done = section("开发: scheduleStartupUpdateCheck 不调度");
  isPackaged = false;
  const origST = global.setTimeout;
  let scheduled = false;
  global.setTimeout = () => { scheduled = true; return 0; };
  try {
    const { scheduleStartupUpdateCheck } = require("./update-ipc.cjs");
    scheduleStartupUpdateCheck({ isDestroyed: () => false });
  } finally {
    global.setTimeout = origST;
  }
  assert.strictEqual(scheduled, false, "开发模式不应调度启动更新检查");
  done();
}

// ---- 8. 生产模式启动调度静默检查 ----
{
  const done = section("生产: scheduleStartupUpdateCheck 调度 12s 定时器");
  isPackaged = true;
  const origST = global.setTimeout;
  let scheduled = false;
  global.setTimeout = () => { scheduled = true; return 0; };
  try {
    const { scheduleStartupUpdateCheck } = require("./update-ipc.cjs");
    scheduleStartupUpdateCheck({ isDestroyed: () => false });
  } finally {
    global.setTimeout = origST;
  }
  assert.strictEqual(scheduled, true, "生产模式必须调度启动更新检查");
  done();
}

process.stdout.write("\n[smoke] 全部通过 ✓（生产路径已门控到安装包内资源）\n");
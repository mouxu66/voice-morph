// 冒烟：生产/开发加载路径门控（resolveProjectRoot + frontendHtmlCandidates）。
// 运行：node web/electron/smoke-loadpath.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 回归点（2026-09-12 坑）：安装版（app.isPackaged=true）绝不回退到本机 D:\变声 源码根，
// 否则自动更新装完新安装包，应用仍读 d:\变声\web\dist 旧前端、跑 d:\变声\m2_server 旧后端。
//
// 模式参考 2026-09-03 记录：顶替 electron 桩，让纯 Node 能加载主进程模块。
// 2026-09-14：改用同目录的 `electron-stub.cjs`（合成 id，不查磁盘）—— 原来的
// `require.resolve("electron")` 要求本机装过 `web/node_modules`，于是 CI（backend job
// 不装 npm 包）上这个冒烟根本跑不起来，只能在本机凭自觉跑。
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { installElectronStub } = require("./electron-stub.cjs");

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
installElectronStub(electronStub);

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

// ---- 4. 生产: frontendHtmlCandidates 只有一个候选 = resources/backend/web_dist ----
{
  const done = section("生产: 前端候选唯一且是 resources/backend/web_dist");
  const res = process.resourcesPath;
  const cands = backend.frontendHtmlCandidates();
  assert.strictEqual(cands.length, 1, "生产模式必须是单候选，绝无 asar 兜底");
  assert.strictEqual(cands[0], path.join(res, "backend", "web_dist", "index.html"));
  assert.ok(!cands[0].toLowerCase().includes("变声"),
    "生产前端候选不得指向 D:\\变声 源码构建");
  const picked = cands.find((p) => fs.existsSync(p));
  assert.strictEqual(picked, path.join(res, "backend", "web_dist", "index.html"),
    "生产 find 应命中 extraResources web_dist（即便源码根 web/dist 存在）");
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

// ---- 9. 包外资源注入：VM_* 全部指向 D:\变声（模型/数据留包外） ----
{
  const done = section("包外资源: externalResourceEnv 注入 VM_* 指向 D:\\变声");
  const env = backend.externalResourceEnv();
  const realSrc = path.join("D:\\变声", "m2_server", "server.py");
  if (fs.existsSync(realSrc)) {
    // 本机（开发/打包同时在 D:\变声 上跑）：模型目录注入必须命中包外根源
    const ttsModels = path.join("D:\\变声", "tts_models");
    if (fs.existsSync(ttsModels)) {
      assert.strictEqual(env.VM_TTS_MODELS_DIR, ttsModels, "VM_TTS_MODELS_DIR 应指向 D:\\变声\\tts_models");
    }
    assert.ok(!("VM_TTS_MODELS_DIR" in env) || env.VM_TTS_MODELS_DIR.startsWith("D:\\变声"));
    assert.ok(!("VM_TTS_VENV_PY" in env) || env.VM_TTS_VENV_PY.startsWith("D:\\变声"));
    assert.ok(!("VM_PROJECT_ROOT" in env) || env.VM_PROJECT_ROOT === "D:\\变声");
  } else {
    // 分发机（无 D:\变声）：不得注入空的假路径
    assert.deepStrictEqual(env, {}, "无 D:\\变声 时不应注入任何 VM_ 路径变量");
  }
  done();
}

// ---- 10. 开发模式双入口守卫：update:check IPC 也不触发网络 ----
(async () => {
  const done = section("开发: update:check IPC 返回 dev 关闭响应，不调用 updater");
  isPackaged = false;
  const updater = require("./updater.cjs");
  const real = updater.checkForUpdates;
  let calls = 0;
  updater.checkForUpdates = async () => { calls++; return await real(); };
  try {
    const handlers = {};
    electronStub.ipcMain.handle = (name, fn) => { handlers[name] = fn; };
    const { registerUpdateIpc } = require("./update-ipc.cjs");
    registerUpdateIpc();
    const r = await handlers["update:check"]({});
    assert.strictEqual(calls, 0, "dev 模式 update:check 不应调用 checkForUpdates（不得发起网络）");
    assert.strictEqual(r.configured, false, "dev 模式应返回未配置更新源");
    assert.strictEqual(r.hasUpdate, false);
  } finally {
    updater.checkForUpdates = real;
    electronStub.ipcMain.handle = () => {};
  }
  done();
})();

// ---- 11. test-vm-env：spawn 环境 VM_ 注入规则 ----
{
  const done = section("环境: 不覆盖用户预设 / 未设兜底 / 无 D:\\变声 不注入");
  const root = "C:\\pkg\\backend";
  const dataRoot = "C:\\data\\vm";

  // A. 用户已设 → 用户赢（媒体/输出/TTS/RVC 全保留），PYTHONPATH/PYTHONIOENCODING 仍注入
  const userEnv = {
    VM_MEDIA_DIR: "C:\\user\\media",
    VM_OUTPUTS_DIR: "C:\\user\\out",
    VM_TTS_MODELS_DIR: "C:\\user\\tts",
    VM_RVC_ROOT: "E:\\RVC",
    EXISTING: "keep-me",
  };
  const envA = backend.buildBackendEnv(root, dataRoot, userEnv);
  assert.strictEqual(envA.VM_MEDIA_DIR, "C:\\user\\media", "用户 VM_MEDIA_DIR 不得被覆盖");
  assert.strictEqual(envA.VM_OUTPUTS_DIR, "C:\\user\\out");
  assert.strictEqual(envA.VM_TTS_MODELS_DIR, "C:\\user\\tts");
  assert.strictEqual(envA.VM_RVC_ROOT, "E:\\RVC", "VM_RVC_ROOT 仅用户可设，Electron 不注入");
  assert.strictEqual(envA.EXISTING, "keep-me");
  assert.strictEqual(envA.PYTHONIOENCODING, "utf-8");
  assert.ok(envA.PYTHONPATH.includes(root), "PYTHONPATH 应含后端根");

  // B. 未设 → VM_MEDIA_DIR/VM_OUTPUTS_DIR 兜底 dataRoot；Qwen 跟随推导不注入
  const envB = backend.buildBackendEnv(root, dataRoot, {});
  assert.strictEqual(envB.VM_MEDIA_DIR, path.join(dataRoot, "media"));
  assert.strictEqual(envB.VM_OUTPUTS_DIR, path.join(dataRoot, "outputs"));
  assert.ok(!("VM_QWEN_MODEL_DIR" in envB), "VM_QWEN_MODEL_DIR 跟随 TTS_MODELS_DIR 推导");

  // C. 模拟 D:\变声 不存在（externalResourceEnv 空）→ 不注入任何模型变量
  const realExt = backend.externalResourceEnv;
  backend.externalResourceEnv = () => ({});
  try {
    const envC = backend.buildBackendEnv(root, dataRoot, {});
    assert.ok(!("VM_TTS_MODELS_DIR" in envC), "无 D:\\变声 不得注入 VM_TTS_MODELS_DIR");
    assert.ok(!("VM_TTS_VENV_PY" in envC), "无 D:\\变声 不得注入 VM_TTS_VENV_PY");
    assert.ok(!("VM_PROJECT_ROOT" in envC), "无 D:\\变声 不得注入 VM_PROJECT_ROOT");
  } finally {
    backend.externalResourceEnv = realExt;
  }

  // D. 本机 D:\变声 存在 → 模型变量注入（与第 9 步同条件）
  if (fs.existsSync(path.join("D:\\变声", "m2_server", "server.py"))) {
    const envD = backend.buildBackendEnv(root, dataRoot, {});
    if (fs.existsSync(path.join("D:\\变声", "tts_models"))) {
      assert.strictEqual(envD.VM_TTS_MODELS_DIR, path.join("D:\\变声", "tts_models"));
    }
    if (fs.existsSync(path.join("D:\\变声", "tts_trial", "venv312", "Scripts", "python.exe"))) {
      assert.strictEqual(envD.VM_TTS_VENV_PY, path.join("D:\\变声", "tts_trial", "venv312", "Scripts", "python.exe"));
    }
    if (fs.existsSync(path.join("D:\\变声", "tts_trial", "venv312"))) {
      assert.strictEqual(envD.VM_PROJECT_ROOT, "D:\\变声");
    }
  }
  done();
}

// ---- 12. 数据根（用户状态放哪）+ 旧位置迁移 ----
// 2026-09-21 实测的坑：安装版曾经用「root 下有 media 吗」当探针，而 media/voicebank
// 是后端自己运行时创建的 ⇒ 跑过一次后探针永远为真，数据根翻转到安装目录，用户数据
// 从此写在 resources/backend 里（随版本覆盖 = 更新即丢）。实测证据：安装目录的
// outputs/market 的 mtime 就是操作当天，而 %APPDATA%\voice-morph-desktop\outputs 是空的。
{
  const done = section("数据根: 安装版固定 userData / 源码模式不动 / 迁移只补不改不删");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vm-dataroot-"));
  const rootWithMedia = path.join(tmp, "install", "backend");
  fs.mkdirSync(path.join(rootWithMedia, "media", "voicebank"), { recursive: true });

  // A. ★核心：安装版即使 root 自带 media，也必须把用户状态放 userData
  isPackaged = true;
  assert.strictEqual(
    backend.resolveDataRoot(rootWithMedia),
    userDataDir,
    "安装版的数据根必须是 userData —— root 下有 media 不能把用户数据钉在安装目录里",
  );
  // 旧规则确实会返回 root ⇒ 两者不同 ⇒ 会触发迁移（这就是要迁的原因）
  assert.strictEqual(backend.legacyDataRoot(rootWithMedia), rootWithMedia);
  assert.notStrictEqual(backend.resolveDataRoot(rootWithMedia), backend.legacyDataRoot(rootWithMedia));

  // B. 源码模式：root 自带 media → 数据留在仓库里（工具/测试都指着它）
  isPackaged = false;
  assert.strictEqual(backend.resolveDataRoot(rootWithMedia), rootWithMedia);
  assert.strictEqual(backend.legacyDataRoot(rootWithMedia), rootWithMedia, "源码模式新旧规则相同 ⇒ 不迁移");

  // C. media 子目录名单冻结：只迁用户产出的三个，别顺手把仓库素材（assets 等）搬走
  assert.deepStrictEqual(
    [...backend.USER_MEDIA_SUBDIRS].sort(),
    ["clips", "raw_videos", "voicebank"],
    "USER_MEDIA_SUBDIRS 变了？整体拷 media 会把仓库素材一起搬走",
  );

  // D. 迁移：旧 outputs 有文件 + 新位置空 → 拷齐并记账
  const legacy = path.join(tmp, "legacy");
  const fresh = path.join(tmp, "fresh");
  fs.mkdirSync(path.join(legacy, "outputs", "market"), { recursive: true });
  fs.writeFileSync(path.join(legacy, "outputs", "plugins.json"), '{"disabled":["sound.tts"]}\n');
  fs.writeFileSync(path.join(legacy, "outputs", "market", "a.pth"), "x".repeat(100));
  fs.mkdirSync(path.join(legacy, "media", "voicebank", "kangaroo"), { recursive: true });
  fs.writeFileSync(path.join(legacy, "media", "voicebank", "kangaroo", "ref.wav"), "riff");
  fs.mkdirSync(path.join(legacy, "media", "assets"), { recursive: true });
  fs.writeFileSync(path.join(legacy, "media", "assets", "builtin.png"), "not-user-data");

  const mig1 = backend.migrateLegacyData(legacy, fresh);
  assert.strictEqual(mig1.status, "migrated");
  assert.strictEqual(mig1.files, 2, "outputs 下两个文件都应拷过去");
  assert.ok(fs.existsSync(path.join(fresh, "outputs", "market", "a.pth")));
  assert.strictEqual(
    fs.readFileSync(path.join(fresh, "outputs", "plugins.json"), "utf-8"),
    '{"disabled":["sound.tts"]}\n',
    "能力开关必须跟着搬（否则用户会以为开关被重置了）",
  );
  assert.ok(
    fs.existsSync(path.join(fresh, "media", "voicebank", "kangaroo", "ref.wav")),
    "音色参考是用户数据，要迁",
  );
  assert.ok(
    !fs.existsSync(path.join(fresh, "media", "assets")),
    "media/assets 不是用户数据，不该被搬走",
  );
  assert.ok(fs.existsSync(path.join(legacy, "outputs", "market", "a.pth")), "**只拷不删**：旧位置必须原样保留");

  // E. 再跑一次：有记账 → 不重跑
  const mig2 = backend.migrateLegacyData(legacy, fresh);
  assert.strictEqual(mig2.status, "skipped");
  assert.match(mig2.reason, /迁移记录/);

  // F. 目标已有用户数据 → 不覆盖（只记一条「已跳过」）
  const legacy2 = path.join(tmp, "legacy2");
  const used = path.join(tmp, "used");
  fs.mkdirSync(path.join(legacy2, "outputs"), { recursive: true });
  fs.writeFileSync(path.join(legacy2, "outputs", "old.json"), "{}");
  fs.mkdirSync(path.join(used, "outputs"), { recursive: true });
  fs.writeFileSync(path.join(used, "outputs", "new.json"), '{"fresh":true}');
  const mig3 = backend.migrateLegacyData(legacy2, used);
  assert.strictEqual(mig3.status, "skipped");
  assert.strictEqual(fs.readFileSync(path.join(used, "outputs", "new.json"), "utf-8"), '{"fresh":true}');
  assert.ok(!fs.existsSync(path.join(used, "outputs", "old.json")), "新位置已有数据就不该把旧的掺进去");
  assert.ok(fs.existsSync(path.join(used, "outputs", backend.MIGRATE_MARKER)), "跳过也要记账，否则每次启动都重算");

  // G. 同一个根 / 缺参数 都是 no-op
  assert.strictEqual(backend.migrateLegacyData(fresh, fresh).status, "skipped");
  assert.strictEqual(backend.migrateLegacyData("", fresh).status, "skipped");

  isPackaged = false;
  fs.rmSync(tmp, { recursive: true, force: true });
  done();
}

process.stdout.write("\n[smoke] 全部通过 ✓（生产路径已门控，更新检查双入口 dev 守卫，数据根门控+迁移）\n");
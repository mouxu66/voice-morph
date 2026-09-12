// setup-ipc.cjs 集成冒烟：验证首启引导的 IPC 通道注册齐全、调用链不抛。
//
// 运行：node tools/test-setup-ipc.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 为什么需要单独一层：model-setup.cjs 是纯逻辑（已由 test-model-setup.cjs 覆盖），
// 但 registerSetupIpc / runFirstRunGuide 会真的调 electron 的 dialog 与 ipcMain。
// 这里用 require.cache 顶替 electron 模块，记录注册的通道并逐个真实调用，
// 验证「渲染层拿到的返回值形状」与「用户取消/选错路径」等分支都不会炸。
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

let userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-userdata-"));
// dialog 行为可切换：默认用户取消
let dialogPlan = { pickResult: { canceled: true }, msgChoice: 1 };
const dialogCalls = [];

const electronEntry = require.resolve("electron", { paths: [path.join(__dirname, "..", "web")] });
require.cache[electronEntry] = {
  id: electronEntry, filename: electronEntry, loaded: true,
  exports: {
    app: {
      isPackaged: true,
      getPath: (name) => (name === "userData" ? userDataDir : userDataDir),
      quit: () => { dialogCalls.push({ kind: "quit" }); },
    },
    ipcMain: {
      handle: (name, fn) => { handlers[name] = fn; },
    },
    dialog: {
      showMessageBoxSync: (_win, opts) => {
        // 兼容 (win, opts) 与 (opts) 两种调用形态
        const o = opts || _win;
        dialogCalls.push({ kind: "message", title: o.title, buttons: o.buttons });
        return dialogPlan.msgChoice;
      },
      showOpenDialog: async (...args) => {
        const opts = args.length > 1 ? args[1] : args[0];
        dialogCalls.push({ kind: "open", title: opts.title });
        return dialogPlan.pickResult;
      },
    },
    shell: { showItemInFolder: () => { dialogCalls.push({ kind: "reveal" }); } },
    BrowserWindow: function () {},
  },
};

const handlers = {};
const appConfig = require("../web/electron/app-config.cjs");
const setup = require("../web/electron/setup-ipc.cjs");

// ---------- 夹具 ----------
function makeFullEnv() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipcroot-"));
  const models = path.join(root, "tts_models");
  fs.mkdirSync(path.join(models, "qwen3-tts-1.7b-base"), { recursive: true });
  fs.writeFileSync(path.join(models, "qwen3-tts-1.7b-base", "config.json"), "{}", "utf-8");
  fs.mkdirSync(path.join(models, "qwen3-tts-tokenizer-12hz"), { recursive: true });
  const py = path.join(root, "tts_trial", "venv312", "Scripts", "python.exe");
  fs.mkdirSync(path.dirname(py), { recursive: true });
  fs.writeFileSync(py, "stub", "utf-8");
  const rvc = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipcrvc-"));
  fs.mkdirSync(path.join(rvc, "logs"), { recursive: true });
  return { root, models, py, rvc };
}

const checks = [];
function t(name, fn) { checks.push({ name, fn }); }

t("registerSetupIpc: 六个通道全部注册", () => {
  setup.registerSetupIpc(() => null);
  for (const ch of ["setup:status", "setup:pick-dir", "setup:save", "setup:dismiss",
                    "setup:run-wizard", "setup:reset", "setup:show-config"]) {
    assert.strictEqual(typeof handlers[ch], "function", `缺少通道 ${ch}`);
  }
});

t("setup:status: 未配置时返回 missing 全列，且不需要任何输入", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-empty-"));
  const st = await handlers["setup:status"]({});
  assert.strictEqual(st.allOk, false);
  assert.ok(st.missing.length >= 1);
  assert.ok(st.configPath.endsWith("config.json"));
  assert.strictEqual(typeof st.setupSeen, "boolean");
  // items 形状必须是渲染层能直接消费的
  for (const item of st.items) {
    assert.ok(typeof item.key === "string");
    assert.ok(typeof item.label === "string");
    assert.strictEqual(typeof item.ok, "boolean");
    assert.ok(["config", "env", "derived", "none"].includes(item.source), `source 非法：${item.source}`);
  }
});

t("setup:pick-dir: 用户取消 → 返回 { canceled: true }，不落盘", async () => {
  dialogPlan.pickResult = { canceled: true };
  const before = appConfig.load();
  const r = await handlers["setup:pick-dir"]({}, "tts_models");
  assert.strictEqual(r.canceled, true);
  assert.deepStrictEqual(appConfig.load(), before, "取消不应改动配置");
});

t("setup:pick-dir: 选中合法目录 → 返回路径且 ok=true，仍不落盘", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-pick-"));
  const { models } = makeFullEnv();
  dialogPlan.pickResult = { canceled: false, filePaths: [models] };
  const r = await handlers["setup:pick-dir"]({}, "tts_models");
  assert.strictEqual(r.canceled, false);
  assert.strictEqual(r.path, models);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(appConfig.load().ttsModelsDir, "", "取目录阶段不落盘，须显式 setup:save");
});

t("setup:pick-dir: 选中不合法目录 → 提示后用户选「仍然使用」返回 ok=false", async () => {
  const bogus = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-bogus-"));
  dialogPlan.pickResult = { canceled: false, filePaths: [bogus] };
  dialogPlan.msgChoice = 1; // 「仍然使用」
  const r = await handlers["setup:pick-dir"]({}, "tts_models");
  assert.strictEqual(r.canceled, false);
  assert.strictEqual(r.ok, false, "校验不过必须如实告知");
  assert.ok(r.reason.length > 0);
});

t("setup:pick-dir: 校验不过时选「取消」→ 返回 canceled", async () => {
  const bogus = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-bogus2-"));
  dialogPlan.pickResult = { canceled: false, filePaths: [bogus] };
  dialogPlan.msgChoice = 2; // 「取消」
  const r = await handlers["setup:pick-dir"]({}, "rvc_root");
  assert.strictEqual(r.canceled, true);
  assert.ok(!fs.existsSync(path.join(bogus, "logs")), "不应凭空建目录");
});

t("setup:pick-dir: 未知 kind → 安全返回 canceled", async () => {
  const r = await handlers["setup:pick-dir"]({}, "not_a_real_kind");
  assert.strictEqual(r.canceled, true);
});

t("setup:save: 写入配置后 status 立即反映为已就绪", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-save-"));
  const { models, rvc } = makeFullEnv();
  const r = await handlers["setup:save"]({}, { ttsModelsDir: models, rvcRoot: rvc });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.allOk, true, "完整配置保存后应 reported allOk");
  assert.strictEqual(r.ttsOk, true);
  assert.strictEqual(r.rvcOk, true);
  assert.deepStrictEqual(r.missing, []);
});

t("setup:save: 只传部分字段不清空其他字段", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-partial-"));
  const { models, rvc } = makeFullEnv();
  await handlers["setup:save"]({}, { ttsModelsDir: models, rvcRoot: rvc });
  const r = await handlers["setup:save"]({}, { setupSeen: true });
  assert.strictEqual(r.config.ttsModelsDir, models, "只改 setupSeen 不得丢路径");
  assert.strictEqual(r.config.rvcRoot, rvc);
  assert.strictEqual(r.setupSeen, true);
});

t("setup:dismiss: 标记已看过，不再自动弹引导", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-dismiss-"));
  const r = await handlers["setup:dismiss"]({});
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.setupSeen, true);
  assert.strictEqual(appConfig.load().setupSeen, true);
});

t("setup:reset: 清空路径并把 setupSeen 复位（可重新走引导）", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-reset-"));
  const { models } = makeFullEnv();
  await handlers["setup:save"]({}, { ttsModelsDir: models, setupSeen: true });
  const r = await handlers["setup:reset"]({});
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.config.ttsModelsDir, "");
  assert.strictEqual(r.setupSeen, false, "复位后应能重新弹引导");
});

t("setup:show-config: 调起资源管理器定位 config.json", async () => {
  dialogCalls.length = 0;
  const r = await handlers["setup:show-config"]({});
  assert.strictEqual(r.ok, true);
  assert.ok(dialogCalls.some((c) => c.kind === "reveal"), "应调用 showItemInFolder");
});

t("runFirstRunGuide: 已全部就绪 → 直接返回 false，不弹任何窗", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-guide-ok-"));
  const { models, rvc } = makeFullEnv();
  appConfig.save({ ttsModelsDir: models, rvcRoot: rvc });
  dialogCalls.length = 0;
  const changed = await setup.runFirstRunGuide(null);
  assert.strictEqual(changed, false);
  assert.strictEqual(dialogCalls.length, 0, "全就绪时不该弹引导");
});

t("runFirstRunGuide: 用户选「稍后配置」→ 标 setupSeen 且返回 false（不阻塞）", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-guide-later-"));
  dialogPlan.msgChoice = 1; // 「稍后配置」
  dialogCalls.length = 0;
  const changed = await setup.runFirstRunGuide(null);
  assert.strictEqual(changed, false, "稍后配置不应触发后端重启");
  assert.strictEqual(appConfig.load().setupSeen, true, "必须落盘 setupSeen，否则每次启动都弹");
  assert.ok(dialogCalls.some((c) => c.kind === "message"), "应弹过引导对话框");
});

t("runFirstRunGuide: 用户选「退出应用」→ 调 app.quit 且不写配置", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-guide-quit-"));
  dialogPlan.msgChoice = 2; // 「退出应用」
  dialogCalls.length = 0;
  const changed = await setup.runFirstRunGuide(null);
  assert.strictEqual(changed, false);
  assert.ok(dialogCalls.some((c) => c.kind === "quit"), "应调用 app.quit");
  assert.strictEqual(appConfig.load().setupSeen, false, "退出流程不该改配置");
});

t("runFirstRunGuide: 选「现在配置」且用户选好目录 → 返回 true 并落盘", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-guide-now-"));
  const { models, rvc } = makeFullEnv();
  dialogPlan.msgChoice = 0; // 「现在配置」
  // 向导会按缺失项依次取目录：第 1 次给 tts_models，第 2 次给 rvc，其余取消
  const queue = [
    { canceled: false, filePaths: [models] },
    { canceled: false, filePaths: [models] },  // tts_venv（会被推导，但向导仍会问）
    { canceled: false, filePaths: [rvc] },
  ];
  const orig = dialogPlan.pickResult;
  let i = 0;
  const realOpen = require.cache[electronEntry].exports.dialog.showOpenDialog;
  require.cache[electronEntry].exports.dialog.showOpenDialog = async (...args) => {
    const o = args.length > 1 ? args[1] : args[0];
    if (String(o.title).includes("tts_models")) return queue[0];
    if (String(o.title).includes("解释器")) return { canceled: true }; // 跳过（可推导）
    if (String(o.title).includes("RVC")) return queue[2];
    return { canceled: true };
  };
  try {
    const changed = await setup.runFirstRunGuide(null);
    assert.strictEqual(changed, true, "有配置写入时应返回 true 以触发后端重启");
    const c = appConfig.load();
    assert.strictEqual(c.ttsModelsDir, models);
    assert.strictEqual(c.rvcRoot, rvc);
    assert.strictEqual(c.setupSeen, true);
  } finally {
    require.cache[electronEntry].exports.dialog.showOpenDialog = realOpen;
    dialogPlan.pickResult = orig;
  }
});

t("runSetupWizard: 全部取消 → 返回 false，不写任何配置", async () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-ipc-wizard-cancel-"));
  const realOpen = require.cache[electronEntry].exports.dialog.showOpenDialog;
  require.cache[electronEntry].exports.dialog.showOpenDialog = async () => ({ canceled: true });
  try {
    const changed = await setup.runSetupWizard(null, { onlyMissing: false });
    assert.strictEqual(changed, false);
    assert.strictEqual(appConfig.load().ttsModelsDir, "");
  } finally {
    require.cache[electronEntry].exports.dialog.showOpenDialog = realOpen;
  }
});

// ---------- 收尾 ----------
(async function main() {
  let pass = 0, fail = 0;
  for (const { name, fn } of checks) {
    try {
      await fn();
      pass += 1;
      process.stdout.write(`  ✓ ${name}\n`);
    } catch (e) {
      fail += 1;
      process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`);
    }
  }
  process.stdout.write(`\n[test-setup-ipc] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();

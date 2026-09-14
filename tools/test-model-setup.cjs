// 首启模型引导专项测试（app-config.cjs + model-setup.cjs + externalResourceEnv 注入链）
//
// 运行：node tools/test-model-setup.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 覆盖：
//   A. 配置读写：缺失/损坏/原子写/字段规整/重置
//   B. 路径校验：TTS 模型（缺 config.json、缺 tokenizer）、venv、RVC 根
//   C. 路径推导：从 tts_models 推出 venv312 解释器与项目根
//   D. 注入链：配置存在 → 注入 VM_*；配置缺失 → 不注入；用户进程 env 仍优先
//
// 用真 electron 桩（require.cache 顶替）+ 真临时目录树，不 mock fs。
const assert = require("node:assert");
const { installElectronStub } = require("./electron-stub.cjs");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

let userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-setup-userdata-"));
// 桩装在 any `web/electron/*.cjs` 之前；不依赖本机是否装过 web/node_modules
const electronEntry = installElectronStub({
  app: {
    isPackaged: false,
    getPath: (name) => (name === "userData" ? userDataDir : userDataDir),
    quit: () => {},
  },
  ipcMain: { handle: () => {} },
  dialog: { showMessageBoxSync: () => 0, showOpenDialog: async () => ({ canceled: true }) },
  shell: { showItemInFolder: () => {} },
  BrowserWindow: function () {},
});

const appConfig = require("../web/electron/app-config.cjs");
const modelSetup = require("../web/electron/model-setup.cjs");
const backend = require("../web/electron/backend.cjs");

const checks = [];
function t(name, fn) { checks.push({ name, fn }); }

// ---------- 夹具：构造一棵"完整可用"的模型目录树 ----------
/** 建一个假的 tts_models 目录（可选省略某些子项以模拟损坏） */
function makeTtsModels({ withModel = true, withTokenizer = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vm-models-"));
  const models = path.join(root, "tts_models");
  fs.mkdirSync(models, { recursive: true });
  if (withModel) {
    const m = path.join(models, "qwen3-tts-1.7b-base");
    fs.mkdirSync(m, { recursive: true });
    fs.writeFileSync(path.join(m, "config.json"), "{}", "utf-8");
  }
  if (withTokenizer) {
    fs.mkdirSync(path.join(models, "qwen3-tts-tokenizer-12hz"), { recursive: true });
  }
  return { root, models };
}

/** 在给定项目根下建 venv312 解释器 */
function makeVenv(projectRoot) {
  const py = path.join(projectRoot, "tts_trial", "venv312", "Scripts", "python.exe");
  fs.mkdirSync(path.dirname(py), { recursive: true });
  fs.writeFileSync(py, "stub", "utf-8");
  return py;
}

/** 建一个像模像样的 RVC 整合包 */
function makeRvcRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vm-rvc-"));
  fs.mkdirSync(path.join(root, "logs"), { recursive: true });
  fs.mkdirSync(path.join(root, "tools"), { recursive: true });
  return root;
}

// ============================================================
// A. 配置读写
// ============================================================

t("app-config: 文件缺失时返回默认值，不抛异常", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-empty-cfg-"));
  const c = appConfig.load();
  assert.strictEqual(c.ttsModelsDir, "");
  assert.strictEqual(c.rvcRoot, "");
  assert.strictEqual(c.setupSeen, false);
  assert.strictEqual(c.version, 1);
});

t("app-config: 保存后能读回（原子写不留 tmp 残渣）", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-cfg-"));
  const ok = appConfig.save({ ttsModelsDir: "E:\\models\\tts_models", setupSeen: true });
  assert.strictEqual(ok, true);
  const c = appConfig.load();
  assert.strictEqual(c.ttsModelsDir, "E:\\models\\tts_models");
  assert.strictEqual(c.setupSeen, true);
  assert.ok(fs.existsSync(appConfig.configPath()));
  assert.ok(!fs.existsSync(`${appConfig.configPath()}.tmp`), "临时文件应已被 rename 消费掉");
});

t("app-config: 文件损坏（非法 JSON）时回落默认值，不抛", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-broken-cfg-"));
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(appConfig.configPath(), "{ this is not json", "utf-8");
  const c = appConfig.load();
  assert.strictEqual(c.ttsModelsDir, "");
  assert.strictEqual(c.setupSeen, false);
});

t("app-config: 字段规整 —— 去空白/去包裹引号/丢弃未知键/类型不符回落", () => {
  const n = appConfig.normalize({
    ttsModelsDir: '  "D:\\变声\\tts_models"  ',
    ttsVenvPy: 12345,            // 类型不对 → 回落空
    rvcRoot: "D:\\RVC",
    setupSeen: "yes",            // 非 true → false
    unknownKey: "should-be-dropped",
  });
  assert.strictEqual(n.ttsModelsDir, "D:\\变声\\tts_models", "应去掉首尾空白与包裹引号");
  assert.strictEqual(n.ttsVenvPy, "", "非字符串应回落空值");
  assert.strictEqual(n.rvcRoot, "D:\\RVC");
  assert.strictEqual(n.setupSeen, false, "非布尔 true 一律 false");
  assert.ok(!("unknownKey" in n), "未知键应被丢弃");
});

t("app-config: 部分保存不丢已有字段（merge 语义）", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-merge-cfg-"));
  appConfig.save({ ttsModelsDir: "E:\\a", rvcRoot: "E:\\rvc" });
  appConfig.save({ setupSeen: true });
  const c = appConfig.load();
  assert.strictEqual(c.ttsModelsDir, "E:\\a", "setupSeen 的写入不得清掉已有路径");
  assert.strictEqual(c.rvcRoot, "E:\\rvc");
  assert.strictEqual(c.setupSeen, true);
});

t("app-config: reset 清空路径但保留 setupSeen", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-reset-cfg-"));
  appConfig.save({ ttsModelsDir: "E:\\a", rvcRoot: "E:\\rvc", setupSeen: true });
  appConfig.reset();
  const c = appConfig.load();
  assert.strictEqual(c.ttsModelsDir, "");
  assert.strictEqual(c.rvcRoot, "");
  assert.strictEqual(c.setupSeen, true, "reset 只清路径，不该让引导重新弹");
});

t("app-config: anyConfigured 判定", () => {
  assert.strictEqual(appConfig.anyConfigured({ ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "" }), false);
  assert.strictEqual(appConfig.anyConfigured({ ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "D:\\RVC" }), true);
});

// ============================================================
// B. 路径校验
// ============================================================

t("checkTtsModels: 完整目录通过", () => {
  const { models } = makeTtsModels();
  assert.strictEqual(modelSetup.checkTtsModels(models).ok, true);
});

t("checkTtsModels: 只有模型没有分词器 → 失败（不能只判目录存在）", () => {
  const { models } = makeTtsModels({ withTokenizer: false });
  const r = modelSetup.checkTtsModels(models);
  assert.strictEqual(r.ok, false);
  assert.ok(r.reason.includes("qwen3-tts-tokenizer-12hz"), `原因应点名缺哪个，实际：${r.reason}`);
});

t("checkTtsModels: 模型目录缺 config.json → 失败", () => {
  const { models } = makeTtsModels({ withModel: false });
  const r = modelSetup.checkTtsModels(models);
  assert.strictEqual(r.ok, false);
  assert.ok(r.reason.includes("config.json"));
});

t("checkTtsModels: 空值与不存在路径 → 失败且不抛", () => {
  assert.strictEqual(modelSetup.checkTtsModels("").ok, false);
  assert.strictEqual(modelSetup.checkTtsModels("").reason, "未配置");
  assert.strictEqual(modelSetup.checkTtsModels("Z:\\no\\such\\dir").ok, false);
});

t("checkTtsModels: 用户误选项目根（D:\\变声）→ 失败并提示去选 tts_models", () => {
  // 项目根下没有 qwen3-tts-1.7b-base 直接子目录
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), "vm-projroot-"));
  const r = modelSetup.checkTtsModels(projectRoot);
  assert.strictEqual(r.ok, false);
});

t("checkTtsVenv: 文件存在通过，目录/不存在失败", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vm-venv-"));
  const py = makeVenv(root);
  assert.strictEqual(modelSetup.checkTtsVenv(py).ok, true);
  assert.strictEqual(modelSetup.checkTtsVenv(path.dirname(py)).ok, false, "指向目录不算");
  assert.strictEqual(modelSetup.checkTtsVenv("").ok, false);
});

t("checkRvcRoot: 含 logs/ 通过；空目录/不存在失败", () => {
  const rvc = makeRvcRoot();
  assert.strictEqual(modelSetup.checkRvcRoot(rvc).ok, true);
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "vm-empty-rvc-"));
  assert.strictEqual(modelSetup.checkRvcRoot(empty).ok, false);
  assert.ok(modelSetup.checkRvcRoot(empty).reason.includes("不像 RVC 整合包"));
  assert.strictEqual(modelSetup.checkRvcRoot("").ok, false);
});

// ============================================================
// C. 路径推导
// ============================================================

t("deriveTtsChain: 选 tts_models 目录 → 自动推出 venv312 解释器", () => {
  const { root, models } = makeTtsModels();
  const py = makeVenv(root);
  const chain = modelSetup.deriveTtsChain(models);
  assert.strictEqual(chain.projectRoot, root);
  assert.strictEqual(chain.venvPy, py);
  assert.strictEqual(chain.found, true);
});

t("deriveTtsChain: 用户直接选项目根 → 同样能推出（venv 不存在则 found=false）", () => {
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), "vm-bare-root-"));
  const chain = modelSetup.deriveTtsChain(bare);
  assert.strictEqual(chain.projectRoot, bare);
  assert.strictEqual(chain.found, false, "没有 venv312 时不应谎报 found");
});

t("resolveConfig: 手填 venv 优先于推导结果", () => {
  const { root, models } = makeTtsModels();
  makeVenv(root);
  const manual = "X:\\manual\\python.exe";
  const r = modelSetup.resolveConfig({ ttsModelsDir: models, ttsVenvPy: manual, rvcRoot: "" });
  assert.strictEqual(r.ttsVenvPy, manual, "用户手填必须赢");
});

t("resolveConfig: 只有 rvcRoot 时 TTS 相关字段为空", () => {
  const r = modelSetup.resolveConfig({ ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "D:\\RVC" });
  assert.strictEqual(r.ttsModelsDir, "");
  assert.strictEqual(r.ttsVenvPy, "");
  assert.strictEqual(r.projectRoot, "");
  assert.strictEqual(r.rvcRoot, "D:\\RVC");
});

t("checkModelEnv: 全缺失时 ttsOk/rvcOk 均 false 且 missing 列全", () => {
  const st = modelSetup.checkModelEnv(
    { ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "" },
    {}, // 无环境变量
  );
  assert.strictEqual(st.ttsOk, false);
  assert.strictEqual(st.rvcOk, false);
  assert.deepStrictEqual(st.missing, ["tts_models", "tts_venv", "rvc_root"]);
  assert.strictEqual(st.allOk, false);
});

t("checkModelEnv: 全配好时 allOk，且 source 标注为 config/derived", () => {
  const { root, models } = makeTtsModels();
  const py = makeVenv(root);
  const rvc = makeRvcRoot();
  const st = modelSetup.checkModelEnv(
    { ttsModelsDir: models, ttsVenvPy: "", rvcRoot: rvc },
    {},
  );
  assert.strictEqual(st.allOk, true);
  assert.strictEqual(st.ttsOk, true);
  assert.strictEqual(st.rvcOk, true);
  assert.deepStrictEqual(st.missing, []);
  const venvItem = st.items.find((i) => i.key === "tts_venv");
  assert.strictEqual(venvItem.path, py, "venv 应由模型目录推导出来");
  assert.strictEqual(venvItem.source, "derived");
  assert.strictEqual(st.items.find((i) => i.key === "tts_models").source, "config");
});

t("checkModelEnv: 环境变量已设时不应误报未配置", () => {
  const { root, models } = makeTtsModels();
  const py = makeVenv(root);
  const rvc = makeRvcRoot();
  const st = modelSetup.checkModelEnv(
    { ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "" },
    { VM_TTS_MODELS_DIR: models, VM_TTS_VENV_PY: py, VM_RVC_ROOT: rvc },
  );
  assert.strictEqual(st.allOk, true, "进程环境变量生效时不应提示未配置");
  assert.strictEqual(st.items.find((i) => i.key === "tts_models").source, "env");
});

t("checkModelEnv: TTS 模型在但 venv 推不出来 → ttsOk 仍为 false（口径：两者都要）", () => {
  const { models } = makeTtsModels(); // 不建 venv
  const st = modelSetup.checkModelEnv({ ttsModelsDir: models, ttsVenvPy: "", rvcRoot: "" }, {});
  assert.strictEqual(st.ttsOk, false);
  assert.ok(st.missing.includes("tts_venv"));
});

// ============================================================
// D. 注入链（externalResourceEnv / buildBackendEnv）
// ============================================================

t("注入: 配置存在 → VM_TTS_MODELS_DIR / VM_TTS_VENV_PY / VM_PROJECT_ROOT / VM_RVC_ROOT 全部注入", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-inject-"));
  const { root, models } = makeTtsModels();
  const py = makeVenv(root);
  const rvc = makeRvcRoot();
  appConfig.save({ ttsModelsDir: models, rvcRoot: rvc, setupSeen: true });

  const env = backend.externalResourceEnv();
  assert.strictEqual(env.VM_TTS_MODELS_DIR, models);
  assert.strictEqual(env.VM_TTS_VENV_PY, py);
  assert.strictEqual(env.VM_PROJECT_ROOT, root, "venv 推出来时可注入 VM_PROJECT_ROOT");
  assert.strictEqual(env.VM_RVC_ROOT, rvc, "RVC 根应由用户配置注入");
});

t("注入: 配置为空（干净机器语义）→ 不注入任何 VM_ 模型变量", () => {
  // 本测试同时模拟"没有 D:\\变声"：D 盘不存在时 externalResourceEnv 第二级探测自然为空。
  // 若本机确实有 D:\\变声，第二级会兜住 —— 这与设计一致（本机开发态保持既有行为），
  // 因此这里只断言「配置层不产生注入」，在 D:\\变声 存在时用 VM_PROJECT_ROOT 之外的方式验证。
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-noinject-"));
  appConfig.save({ ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "", setupSeen: true });
  const env = backend.externalResourceEnv();
  const legacyExists = fs.existsSync(path.join("D:\\变声", "m2_server", "server.py"));
  if (legacyExists) {
    // 本机：第二级兜底，注入的必然是 D:\\变声 系路径，绝不能是我们"空配置"产生的假值
    for (const k of ["VM_TTS_MODELS_DIR", "VM_TTS_VENV_PY", "VM_RVC_ROOT"]) {
      if (env[k]) assert.ok(env[k].startsWith("D:\\变声"), `${k} 只能来自 D:\\变声 兜底，实际 ${env[k]}`);
    }
    assert.ok(!("VM_RVC_ROOT" in env), "②级不注入 VM_RVC_ROOT（RVC 位置因机而异）");
  } else {
    assert.deepStrictEqual(env, {}, "无配置且无 D:\\变声 时不得注入任何 VM_ 变量");
  }
});

t("注入: 配置路径校验不过 → 不注入（配错不如不配，让 /diagnose 明确报未配置）", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-badpath-"));
  // 指向一个存在但没有模型结构的目录
  const bogus = fs.mkdtempSync(path.join(os.tmpdir(), "vm-bogus-"));
  appConfig.save({ ttsModelsDir: bogus, rvcRoot: bogus, setupSeen: true });
  const env = backend.externalResourceEnv();
  assert.notStrictEqual(env.VM_TTS_MODELS_DIR, bogus, "校验不过的模型目录不得注入");
  assert.notStrictEqual(env.VM_RVC_ROOT, bogus, "校验不过的 RVC 根不得注入");
});

t("注入: D:\\变声 存在时，配置层优先于自动探测（用户改目录后能覆盖本机旧路径）", () => {
  const legacyExists = fs.existsSync(path.join("D:\\变声", "m2_server", "server.py"));
  if (!legacyExists) return; // 干净机器无此场景
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-prio-"));
  const { root, models } = makeTtsModels();
  makeVenv(root);
  appConfig.save({ ttsModelsDir: models, setupSeen: true });
  const env = backend.externalResourceEnv();
  assert.strictEqual(env.VM_TTS_MODELS_DIR, models, "①用户配置必须赢过 ②D:\\变声 探测");
});

t("buildBackendEnv: 用户进程环境变量仍优先于配置注入（用户显式设的值必须赢）", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-envwin-"));
  const { root, models } = makeTtsModels();
  makeVenv(root);
  appConfig.save({ ttsModelsDir: models });

  const env = backend.buildBackendEnv("C:\\pkg\\backend", "C:\\data", {
    VM_TTS_MODELS_DIR: "C:\\user\\owns\\this",
  });
  assert.strictEqual(env.VM_TTS_MODELS_DIR, "C:\\user\\owns\\this", "进程 env 优先级最高");
  assert.strictEqual(env.PYTHONIOENCODING, "utf-8");
  assert.strictEqual(env.VM_MEDIA_DIR, path.join("C:\\data", "media"));
});

t("buildBackendEnv: 配置注入的值能落到最终 spawn env", () => {
  userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-finalenv-"));
  const { root, models } = makeTtsModels();
  const py = makeVenv(root);
  const rvc = makeRvcRoot();
  appConfig.save({ ttsModelsDir: models, rvcRoot: rvc });

  const env = backend.buildBackendEnv("C:\\pkg\\backend", "C:\\data", {});
  assert.strictEqual(env.VM_TTS_MODELS_DIR, models);
  assert.strictEqual(env.VM_TTS_VENV_PY, py);
  assert.strictEqual(env.VM_RVC_ROOT, rvc);
});

t("注入: app-config 抛异常时不影响后端拉起（容错）", () => {
  // 模拟配置文件读取路径炸掉：把 userData 换成一个不可用路径
  const saved = userDataDir;
  userDataDir = path.join(saved, "no", "such", "deep", "dir", "\0invalid");
  let env;
  assert.doesNotThrow(() => { env = backend.externalResourceEnv(); });
  assert.ok(env && typeof env === "object");
  userDataDir = saved;
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
  process.stdout.write(`\n[test-model-setup] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();

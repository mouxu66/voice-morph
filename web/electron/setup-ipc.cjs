// 首次启动引导 + 模型配置 IPC
//
// 解决的问题：安装包不带模型（4.9G 留包外）。干净机器（无 D:\变声 / D:\RVC）上后端能起，
// 但 TTS / RVC 核心功能不可用，用户只看到一片报错，不知道怎么修。
//
// 设计原则：
// - **不阻塞启动**：引导只写配置，不下载、不建 venv、不 waiting。缺失时禁用相关功能 +
//   提示「TTS 未配置」即可，其余功能照常。
// - **只自动弹一次**：`setupSeen=true` 后不再自动弹窗（避免每次启动骚扰），改由 UI 常驻
//   降级提示 + 设置面板入口承接。用户随时可从设置里重新打开。
// - 所有取目录操作走 dialog.showOpenDialog（主进程），渲染层只拿路径字符串。
const { app, dialog, ipcMain, BrowserWindow } = require("electron");

const appConfig = require("./app-config.cjs");
const modelSetup = require("./model-setup.cjs");

/** 各配置项对应的目录选择器提示语与推导规则 */
const PICK_SPECS = {
  tts_models: {
    title: "选择 tts_models 模型目录",
    message:
      "请选择包含 qwen3-tts-1.7b-base 与 qwen3-tts-tokenizer-12hz 两个子目录的 tts_models 目录。\n\n" +
      "模型体积较大（约 4G），不随安装包分发，需单独下载或从旧机器拷贝。",
    /** 校验失败时给出更具体的纠正提示（而非只说"路径不对"） */
    validate: (p) => modelSetup.checkTtsModels(p),
    hint: "选中的目录里必须能看到 qwen3-tts-1.7b-base/ 与 qwen3-tts-tokenizer-12hz/。",
  },
  tts_venv: {
    title: "选择 TTS 专用解释器",
    message:
      "请选择 TTS 专用虚拟环境里的 python.exe（通常在 tts_trial\\venv312\\Scripts\\python.exe）。\n\n" +
      "若模型目录选对了，这一项通常会自动推导，无需手动指定。",
    validate: (p) => modelSetup.checkTtsVenv(p),
    hint: "需要指向 venv312 下的 python.exe 文件。",
  },
  rvc_root: {
    title: "选择 RVC 整合包根目录",
    message:
      "请选择 RVC 整合包根目录（含 rvc/ 、logs/ 、tools/ 等子目录的那个文件夹）。\n\n" +
      "实时变声依赖它；暂时不配也可以，TTS 与离线变声仍可使用。",
    validate: (p) => modelSetup.checkRvcRoot(p),
    hint: "目录里应能看到 rvc/ 或 logs/ 或 tools/。",
  },
};

/** 当前环境完整状态（含配置原文与检测结果），供 UI 面板与首启引导共用 */
function currentStatus() {
  const config = appConfig.load();
  const check = modelSetup.checkModelEnv(config);
  return {
    config,
    configPath: appConfig.configPath(),
    /** 检测结果（含每项 ok/path/source/reason） */
    items: check.items,
    ttsOk: check.ttsOk,
    rvcOk: check.rvcOk,
    allOk: check.allOk,
    missing: check.missing,
    /** 用户在此前是否处理过引导 */
    setupSeen: config.setupSeen === true,
  };
}

/**
 * 弹目录选择器；选完当场校验，不合格就提示重选或"仍然使用"。
 * @returns {Promise<{ canceled: boolean, path?: string, ok?: boolean, reason?: string }>}
 */
async function pickDir(kind, parentWindow = null) {
  const spec = PICK_SPECS[kind];
  if (!spec) return { canceled: true };

  // tts_venv 选的是文件不是目录
  const isFilePick = kind === "tts_venv";
  for (;;) {
    const opts = {
      title: spec.title,
      message: spec.message,
      buttonLabel: "使用此位置",
      ...(isFilePick
        ? { properties: ["openFile"], filters: [{ name: "python.exe", extensions: ["exe"] }] }
        : { properties: ["openDirectory"] }),
    };
    const r = parentWindow
      ? await dialog.showOpenDialog(parentWindow, opts)
      : await dialog.showOpenDialog(opts);
    if (r.canceled || !r.filePaths || !r.filePaths.length) return { canceled: true };

    const picked = r.filePaths[0];
    const verdict = spec.validate(picked);
    if (verdict.ok) return { canceled: false, path: picked, ok: true, reason: "" };

    // 校验不过：让用户决定"重选 / 仍然使用 / 取消"，而不是无声地接受坏路径
    const choice = dialog.showMessageBoxSync(parentWindow || undefined, {
      type: "warning",
      title: "路径校验未通过",
      message: `这个位置看起来不对：${verdict.reason}`,
      detail: `${spec.hint}\n\n所选路径：${picked}`,
      buttons: ["重新选择", "仍然使用", "取消"],
      defaultId: 0,
      cancelId: 2,
    });
    if (choice === 0) continue;
    if (choice === 2) return { canceled: true };
    return { canceled: false, path: picked, ok: false, reason: verdict.reason };
  }
}

/**
 * 首启引导对话框（仅打包版 + setupSeen=false + 有缺失时调用）。
 * 返回 true 表示用户已完成配置（调用方据此重启后端）。
 */
async function runFirstRunGuide(parentWindow = null) {
  const st = currentStatus();
  if (st.allOk) return false;

  const missingLabels = st.items.filter((i) => !i.ok).map((i) => `· ${i.label}`).join("\n");
  const choice = dialog.showMessageBoxSync(parentWindow || undefined, {
    type: "warning",
    title: "首次使用：需要指定模型位置",
    message: "检测到本机还没有语音模型，TTS / 实时变声暂时不可用。",
    detail:
      `缺少以下资源：\n${missingLabels}\n\n` +
      "安装包不包含模型文件（体积约 4G），需要你指定它们在本机的位置。\n" +
      "现在配置 → 选好目录后相关功能立即可用；稍后配置 → 其余功能照常使用，\n" +
      "可随时在右上角设置里重新配置。",
    buttons: ["现在配置", "稍后配置", "退出应用"],
    defaultId: 0,
    cancelId: 1,
  });

  if (choice === 2) {
    app.quit();
    return false;
  }
  if (choice === 1) {
    appConfig.save({ setupSeen: true });
    return false;
  }

  // 现在配置：按缺失项依次引导（RVC 可选，用户跳过就跳过）
  const changed = await runSetupWizard(parentWindow, { onlyMissing: true });
  appConfig.save({ setupSeen: true });
  return changed;
}

/**
 * 配置向导：逐项选择缺失的路径并保存。
 * @param {object} [opts] onlyMissing=true 时跳过已就绪的项
 * @returns {Promise<boolean>} 是否有配置被写入（调用方据此决定要不要重启后端）
 */
async function runSetupWizard(parentWindow = null, opts = {}) {
  const st = currentStatus();
  const patch = {};
  for (const item of st.items) {
    if (opts.onlyMissing && item.ok) continue;
    // RVC 允许跳过：TTS 通了就有可用功能，不强行卡住用户
    const r = await pickDir(item.key, parentWindow);
    if (r.canceled) continue;
    if (item.key === "tts_models") patch.ttsModelsDir = r.path;
    else if (item.key === "tts_venv") patch.ttsVenvPy = r.path;
    else if (item.key === "rvc_root") patch.rvcRoot = r.path;
  }
  if (!Object.keys(patch).length) return false;
  const ok = appConfig.save(patch);
  return ok;
}

function registerSetupIpc(getWindow) {
  // 只读：UI 打开面板/挂角标时轮询，绝不弹窗
  ipcMain.handle("setup:status", () => currentStatus());

  // 取目录（弹系统选择器 + 当场校验），只返回路径，不落盘
  ipcMain.handle("setup:pick-dir", async (_e, kind) => pickDir(kind, getWindow()));

  // 保存配置（可传部分字段）；返回保存后的最新状态
  ipcMain.handle("setup:save", async (_e, patch) => {
    const ok = appConfig.save(patch || {});
    return { ok, ...currentStatus() };
  });

  // 稍后配置：标记已看过引导，不再自动弹
  ipcMain.handle("setup:dismiss", async () => {
    appConfig.save({ setupSeen: true });
    return { ok: true, ...currentStatus() };
  });

  // 重新打开配置向导（设置面板入口）
  ipcMain.handle("setup:run-wizard", async () => {
    const changed = await runSetupWizard(getWindow(), { onlyMissing: false });
    appConfig.save({ setupSeen: true });
    return { changed, ...currentStatus() };
  });

  // 重置：清空所有路径配置并重新标记未看过引导（用于"重新配置"）
  ipcMain.handle("setup:reset", async () => {
    appConfig.reset();
    appConfig.save({ setupSeen: false });
    return { ok: true, ...currentStatus() };
  });

  // 打开配置文件所在目录，便于用户手改/排障
  ipcMain.handle("setup:show-config", async () => {
    const { shell } = require("electron");
    try {
      shell.showItemInFolder(appConfig.configPath());
    } catch { /* 忽略 */ }
    return { ok: true, path: appConfig.configPath() };
  });
}

module.exports = {
  PICK_SPECS,
  currentStatus,
  pickDir,
  runFirstRunGuide,
  runSetupWizard,
  registerSetupIpc,
};

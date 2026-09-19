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
const { app, dialog, ipcMain, shell } = require("electron");

const appConfig = require("./app-config.cjs");
const modelSetup = require("./model-setup.cjs");
const modelScan = require("./model-scan.cjs");
const modelGuides = require("./model-guides.cjs");
const buildWatch = require("./build-watch.cjs");

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

/**
 * 扫描本机找可用资源。
 *
 * `extraRoots` 把「已配置路径」和「用户设的 VM_* 环境变量」一起喂进去，让扫描能从
 * 旧位置往回找 —— 用户把 RVC 从 D 盘挪到 E 盘时，这一条能让他不用手选。
 *
 * 失败不抛：首启链路上的任何异常都不该变成"应用打不开"，最差退回"没找到"。
 */
async function scanResources(opts = {}) {
  try {
    const config = appConfig.load();
    const extra = [
      config.ttsModelsDir, config.ttsVenvPy, config.rvcRoot,
      process.env.VM_TTS_MODELS_DIR, process.env.VM_TTS_VENV_PY, process.env.VM_RVC_ROOT,
    ].filter(Boolean);
    const result = await modelScan.scan({
      kinds: opts.kinds && opts.kinds.length ? opts.kinds : undefined,
      extraRoots: extra,
      maxDirs: opts.maxDirs,
      timeBudgetMs: opts.timeBudgetMs,
    });
    return { ok: true, ...result };
  } catch (err) {
    return { ok: false, reason: String((err && err.message) || err), candidates: {}, stats: null };
  }
}

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

/** 检测项 key → app-config 里的字段名（向导与"用推荐位置"共用，避免两处各写一份） */
const CONFIG_KEY = {
  tts_models: "ttsModelsDir",
  tts_venv: "ttsVenvPy",
  rvc_root: "rvcRoot",
};

/** `{ kind: path }` → app-config patch */
function toPatch(picks) {
  const patch = {};
  for (const [kind, p] of Object.entries(picks)) {
    if (CONFIG_KEY[kind] && p) patch[CONFIG_KEY[kind]] = p;
  }
  return patch;
}

/**
 * 首启引导对话框（仅打包版 + setupSeen=false + 有缺失时调用）。
 *
 * 2026-09-14 起：**先自己扫一遍**再决定跟用户说什么。
 * 原先一上来就是"请选择目录"，而模型可能早就躺在本机（用户从旧机器拷过来、
 * 或只是重装了应用）—— 让人工在十几个盘符里翻，是把系统的活推给人。
 *
 * 返回 true 表示用户已完成配置（调用方据此重启后端）。
 *
 * @param {object|null} parentWindow
 * @param {{ scan?: Function }} [deps] 注入点：测试用。真扫描要 0.5–3s 且结果随机器变，
 *   靠它把首启分支测成确定性的。
 */
async function runFirstRunGuide(parentWindow = null, deps = {}) {
  const st = currentStatus();
  if (st.allOk) return false;

  const scanFn = deps.scan || scanResources;
  const found = await scanFn({ kinds: st.missing });
  const picks = {};
  const foundLines = [];
  for (const kind of st.missing) {
    const top = ((found.candidates || {})[kind] || [])[0];
    if (!top) continue;
    picks[kind] = top.path;
    foundLines.push(`· ${modelScan.KIND_LABEL[kind] || kind} → ${top.path}`);
  }
  const foundCount = Object.keys(picks).length;

  // 分支一：本机就有现成的 —— 直接提议使用（省掉"翻盘符"这一步）
  if (foundCount > 0) {
    const stillMissing = st.missing.filter((k) => !picks[k]);
    const choice = dialog.showMessageBoxSync(parentWindow || undefined, {
      type: "question",
      title: "首次使用：已在本机找到可用资源",
      message: `自动扫描到 ${foundCount} 项可用资源，直接使用吗？`,
      detail:
        `扫描结果：\n${foundLines.join("\n")}\n\n` +
        (stillMissing.length
          ? `没扫到的项：${stillMissing.map((k) => modelScan.KIND_LABEL[k] || k).join("、")}` +
            `（可稍后在设置里手动指定）\n\n`
          : "") +
        "选「直接使用」后会自动重启后端，相关功能立即可用。",
      buttons: ["直接使用", "我自己选目录", "稍后配置"],
      defaultId: 0,
      cancelId: 2,
    });
    if (choice === 2) {
      appConfig.save({ setupSeen: true });
      return false;
    }
    if (choice === 0) {
      const ok = appConfig.save(toPatch(picks));
      appConfig.save({ setupSeen: true });
      return ok;
    }
    const changed = await runSetupWizard(parentWindow, { onlyMissing: true });
    appConfig.save({ setupSeen: true });
    return changed;
  }

  // 分支二：本机确实没有 —— 这时最该给的是「去哪下」，而不是一个空的目录选择器。
  // 按钮顺序：0 打开下载指引 / 1 我自己选目录 / 2 稍后配置 / 3 退出应用
  const firstKind = st.missing[0];
  const guide = modelGuides.guideFor(firstKind);
  const missingLabels = st.items.filter((i) => !i.ok).map((i) => `· ${i.label}`).join("\n");
  const choice = dialog.showMessageBoxSync(parentWindow || undefined, {
    type: "warning",
    title: "首次使用：需要先准备模型文件",
    message: "本机没有扫到可用的语音模型，TTS / 实时变声暂时不可用。",
    detail:
      `缺少以下资源：\n${missingLabels}\n\n` +
      "模型体积较大（合计约 20 GB）、且使用条款各不相同，因此不随安装包分发。\n" +
      "设置面板里每一项都有下载指引：官方链接、体积、以及下完该放成什么目录结构。\n\n" +
      (guide ? `当前这项：${guide.label} · ${guide.sizeText}` : ""),
    buttons: ["打开下载指引", "我自己选目录", "稍后配置", "退出应用"],
    defaultId: 0,
    cancelId: 2,
  });

  if (choice === 3) {
    app.quit();
    return false;
  }
  if (choice === 0) {
    // 只打开**已知常量**里的链接，不接受任何外部传值（见 model-guides.resolveLink）
    const link = modelGuides.resolveLink(firstKind, 0);
    if (link) {
      try {
        await shell.openExternal(link.url);
      } catch { /* 打不开就算了，别让首启卡住 */ }
    }
    appConfig.save({ setupSeen: true });
    return false;
  }
  if (choice === 2) {
    appConfig.save({ setupSeen: true });
    return false;
  }
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
  const picks = {};
  for (const item of st.items) {
    if (opts.onlyMissing && item.ok) continue;
    // RVC 允许跳过：TTS 通了就有可用功能，不强行卡住用户
    const r = await pickDir(item.key, parentWindow);
    if (r.canceled) continue;
    picks[item.key] = r.path;
  }
  const patch = toPatch(picks);
  if (!Object.keys(patch).length) return false;
  return appConfig.save(patch);
}

function registerSetupIpc(getWindow) {
  // 只读：UI 打开面板/挂角标时轮询，绝不弹窗
  ipcMain.handle("setup:status", () => currentStatus());

  // 下载指引（纯数据，一次性全给，省得渲染层按项多轮往返）
  ipcMain.handle("setup:guides", () => ({
    verifiedAt: modelGuides.VERIFIED_AT,
    guides: modelGuides.allGuides(),
  }));

  // 打开指引里的某条链接。渲染层只传「kind + 序号」——URL 由主进程从常量里查，
  // 渲染层永远无法让主进程打开任意地址。
  ipcMain.handle("setup:open-guide-link", async (_e, kind, index) => {
    const link = modelGuides.resolveLink(kind, index);
    if (!link) return { ok: false, reason: "链接不存在" };
    try {
      await shell.openExternal(link.url);
      return { ok: true, url: link.url };
    } catch (err) {
      return { ok: false, reason: String((err && err.message) || err) };
    }
  });

  // 自动扫描本机找可用资源（先在已有配置/环境变量附近找，再从常用位置与盘符根找）
  ipcMain.handle("setup:scan", async (_e, opts) => scanResources(opts || {}));

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
    try {
      shell.showItemInFolder(appConfig.configPath());
    } catch { /* 忽略 */ }
    return { ok: true, path: appConfig.configPath() };
  });

  // ==================== 构建监听器 IPC ====================
  ipcMain.handle("build-watch:start", async () => {
    const result = buildWatch.startWatcher();
    return { ...result, timestamp: Date.now() };
  });

  ipcMain.handle("build-watch:stop", async () => {
    const result = buildWatch.stopWatcher();
    return { ...result, timestamp: Date.now() };
  });

  ipcMain.handle("build-watch:status", async () => {
    return buildWatch.getWatcherStatus();
  });
}

module.exports = {
  PICK_SPECS,
  CONFIG_KEY,
  toPatch,
  currentStatus,
  scanResources,
  pickDir,
  runFirstRunGuide,
  runSetupWizard,
  registerSetupIpc,
};

// 打包配置守卫（纯静态、只读 package.json，不依赖 electron / node_modules）：
//   本文件锁住一批「改了会静默出事」的 electron-builder 配置。
//
// 为什么需要它：这些字段错了不会报错，只会让**用户机器上出现第二份安装**、或者
// 让发行物里混进不该分发的东西 —— 两种都是装完之后才发现的。
//
// 运行：node tools/test-packaging.cjs —— 退出码 0 = 通过，非 0 = 失败。
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const PKG = path.join(__dirname, "..", "web", "package.json");
const pkg = JSON.parse(fs.readFileSync(PKG, "utf8"));
const build = pkg.build || {};
const nsis = build.nsis || {};

let pass = 0;
let fail = 0;
function t(name, fn) {
  try {
    fn();
    pass++;
    console.log("  ok   " + name);
  } catch (e) {
    fail++;
    console.log("  FAIL " + name + "\n         " + e.message);
  }
}

// ---------------------------------------------------------------- 身份标识
// appId 变了 => 注册表 GUID 变了 => 安装器认不出旧版本 => 装出并存的第二份。
// 这是「更新后桌面出现两个版本」最直接的成因，必须钉死。
t("appId 固定为 com.voicemorph.desktop", () => {
  assert.strictEqual(build.appId, "com.voicemorph.desktop");
});

t("productName 为 变声工坊", () => {
  assert.strictEqual(build.productName, "变声工坊");
});

// productName / shortcutName 里若混进版本号，每发一版就会生成一个新名字的
// 桌面图标，旧的不会被改名覆盖 —— 直接表现为「多了个新版本」。
t("productName 不含版本号（否则每版多一个桌面图标）", () => {
  assert.ok(
    !/\d+\.\d+/.test(build.productName),
    `productName 含版本样式数字：${build.productName}`
  );
});

// ---------------------------------------------------------------- NSIS 行为
// 显式钉死快捷方式名：留空会回落到 productName，而 productName 一旦改动，
// 安装器就要靠注册表里的 ShortcutName 去 Rename 旧图标（能做，但别依赖）。
t("shortcutName 固定为 变声工坊", () => {
  assert.strictEqual(nsis.shortcutName, "变声工坊");
});

// 关掉安装目录选择页 => 升级时不再出现可改路径的输入框 => 必定装回原位。
// 首次安装如需自选目录，走命令行：Setup.exe /D=D:\Apps\VoiceMorph
t("allowToChangeInstallationDirectory 关闭（升级原地替换，不给改错机会）", () => {
  assert.strictEqual(nsis.allowToChangeInstallationDirectory, false);
});

t("oneClick 关闭（保留安装向导，非静默安装）", () => {
  assert.strictEqual(nsis.oneClick, false);
});

// 卸载保留用户配置：模型路径 / 声卡设置 / 更新源都在 userData 里，
// 卸了就丢，用户得重新配一遍。
t("deleteAppDataOnUninstall 关闭（卸载保留用户配置）", () => {
  assert.strictEqual(nsis.deleteAppDataOnUninstall, false);
});

t("artifactName 带 ${version}（同名包不互相覆盖）", () => {
  assert.ok(
    String(nsis.artifactName || "").includes("${version}"),
    "artifactName 必须含 ${version}"
  );
});

// perMachine 打开会要求管理员权限并写入 HKLM；本项目一直是用户级安装
// （HKCU + %LOCALAPPDATA%\Programs），改了会让免管理员安装失效。
t("未开启 perMachine（保持用户级安装，免管理员）", () => {
  assert.notStrictEqual(nsis.perMachine, true);
});

// ---------------------------------------------------------------- 产物与合规
t("输出目录是 release2（发版脚本按此读产物）", () => {
  assert.strictEqual(build.directories && build.directories.output, "release2");
});

t("win 目标包含 nsis", () => {
  const target = build.win && build.win.target;
  const list = Array.isArray(target) ? target : [target];
  assert.ok(list.includes("nsis"), `win.target = ${JSON.stringify(target)}`);
});

t("签名证书路径指向 certs/black-seraph.pfx", () => {
  assert.strictEqual(
    build.win && build.win.certificateFile,
    "../certs/black-seraph.pfx"
  );
});

t("files 只收 dist 与 electron（外加 node_modules 排除）", () => {
  assert.deepStrictEqual(build.files, [
    "dist/**/*",
    "electron/**/*",
    "!node_modules/**/*",
  ]);
});

// electron-builder 会把 `dependencies` **整棵树**拷进 app.asar，而 `files` 里的
// 正向白名单（`dist/**/*`、`electron/**/*`）**管不住它** —— 只有显式的 `!node_modules/**/*` 能拦住。
// 2026-09-19 实测：不排除时 app.asar 47.1MB / 5305 条目（`node_modules` 占 37.4MB / 5236 条目，
// lucide-react 单独 19MB）；排除后 6.74MB / 71 条目，安装包 101.5MB → 93.4MB。
// 这条排除能成立的前提是**主进程不 require 任何第三方包**（目前为 0）；
// 语义级验证（包括"主进程真 require 了包就该改白名单"）在
// `m2_server/tests/test_desktop_packaging.py`。
t("files 排除 node_modules（否则 asar 白胖约 40MB）", () => {
  assert.ok(
    (build.files || []).includes("!node_modules/**/*"),
    "build.files 丢了 `!node_modules/**/*` —— app.asar 会从 6.7MB 涨回 47MB"
  );
});

// 发行物不得夹带模型权重或研究用途组件（RVC 底模同仓协议写「仅供研究」、
// demucs 权重训练自 MUSDB18、Seed-VC 是 GPL-3.0）。证据见 THIRD_PARTY_NOTICES.md。
t("extraResources 不夹带模型权重 / Seed-VC / RVC 底模", () => {
  const raw = JSON.stringify(build.extraResources || []);
  const forbidden = [
    "seed_vc",
    "seed-vc",
    "checkpoints",
    "weights",
    "hubert",
    "rmvpe",
    "pretrained_v2",
    "models/",
  ];
  const hit = forbidden.filter((k) => raw.includes(k));
  assert.deepStrictEqual(hit, [], `extraResources 命中禁项：${hit.join(", ")}`);
});

t("extraResources 不含 tests / __pycache__（服务端只带运行所需）", () => {
  const m2 = (build.extraResources || []).find((r) => r.to === "backend/m2_server");
  assert.ok(m2, "未找到 backend/m2_server 条目");
  const filter = m2.filter || [];
  for (const need of ["!**/__pycache__/**", "!**/tests/**"]) {
    assert.ok(filter.includes(need), `m2_server filter 缺少 ${need}`);
  }
});

// 2026-09-19：这里曾有一条 `!**/data/**`，把 `m2_server/data/rvc_texts.txt`
// （4KB，**生产文件**：RVC 训练语料模板）挡在安装包外。`config.load_rvc_texts()`
// 有内置 20 句兜底 → 安装版**不报错**，只是把语料从 100+ 句悄悄退回 20 句。
// 该目录下只有这一份文件，整目录排除没有任何收益。
t("m2_server filter 没把 data/ 整目录排除掉", () => {
  const m2 = (build.extraResources || []).find((r) => r.to === "backend/m2_server");
  const filter = m2.filter || [];
  assert.ok(
    !filter.includes("!**/data/**"),
    "`!**/data/**` 会漏掉 m2_server/data/rvc_texts.txt（生产文件，静默退化）"
  );
});

// 2026-09-19：`tools/` 条目原本**一条 filter 都没有** → `tools/desktop-control/out/`
// （87 张调试截图 / 95MB）、`__pycache__`、`.pytest_cache`、`.bak-*` 全部进包，
// 实测重打一次安装包从 101MB 冲到 180MB。
// 口径与 `tools/verify_backend_sync.py` 的 `_IGNORE_RULES` 对齐（那边是唯一权威）。
t("extraResources 的 tools 条目排除了开发期产物", () => {
  const tools = (build.extraResources || []).find((r) => r.to === "backend/tools");
  assert.ok(tools, "未找到 backend/tools 条目");
  const filter = tools.filter || [];
  for (const need of [
    "!**/__pycache__/**",
    "!**/desktop-control/**",
    "!**/outputs/**",
    "!**/.pytest_cache/**",
  ]) {
    assert.ok(filter.includes(need), `tools filter 缺少 ${need}`);
  }
});

// ---------------------------------------------------------------- 结论
process.stdout.write(`\n[test-packaging] ${pass} 通过, ${fail} 失败\n`);
process.exit(fail ? 1 : 0);

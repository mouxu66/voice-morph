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

t("files 只收 dist 与 electron", () => {
  assert.deepStrictEqual(build.files, ["dist/**/*", "electron/**/*"]);
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

// ---------------------------------------------------------------- 结论
process.stdout.write(`\n[test-packaging] ${pass} 通过, ${fail} 失败\n`);
process.exit(fail ? 1 : 0);

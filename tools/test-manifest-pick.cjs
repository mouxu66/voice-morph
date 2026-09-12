// 更新清单「选包」专项测试（make-update-manifest.cjs 的纯函数区）：
//   候选过滤（.exe / uninstall / 中文名）、版本号独立片段匹配、按版本选包 vs mtime 退化。
//
// 运行：node tools/test-manifest-pick.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 为什么值得单测：选包错了 = 把旧版/错包的 sha256+size 发进 latest.json，
// 用户「升级」后装回旧版（静默降级/更新死循环），且下载校验能通过、日志无异常，极难发现。
const assert = require("node:assert");
const {
  isInstallerCandidate,
  versionMatcher,
  pickInstaller,
} = require("../web/electron/make-update-manifest.cjs");

let pass = 0;
let fail = 0;
function t(name, fn) {
  try {
    fn();
    pass += 1;
    process.stdout.write(`  ✓ ${name}\n`);
  } catch (e) {
    fail += 1;
    process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`);
  }
}

process.stdout.write("[test-manifest-pick] 候选过滤\n");

t("接受 ASCII 安装包名", () => {
  assert.strictEqual(isInstallerCandidate("VoiceMorph-Setup-0.2.2.exe"), true);
});

t("大小写不敏感接受 .EXE", () => {
  assert.strictEqual(isInstallerCandidate("VoiceMorph-Setup-0.2.2.EXE"), true);
});

t("排除 .blockmap（扩展名不是 .exe）", () => {
  assert.strictEqual(isInstallerCandidate("VoiceMorph-Setup-0.2.2.exe.blockmap"), false);
});

t("排除 uninstall 程序", () => {
  assert.strictEqual(isInstallerCandidate("Uninstall VoiceMorph.exe"), false);
  assert.strictEqual(isInstallerCandidate("voice-morph-uninstaller.exe"), false);
});

t("排除中文名旧包（防发错包 / 中文 URL 编码脆弱）", () => {
  assert.strictEqual(isInstallerCandidate("变声工坊-Setup-0.2.2.exe"), false);
});

t("排除无扩展名与其它格式", () => {
  assert.strictEqual(isInstallerCandidate("setup.zip"), false);
  assert.strictEqual(isInstallerCandidate("VoiceMorph-Setup-0.2.2"), false);
});

process.stdout.write("[test-manifest-pick] 版本号匹配\n");

t("版本号后跟 .exe 能匹配（不能因扩展名分隔符漏配）", () => {
  assert.strictEqual(versionMatcher("0.2.2").test("VoiceMorph-Setup-0.2.2.exe"), true);
});

t("0.2.2 不误配 0.2.20", () => {
  assert.strictEqual(versionMatcher("0.2.2").test("VoiceMorph-Setup-0.2.20.exe"), false);
});

t("0.2.2 不误配 0.2.2x 其它后缀形态", () => {
  assert.strictEqual(versionMatcher("0.2.2").test("VoiceMorph-Setup-0.2.21.exe"), false);
});

t("0.2.1 不误配 0.2.10 但匹配 0.2.1", () => {
  assert.strictEqual(versionMatcher("0.2.1").test("VoiceMorph-Setup-0.2.10.exe"), false);
  assert.strictEqual(versionMatcher("0.2.1").test("VoiceMorph-Setup-0.2.1.exe"), true);
});

t("带 v 前缀的版本号归一化", () => {
  assert.strictEqual(versionMatcher("v0.2.2").test("VoiceMorph-Setup-0.2.2.exe"), true);
});

t("版本号出现在文件名中间（前缀无连字符）也能匹配", () => {
  assert.strictEqual(versionMatcher("0.2.2").test("voice0.2.2-x.exe"), true);
});

process.stdout.write("[test-manifest-pick] 选包策略\n");

// 候选列表约定：已按 mtime 倒序（列表首个 = 最新）
const cand = (...names) => names.map((f) => ({ f, t: 0 }));

t("精确匹配优先于 mtime：旧版本包 mtime 更新也不选它", () => {
  // 0.2.1 排在前面（mtime 最新），但目标版本是 0.2.2 → 必须选 0.2.2
  const r = pickInstaller(cand("VoiceMorph-Setup-0.2.1.exe", "VoiceMorph-Setup-0.2.2.exe"), "0.2.2");
  assert.deepStrictEqual(r, { exe: "VoiceMorph-Setup-0.2.2.exe", exact: true });
});

t("精确匹配命中多个时取列表首个（= mtime 最新）", () => {
  const r = pickInstaller(cand("VoiceMorph-Setup-0.2.2.exe", "aaa-0.2.2.exe"), "0.2.2");
  assert.deepStrictEqual(r, { exe: "VoiceMorph-Setup-0.2.2.exe", exact: true });
});

t("无精确匹配时退化为取最新并标记 exact=false", () => {
  const r = pickInstaller(cand("VoiceMorph-Setup-0.3.0.exe", "VoiceMorph-Setup-0.2.9.exe"), "0.2.2");
  assert.deepStrictEqual(r, { exe: "VoiceMorph-Setup-0.3.0.exe", exact: false });
});

t("空候选返回 null", () => {
  assert.deepStrictEqual(pickInstaller([], "0.2.2"), { exe: null, exact: false });
});

t("退化为最新时不会被 .blockmap 之类污染（候选已过滤）", () => {
  const files = [
    "VoiceMorph-Setup-0.2.2.exe.blockmap",
    "VoiceMorph-Setup-0.2.2.exe",
    "变声工坊-Setup-0.3.0.exe",
    "uninstall.exe",
  ];
  const candidates = files
    .filter(isInstallerCandidate)
    .map((f) => ({ f, t: 0 }));
  assert.deepStrictEqual(candidates.map((c) => c.f), ["VoiceMorph-Setup-0.2.2.exe"]);
  assert.deepStrictEqual(pickInstaller(candidates, "0.2.2"), {
    exe: "VoiceMorph-Setup-0.2.2.exe",
    exact: true,
  });
});

process.stdout.write(`\n[test-manifest-pick] ${pass} 通过, ${fail} 失败\n`);
process.exit(fail === 0 ? 0 : 1);

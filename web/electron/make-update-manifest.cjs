// 生成更新清单 latest.json（发布新版本时跑一次）。
//
// 用法（项目根目录执行）：
//   node web/electron/make-update-manifest.cjs --dir release2 ^
//        --base-url https://example.com/voicemorph/download ^
//        --notes docs/whats-new/0.3.0.md
//
// 参数：
//   --dir        electron-builder 的产物目录（默认 release2）
//   --base-url   安装包与清单对外可访问的基础地址（末尾不要带 /）
//   --notes      更新说明文件（纯文本 / markdown 均可，内容进 notes 字段）
//   --notes-text 更新说明短文本（直接给字符串，优先级低于 --notes 文件）
//   --mandatory  标记强制更新（前端不给"跳过此版本"）
//   --version    覆盖版本号（默认取 package.json 的 version）
//
// 产出：<dir>/latest.json，把它和安装包一起上传到 --base-url 指向的位置即可。
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// ============ 纯函数区（可被 tools/test-manifest-pick.cjs require 单测） ============

/** 候选安装包文件名过滤：只留 .exe、排除 uninstall、只接受纯 ASCII 名。
 *  排除中文名旧包的理由：artifactName 曾用中文（变声工坊-Setup-*.exe），改名后若 release2 里
 *  还留着同版本的中文旧包，会与 ASCII 新包同时命中版本号 → 可能发错包（中文 URL 编码脆弱）。 */
function isInstallerCandidate(fileName) {
  if (!/\.exe$/i.test(fileName)) return false;
  if (/uninstall/i.test(fileName)) return false;
  return /^[\x20-\x7E]+$/.test(fileName);
}

/** 版本号「独立片段」正则。
 *  不能简单用 [^0-9.] 排除，否则 `...-0.2.2.exe` 里版本号之后紧跟的 `.`（扩展名分隔符）
 *  会被判为非法边界而漏配；正确做法是用负向前瞻排除「后接数字」，使 0.2.2 不误配 0.2.20。 */
function versionMatcher(version) {
  const wantVer = String(version).replace(/^v/i, "");
  const verEsc = wantVer.replace(/\./g, "\\.");
  return new RegExp(`(?:^|[^0-9.])${verEsc}(?![0-9])`);
}

/** 从候选列表（已按 mtime 倒序）里按版本号选包。
 *  返回 { exe, exact }；exact=false 表示退化为「取最新」。
 *  选包策略（顺序敏感，发版安全关键）：
 *    1. 优先「文件名精确含目标版本号」的包（artifactName 模板确定性）。
 *    2. 精确匹配命中多个时取 mtime 最新（即列表首个）。
 *    3. 完全没命中才退化为「取 mtime 最新」，并打警告。
 *  坑：纯按 mtime 取最新会在「重打包旧版本」时静默选错包（把旧版清单发出去 = 用户更新死循环）。 */
function pickInstaller(candidates, version) {
  const verRe = versionMatcher(version);
  const exact = candidates.filter((e) => verRe.test(e.f));
  if (exact.length) return { exe: exact[0].f, exact: true };
  if (candidates.length) return { exe: candidates[0].f, exact: false };
  return { exe: null, exact: false };
}

module.exports = { isInstallerCandidate, versionMatcher, pickInstaller };

// require 时只导出纯函数，不跑主流程（单测需要）
if (require.main !== module) return;

// ============ 主流程 ============

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

// electron-builder 在 web/ 下跑（npm run electron:build），产物目录也在 web/ 下，
// 所以相对路径一律相对 web/ 解析，从项目根或 web/ 执行都一致。
const webDir = path.join(__dirname, "..");
const dir = path.resolve(webDir, arg("dir", "release2"));
const baseUrl = arg("base-url", "").replace(/\/+$/, "");
const notesFile = arg("notes", "");
const notesText = arg("notes-text", "");
const mandatory = process.argv.includes("--mandatory");

if (!fs.existsSync(dir)) {
  console.error(`找不到产物目录：${dir}（先跑 npm run electron:build）`);
  process.exit(1);
}

// 版本号：优先命令行，其次 web/package.json
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf-8"));
const version = arg("version", pkg.version);

const allExes = fs.readdirSync(dir)
  .filter(isInstallerCandidate)
  .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
  .sort((a, b) => b.t - a.t);

const wantVer = String(version).replace(/^v/i, "");
const picked = pickInstaller(allExes, version);
const exe = picked.exe;
if (exe && !picked.exact) {
  console.warn(`⚠ 没有文件名精确匹配版本 ${wantVer} 的安装包，退化为取最新的 ${exe}`);
  console.warn(`  已找到的包：${allExes.map((e) => e.f).join(", ")}`);
}
if (!exe) {
  console.error(`在 ${dir} 里没找到安装包 .exe`);
  process.exit(1);
}

const exePath = path.join(dir, exe);
const sha256 = crypto.createHash("sha256").update(fs.readFileSync(exePath)).digest("hex");
// 说明来源优先级：--notes 文件 > --notes-text 短文本
const notes = notesFile && fs.existsSync(notesFile)
  ? fs.readFileSync(notesFile, "utf-8").trim()
  : String(notesText || "").trim();

const manifest = {
  version,
  notes,
  pub_date: new Date().toISOString(),
  url: baseUrl ? `${baseUrl}/${encodeURIComponent(exe)}` : exe,
  sha256,
  size: fs.statSync(exePath).size,
  mandatory,
};

const out = path.join(dir, "latest.json");
fs.writeFileSync(out, JSON.stringify(manifest, null, 2), "utf-8");

console.log("已生成更新清单：", out);
console.log(JSON.stringify(manifest, null, 2));
if (!baseUrl) {
  console.log("\n注意：未给 --base-url，url 字段是本地文件名，上传前请手动补全下载地址。");
}

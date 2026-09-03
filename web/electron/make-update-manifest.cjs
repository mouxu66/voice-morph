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
//   --mandatory  标记强制更新（前端不给"跳过此版本"）
//   --version    覆盖版本号（默认取 package.json 的 version）
//
// 产出：<dir>/latest.json，把它和安装包一起上传到 --base-url 指向的位置即可。
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

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
const mandatory = process.argv.includes("--mandatory");

if (!fs.existsSync(dir)) {
  console.error(`找不到产物目录：${dir}（先跑 npm run electron:build）`);
  process.exit(1);
}

// 版本号：优先命令行，其次 web/package.json
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf-8"));
const version = arg("version", pkg.version);

// 找 NSIS 安装包（排除 latest.json 与其它中间产物）
const exe = fs.readdirSync(dir).find((f) => /\.exe$/i.test(f) && !/uninstall/i.test(f));
if (!exe) {
  console.error(`在 ${dir} 里没找到安装包 .exe`);
  process.exit(1);
}

const exePath = path.join(dir, exe);
const sha256 = crypto.createHash("sha256").update(fs.readFileSync(exePath)).digest("hex");
const notes = notesFile && fs.existsSync(notesFile)
  ? fs.readFileSync(notesFile, "utf-8").trim()
  : "";

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

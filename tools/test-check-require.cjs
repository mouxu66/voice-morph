#!/usr/bin/env node
/**
 * test-check-require.cjs —— 给 tools/check-require.cjs 自身的测试（6 例）。
 *
 * 为什么必须有：静态检查工具本身是「规则 + 正则」，规则退化会伪装成「代码有 bug」
 * （反向浪费排查时间），或对真 bug 视而不见（等于没装）。上次 lint 就踩过一次误报。
 * 所以这里同时锁两个方向：
 *   - 能抓到（用例 b：真缺 require 必须 FAIL）
 *   - 不乱抓（用例 c/d/e/f：合法写法必须不报）
 *
 * 用例：
 *   a 真仓 scan  → PASS
 *   b 用了 fs. 但没 require → 报错 + exit 1
 *   c let url = new URL(x) → 不报（防假阳性，updater.cjs 真实写法）
 *   d const fs = require("fs") + fs.readFileSync → 不报
 *   e require("node:fs") + fs. → 不报（node: 前缀归一）
 *   f const { existsSync } = require("fs") → 不报（解构）
 *
 * 运行：node tools/test-check-require.cjs
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");
const { checkFile } = require("./check-require.cjs");

const ROOT = path.join(__dirname, "..");
const ELECTRON_DIR = path.join(ROOT, "web", "electron");
const results = [];
let allOk = true;

function report(ok, label, detail) {
  if (!ok) allOk = false;
  results.push(`[${ok ? "PASS" : "FAIL"}] ${label} -- ${detail}`);
}

/** 造一个临时目录，里面放一个待检查的 .cjs，返回目录路径。 */
function makeTempModule(name, src) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "checkreq_"));
  fs.writeFileSync(path.join(dir, name), src, "utf-8");
  return dir;
}

// ---------------------------------------------------------------------------
// 用例 a：真仓 scan（修好后应 PASS）
// ---------------------------------------------------------------------------
{
  const files = fs
    .readdirSync(ELECTRON_DIR)
    .filter((f) => f.endsWith(".cjs"))
    .map((f) => path.join(ELECTRON_DIR, f));
  const bad = files.map(checkFile).filter((r) => r.missing.length);
  report(
    bad.length === 0,
    "a) 真仓 electron/*.cjs 全部通过",
    bad.length ? bad.map((b) => `${b.file}: ${b.missing.map((m) => m.ident).join(",")}`).join("; ") : `ok（${files.length} 文件）`
  );
}

// ---------------------------------------------------------------------------
// 用例 b：用了 fs. 但没 require → 必须报错
// ---------------------------------------------------------------------------
{
  const dir = makeTempModule(
    "b.cjs",
    `const path = require("path");\nconst p = path.join("a", "b");\nif (fs.existsSync(p)) { console.log("x"); }\n`
  );
  const r = checkFile(path.join(dir, "b.cjs"));
  const hit = r.missing.find((m) => m.ident === "fs");
  report(
    Boolean(hit),
    "b) 用了 fs. 但缺 require → 报出 fs",
    hit ? `ok（提示 require("${hit.modules[0]}")）` : `未报出（missing=${JSON.stringify(r.missing)}）`
  );
}

// ---------------------------------------------------------------------------
// 用例 c：let url = new URL(x); url.protocol → 不报（防假阳性）
// ---------------------------------------------------------------------------
{
  const dir = makeTempModule(
    "c.cjs",
    `function f(urlStr) {\n  let url;\n  url = new URL(urlStr);\n  const mod = url.protocol === "http:" ? 1 : 2;\n  return mod;\n}\nmodule.exports = { f };\n`
  );
  const r = checkFile(path.join(dir, "c.cjs"));
  const hit = r.missing.find((m) => m.ident === "url");
  report(
    !hit,
    "c) let url = new URL(x) → 不报（局部变量，非 url 模块）",
    hit ? `误报了 url` : "ok"
  );
}

// ---------------------------------------------------------------------------
// 用例 d：const fs = require("fs") + fs.readFileSync → 不报
// ---------------------------------------------------------------------------
{
  const dir = makeTempModule(
    "d.cjs",
    `const fs = require("fs");\nconst path = require("path");\nconst p = path.join("a", "b");\nif (fs.existsSync(p)) { fs.readFileSync(p); }\nmodule.exports = {};\n`
  );
  const r = checkFile(path.join(dir, "d.cjs"));
  report(
    r.missing.length === 0,
    "d) const fs = require(\"fs\") + fs. 用法 → 不报",
    r.missing.length ? `误报：${r.missing.map((m) => m.ident).join(",")}` : "ok"
  );
}

// ---------------------------------------------------------------------------
// 用例 e：require("node:fs") + fs. → 不报（前缀归一）
// ---------------------------------------------------------------------------
{
  const dir = makeTempModule(
    "e.cjs",
    `const fs = require("node:fs");\nconst os = require("node:os");\nconsole.log(fs.existsSync(os.tmpdir()));\nmodule.exports = {};\n`
  );
  const r = checkFile(path.join(dir, "e.cjs"));
  report(
    r.missing.length === 0,
    "e) require(\"node:fs\") + fs. → 不报（node: 前缀归一）",
    r.missing.length ? `误报：${r.missing.map((m) => m.ident).join(",")}` : "ok"
  );
}

// ---------------------------------------------------------------------------
// 用例 f：const { existsSync } = require("fs") → 不报（解构）
// ---------------------------------------------------------------------------
{
  const dir = makeTempModule(
    "f.cjs",
    `const { existsSync, readFileSync } = require("fs");\nif (existsSync("a")) { readFileSync("a"); }\nmodule.exports = {};\n`
  );
  const r = checkFile(path.join(dir, "f.cjs"));
  report(
    r.missing.length === 0,
    "f) 解构 require(\"fs\") 直接用 existsSync → 不报",
    r.missing.length ? `误报：${r.missing.map((m) => m.ident).join(",")}` : "ok"
  );
}

// ---------------------------------------------------------------------------
// 用例 g（附加）：CLI 退出码 —— 缺 require 的目录必须 exit 1，干净的必须 exit 0
// ---------------------------------------------------------------------------
{
  const badDir = makeTempModule("g.cjs", `if (fs.existsSync("a")) { console.log(1); }\n`);
  const dirty = spawnSync(process.execPath, [path.join(ROOT, "tools", "check-require.cjs"), badDir], {
    encoding: "utf-8",
  });
  report(
    dirty.status === 1,
    "g) CLI：缺 require 时退出码 = 1",
    `exit=${dirty.status}（期望 1）`
  );

  const goodDir = makeTempModule("g2.cjs", `const fs = require("fs");\nif (fs.existsSync("a")) {}\n`);
  const clean = spawnSync(process.execPath, [path.join(ROOT, "tools", "check-require.cjs"), goodDir], {
    encoding: "utf-8",
  });
  report(
    clean.status === 0,
    "g) CLI：无缺失时退出码 = 0",
    `exit=${clean.status}（期望 0）`
  );
}

console.log("");
console.log("===== test-check-require =====");
for (const r of results) console.log(r);
console.log("==============================");
console.log(allOk ? "RESULT: PASS" : "RESULT: FAIL");
process.exit(allOk ? 0 : 1);

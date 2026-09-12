#!/usr/bin/env node
/**
 * check-require.cjs —— 静态检查 electron/*.cjs 里「用了 Node 内建模块的标识符，但顶部没 require」。
 *
 * 为什么需要它（2026-09-12 事故）：
 *   `alt-hint.cjs` 从 `main.cjs` 拆出时漏了 `const fs = require("fs")`，却用了 `fs.existsSync`。
 *   开发模式侥幸没暴露，**打包后主进程 require 阶段即崩** —— `ReferenceError: fs is not defined`，
 *   用户装了 0.2.1/0.2.2 直接打不开。这类 bug 编译器不报、运行时才炸、且炸在启动路径上。
 *
 * 设计要点：
 *   1. **零依赖**：纯正则，不引 acorn/eslint（不为一个 lint 给发行包加依赖）。
 *   2. **必须排除本地声明名**：否则 `let url = new URL(x); url.protocol` 会被误报
 *      「缺 require("url")」—— updater.cjs 真实存在这种写法。误报会诱导人去加没用的
 *      require，把真信号淹没成噪音。
 *   3. **覆盖表放顶部常量**，加新模块只改一处。
 *
 * 用法：node tools/check-require.cjs [目录...]
 *   缺省扫 web/electron。退出码 0 = PASS，1 = FAIL。
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const DEFAULT_DIRS = [path.join(ROOT, "web", "electron")];

// ---------------------------------------------------------------------------
// 覆盖表：内建模块 → 它在代码里被使用的「模块级标识符」。
// 只有这里列出的标识符会被检查（避免把任意全局名都当模块）。
// 需要支持新模块时在这加，别改逻辑。
// ---------------------------------------------------------------------------
const MODULE_IDENTS = {
  fs: ["fs"],
  path: ["path"],
  os: ["os"],
  crypto: ["crypto"],
  child_process: [
    "child_process",
    "execSync",
    "spawnSync",
    "execFileSync",
    "execFile",
    "spawn",
    "exec",
    "fork",
  ],
  url: ["url"],
  util: ["util"],
  events: ["events"],
  stream: ["stream"],
  zlib: ["zlib"],
  http: ["http"],
  https: ["https"],
  net: ["net"],
  tty: ["tty"],
  readline: ["readline"],
  worker_threads: ["worker_threads"],
  string_decoder: ["string_decoder"],
  assert: ["assert"],
  querystring: ["querystring"],
  perf_hooks: ["perf_hooks"],
};

// 标识符 → 能提供它的模块列表（反向索引，一个标识符可能来自多个模块）
const IDENT_TO_MODULES = {};
for (const [mod, idents] of Object.entries(MODULE_IDENTS)) {
  for (const id of idents) {
    (IDENT_TO_MODULES[id] ||= []).push(mod);
  }
}

/** 归一化 require 目标：node:fs → fs；fs/promises → fs（子路径也算覆盖）。 */
function normalizeModuleName(raw) {
  return raw.replace(/^node:/, "");
}

/**
 * 判断某个 require 目标是否提供了 `mod` 模块。
 * fs / node:fs / fs/promises 都算提供了 fs。
 */
function requireCoversModule(reqName, mod) {
  const n = normalizeModuleName(reqName);
  return n === mod || n.startsWith(mod + "/");
}

/** 提取源码里所有 require 的模块名（含数组形式 require(["a","b"])）。 */
function extractRequires(src) {
  const out = new Set();
  const re = /require\(\s*\[?\s*['"]([^'"]+)['"]/g;
  for (const m of src.matchAll(re)) {
    out.add(normalizeModuleName(m[1]));
  }
  // 再补数组形式的后续元素：require(["a", "b"])
  const arrRe = /require\(\s*\[([^\]]+)\]/g;
  for (const m of src.matchAll(arrRe)) {
    for (const s of m[1].matchAll(/['"]([^'"]+)['"]/g)) {
      out.add(normalizeModuleName(s[1]));
    }
  }
  return out;
}

/**
 * 提取「本地声明出来的名字」—— 这些名字即使长得像模块名，也不是模块。
 *
 * 这是防假阳性的核心。覆盖：
 *   const/let/var X = ...            → X
 *   const { a, b: c } = ...          → a, c（含重命名）
 *   const [x, y] = ...               → x, y（数组解构）
 *   function f(...)                  → f
 *   函数参数 (a, b) / (a, {b})        → a, b（粗粒度，宁可多排除也不误报）
 *   class C / catch (e)              → C, e
 *   import X / import { Y }          → X, Y（.mjs 兼容留口）
 */
/**
 * 解析一个声明的左值（LHS），把声明出来的本地名加进 names。
 * 覆盖：普通名、多变量（a = 1, b = 2）、对象解构 { a, b: c }、数组解构 [x, y]。
 */
function addDeclNames(lhsRaw, add) {
  const lhs = (lhsRaw || "").trim();
  if (!lhs) return;
  // 对象解构 { a, b: c } —— 有重命名取冒号右边（那才是真正的本地名），否则取自身
  if (lhs.startsWith("{")) {
    for (const piece of lhs.replace(/[{}]/g, "").split(",")) {
      add(piece.split(":").pop());
    }
    return;
  }
  // 数组解构 [a, b]
  if (lhs.startsWith("[")) {
    for (const piece of lhs.replace(/[[\]]/g, "").split(",")) add(piece);
    return;
  }
  // 普通：可能是 `a` 或 `a = 1, b = 2`
  for (const piece of lhs.split(",")) add(piece.split("=")[0]);
}

function extractLocalNames(src) {
  const names = new Set();
  const add = (n) => {
    const t = (n || "").trim();
    if (t && /^[A-Za-z_$][\w$]*$/.test(t)) names.add(t);
  };

  // const/let/var 声明（含解构）
  for (const m of src.matchAll(/\b(?:const|let|var)\s+([^;=\n]+?)\s*=/g)) {
    addDeclNames(m[1], add);
  }
  // 无初始值的声明：`let url;` —— 后面才赋值（updater.cjs 就是这么写的：
  //   let url; url = new URL(urlStr); 然后 url.protocol
  // 不认这个就会把局部变量 url 误报成「缺 require("url")」）。
  for (const m of src.matchAll(/\b(?:const|let|var)\s+([^;=\n]+);/g)) {
    addDeclNames(m[1], add);
  }

  // function 名
  for (const m of src.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)/g)) add(m[1]);
  // class 名
  for (const m of src.matchAll(/\bclass\s+([A-Za-z_$][\w$]*)/g)) add(m[1]);
  // catch (e)
  for (const m of src.matchAll(/\bcatch\s*\(\s*([A-Za-z_$][\w$]*)/g)) add(m[1]);
  // import 形式（.mjs / 转译器产物）
  for (const m of src.matchAll(/\bimport\s+([A-Za-z_$][\w$]*)/g)) add(m[1]);
  for (const m of src.matchAll(/\bimport\s*\{([^}]+)\}/g)) {
    for (const piece of m[1].split(",")) add(piece.split(":").pop());
  }

  // 函数参数：粗粒度提取所有括号里的裸标识符（宁可多排除，也不误报）
  for (const m of src.matchAll(/function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)/g)) {
    for (const piece of m[1].split(",")) add(piece.split("=")[0].replace(/[{}[\]]/g, ""));
  }
  for (const m of src.matchAll(/\(([^)]*)\)\s*=>/g)) {
    for (const piece of m[1].split(",")) add(piece.split("=")[0].replace(/[{}[\]]/g, ""));
  }
  // 方法简写参数（module.exports = { foo(a, b) {} }）—— 粗略扫 { name(...) {} }
  for (const m of src.matchAll(/[{,]\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{/g)) {
    add(m[1]);
    for (const piece of (m[2] || "").split(",")) add(piece.split("=")[0].replace(/[{}[\]]/g, ""));
  }

  return names;
}

/** 源码里是否真的「用到」了这个标识符（ident. / ident( / ident[）。 */
function isIdentUsed(src, ident) {
  // 前面不能是 . 或标识符字符（排除 a.fs. / myfs. 这类属性名）
  const esc = ident.replace(/[$]/g, "\\$");
  const re = new RegExp("(^|[^.\\w$])" + esc + "\\s*[.([]", "m");
  return re.test(src);
}

/** 检查单个文件，返回 { file, requires: [], missing: [{ident, modules}] }。 */
function checkFile(filePath) {
  const src = fs.readFileSync(filePath, "utf-8");
  const requires = extractRequires(src);
  const locals = extractLocalNames(src);
  const missing = [];

  for (const [ident, mods] of Object.entries(IDENT_TO_MODULES)) {
    if (locals.has(ident)) continue;           // 本地声明的，不是模块
    if (!isIdentUsed(src, ident)) continue;    // 没用到
    const covered = mods.some((mod) => [...requires].some((r) => requireCoversModule(r, mod)));
    if (!covered) missing.push({ ident, modules: mods });
  }

  return { file: path.basename(filePath), requires: [...requires], missing };
}

function collectFiles(dirs) {
  const files = [];
  for (const d of dirs) {
    if (!fs.existsSync(d)) continue;
    const st = fs.statSync(d);
    if (st.isFile()) { files.push(d); continue; }
    for (const name of fs.readdirSync(d).sort()) {
      if (name.endsWith(".cjs") || name.endsWith(".mjs")) files.push(path.join(d, name));
    }
  }
  return files;
}

function main(argv) {
  const dirs = argv.length ? argv.map((a) => path.resolve(a)) : DEFAULT_DIRS;
  const files = collectFiles(dirs);
  if (!files.length) {
    console.error(`[check-require] 未找到待检查文件：${dirs.join(", ")}`);
    return 1;
  }

  const results = files.map(checkFile);
  const width = Math.max(...results.map((r) => r.file.length), 20);

  console.log("");
  console.log("===== check-require =====");
  console.log(
    "file".padEnd(width) + " | requires".padEnd(46) + " | 结论"
  );
  console.log("-".repeat(width + 46 + 16));

  let failed = 0;
  for (const r of results) {
    const reqTxt = r.requires.length ? r.requires.join(", ") : "(无)";
    let verdict;
    if (r.missing.length) {
      failed++;
      verdict = "FAIL 缺 require: " + r.missing.map((m) => `${m.ident} → 需 require("${m.modules[0]}")`).join("; ");
    } else {
      verdict = "ok";
    }
    console.log(r.file.padEnd(width) + " | " + reqTxt.slice(0, 44).padEnd(44) + " | " + verdict);
  }

  console.log("-".repeat(width + 46 + 16));
  if (failed) {
    console.log(`RESULT: FAIL（${failed}/${results.length} 个文件缺 require）`);
    console.log("");
    console.log("说明：这些标识符被使用但没 require，打包后会在 require 阶段抛 ReferenceError。");
    console.log("修法：在文件顶部补 const <ident> = require(\"<模块>\")。");
    return 1;
  }
  console.log(`RESULT: PASS（${results.length} 个文件全部通过）`);
  return 0;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = {
  MODULE_IDENTS,
  IDENT_TO_MODULES,
  normalizeModuleName,
  requireCoversModule,
  extractRequires,
  extractLocalNames,
  isIdentUsed,
  checkFile,
  collectFiles,
  main,
};

// audit_asar_freshness.cjs —— 已安装 app.asar 的「陈旧审计」：逐文件对账源码主进程。
//
// 为什么需要它（2026-09-21 数据根迁移时实测）：
//   asar 里的 electron/*.cjs 只有重打 asar 才会更新，而三条同步链路（sync_backend /
//   ship_frontend / backend_autosync）都碰不到它。结果就是：源码一直往前走，
//   包内主进程停在构建那天，没有任何门禁报红 —— 直到某天人工 diff 才发现差了 6 个
//   文件、还缺 2 个新文件（其中 setup-ipc.cjs 依赖缺的 build-watch.cjs，单独换会
//   MODULE_NOT_FOUND）。「版本号对」不等于「内容对」：包内 package.json 的 version
//   只反映装机那天，此后每次定向重打都不会动它。
//
// 判什么（默认目标 = 本机安装目录的 resources/app.asar）：
//   ① 缺失 —— 源码 web/electron/**/*.cjs + pet/** 有、包里没有 → 若被现有包内
//      模块 require 则**启动即崩**（致命）；
//   ② 陈旧 —— 两边都有但内容不同 → 列出（其中 smoke-*.cjs 是开发期自检，标注为
//      「允许陈旧」不计失败）；
//   ③ 多余 —— 包里有、源码没有 → 列出（可能是被删过的旧模块，残留要人确认）；
//   ④ 附带报告包内 version 与 _asarVersion（若有），让人看见「版本号 vs 内容」的错位。
//
// 退出码：0 = 无致命差异（陈旧/多余可为 0 也可为白名单内的允许项）；
//         1 = 有缺失或非白名单陈旧/多余。CI 无安装目录时直接 SKIP（exit 0）。
//
// 用法：
//   node tools/audit_asar_freshness.cjs                 # 默认目标（本机安装目录）
//   node tools/audit_asar_freshness.cjs --asar <path>   # 显式指定 asar
//   node tools/audit_asar_freshness.cjs --json          # 机器可读
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const args = process.argv.slice(2);
function argOf(flag) {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : null;
}
const asJson = args.includes("--json");

const RESOURCES = path.join(
  os.homedir(), "AppData", "Local", "Programs", "voice-morph-desktop", "resources",
);
const ASAR = argOf("--asar") || path.join(RESOURCES, "app.asar");

/** 默认允许陈旧的开发期自检脚本（打进包只是 electron-builder files 收整个目录的副作用） */
const STALE_ALLOWED = new Set(["smoke-loadpath.cjs", "smoke-pet-render.cjs"]);

/** 静态 require 图：谁依赖谁（.cjs 相对引用），用于判「缺失是否致命」 */
function requireGraph(srcDir) {
  const graph = new Map();
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (!e.name.endsWith(".cjs")) continue;
      const deps = [];
      const text = fs.readFileSync(p, "utf-8");
      for (const m of text.matchAll(/require\(\s*["'](\.[^"']+)["']\s*\)/g)) {
        let rel = m[1];
        // 归一化到包内相对 electron/ 的 posix 路径
        let resolved = path.posix.normalize(
          path.posix.join(path.posix.dirname(path.relative(srcDir, p).replaceAll("\\", "/")), rel),
        );
        if (!resolved.endsWith(".cjs")) resolved += ".cjs";
        deps.push(resolved);
      }
      graph.set(path.relative(srcDir, p).replaceAll("\\", "/"), deps);
    }
  };
  walk(srcDir);
  return graph;
}

function main() {
  if (!fs.existsSync(ASAR)) {
    if (asJson) console.log(JSON.stringify({ status: "SKIP", reason: "asar 不存在" }));
    else console.log("[asar-freshness] SKIP：未找到安装包 asar（本机没装？）", ASAR);
    return 0;
  }

  // 解包到临时目录（只读审计，用完即删）
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vm-asar-audit-"));
  const { execFileSync } = require("node:child_process");
  const asarBin = path.join(__dirname, "..", "web", "node_modules", "@electron", "asar", "bin", "asar.js");
  if (!fs.existsSync(asarBin)) {
    console.log("[asar-freshness] SKIP：web/node_modules 里没有 @electron/asar");
    return 0;
  }
  execFileSync(process.execPath, [asarBin, "extract", ASAR, tmp], { stdio: "ignore" });

  const srcElectron = path.join(__dirname, "..", "web", "electron");
  const pkgElectron = path.join(tmp, "electron");

  const srcFiles = new Set();
  (function walk(dir, prefix) {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) { walk(path.join(dir, e.name), rel); continue; }
      if (e.name.endsWith(".cjs") || e.name.endsWith(".html")) srcFiles.add(rel);
    }
  })(srcElectron, "");

  const pkgFiles = new Set();
  (function walk(dir, prefix) {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) { walk(path.join(dir, e.name), rel); continue; }
      if (e.name.endsWith(".cjs") || e.name.endsWith(".html")) pkgFiles.add(rel);
    }
  })(pkgElectron, "");

  const missing = [...srcFiles].filter((f) => !pkgFiles.has(f)).sort();
  const stale = [];
  for (const f of [...srcFiles].filter((f) => pkgFiles.has(f)).sort()) {
    const a = fs.readFileSync(path.join(srcElectron, f));
    const b = fs.readFileSync(path.join(pkgElectron, f));
    if (!a.equals(b)) stale.push(f);
  }
  const extra = [...pkgFiles].filter((f) => !srcFiles.has(f)).sort();

  // 缺失是否致命：被包内现存模块静态 require 到就崩
  const graph = requireGraph(pkgElectron);
  const fatalMissing = missing.filter((m) =>
    [...graph.entries()].some(([, deps]) => deps.includes(m)));

  // 包内版本信息（人读用）：version 反映装机那天；_asarVersion 是定向重打时留下的标注
  let pkgVersion = null, asarVersion = null;
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(tmp, "package.json"), "utf-8"));
    pkgVersion = pkg.version || null;
    asarVersion = pkg._asarVersion || null;
  } catch { /* 保持 null */ }

  const staleFatal = stale.filter((f) => !STALE_ALLOWED.has(path.basename(f)));
  const ok = fatalMissing.length === 0 && staleFatal.length === 0;
  const summary = {
    asar: ASAR, pkgVersion, asarVersion,
    missing, fatalMissing, stale, staleFatal, staleAllowed: stale.filter((f) => STALE_ALLOWED.has(path.basename(f))),
    extra, ok,
  };

  if (asJson) {
    console.log(JSON.stringify(summary, null, 2));
  } else {
    console.log(`[asar-freshness] 目标: ${ASAR}`);
    console.log(`  包内 version=${pkgVersion ?? "?"}${asarVersion ? ` · _asarVersion=${asarVersion}` : ""}`);
    if (missing.length) {
      console.log(`  缺失 ${missing.length} 个：${missing.join(", ")}`);
      if (fatalMissing.length) console.log(`  ★ 其中被包内模块 require（致命）：${fatalMissing.join(", ")}`);
    }
    if (stale.length) {
      console.log(`  陈旧 ${stale.length} 个：`);
      for (const f of stale) console.log(`    ${f}${STALE_ALLOWED.has(path.basename(f)) ? "  （允许：开发期自检）" : "  ★"}`);
    }
    if (extra.length) console.log(`  多余 ${extra.length} 个：${extra.join(", ")}`);
    if (!missing.length && !stale.length && !extra.length) console.log("  与源码逐字节一致 ✓");
    console.log(ok && (missing.length || extra.length) === 0
      ? "  RESULT: OK"
      : ok ? "  RESULT: OK（仅白名单内差异）" : "  RESULT: FAIL —— 需重打 asar（见 docs/分发与打包说明.md §7.2.1）");
  }

  fs.rmSync(tmp, { recursive: true, force: true });
  return ok ? 0 : 1;
}

process.exit(main());

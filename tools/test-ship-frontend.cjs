#!/usr/bin/env node
/**
 * test-ship-frontend.cjs —— 守护 `tools/ship_frontend.cjs` 的路径护栏与差异计算。
 *
 * 为什么值得测：ship 脚本会**删目标目录下的文件**，它唯一的防线就是
 * `assertSafeTarget()`。这条护栏如果松了，一次路径算错就是不可逆的破坏，
 * 而且这类 bug 平时跑不出来（正常路径下永远通过）。
 * 另外 `planSync()` 决定"删哪些"，漏算会留下陈旧资源、多算会删掉在用的文件。
 */
"use strict"

const assert = require("node:assert")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")

const { defaultTarget, assertSafeTarget, planSync, copyTree, diffTrees, TARGET_SUFFIX } = require("./ship_frontend.cjs")

const results = []
let allOk = true
function report(ok, label, detail) {
  results.push(`${ok ? "[PASS]" : "[FAIL]"} ${label}${detail ? ` -- ${detail}` : ""}`)
  if (!ok) allOk = false
}

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ship-test-"))

/** 造一棵文件树：{ "a/b.txt": "x" } */
function makeTree(root, files) {
  for (const [rel, body] of Object.entries(files)) {
    const abs = path.join(root, ...rel.split("/"))
    fs.mkdirSync(path.dirname(abs), { recursive: true })
    fs.writeFileSync(abs, body)
  }
  return root
}

// ---- a) defaultTarget ----
{
  const win = defaultTarget({
    platform: "win32",
    env: { LOCALAPPDATA: "C:\\Users\\demo\\AppData\\Local" },
    home: "/home/demo",
  })
  report(
    typeof win === "string" && win.endsWith(path.join("Programs", TARGET_SUFFIX)),
    "a) win32 + LOCALAPPDATA → 指向 Programs/<target>",
    String(win),
  )

  const fallback = defaultTarget({ platform: "win32", env: {}, home: path.join("C:", "Users", "demo") })
  report(
    typeof fallback === "string" && fallback.includes("AppData"),
    "a) win32 无 LOCALAPPDATA → 回落到 <home>/AppData/Local",
    String(fallback),
  )

  report(defaultTarget({ platform: "linux", env: {} }) === null, "a) 非 Windows → null")

  const overridden = defaultTarget({ platform: "linux", env: { VM_DESKTOP_BACKEND: "/custom/backend" } })
  report(overridden === "/custom/backend", "a) VM_DESKTOP_BACKEND 优先", String(overridden))
}

// ---- b) assertSafeTarget 护栏 ----
{
  const good = path.join(tmpRoot, "voice-morph-desktop", "resources", "backend")
  let accepted = null
  try {
    accepted = assertSafeTarget(good)
  } catch {
    /* 期望通过 */
  }
  report(accepted === path.resolve(good), "b) 合法安装目录 → 接受")

  const rejects = [
    ["null", null],
    ["盘符根/文件系统根", path.parse(process.cwd()).root],
    ["任意项目目录", path.join(tmpRoot, "some", "other", "place")],
    ["层级过浅", path.join(path.parse(process.cwd()).root, "temp")],
    ["看似接近但结尾不符", path.join(tmpRoot, "voice-morph-desktop", "resources", "backend-old")],
  ]
  for (const [label, input] of rejects) {
    let threw = false
    try {
      assertSafeTarget(input)
    } catch {
      threw = true
    }
    report(threw, `b) 拒绝：${label}`, String(input))
  }
}

// ---- c) planSync 差异计算 ----
{
  const src = makeTree(path.join(tmpRoot, "dist"), {
    "index.html": "<html>new</html>",
    "assets/a.js": "A",
    "assets/b.css": "B",
  })
  const dst = makeTree(path.join(tmpRoot, "web_dist"), {
    "index.html": "<html>old</html>",
    "assets/a.js": "A",
    "assets/old.js": "OLD",
    "assets/old.css": "OLDCSS",
  })

  const plan = planSync(src, dst)
  assert(plan.count === 3, `count 应为 3，实际 ${plan.count}`)
  assert.deepStrictEqual(plan.stale, ["assets/old.css", "assets/old.js"])
  report(plan.count === 3, "c) planSync 统计源文件数", `count=${plan.count}`)
  report(
    JSON.stringify(plan.stale) === JSON.stringify(["assets/old.css", "assets/old.js"]),
    "c) planSync 找出多余文件（换 hash 后的陈旧资源）",
    plan.stale.join(", ") || "(空)",
  )
  // 源里有、目标没有的：assets/b.css（新增资源）
  report(plan.missing.length === 1 && plan.missing[0] === "assets/b.css", "c) planSync 认出目标缺失的新资源", plan.missing.join(", "))

  const empty = planSync(src, path.join(tmpRoot, "not-exists"))
  report(
    empty.stale.length === 0 && empty.missing.length === 3,
    "c) 目标不存在 → 全部计为缺失、不产生删除项",
    `missing=${empty.missing.length}`,
  )
}

// ---- d) copyTree 覆盖式递归拷贝 ----
{
  const src = makeTree(path.join(tmpRoot, "d-src"), {
    "index.html": "NEW",
    "assets/a.js": "A2",
    "assets/deep/nested/b.css": "B2",
  })
  const dst = makeTree(path.join(tmpRoot, "d-dst"), {
    "index.html": "OLD",
    "assets/keep.me": "KEEP",
  })

  const { copied, failed } = copyTree(src, dst)
  report(copied === 3 && failed.length === 0, "d) copyTree 拷满 3 个文件且无失败", `copied=${copied}`)
  report(
    fs.readFileSync(path.join(dst, "index.html"), "utf8") === "NEW",
    "d) copyTree 覆盖了已存在的文件",
  )
  report(
    fs.existsSync(path.join(dst, "assets", "deep", "nested", "b.css")),
    "d) copyTree 建出了缺失的嵌套目录",
  )
  report(
    fs.readFileSync(path.join(dst, "assets", "keep.me"), "utf8") === "KEEP",
    "d) copyTree 不删除目标已有文件（删除交给 plan.stale 精确执行）",
  )
}

// ---- e) diffTrees 逐字节比对（ship 的自检就是它）----
{
  const src = makeTree(path.join(tmpRoot, "e-src"), { "index.html": "SAME", "assets/a.js": "AAA" })
  const same = makeTree(path.join(tmpRoot, "e-same"), { "index.html": "SAME", "assets/a.js": "AAA" })
  const d1 = diffTrees(src, same)
  report(
    d1.mismatched.length === 0 && d1.missing.length === 0 && d1.extra.length === 0,
    "e) 完全一致 → 三类差异都为 0",
    `total=${d1.total}`,
  )

  const drift = makeTree(path.join(tmpRoot, "e-drift"), { "index.html": "DIFF", "assets/old.js": "X" })
  const d2 = diffTrees(src, drift)
  report(d2.mismatched.includes("index.html"), "e) 内容不同 → 计入 mismatched", d2.mismatched.join(", "))
  report(d2.missing.includes("assets/a.js"), "e) 源有目标无 → 计入 missing", d2.missing.join(", "))
  report(d2.extra.includes("assets/old.js"), "e) 目标有源无 → 计入 extra", d2.extra.join(", "))
}

fs.rmSync(tmpRoot, { recursive: true, force: true })

console.log("")
console.log("===== test-ship-frontend =====")
for (const r of results) console.log(r)
console.log("==============================")
console.log(allOk ? "RESULT: PASS" : "RESULT: FAIL")
process.exit(allOk ? 0 : 1)

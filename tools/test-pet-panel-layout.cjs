#!/usr/bin/env node
/**
 * test-pet-panel-layout.cjs —— 桌宠快捷面板「在 220×480 透明窗里放得下」的布局 + 纵向预算门禁。
 *
 * 运行：
 *   node tools/test-pet-panel-layout.cjs             # 静态 + 预算扫描 + 默认 5 个场景
 *   node tools/test-pet-panel-layout.cjs --fast      # 静态 + 预算扫描（pre-commit 走这条）
 *   node tools/test-pet-panel-layout.cjs --static    # 只要静态（没有浏览器也能跑，CI 用）
 *   VM_PET_LAYOUT_ALL=1 node tools/test-pet-panel-layout.cjs        # 全部 13 个场景
 *   VM_PET_BUDGET_UPDATE=1 node tools/test-pet-panel-layout.cjs     # ★ 重算基线（会打成本表）
 * 退出码 0 = 通过，非 0 = 失败。（tools/check.py 的 petlayout 步跑的是 --fast 那条。）
 *
 * ---------------------------------------------------------------------------
 * 这个门禁要解决的**两件事**（2026-09-29 改造前只有第一件）
 *
 * 1) 「被切」：气泡与面板共用一个 480px 高的窗口，可用高 = 480 - 18(#root) - 150(角色)
 *    - 6(面板下边距) - 气泡 - 6(气泡下边距)。气泡一起来面板就被 flex 压扁，底部
 *    「最近发送」被切掉一截并冒出内部滚动条（2026-09-17 实测：guide 切 6px / think 16px /
 *    offline 34px）。判据是 scrollHeight > clientHeight —— 「看起来没问题」不是证据。
 *
 * 2) ★「余量被吃掉」：**只看溢出是不够的**。面板余量本来就很薄（实测最紧的场景只剩 2px：
 *    fxpick / offline），所以新加一个控件完全可能「今天刚好不溢出、下一个人再动手就爆」。
 *    那时没人知道是谁吃掉的。所以把**余量本身**当成不变量：
 *        reserve = capacity - need      （布局最多给多高 − 内容需要多高）
 *        reserve 只能变多，不能变少（超出容差即红），基线见 pet-panel-layout-budget.json。
 *    越界时报告会**指名道姓**：哪个块从多少变成多少（+N px）、哪个块是新出现的。
 *
 * 为什么「余量」要这么算：面板是 height:auto + max-height:300px，内容不超时
 * clientHeight 恰好等于内容高 —— 于是 clientHeight - scrollHeight **恒为 0**，
 * 只在被切时才是负数。它是「切了多少」，不是「还剩多少」。真正的余量必须拿
 * 「布局能给的上限」（临时把面板顶到 1000px 实测，见 pet-panel-preview 的 capacityOf）
 * 减去「内容需要的高」。
 *
 * 为什么覆盖不靠手写场景清单：预算扫描（measureSweep）在**一次页面加载**里把所有
 * 功能页页签（从 DOM 枚举：tabRow 里的 button[data-tab]）× {无气泡, 大头气泡} 量一遍
 * （约 0.9s）。所以加第四页、加一个新控件，都会自动被扫到 —— 不存在「新页没人给它写场景，
 * 于是它多长都看不见」这种漏。
 *
 * ⚠️ 需要 Playwright 缓存里的 chromium-headless-shell（或 VM_CHROME 指定）。
 * 找不到时**大声跳过**浏览器部分（静态部分照跑）—— CI 机器不一定装浏览器，
 * 不能因此把整条 check 判红。要让「缺浏览器」也算失败，设 VM_PET_LAYOUT_REQUIRE=1。
 */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const preview = require("./pet-panel-preview.cjs");

const ROOT = preview.ROOT;
const PET_HTML = process.env.VM_PET_HTML || path.join(ROOT, "web", "electron", "pet", "pet.html");
const BUDGET_FILE = process.env.VM_PET_BUDGET || path.join(ROOT, "tools", "pet-panel-layout-budget.json");

/** 默认只量几个「把不变量钉住」的场景（每个场景要起一次 headless Chromium，约 0.7s）。
 *  预算扫描（sweep）不在这份名单里 —— 它是自动全页覆盖，见文件头。 */
const DEFAULT_SCENES = ["ok-dark", "guide-dark", "offline-dark", "fxpick-dark", "holdrec-dark",
                        "holdwarn-dark"];   // 预检说「注定失败」的那一态：文案最长，专门量它
const ALL_SCENES = preview.SCENES;

const argv = process.argv.slice(2);
const FAST = argv.includes("--fast");
const STATIC_ONLY = argv.includes("--static");
const UPDATE = process.env.VM_PET_BUDGET_UPDATE === "1";
const argvScenes = argv.filter((a) => !a.startsWith("-"));

let pass = 0;
const failures = [];
function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log("  ok   " + name);
  } catch (e) {
    failures.push(name + " —— " + e.message);
    console.log("  FAIL " + name + "\n       " + e.message);
  }
}

// ---------------------------------------------------------------------------
// 判据本身：余量 / 块高基线比对（纯函数 —— 好让下面的自检能单独喂它假数据）
// ---------------------------------------------------------------------------

/**
 * 拿一次量测跟基线比。
 * @returns {{status:string, dReserve?:number, worse?:boolean, added:Array, grew:Array, shrunk:Array, removed:Array}}
 *   added  = 基线里没有、这次有的块（新增控件）
 *   grew   = 变高的块（从多少 → 多少）
 */
function budgetDiff(base, meas, tol) {
  const out = { status: "cmp", dReserve: meas.reserve - base.reserve, added: [], grew: [], shrunk: [], removed: [] };
  for (const [id, h] of Object.entries(meas.blocks || {})) {
    if (!(id in (base.blocks || {}))) {
      if (h > 0) out.added.push({ id, h });
    } else if (h > base.blocks[id]) {
      out.grew.push({ id, from: base.blocks[id], to: h, dx: h - base.blocks[id] });
    } else if (h < base.blocks[id]) {
      out.shrunk.push({ id, from: base.blocks[id], to: h, dx: h - base.blocks[id] });
    }
  }
  out.removed = Object.keys(base.blocks || {}).filter((id) => !(id in (meas.blocks || {})));
  out.worse = out.dReserve < -tol;
  return out;
}

/** 把一份比对结果写成人能看懂的一段（谁吃掉了余量）。 */
function describeDiff(key, diff, base, meas) {
  const lines = [
    key + "：余量 " + base.reserve + "px → " + meas.reserve + "px（" +
      (diff.dReserve > 0 ? "+" : "") + diff.dReserve + "px，" +
      "内容需要 " + base.need + "→" + meas.need + "px，布局能给 " + base.capacity + "→" + meas.capacity + "px）",
  ];
  const who = [];
  for (const a of diff.added) who.push("新增 " + a.id + "（+" + a.h + "px）");
  for (const g of diff.grew) who.push(g.id + " " + g.from + "→" + g.to + "px（+" + g.dx + "）");
  if (who.length) lines.push("      吃掉余量的： " + who.join("、"));
  if (diff.removed.length) lines.push("      基线里有、这次没有： " + diff.removed.join("、"));
  if (diff.shrunk.length) lines.push("      变矮的： " + diff.shrunk.map((s) => s.id + " " + s.dx + "px").join("、"));
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// 静态层（不需要浏览器）——「新页/新控件必须登记」的兜底
// ---------------------------------------------------------------------------

/** 从 pet.html 的 tabRow 里读页签名（唯一真相源是页面本身，不另抄一份清单）。 */
function tabsInHtml() {
  const html = fs.readFileSync(PET_HTML, "utf-8");
  const row = html.slice(html.indexOf('id="tabRow"'));
  const end = row.indexOf("</div>");
  const body = end > 0 ? row.slice(0, end) : row.slice(0, 2000);
  return [...body.matchAll(/data-tab="([^"]+)"/g)].map((m) => m[1]);
}

function readBudget() {
  if (!fs.existsSync(BUDGET_FILE)) return null;
  try {
    return JSON.parse(fs.readFileSync(BUDGET_FILE, "utf-8"));
  } catch (e) {
    return { _broken: String(e) };
  }
}

function writeBudget(keys, tol) {
  const payload = {
    _note:
      "桌宠面板纵向预算基线（自动生成）。更新：node tools/test-pet-panel-layout.cjs " +
      "（设 VM_PET_BUDGET_UPDATE=1，会打印一份「这次吃了多少」的成本表）。" +
      "reserve = capacity - need；门禁要求 reserve 只能变多（容差 tolerance_px）。" +
      "reserve 为负 = 该状态底部已被切（已知不足，见 docs/桌宠遥控器.md）。",
    tolerance_px: tol,
    keys,
  };
  fs.writeFileSync(BUDGET_FILE, JSON.stringify(payload, null, 2) + "\n", "utf-8");
}

const TOL = 1; // px。字体的亚像素/取整会让同一个页面差 1px —— 那不该判红，但也不许更大。
const budget = readBudget();

console.log("===== test-pet-panel-layout =====");
console.log("模式下：" + (UPDATE ? "更新基线" : FAST ? "fast（静态+预算扫描）" : STATIC_ONLY ? "静态" : "静态+扫描+场景"));

// ---- 静态 1：基线文件本身 ----
check("预算基线文件存在且可解析", () => {
  assert.ok(budget, "缺 " + path.relative(ROOT, BUDGET_FILE) + " —— 跑 VM_PET_BUDGET_UPDATE=1 node tools/test-pet-panel-layout.cjs 生成");
  assert.ok(!budget._broken, "基线 JSON 坏了：" + budget._broken);
  assert.strictEqual(budget.tolerance_px, TOL, "容差被改过？它和本文件的 TOL 必须一致");
  assert.ok(budget.keys && Object.keys(budget.keys).length > 0, "基线里一个 key 都没有");
});

const baseKeys = (budget && budget.keys) || {};

// ---- 静态 2：每个功能页都必须有预算基线（新加的页不能没有余量台账）----
check("每个功能页（tabRow 里的 data-tab）都有预算基线", () => {
  const tabs = tabsInHtml();
  assert.ok(tabs.length >= 3, "tabRow 里只读到 " + tabs.length + " 个页签 —— 抽取正则失效了？");
  const missing = [];
  for (const t of tabs) {
    for (const ph of ["bubble0", "bubble1"]) {
      const k = "sweep:" + t + ":" + ph;
      if (!(k in baseKeys)) missing.push(k);
    }
  }
  assert.deepStrictEqual(missing, [],
    "这些页/状态没有预算基线： " + missing.join("、") +
    "\n       新增了一个功能页（或改了页签名）就必须重算基线：" +
    " VM_PET_BUDGET_UPDATE=1 node tools/test-pet-panel-layout.cjs（它会打印成本表，确认在预算内再提交）");
});

// ---- 静态 3：基线里的块名不能是死的（防「删了控件、基线留着」）----
check("基线里的控件的真在 pet.html 里（防死条目）", () => {
  const html = fs.readFileSync(PET_HTML, "utf-8");
  const dead = [];
  for (const [key, entry] of Object.entries(baseKeys)) {
    for (const id of Object.keys(entry.blocks || {})) {
      if (!/^[A-Za-z][\w-]*$/.test(id)) continue;      // 没有 id 的块用 className 记的，跳过
      if (!html.includes('id="' + id + '"')) dead.push(key + " → #" + id);
    }
  }
  assert.deepStrictEqual(dead, [],
    "基线里引用了 pet.html 里已不存在的控件（删控件后没重算基线）： " + dead.slice(0, 6).join("、") +
    (dead.length > 6 ? " 等 " + dead.length + " 条" : ""));
});

// ---- 静态 4：判据自检（让门禁自己证明它看得见）----
// 这段不依赖浏览器，也不依赖真数据：喂给 budgetDiff 一批假量测，确认
// 「余量变少」「新增控件」「长高」都真的会被判红、「余量变多」「容差内」不会。
// 少了它，判据写错（比如把 `<` 写成 `<=`、或去比了恒为 0 的 slack）会**静默放行**。
check("判据自检：余量变少 / 新增控件 / 长高 都会被判红", () => {
  const base = { reserve: 10, need: 100, capacity: 110, blocks: { a: 20, b: 30 } };
  const meas = (reserve, blocks) => ({ reserve, need: 100 + (10 - reserve), capacity: 110, blocks });

  assert.strictEqual(budgetDiff(base, meas(10, { a: 20, b: 30 }), TOL).worse, false, "完全没变不该判红");
  assert.strictEqual(budgetDiff(base, meas(11, { a: 20, b: 30 }), TOL).worse, false, "余量变多不该判红");
  assert.strictEqual(budgetDiff(base, meas(9, { a: 20, b: 30 }), TOL).worse, false, "容差内（1px）不该判红");

  const worse = budgetDiff(base, meas(0, { a: 20, b: 30, c: 10 }), TOL);
  assert.strictEqual(worse.worse, true, "余量少 10px 必须判红");
  assert.deepStrictEqual(worse.added, [{ id: "c", h: 10 }], "新增的块必须被指名");

  const grew = budgetDiff(base, meas(4, { a: 20, b: 36 }), TOL);
  assert.strictEqual(grew.worse, true, "长高 6px 必须判红");
  assert.deepStrictEqual(grew.grew, [{ id: "b", from: 30, to: 36, dx: 6 }]);

  // 零高度的新增块（隐藏页里的占位）不该被当成「吃掉了余量」 —— 它没吃
  assert.deepStrictEqual(budgetDiff(base, meas(10, { a: 20, b: 30, c: 0 }), TOL).added, []);
});

// ---------------------------------------------------------------------------
// 浏览器层
// ---------------------------------------------------------------------------

const chrome = preview.findChrome();
if (!chrome) {
  console.log("");
  console.log("  **************************************************************");
  console.log("  [skip] 找不到 chromium-headless-shell，**量测部分没有执行**。");
  console.log("         面板溢出/预算这类问题在本机未被验证 —— 别把这条 skip 当通过。");
  console.log("         静态部分（含「新页必须有基线」）已跑完：见上面的 ok/FAIL。");
  console.log("         装 Playwright，或设 VM_CHROME 指向 chrome.exe 后重跑。");
  console.log("  **************************************************************");
  console.log("");
  if (process.env.VM_PET_LAYOUT_REQUIRE === "1") {
    console.log("VM_PET_LAYOUT_REQUIRE=1，缺浏览器视为失败。");
    process.exit(1);
  }
  console.log("===== " + (failures.length ? "FAIL" : "OK") + "（静态） =====");
  console.log(pass + " 通过, " + failures.length + " 失败");
  failures.forEach((f) => console.log("  - " + f));
  process.exit(failures.length ? 1 : 0);
}
console.log("chromium: " + chrome);

/** 本次量到的所有 key（后面既用于比对，也用于更新基线）。 */
const measured = {};

function record(entry) {
  measured[entry.scen] = {
    need: entry.budget.scrollH,
    capacity: entry.budget.capacity,
    reserve: entry.budget.reserve,
    blocks: Object.fromEntries((entry.blocks || []).map((b) => [b.id, b.h])),
    tab: entry.tab,
    controls: entry.controls || [],
  };
}

// ---- 预算扫描：所有功能页 × 两种气泡（一次页面加载）----
console.log("--- 预算扫描（所有功能页 × 无气泡/大头气泡）");
const sweep = STATIC_ONLY ? { all: [] } : preview.measureSweep({});
if (!STATIC_ONLY) {
  check("预算扫描能渲染并回传量测数据", () => {
    assert.ok(sweep.all.length > 0, "一条 MEASURE 都没拿到（页面脚本可能抛异常）\n" + sweep.err.split("\n").slice(-6).join("\n"));
  });
}
for (const entry of sweep.all) record(entry);

if (sweep.all.length) {
  const tabs = sweep.all[0].tabs || [];
  check("预算扫描覆盖了所有功能页（每页 × 2 种气泡）", () => {
    const want = [];
    for (const t of tabs) want.push("sweep:" + t + ":bubble0", "sweep:" + t + ":bubble1");
    const got = sweep.all.map((m) => m.scen);
    const missing = want.filter((k) => !got.includes(k));
    assert.deepStrictEqual(missing, [], "扫描漏了：" + missing.join("、") + "（tabs=" + JSON.stringify(tabs) + "）");
  });
}

// ---- 每个量到的 key：余量不许变少 + 不许溢出 ----
console.log("--- 纵向预算（reserve = 布局能给 − 内容需要；只许变多）");
const knownCuts = [];
for (const [key, m] of Object.entries(measured)) {
  const base = baseKeys[key];
  if (!base) continue;
  const diff = budgetDiff(base, m, TOL);
  if (m.reserve < 0 && base.reserve < 0) knownCuts.push(key + "：" + base.reserve + " → " + m.reserve + "px");

  check(key + "：余量没有变少（" + base.reserve + "px → " + m.reserve + "px）", () => {
    assert.ok(!diff.worse,
      describeDiff(key, diff, base, m) +
      "\n       纵向预算是死的：面板 max-height 300px，而最紧的场景实测只剩 2px。" +
      "\n       两条路：① 精简/收窄新控件；② 让它参与让位（#root.compact）。" +
      "\n       确认这次增长是必须的，再 VM_PET_BUDGET_UPDATE=1 重算基线（会打印成本表）。");
  });
}

for (const [key, m] of Object.entries(measured)) {
  if (!(key in baseKeys)) continue;
  if (baseKeys[key].reserve < 0) continue;   // 已被切的状态另有专门的检查在下面
  check(key + "：底部没有被切（内容 ≤ 布局能给）", () => {
    assert.ok(m.reserve >= 0,
      "内容需要 " + m.need + "px，布局只给 " + m.capacity + "px —— 底部被切 " +
      Math.abs(m.reserve) + "px 并冒出内部滚动条。最占地方的块：" +
      Object.entries(m.blocks).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([id, h]) => id + "=" + h + "px").join("、"));
  });
}

if (knownCuts.length) {
  console.log("  ⚠  已知溢出（基线里就记着的负数余量，不拦新改动，但别让它变差）：");
  knownCuts.forEach((c) => console.log("      " + c));
}

// ---- 场景模式：既有的那几条不变量（让位/不裁字/不透明/角色不挪）----
const scenes = STATIC_ONLY
  ? []
  : (argvScenes.length ? argvScenes
    : (UPDATE || process.env.VM_PET_LAYOUT_ALL === "1") ? ALL_SCENES
      : FAST ? [] : DEFAULT_SCENES);

let got = {};
if (scenes.length) {
  console.log("--- 场景：" + scenes.join(", "));
  const r = preview.measureScenes(scenes, {});
  got = r.scenes;
  for (const s of scenes) {
    const m = got[s];
    if (!m) {
      check(s + "：能渲染并回传量测数据", () => {
        throw new Error("没有拿到该场景的 MEASURE 输出（页面脚本可能抛异常了）");
      });
      continue;
    }
    record(m);

    // 1) 不变量：面板内容必须装得下（这才是「被切」的直接证据）
    check(s + "：面板内容不溢出（scrollHeight ≤ clientHeight）", () => {
      assert.strictEqual(m.panelOverflow, false,
        "面板内容 " + m.panelScrollH + "px 装进 " + m.panelClientH + "px —— " +
        "底部会被切掉 " + (m.panelScrollH - m.panelClientH) + "px 并冒出内部滚动条。" +
        "气泡在屏时应由 #root.compact 让位（收起 #recent），检查 setBubbleVisible 是否同步了 .compact");
    });

    // 2) 不变量：没有任何东西被挤出窗口
    check(s + "：没有元素被挤出窗口（root.scrollHeight ≤ 窗高）", () => {
      assert.ok(m.rootScrollH <= m.win.h,
        "root.scrollHeight=" + m.rootScrollH + " > 窗高 " + m.win.h);
    });

    // 3) 不变量：控件文字都不许被裁（204px 宽度是硬预算）
    //    判据是 Range 量出的文字自然宽 vs 内容盒宽（见 pet-panel-preview 里 clip 的注释）：
    //    按钮带 overflow:hidden，scrollWidth 会被钳到 clientWidth —— 那才是假绿。
    //    控件清单是**自动枚举**的（面板里每个 <button>/<input> + 状态条三个 chip），
    //    所以新加的按钮不用谁记得来登记，挤窄了就会红。
    check(s + "：控件文字都没有被裁（文字自然宽 ≤ 内容盒宽）", () => {
      const bad = (m.controls || []).filter((c) => c.clipped);
      assert.deepStrictEqual(bad, [],
        "被裁的元素：" + bad.map((b) => b.id + "(" + b.txt + " " + b.textW + ">" + b.availW + ")").join(", "));
    });

    // 4) 不变量：入场动画结束后面板必须完全不透明（否则截图/肉眼会误判成「颜色发灰」）
    check(s + "：面板动画结束后 opacity = 1", () => {
      assert.strictEqual(m.panelOpacity, "1", "opacity=" + m.panelOpacity);
    });

    // 5) 让位规则真的生效：有气泡时 #recent 收起，没气泡时它在
    const recent = (m.blocks || []).find((b) => b.id === "recent");
    if (recent) {
      check(s + "：让位规则与气泡显隐一致（有气泡→#recent 收起；无气泡→可见）", () => {
        if (m.bubble > 0) {
          assert.strictEqual(recent.disp, "none",
            "气泡在屏（" + m.bubble + "px）但 #recent 仍占 " + recent.h + "px —— 面板没让位");
        } else {
          assert.notStrictEqual(recent.disp, "none",
            "没有气泡，面板是完整态，#recent 不该被藏起来");
        }
      });
    }
  }

  // 6) 各场景的角色位置必须一致 —— 面板让位不该把角色顶走
  const tops = scenes.map((s) => got[s] && got[s].petTop).filter((v) => typeof v === "number");
  if (tops.length > 1) {
    check("各场景角色位置一致（让位只动面板，不该顶走角色）", () => {
      const uniq = [...new Set(tops)];
      assert.strictEqual(uniq.length, 1, "各场景 petTop 不一致：" +
        scenes.map((s) => s + "=" + (got[s] && got[s].petTop)).join(", "));
    });
  }
}

// ---------------------------------------------------------------------------
// 预算表（绿灯也打出来 —— 「现在还剩多少」应该是随时能看见的数字）
// ---------------------------------------------------------------------------

const rows = Object.entries(measured).map(([key, m]) => {
  const base = baseKeys[key];
  return {
    key,
    over: !base,
    need: m.need,
    capacity: m.capacity,
    reserve: m.reserve,
    base: base ? base.reserve : null,
    delta: base ? m.reserve - base.reserve : null,
  };
});
rows.sort((a, b) => a.reserve - b.reserve);
console.log("--- 纵向预算表（按余量升序，最紧的在最上面）");
console.log("    key".padEnd(30) + "reserve   基线   Δ   need/capacity");
for (const r of rows) {
  console.log("    " + r.key.padEnd(27) +
    String(r.reserve + "px").padStart(6) + "  " +
    (r.base === null ? "  —  " : String(r.base + "px").padStart(6)) + "  " +
    (r.delta === null ? " — " : ((r.delta > 0 ? "+" : "") + r.delta + "px").padStart(5)) + "  " +
    r.need + "/" + r.capacity + (r.over ? "   ← 基线里没有（新增）" : ""));
}

// ---------------------------------------------------------------------------
// 更新基线：必须先打印成本表，再落盘（成本是要被人看见的）
// ---------------------------------------------------------------------------

if (UPDATE) {
  const keys = {};
  for (const [key, m] of Object.entries(measured)) {
    keys[key] = { need: m.need, capacity: m.capacity, reserve: m.reserve, blocks: m.blocks };
  }
  console.log("--- 更新基线：" + path.relative(ROOT, BUDGET_FILE) + "（" + Object.keys(keys).length + " 个 key）");
  for (const r of rows) {
    if (r.delta !== null && r.delta !== 0) {
      const m = measured[r.key];
      console.log("    " + (r.delta > 0 ? "＋" : "－") + " " + r.key + "：" + r.base + " → " + r.reserve + "px" +
        "   内容 " + baseKeys[r.key].need + " → " + m.need + "px" +
        (baseKeys[r.key].need !== m.need ? "" : "（高度没变，变的是布局能给的高）"));
    }
  }
  writeBudget(keys, TOL);
  console.log("    已写入。这个文件要跟着代码一起提交 —— 它就是「预算」的凭据。");
}

console.log("===== " + (failures.length ? "FAIL" : "OK") + " =====");
console.log(pass + " 通过, " + failures.length + " 失败");
if (failures.length) {
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
process.exit(0);

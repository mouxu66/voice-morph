#!/usr/bin/env node
/**
 * test-sfx-window-renderer.cjs —— 悬浮特效声板窗渲染层（`pet/sfx-window.html`）的守卫。
 *
 * 运行：node tools/test-sfx-window-renderer.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 起不了真 Electron GUI（沙箱里 Chromium GPU 进程必崩），所以喂一套最小 DOM 桩
 * 跑**真实内联脚本**，再驱动目录/点击/失败做行为断言。
 *
 * 覆盖的回归点（都对应设计稿里写明的那几条）：
 *   1. 内联脚本必须能从头执行到尾（顶层抛异常 = 声板窗永远空白）
 *   2. 脚本引用的每个 id 都必须真在 HTML 里存在（漏一个就是静默失效）
 *   3. 格子图标必须来自 catalog 的 `icon`，缺了退到通用图标
 *      —— 绝不在渲染层再维护一份「id → emoji」映射（那正是这次要消灭的第二份规则）
 *   4. 点击 → 只把 **id** 交给主进程（渲染层不许出现任何文件路径概念）
 *   5. 播放失败必须**看得见**：格子留红边 + 状态行写出原因（静默失败最不可接受）
 *   6. 从录音引导推来空目录/失败时，状态行要说话而不是画一片空白
 *   7. 不许用 backdrop-filter（alt-hint 实测：Windows 透明窗拿不到真模糊）
 *   8. color-scheme 只能挂 #card，不能挂 :root（挂了透明窗会变成一块实心矩形）
 *   9. 窗口是 hide/show 复用而非重载页面 → visibilitychange 必须重播入场动画并重拉目录
 */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const realSetTimeout = setTimeout;

function stripJsComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");
}
function stripCssComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "");
}

const HTML_PATH = path.join(__dirname, "..", "web", "electron", "pet", "sfx-window.html");
const html = fs.readFileSync(HTML_PATH, "utf-8");
const scriptM = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptM, "sfx-window.html 里没找到内联 <script>");
const code = scriptM[1];
const codeStripped = stripJsComments(code);
const styleM = html.match(/<style>([\s\S]*?)<\/style>/);
assert.ok(styleM, "sfx-window.html 里没找到内联 <style>");
const css = stripCssComments(styleM[1]);

// ---------------- 最小 DOM 桩 ----------------
function mkEl(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    id: "",
    title: "",
    type: "",
    dataset: {},
    children: [],
    _cls: new Set(),
    _listeners: {},
    style: {},
    offsetWidth: 0,
    textContent: "",
    get className() { return [...el._cls].join(" "); },
    set className(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    classList: {
      add: (...c) => c.forEach((x) => el._cls.add(x)),
      remove: (...c) => c.forEach((x) => el._cls.delete(x)),
      contains: (c) => el._cls.has(c),
      toggle: (c, on) => { if (on === undefined ? !el._cls.has(c) : on) el._cls.add(c); else el._cls.delete(c); },
    },
    addEventListener(type, fn) { (el._listeners[type] = el._listeners[type] || []).push(fn); },
    removeEventListener() {},
    appendChild(child) { el.children.push(child); child.parent = el; return child; },
    fire(type, ev) { for (const fn of el._listeners[type] || []) fn(ev || { target: el, preventDefault() {}, stopPropagation() {} }); },
    all() {
      const out = [];
      for (const c of el.children) { out.push(c); out.push(...c.all()); }
      return out;
    },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return el.all().filter((e) => e._cls.has(cls));
    },
    querySelector(sel) {
      const m = sel.match(/^\.([\w-]+)\[data-id="(.*)"\]$/);
      if (m) return el.all().find((e) => e._cls.has(m[1]) && e.dataset.id === m[2]) || null;
      return el.querySelectorAll(sel)[0] || null;
    },
  };
  // grid.textContent = "" 的语义是**清空子节点**（脚本用它重画格子）
  Object.defineProperty(el, "textContent", {
    get() { return el.children.length ? "" : el._text || ""; },
    set(v) { el._text = v; el.children.length = 0; },
  });
  return el;
}

const byId = {};
const docListeners = {};
const doc = {
  hidden: false,
  getElementById(id) {
    if (!byId[id]) { byId[id] = mkEl("div"); byId[id].id = id; }
    return byId[id];
  },
  createElement: (tag) => mkEl(tag),
  addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  fire(type) { for (const fn of docListeners[type] || []) fn({}); },
};
const timers = [];
let refreshCb = null;
const sfxCalls = [];
let listPlan = { ok: true, items: [] };
let playPlan = { ok: true, duration_s: 1.4 };

const sandbox = {
  document: doc,
  console,
  CSS: { escape: (s) => String(s) },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: () => {},
};
const winListeners = {};
sandbox.window = {
  addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
  removeEventListener() {},
  fire(type, ev) { for (const fn of winListeners[type] || []) fn(ev || {}); },
  sfx: {
    list: () => { sfxCalls.push(["list"]); return Promise.resolve(listPlan); },
    play: (id) => { sfxCalls.push(["play", id]); return Promise.resolve(playPlan); },
    hide: () => { sfxCalls.push(["hide"]); },
    toggle: () => { sfxCalls.push(["toggle"]); },
    info: () => { sfxCalls.push(["info"]); return Promise.resolve({ hotkey: "Control+Alt+S", visible: true, focused: false }); },
    dragStart: () => { sfxCalls.push(["dragStart"]); },
    dragMove: () => { sfxCalls.push(["dragMove"]); },
    dragEnd: () => { sfxCalls.push(["dragEnd"]); },
    onRefresh: (cb) => { refreshCb = cb; },
  },
};

// ---------------- 跑真实内联脚本 ----------------
let bootError = null;
try {
  vm.runInNewContext(code, vm.createContext(sandbox), { filename: "sfx-window.html<script>" });
} catch (e) {
  bootError = e;
}
const el = (id) => doc.getElementById(id);
const cells = () => el("grid").querySelectorAll(".cell");
const flush = () => new Promise((r) => realSetTimeout(r, 0));

let pass = 0;
const failures = [];
async function check(name, fn) {
  try {
    await fn();
    pass += 1;
    console.log("  ok   " + name);
  } catch (e) {
    failures.push(name + " —— " + e.message);
    console.log("  FAIL " + name + "\n       " + e.message);
  }
}

(async () => {
  await check("内联脚本能从头执行到尾", () => {
    assert.strictEqual(bootError, null, "顶层抛异常：" + (bootError && bootError.message));
  });

  await check("脚本引用的每个 id 都在 HTML 里存在", () => {
    const refs = [...codeStripped.matchAll(/getElementById\(\s*"([^"]+)"\s*\)/g)].map((m) => m[1]);
    assert.ok(refs.length >= 5, "getElementById 引用太少，脚本可能没跑起来");
    const missing = refs.filter((id) => !new RegExp('id="' + id + '"').test(html));
    assert.deepStrictEqual(missing, [], "HTML 里缺这些 id：" + missing.join(", "));
  });

  await check("首帧就自取一次目录（主进程的推送可能早于脚本）", () => {
    assert.ok(sfxCalls.some((c) => c[0] === "list"), "应调 window.sfx.list()");
  });

  await check("格子按 catalog 渲染：图标取 item.icon，名字与 data-id 用 id", () => {
    refreshCb({
      ok: true,
      items: [
        { id: "boom", name: "爆炸", icon: "💥", duration_s: 1.4, count: 3 },
        { id: "weird", name: "鬼畜", icon: "👻", duration_s: 1.0, count: 0 },
      ],
    });
    const cs = cells();
    assert.strictEqual(cs.length, 2);
    assert.strictEqual(cs[0].dataset.id, "boom");
    assert.strictEqual(cs[0].children[0].textContent, "💥");
    assert.strictEqual(cs[0].children[1].textContent, "爆炸");
  });

  await check("catalog 没给 icon 时退到通用图标（而不是空白格）", () => {
    refreshCb({ ok: true, items: [{ id: "mine", name: "我导入的", duration_s: 0.8, count: 0 }] });
    const cs = cells();
    assert.strictEqual(cs.length, 1);
    assert.strictEqual(cs[0].children[0].textContent, "🎧");
  });

  await check("点击格子：只把 id 交给主进程（渲染层不碰路径），并乐观高亮", async () => {
    sfxCalls.length = 0;
    playPlan = { ok: true, duration_s: 1.4 };
    refreshCb({ ok: true, items: [{ id: "boom", name: "爆炸", icon: "💥", duration_s: 1.4, count: 1 }] });
    const c = cells()[0];
    c.fire("click");
    assert.ok(c.classList.contains("on"), "点下去应立刻高亮（one-shot 语义：点击即响）");
    await flush();
    assert.deepStrictEqual(sfxCalls.filter((x) => x[0] === "play"), [["play", "boom"]]);
    assert.strictEqual(c.classList.contains("err"), false);
    // 高亮保留 duration 那么久（不早于素材时长）
    const t = timers.filter((x) => x.ms >= 300).pop();
    assert.ok(t, "应排一个清除高亮的定时器");
  });

  await check("播放失败：格子留红边 + 状态行写出后端原话（静默失败最不可接受）", async () => {
    playPlan = { ok: false, error: "CABLE 设备找不到" };
    const c = cells()[0];
    c.fire("click");
    await flush();
    assert.ok(c.classList.contains("err"), "失败应留红边");
    assert.strictEqual(c.classList.contains("on"), false, "失败不该还在高亮");
    assert.ok(el("status").classList.contains("show"), "状态行应显示");
    assert.match(el("status").textContent, /CABLE/, "要把原因写出来，不能只说「失败」");
  });

  await check("下一次成功播放会清掉上一次的红边与状态行", async () => {
    playPlan = { ok: true, duration_s: 0.4 };
    cells()[0].fire("click");
    await flush();
    assert.strictEqual(cells()[0].classList.contains("err"), false);
    assert.strictEqual(el("status").classList.contains("show"), false);
  });

  await check("目录取不到（后端没起）时状态行要说话，且不画空白", () => {
    refreshCb({ ok: false, items: [], error: "后端没响应（应用可能在启动中）" });
    assert.ok(el("status").classList.contains("show"));
    assert.match(el("status").textContent, /后端没响应/);
  });

  await check("空目录给一句可读的说明（而不是一片空白让人以为坏了）", () => {
    refreshCb({ ok: true, items: [] });
    assert.strictEqual(cells().length, 0);
    const empty = el("grid").children[0];
    assert.ok(empty, "空目录也得有一个说明节点");
    assert.strictEqual(empty.id, "empty");
    assert.match(empty.textContent, /还没有音效素材/);
  });

  await check("收起按钮走 sfx.hide（hide 而非销毁，下次唤出是热的）", () => {
    sfxCalls.length = 0;
    el("close").fire("click");
    assert.deepStrictEqual(sfxCalls.filter((c) => c[0] === "hide").length, 1);
  });

  await check("标题栏拖动：按下 → 移动 → 松开三段都要报给主进程", () => {
    sfxCalls.length = 0;
    el("bar").fire("mousedown", { button: 0, target: el("bar"), preventDefault() {} });
    assert.strictEqual(sfxCalls.filter((c) => c[0] === "dragStart").length, 1, "按下应报 dragStart");
    sandbox.window.fire("mousemove");
    assert.strictEqual(sfxCalls.filter((c) => c[0] === "dragMove").length, 1, "移动应报 dragMove");
    sandbox.window.fire("mouseup");
    assert.strictEqual(sfxCalls.filter((c) => c[0] === "dragEnd").length, 1, "松开应报 dragEnd");
    // 松手后再移动不该继续拖（否则窗口会跟着光标跑）
    sandbox.window.fire("mousemove");
    assert.strictEqual(sfxCalls.filter((c) => c[0] === "dragMove").length, 1);
  });

  await check("热键提示来自主进程（不在渲染层硬编码一份可能过期的快捷键）", async () => {
    await flush();
    assert.strictEqual(el("hk").textContent, "Control+Alt+S");
  });

  await check("窗口重新露面：重播入场动画并重拉目录（hide/show 复用，不重载页面）", () => {
    el("card").classList.remove("enter");
    sfxCalls.length = 0;
    doc.fire("visibilitychange");
    assert.ok(el("card").classList.contains("enter"), "应重播入场动画");
    assert.ok(sfxCalls.some((c) => c[0] === "list"), "应重拉一次目录（计数会变）");
  });

  await check("样式：不用 backdrop-filter（Windows 透明窗拿不到真模糊）", () => {
    assert.ok(!/backdrop-filter/.test(css), "出现了 backdrop-filter");
  });

  await check("样式：color-scheme 只挂 #card，绝不挂 :root（挂 :root 会画出实心底色）", () => {
    assert.ok(/#card\s*\{[^}]*color-scheme/.test(css), "#card 上应有 color-scheme");
    assert.ok(!/:root\s*\{[^}]*color-scheme/.test(css), ":root 上不该有 color-scheme");
  });

  await check("视觉令牌与应用同源（暗色台面 + 亮色独立成块）", () => {
    assert.match(css, /prefers-color-scheme:\s*light/, "缺亮色主题块");
    assert.match(css, /--c-surface:\s*22 27 38/, "暗色台面应与 pet.html 同值");
  });

  console.log(`\n${pass} 通过 / ${failures.length} 失败`);
  if (failures.length) {
    for (const f of failures) console.log("  - " + f);
    process.exit(1);
  }
})();

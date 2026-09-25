#!/usr/bin/env node
/**
 * test-sfx-window.cjs —— 悬浮特效声板窗（`web/electron/sfx-window.cjs`）的守卫。
 *
 * 运行：node tools/test-sfx-window.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 为什么这些断言值得单独写：
 *   这个窗口的**全部价值**是一条看不见的性质 —— 点它不抢前台窗口，按住 Alt 的
 *   微信录音才不会被打断。而这条性质只由三行建窗参数支撑（`focusable:false` +
 *   `show:false` + `showInactive()`）。谁哪天把它们「统一成主窗那种写法」，
 *   功能看着还在、录音却开始断 —— 那是最难查的一类回归，所以这里逐条钉死。
 *
 * 沙箱里起不了真 Electron GUI（Chromium GPU 进程必崩），所以用 electron 桩 +
 * 一个**记录调用的假 BrowserWindow**：能断言「建窗时给了什么参数、之后调了哪些方法」。
 * 真实鼠标点下去会不会抢焦点**测不到**（CDP 的可信事件也不走 OS 激活路径），
 * 那一条只能真机验收（docs/真机验收清单-2026-09-21.md §K A1）。
 */
"use strict";
const assert = require("node:assert");
const { installElectronStub } = require("./electron-stub.cjs");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-sfxw-userdata-"));
const WORK_AREA = { x: 0, y: 0, width: 1920, height: 1040 };

// ---------- 假 BrowserWindow：记录构造参数与每一次方法调用 ----------
const windows = [];
function FakeBrowserWindow(opts) {
  const rec = { opts, calls: [], visible: false, destroyed: false, focused: false, pos: [opts.x, opts.y] };
  const self = {
    webContents: {
      once: () => {},
      on: () => {},
      send: (...a) => rec.calls.push(["send", ...a]),
      on: () => {},
    },
    loadFile: (f) => rec.calls.push(["loadFile", f]),
    setAlwaysOnTop: (...a) => rec.calls.push(["setAlwaysOnTop", ...a]),
    // alt-hint 建窗时会调它（纯装饰窗才点击穿透）；声板窗**不会**调它 ——
    // 穿透就点不到了，那是这套设计里另一个关键不变量。
    setIgnoreMouseEvents: (v) => rec.calls.push(["setIgnoreMouseEvents", v]),
    on: (ev, fn) => rec.calls.push(["on", ev, typeof fn]),
    isDestroyed: () => rec.destroyed,
    isVisible: () => rec.visible,
    isFocused: () => rec.focused,
    show: () => { rec.visible = true; rec.focused = true; rec.calls.push(["show"]); },
    showInactive: () => { rec.visible = true; rec.focused = false; rec.calls.push(["showInactive"]); },
    hide: () => { rec.visible = false; rec.calls.push(["hide"]); },
    destroy: () => { rec.destroyed = true; rec.calls.push(["destroy"]); },
    getPosition: () => rec.pos,
    setPosition: (x, y) => { rec.pos = [x, y]; rec.calls.push(["setPosition", x, y]); },
    getBounds: () => ({ x: rec.pos[0], y: rec.pos[1], width: opts.width, height: opts.height }),
  };
  rec.win = self;
  windows.push(rec);
  return self;
}

const handlers = {};   // ipcMain.handle 登记的通道
const listeners = {};  // ipcMain.on 登记的通道
const hotkeys = [];
const electronEntry = installElectronStub({
  app: {
    isPackaged: false,
    getPath: () => userDataDir,
  },
  ipcMain: {
    handle: (name, fn) => { handlers[name] = fn; },
    on: (name, fn) => { listeners[name] = fn; },
  },
  screen: {
    getPrimaryDisplay: () => ({ workArea: WORK_AREA }),
    getCursorScreenPoint: () => ({ x: 0, y: 0 }),
  },
  globalShortcut: {
    register: (acc) => { hotkeys.push(acc); return true; },
    unregister: () => {},
    unregisterAll: () => {},
  },
  BrowserWindow: FakeBrowserWindow,
});
assert.ok(electronEntry, "electron 桩应装上");

// ---------- 后端 HTTP 桩：在 require sfx-window 之前替换 httpJson ----------
const backendPath = require.resolve("../web/electron/backend.cjs");
const backend = require(backendPath);
const httpCalls = [];
let httpPlan = () => ({ code: 200, json: { ok: true, items: [] } });
backend.httpJson = (method, apiPath, body) => {
  httpCalls.push({ method, apiPath, body });
  return Promise.resolve(httpPlan(method, apiPath, body));
};

const sfx = require("../web/electron/sfx-window.cjs");
sfx.registerSfxIpc();

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
async function checkAsync(name, fn) {
  try {
    await fn();
    pass += 1;
    console.log("  ok   " + name);
  } catch (e) {
    failures.push(name + " —— " + e.message);
    console.log("  FAIL " + name + "\n       " + e.message);
  }
}

const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "electron", "sfx-window.cjs"), "utf-8");

// ---------------------------------------------------------------- 静态：建窗参数
check("建窗必须是 focusable:false（= WS_EX_NOACTIVATE，点它不改前台窗口）", () => {
  assert.match(SRC, /focusable:\s*false/, "sfx-window.cjs 里找不到 focusable:false");
});

check("源码里不许出现 win.show() 调用（show 会尝试激活 → 抢前台）", () => {
  // 只看**调用**形态：show:false 这个建窗选项是允许的，注释里提到也不算
  const bad = SRC.split("\n").filter((ln) => !ln.trim().startsWith("//") && /\.show\(\s*\)/.test(ln));
  assert.deepStrictEqual(bad, [], "出现了 .show() 调用：" + bad.join(" | "));
});

check("热键常量不含单 Alt（微信按住说话就是 Alt）", () => {
  assert.strictEqual(sfx.SFX_HOTKEY, "Control+Alt+S");
  assert.strictEqual(sfx.isSafeHotkey(sfx.SFX_HOTKEY), true);
  const registered = hotkeys.includes(sfx.SFX_HOTKEY);
  assert.strictEqual(registered, true, "registerSfxIpc() 应注册热键 " + sfx.SFX_HOTKEY);
});

// ---------------------------------------------------------------- 纯函数
check("isSafeHotkey：裸 Alt / Alt+X 不安全，带别的修饰键才安全", () => {
  assert.strictEqual(sfx.isSafeHotkey("Alt"), false);
  assert.strictEqual(sfx.isSafeHotkey("Alt+S"), false);
  assert.strictEqual(sfx.isSafeHotkey("Control+Alt+S"), true);
  assert.strictEqual(sfx.isSafeHotkey("Shift+Alt+S"), true);
  assert.strictEqual(sfx.isSafeHotkey("F1"), true);
  assert.strictEqual(sfx.isSafeHotkey(""), true);
});

check("clampToWorkArea：没记忆时落在右下角、桌宠正上方", () => {
  const p = sfx.clampToWorkArea({ x: null, y: null }, sfx.WINDOW_SIZE, WORK_AREA);
  assert.strictEqual(p.x, WORK_AREA.width - sfx.WINDOW_SIZE.width - 24);
  // 桌宠高 480 贴底 0（本夹具 workArea 高 1040）→ 声板窗在它上方再留 10px
  assert.strictEqual(p.y, WORK_AREA.height - 480 - 10 - sfx.WINDOW_SIZE.height);
  assert.ok(p.x > 0 && p.y > 0);
});

check("clampToWorkArea：换了显示器后旧坐标会被收回屏内（不许丢到看不见的地方）", () => {
  const p = sfx.clampToWorkArea({ x: 9999, y: 9999 }, sfx.WINDOW_SIZE, WORK_AREA);
  assert.strictEqual(p.x, WORK_AREA.width - sfx.WINDOW_SIZE.width);
  assert.strictEqual(p.y, WORK_AREA.height - sfx.WINDOW_SIZE.height);
  const q = sfx.clampToWorkArea({ x: -500, y: -900 }, sfx.WINDOW_SIZE, WORK_AREA);
  assert.strictEqual(q.x, 0);
  assert.strictEqual(q.y, 0);
});

check("playPayload：只带 id，绝不把文件路径交给渲染层", () => {
  const p = sfx.playPayload("boom");
  assert.deepStrictEqual(Object.keys(p), ["id"]);
  assert.strictEqual(p.id, "boom");
  assert.strictEqual(sfx.playPayload(undefined).id, "");
});

// ---------------------------------------------------------------- 显隐不变量
check("showSfxWindow 用 showInactive 而非 show（且只建一个窗口）", () => {
  windows.length = 0;
  sfx.showSfxWindow();
  assert.strictEqual(windows.length, 1);
  const w = windows[0];
  assert.strictEqual(w.opts.focusable, false);
  assert.strictEqual(w.opts.show, false);
  assert.strictEqual(w.opts.alwaysOnTop, true);
  assert.strictEqual(w.opts.skipTaskbar, true);
  const names = w.calls.map((c) => c[0]);
  assert.ok(names.includes("showInactive"), "应调 showInactive");
  assert.ok(!names.includes("show"), "不许调 show（会抢前台）");
  assert.strictEqual(w.win.isFocused(), false);

  sfx.showSfxWindow();   // 再次唤起：复用同一个窗口，不重建
  assert.strictEqual(windows.length, 1);
});

check("拖动结束会落盘位置（userData/sfx-window.json）", () => {
  const w = windows[0];
  listeners["sfx:drag-start"]();
  w.pos = [321, 654];          // 假装主进程已经把它挪到这儿
  listeners["sfx:drag-end"]();
  const saved = JSON.parse(fs.readFileSync(path.join(userDataDir, "sfx-window.json"), "utf-8"));
  assert.deepStrictEqual([saved.x, saved.y], [321, 654]);
});

check("录音引导：press 浮现、prep 不浮现（prep 是全自动发送，界面说别动键鼠）", () => {
  sfx.destroySfxWindow();
  windows.length = 0;
  sfx.onRecordingStage("prep");
  assert.strictEqual(windows.length, 0, "prep 阶段不该弹出声板窗");
  sfx.onRecordingStage("press");
  assert.strictEqual(windows.length, 1, "press 阶段应浮现");
});

check("录音引导 done 后延迟收起（延时到点才 hide）", () => {
  const timers = [];
  const realSetTimeout = global.setTimeout;
  const realClearTimeout = global.clearTimeout;
  global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  global.clearTimeout = () => {};
  try {
    sfx.onRecordingStage("done");
    assert.strictEqual(timers.length, 1, "应排一个自动收起定时器");
    assert.strictEqual(timers[0].ms, sfx.AUTO_HIDE_MS);
    const w = windows[0];
    assert.ok(!w.calls.some((c) => c[0] === "hide"), "到点前不该收起");
    timers[0].fn();
    assert.ok(w.calls.some((c) => c[0] === "hide"), "到点应收起");
  } finally {
    global.setTimeout = realSetTimeout;
    global.clearTimeout = realClearTimeout;
  }
});

check("destroySfxWindow 真的销毁（隐藏窗会钉住 window-all-closed，应用退不掉）", () => {
  const w = windows[0];
  sfx.destroySfxWindow();
  assert.ok(w.calls.some((c) => c[0] === "destroy"));
  assert.strictEqual(w.win.isDestroyed(), true);
  assert.strictEqual(w.win.isVisible(), false);
});

// ---------------------------------------------------------------- IPC 语义
check("登记了全部 IPC 通道", () => {
  for (const ch of ["sfx:list", "sfx:play", "sfx:stop", "sfx:info"]) {
    assert.strictEqual(typeof handlers[ch], "function", "缺 handler：" + ch);
  }
  for (const ch of ["sfx:hide", "sfx:toggle", "sfx:drag-start", "sfx:drag-move", "sfx:drag-end"]) {
    assert.strictEqual(typeof listeners[ch], "function", "缺 listener：" + ch);
  }
});

(async () => {
await checkAsync("sfx:list 把 catalog 原样交出去；后端 503 时**如实回报**而不是空列表", async () => {
  httpPlan = () => ({ code: 200, json: { ok: true, items: [{ id: "boom", name: "爆炸", icon: "💥" }] } });
  const r = await handlers["sfx:list"]();
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.items.length, 1);
  httpCalls.length = 0;
  httpPlan = () => ({ code: 503, json: { detail: "CABLE 设备找不到" } });
  const bad = await handlers["sfx:list"]();
  assert.strictEqual(bad.ok, false);
  assert.match(bad.error, /CABLE/, "失败原因必须原样带出来（静默失败是最不能接受的）");
  assert.deepStrictEqual(bad.items, []);
  httpPlan = () => null;   // 后端根本没起
  const dead = await handlers["sfx:list"]();
  assert.strictEqual(dead.ok, false);
  assert.ok(dead.error, "后端没响应时也要有一句可读的话");
});

await checkAsync("sfx:play 请求体只有 id（不带路径），失败时带出 detail", async () => {
  httpCalls.length = 0;
  httpPlan = () => ({ code: 200, json: { ok: true, playing: true, duration_s: 1.4 } });
  const ok = await handlers["sfx:play"](null, "boom");
  assert.deepStrictEqual(ok, { ok: true, duration_s: 1.4 });
  assert.strictEqual(httpCalls.length, 1);
  assert.strictEqual(httpCalls[0].apiPath, "/api/soundboard/play");
  assert.deepStrictEqual(Object.keys(httpCalls[0].body), ["id"]);
  assert.strictEqual(httpCalls[0].body.id, "boom");

  httpCalls.length = 0;
  httpPlan = () => ({ code: 404, json: { detail: "没有这个音效：nope" } });
  const bad = await handlers["sfx:play"](null, "nope");
  assert.strictEqual(bad.ok, false);
  assert.match(bad.error, /没有这个音效/);
});

await checkAsync("集成：alt-hint 到了 press 阶段要真把声板窗唤起来", async () => {
  // 这条必须测：alt-hint → sfx-window 是**惰性 require**，外面裹了 try/catch，
  // 写错路径会被静静吞掉 —— 症状是「录音引导时声板窗不出现」，而单元用例全绿。
  const altHint = require("../web/electron/alt-hint.cjs");
  sfx.destroySfxWindow();
  windows.length = 0;
  altHint.showAltHint({ stage: "press", sub: "按住 Alt 说话", remainS: 10, progress: 0 });
  const loaded = windows.map((w) => (w.calls.find((c) => c[0] === "loadFile") || [])[1] || "");
  assert.ok(loaded.some((f) => f.includes("sfx-window.html")), "press 阶段未加载声板窗，实际加载了：" + JSON.stringify(loaded));
  assert.ok(loaded.some((f) => f.includes("alt-hint.html")), "引导横幅本身也应出现");
  const sfxRec = windows.find((w) => ((w.calls.find((c) => c[0] === "loadFile") || [])[1] || "").includes("sfx-window.html"));
  assert.ok(sfxRec.calls.some((c) => c[0] === "showInactive"), "声板窗应以 showInactive 现身");
  assert.ok(!sfxRec.calls.some((c) => c[0] === "show"), "声板窗绝不调 show");
  assert.ok(!sfxRec.calls.some((c) => c[0] === "setIgnoreMouseEvents"),
    "声板窗不许开鼠标穿透（穿透就点不到了；这是它与 alt-hint 横幅的关键区别）");
  sfx.destroySfxWindow();
});

await checkAsync("sfx:info 报热键与焦点状态（真机验收靠它读「点完没抢焦点」）", async () => {
  const i = await handlers["sfx:info"]();
  assert.strictEqual(i.hotkey, sfx.SFX_HOTKEY);
  assert.strictEqual(typeof i.visible, "boolean");
  assert.strictEqual(typeof i.focused, "boolean");
});

// ---------------------------------------------------------------- 汇总
// 汇总必须在 await 之后：异步用例漏 await 的话，脚本会带着「全绿」的假象提前退出。
console.log(`\n${pass} 通过 / ${failures.length} 失败`);
if (failures.length) {
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
})();

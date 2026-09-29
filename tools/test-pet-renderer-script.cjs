#!/usr/bin/env node
/**
 * test-pet-renderer-script.cjs —— 桌宠渲染器 pet.html 内联脚本「能跑完整」的守卫。
 *
 * 运行：node tools/test-pet-renderer-script.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 为什么需要它（2026-09-17 用户报障）：
 *   桌面人偶变成一个「透明玻璃球」——只有一圈极淡的圆环和一点蓝灰渐变，
 *   鼠标移上去还是个抓手，点它还挡住后面的窗口。
 *
 *   根因：2026-09-10 b91707b「渲染器动态皮肤」把 `applySkin();` 的调用插到了
 *   `const sprite = document.getElementById("sprite")` **之前**，而 applySkin()
 *   内部要写 sprite.style。顶层 `const` 有暂时性死区（TDZ），于是初始化第一句就抛
 *   `ReferenceError: Cannot access 'sprite' before initialization`，**整个内联脚本中断**。
 *
 *   脚本一断，后面所有事都没发生：
 *     - setSprite() 从未执行 → 精灵图没有 background-image → 只剩 CSS #pet::before
 *       那圈 radial-gradient 光晕 + inset 边框（= 用户看到的「透明玻璃球」）
 *     - 末尾的 `window.pet.setIgnoreMouse(true)`（初始全窗穿透）从未执行
 *       → 整个 220×480 透明窗一直吃鼠标，挡住底下窗口，光标停在 #pet 的
 *         `cursor: grab` 上 → 用户看到的「像手掌，能抓取」
 *     - 状态轮询 / 气泡 / 悬停快捷面板 / 拖拽 全部失效
 *
 * 这个测试在**无 GUI 环境**下跑真实脚本：喂一套最小 DOM 桩，捕获任何顶层异常，
 * 并断言初始化后精灵图确实拿到了图片、外框尺寸变量确实写对了。
 * 起不了真 Electron GUI（沙箱里 Chromium GPU 进程必崩），所以只能这样验。
 *
 * 覆盖的回归点：
 *   1. 内联脚本必须能从头执行到尾（b91707b 的 TDZ 崩溃）
 *   2. applySkin() 的调用点必须晚于 petBox / sprite 的声明
 *   3. 初始化后 sprite 拿到 --fw/--fh，且 background-image 指向本地 svg/*.webp
 *   4. --dw/--dh 必须写在 #pet（消费方）上，否则换非 150 帧尺寸皮肤时光晕与角色错位
 *   5. 初始必须下发「全窗鼠标穿透」，否则透明窗会挡住底下的窗口
 *   6. 【2026-09-17 面板改版新增】脚本引用的每个 id 都必须在 HTML 里存在
 *      —— 面板改成「图标 + 文案 + 状态药丸」后元素变多，漏一个就是静默失效
 *   7. 【同上】绝不能对含图标子节点的元素直写 textContent
 *      —— 按钮里现在有 <svg> 和 <span class="spin">，`btn.textContent = x` 会把它们整个抹掉，
 *         按钮只剩一行文字、图标消失。旧版按钮是纯文本，这个坑才没暴露。
 *   8. 【同上】color-scheme 只能声明在 #panel 上，绝不能落在 :root / html
 *      —— 根元素设 color-scheme 会让 Chromium 去画「默认画布底色」，
 *         而这是 transparent 窗，画布必须保持全透明（否则整窗出现一块实心矩形）。
 *   9. 【同上】面板/气泡必须 box-sizing: border-box
 *      —— 窗口 220px 减 #root 左右各 8px = 204px，正是面板宽度；
 *         用默认 content-box 再加上 padding 与边框就横向溢出、被窗口裁掉。
 *  10. 【同上】状态行不带色调参数时也要显示
 *      —— 原 `className = cls ? "show " + cls : ""` 让 `setStatus("合成中…")` 被 display:none 吞掉。
 *  11. 【同上】后端离线提示必须常驻
 *      —— 原 `if (backendOffline && !wasOffline) { … return }` 只拦住「刚离线」那一帧，
 *         1s 后就被状态机的 setState("idle") 覆盖，提示一闪而过。
 *         本条用 VM_PET_OFFLINE=1 子进程复跑本文件来验（见文件末尾）。
 *  12. 【2026-09-17 面板让位新增】气泡显隐只能有一个写入口（setBubbleVisible）
 *      —— 气泡与面板共用同一个 480px 窗口，气泡一起来面板就被压扁、底部「最近发送」
 *         被切掉（实测 guide 切 6px / think 16px / offline 34px）。让位规则靠
 *         #root.compact 生效，而它必须与气泡显隐严格同步；散着写 bubble.style.display
 *         漏掉一处，那一处就会带着多余的 #recent 去抢高度。所以：写入口唯一 + 运行期验证。
 *  13. 【2026-09-23 长文分段发送】结果播报必须按「批次」口径，看门狗要「凑齐才收尾」
 *      —— 单条的 warning / duration_s 不能说成整批结论（第 1 条静音、后两条正常时，
 *         旧写法会说成「已发出（3 条）但可能没声音」）；中途落败要说清已发出几条、
 *         第几条挂的；「出现新记录」≠「整批发完」—— 早收尾会在第 1 条落地时就打绿勾，
 *         而绿勾又会让用户去动键鼠，正好打断还在进行的录制。
 */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

// 离线模式：把两个状态接口打成失败，用来验证「后端离线」提示是常驻的（回归点 11）
const OFFLINE = process.env.VM_PET_OFFLINE === "1";

/**
 * 剥掉 JS 注释。静态检查必须先做这一步：
 * 「禁止 liveBtn.textContent」这条断言，最先被命中的其实是**解释为什么禁止的注释本身**。
 * 只认行首/空白后的 `//`，避免把 `"http://127.0.0.1:8000"` 里的 `//` 当成注释。
 */
function stripJsComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/[^\n]*/g, "$1");
}
/** 剥掉 CSS 块注释（CSS 没有行注释，`/* ... *\/` 是唯一形式）。 */
function stripCssComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "");
}
/**
 * 从 src 里 braceIdx 处的 `{` 起，按大括号配对取出规则体。
 * 必须做配对而不是「截到文件尾」：令牌名在后续规则里会以 `var(--c-surface)` 的形式反复出现，
 * 用 includes 检查「截尾字符串」的话，即使把亮色令牌块整个删掉也照样能匹配上（假通过）。
 */
function cssBlockBody(src, braceIdx) {
  let depth = 0;
  for (let i = braceIdx; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(braceIdx + 1, i);
    }
  }
  throw new Error("CSS 大括号没有配对，从偏移 " + braceIdx + " 开始");
}

// 默认测仓库里的 pet.html；可选第一个参数指定别的文件（做「反向对照」用：
// 传 b91707b 的旧版本应当 FAIL，证明这个测试真的能拦住该回归）
const PET_HTML = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "..", "web", "electron", "pet", "pet.html");

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

// ---------------- 载入并抽取内联脚本 ----------------
const html = fs.readFileSync(PET_HTML, "utf-8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(m, "pet.html 里没找到内联 <script>");
const code = m[1];
const codeStripped = stripJsComments(code);      // 静态断言一律用剥注释后的版本
const styleM = html.match(/<style>([\s\S]*?)<\/style>/);
assert.ok(styleM, "pet.html 里没找到内联 <style>");
const cssStripped = stripCssComments(styleM[1]);
const htmlLines = html.split("\n");

// ---------------- 最小 DOM 桩（记录所有写入，供断言） ----------------
/**
 * classList 必须是**真**实现，不能是 no-op 桩。
 *
 * 原来的桩 `classList: { add(){}, remove(){}, toggle(){}, contains(){ return false } }`
 * 会让「#root 有没有挂 .compact」这类断言永远拿到 false —— 也就是说
 * 面板让位规则（回归点 12）根本没法在无 GUI 环境下验证。className 与 classList
 * 共享同一个 Set，避免两套状态各说各话。
 */
function mkEl(id) {
  const el = {
    id,
    _cls: new Set(),
    style: {
      _props: {},
      setProperty(k, v) { this._props[k] = String(v); },
      removeProperty(k) { delete this._props[k]; },
    },
    // 记录监听器：这样才能在脚本跑完后「点一下按钮」，对事件驱动的行为做真实断言
    _on: {},
    addEventListener(type, fn) { (this._on[type] = this._on[type] || []).push(fn); },
    removeEventListener() {},
    textContent: "",
    innerHTML: "",
    value: "",
    offsetWidth: 0,
    children: [],
    // 引擎分段的按钮靠 data-eng 认自己是哪一个（b.dataset.eng）
    dataset: {},
    appendChild() {},
    add() {},
    focus() {},
    closest() { return null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, "className", {
    get() { return [...el._cls].join(" "); },
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  el.classList = {
    add(...c) { c.forEach((x) => el._cls.add(x)); },
    remove(...c) { c.forEach((x) => el._cls.delete(x)); },
    toggle(c, on) {
      const want = on === undefined ? !el._cls.has(c) : !!on;
      if (want) el._cls.add(c); else el._cls.delete(c);
      return want;
    },
    contains: (c) => el._cls.has(c),
  };
  return el;
}
const els = {};
const petIpc = [];          // 渲染器下发给主进程的 IPC 调用记录
const petCbs = {};          // 主进程回传的订阅回调（onPreviewResult / onSendResult）
const loadedImages = [];    // new Image().src = ... 记录
const intervals = [];       // setInterval 回调（桩掉定时器，但要能手动再驱动几轮）
/**
 * 取状态轮询定时器（tick）。
 *
 * ⚠️ 不能图省事写 intervals[0]：脚本里待机小剧场的 setInterval(…, 5000) 在
 * setInterval(tick, 1000) **之前**（pet.html 里 1272 行 vs 1283 行），
 * intervals[0] 是小剧场回调，驱动它等于什么也没驱动 —— 第一版这条断言就是这么空转过去的
 * （变异测试里旧缺陷没被拦住才暴露出来）。tick 的唯一标识是周期 1000ms。
 */
function tickInterval() {
  const hits = intervals.filter((it) => it.ms === 1000);
  assert.strictEqual(hits.length, 1,
    "预期恰好一个 1000ms 的轮询定时器（tick），实际 " + hits.length +
    " 个；全部周期：[" + intervals.map((it) => it.ms).join(", ") + "]");
  return hits[0].fn;
}

// 文档级监听器要**记下来**（原来是 no-op 桩）：按住说话有一条关键不变量挂在 document 上
// ——「按住期间不能把窗口设成鼠标穿透」，那是 document.mousemove 里的分支。
global.document = {
  _on: {},
  getElementById: (id) => (els[id] = els[id] || mkEl(id)),
  createElement: () => mkEl("tmp"),
  querySelectorAll: () => [],
  addEventListener(type, fn) { (this._on[type] = this._on[type] || []).push(fn); },
  elementFromPoint: () => null,
  documentElement: mkEl("html"),
  body: mkEl("body"),
  head: mkEl("head"),
};
global.window = {
  pet: {
    setIgnoreMouse: (v) => petIpc.push(["setIgnoreMouse", v]),
    dragStart: () => petIpc.push(["dragStart"]),
    dragMove: () => petIpc.push(["dragMove"]),
    dragEnd: () => petIpc.push(["dragEnd"]),
    previewText: (t, v) => petIpc.push(["previewText", t, v]),
    sendText: (t, v) => petIpc.push(["sendText", t, v]),
    sendWav: (w) => petIpc.push(["sendWav", w]),
    liveToggle: () => petIpc.push(["liveToggle"]),
    showBackendLog: () => petIpc.push(["showBackendLog"]),
    onPreviewResult: (cb) => { petCbs.preview = cb; },
    onSendResult: (cb) => { petCbs.send = cb; },
    // 按住说话（2026-09-29）：两条 IPC + 一条状态回传。
    // 回传单独存 petCbs.mic —— 测试靠它把主进程的真实状态推回渲染器
    // （按钮文案/颜色/秒数全部由那个状态驱动，不是渲染器自己编的）。
    micDown: (v) => petIpc.push(["micDown", v]),
    micUp: () => petIpc.push(["micUp"]),
    onMicState: (cb) => { petCbs.mic = cb; },
  },
  addEventListener() {},
};
global.location = { search: "" };
global.Image = class { set src(v) { loadedImages.push(v); } };
// localStorage 用真实现（Map 兜底）：引擎选择要持久化，桩成 no-op 就断言不了「有没有写进去」。
const localStorageStore = new Map();
global.localStorage = {
  getItem: (k) => (localStorageStore.has(k) ? localStorageStore.get(k) : null),
  setItem: (k, v) => localStorageStore.set(k, String(v)),
  removeItem: (k) => localStorageStore.delete(k),
};
global.Audio = class { pause() {} play() { return Promise.resolve(); } };
global.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return 0; };   // 桩掉轮询定时器，进程才能自然退出
global.self = global;

// 后端桩：状态接口返回「什么都没跑」，皮肤接口返回内置芙宁娜
const fetchCalls = [];        // { url, method } —— 引擎启停必须按「先停再开」的顺序断言
let cascadeRunning = false;   // 模拟「主界面把千问开起来了」
let liveRunning = false;      // 模拟「主界面把 RVC 开起来了」

// 发送前预检的桩（2026-09-29）：默认回「微信已打开、不会重启」这一常态；
// 其它形态由用例自己塞 precheckStub（注定失败 / 会重启 / 字段缺失 / 404 / 直接抛错）。
// 为什么得可控：面板上那条预检行的**全部价值**就在「不同结论要说不同的话」，
// 一律回 ok:true 的桩会把「微信没开」和「后端旧版」这两件事混成同一句话。
const PRECHECK_OK = {
  ok: true, restart_mode: "0", restart_needed: false,
  reason: "VM_WECHAT_RESTART=0 已关闭重启",
  wechat_running: true, block_reason: "",
  hint: "不会重启微信，直接切麦克风到 CABLE Output",
};
let precheckStub = null;
global.fetch = async (url, opts) => {
  const u = String(url);
  const method = String((opts && opts.method) || "GET").toUpperCase();
  fetchCalls.push({ url: u, method });
  // 离线模式：两个状态接口都失败 → 走 pet.html 自己的「后端离线」分支
  if (OFFLINE && (u.includes("/api/cascade/status") || u.includes("/api/rvc/live/status"))) {
    return { ok: false, status: 503, json: async () => ({}) };
  }
  // 启停请求（桌宠直连后端）：一律回 ok，状态由上面的开关决定
  if (method === "POST") return { ok: true, status: 200, json: async () => ({ ok: true }) };
  if (u.includes("/api/cascade/status")) {
    // 要带上 stage/last_text/queued_s：只回 {running:true} 会落进 render() 的 default
    // 分支（setState("build") → 气泡标题「准备中」），断言就测不到引擎名了。
    return { ok: true, status: 200, json: async () => (cascadeRunning
      ? { running: true, stage: "capturing", last_text: "今天天气不错", queued_s: 0, child_error: "" }
      : { running: false }) };
  }
  if (u.includes("/api/rvc/live/status")) {
    // infer_ms / block_ms 是 2026-09-29 后端新加的（tail realtime_gui.log 实测推理耗时）。
    // liveRunning 时才报数 —— 与后端一致（停变声后日志里还留着上次的数，但接口只在跑时报）。
    return { ok: true, status: 200, json: async () => (liveRunning
      ? { live_running: true, infer_ms: 82.4, infer_ms_p95: 130.2, infer_samples: 100, block_ms: 40 }
      : { live_running: false }) };
  }
  if (u.includes("/api/wechat/precheck")) {
    if (typeof precheckStub === "function") return precheckStub();
    if (precheckStub) return precheckStub;
    return { ok: true, status: 200, json: async () => ({ ...PRECHECK_OK }) };
  }
  let body = {};
  // 音色清单（2026-09-29 起多一个 selected：主界面当前选中的音色 id）。
  // 状态条上的「当前音色」就是靠它把「主界面选中」这个空 value 换成一个真名字的。
  if (u.includes("/api/voices")) {
    body = {
      selected: "kangaroo",
      voices: [
        { id: "furina", display_name: "芙宁娜", has_reference: true },
        { id: "kangaroo", display_name: "袋鼠骑士", has_reference: true },
        { id: "noref", display_name: "无参考(应被过滤)", has_reference: false },
      ],
    };
  }
  if (u.includes("/pet-market/applied")) {
    body = {
      id: "furina", frameW: 150, frameH: 150,
      states: {
        idle: { sheet: "idle.webp", frames: 37, dur: 3.0 },
        listen: { sheet: "listen.webp", frames: 37, dur: 3.0 },
        think: { sheet: "think.webp", frames: 37, dur: 3.0 },
        play: { sheet: "play.webp", frames: 22, dur: 1.8 },
        build: { sheet: "build.webp", frames: 16, dur: 1.4 },
        error: { sheet: "error.webp", frames: 37, dur: 3.0 },
      },
    };
  }
  return { ok: true, status: 200, json: async () => body };
};

// ---------------- 执行真实脚本 ----------------
// 末尾追加一行把内部函数导出来：间接 eval 的函数声明**不会**挂到 globalThis
// （实测 `globalThis.setState === undefined`，别指望直接拿），所以在同一个 eval 作用域里
// 显式挂一份。`engine` / `runningEngine` 是 let，只能用 getter 闭包读（对象字面量存不住活绑定）。
// 追加在末尾不影响上面按行号做的错误定位。
const EXPORT_HOOK = "\n;globalThis.__petFns = { setState, setEngine, playGuide, endGuide, setBubbleVisible, settleSendResult,"
  + " setTab, loadVoices,"
  + " getEngine: function () { return engine; },"
  + " getRunningEngine: function () { return runningEngine; },"
  // 按住说话（2026-09-29）：micPhase / activeTab / panelTimer 都是模块作用域的 let，
  // 只能用 getter 闭包读（对象字面量存不住活绑定）。panelTimer 是「按住时面板不许自己收」
  // 那条不变量的唯一观测点。
  + " getMicPhase: function () { return micPhase; },"
  // 发送前预检：形态全靠后端响应决定，只能从外部不同形态地喂（见下面的预检用例）
  + " refreshWechatPrecheck,"
  + " getActiveTab: function () { return activeTab; },"
  + " getPanelTimer: function () { return panelTimer; } };\n";
let thrown = null;
try {
  (0, eval)(code + EXPORT_HOOK);
} catch (e) {
  thrown = e;
}
const petFns = globalThis.__petFns || {};

// 引擎分段的按钮在真实页面里靠 HTML 的 data-eng 认自己（b.dataset.eng）。
// DOM 桩不是解析器，所以从 HTML 里把这对属性抠出来灌进桩 —— 比在测试里硬编码 "rvc"/"qwen"
// 更忠实：HTML 改了 data-eng，测试会跟着走，而不是继续用旧值假通过。
for (const mm of html.matchAll(/id="(eng[A-Za-z]+)"\s+data-eng="([a-z]+)"/g)) {
  els[mm[1]] = els[mm[1]] || mkEl(mm[1]);
  els[mm[1]].dataset = { eng: mm[2] };
}
// 页签同理（setTab 读 b.dataset.tab）。从 HTML 抠出来，改了页签 id/名字测试会跟着走。
for (const mm of html.matchAll(/id="(tab[A-Za-z]+)"\s+data-tab="([a-z]+)"/g)) {
  els[mm[1]] = els[mm[1]] || mkEl(mm[1]);
  els[mm[1]].dataset = { tab: mm[2] };
}
// 音色下拉：真实 <select> 永远有 options（HTMLOptionsCollection），而 DOM 桩不是解析器。
// 不补上，refreshRemoteVoice 的「反查名字」路径就永远跑不到，测试会假绿。
els.voiceSel = els.voiceSel || mkEl("voiceSel");
els.voiceSel.options = [];
els.voiceSel.add = (o) => { els.voiceSel.options.push(o); };

// tick()/loadSkin() 是 async，用真 setTimeout 让 await 链推进完
const flush = () => new Promise((r) => setTimeout(r, 20));

(async () => {
  console.log("===== test-pet-renderer-script =====");

  check("内联脚本能完整执行，不抛任何顶层异常（b91707b TDZ 回归）", () => {
    if (thrown) {
      const line = (thrown.stack || "").match(/<anonymous>:(\d+):(\d+)/);
      let where = "";
      if (line) {
        const htmlLine = html.slice(0, m.index).split("\n").length + Number(line[1]) - 1;
        where = " @ pet.html:" + htmlLine + " → " + (htmlLines[htmlLine - 1] || "").trim();
      }
      throw new Error(thrown.constructor.name + ": " + thrown.message + where);
    }
  });

  check("applySkin() 的调用点晚于 petBox / sprite 的声明", () => {
    const declIdx = code.indexOf("const sprite = document.getElementById");
    // 只认顶层调用（顶格写）；loadSkin() 内部那次 applySkin() 是缩进的，不算
    const callIdx = code.search(/^applySkin\(\);/m);
    assert.ok(declIdx > -1, "找不到 const sprite 声明");
    assert.ok(callIdx > -1, "找不到顶层 applySkin() 调用");
    assert.ok(
      callIdx > declIdx,
      "顶层 applySkin() 在第 " + code.slice(0, callIdx).split("\n").length +
      " 行，早于 const sprite（第 " + code.slice(0, declIdx).split("\n").length +
      " 行）—— 会触发 TDZ ReferenceError 打断整个脚本",
    );
  });

  await flush();

  check("初始化后 sprite 拿到 --fw/--fh（applySkin 真的跑到过）", () => {
    const p = els.sprite && els.sprite.style._props;
    assert.ok(p, "sprite 元素从未被取到");
    assert.strictEqual(p["--fw"], "150px", "--fw 未按内置芙宁娜 150px 写入");
    assert.strictEqual(p["--fh"], "150px", "--fh 未按内置芙宁娜 150px 写入");
  });

  check("--dw/--dh 写在 #pet（var 的消费方）上，不是写在子元素 sprite 上", () => {
    const p = els.pet && els.pet.style._props;
    assert.ok(p, "#pet 元素从未被取到");
    assert.strictEqual(p["--dw"], "150px", "#pet 上没拿到 --dw → ::before 光晕会退回 150px 硬编码");
    assert.strictEqual(p["--dh"], "150px", "#pet 上没拿到 --dh");
  });

  check("精灵图真的被赋了本地 svg/*.webp（不是只剩 ::before 光晕）", () => {
    const bg = els.sprite && els.sprite.style.backgroundImage;
    assert.ok(bg, "sprite 的 background-image 为空 —— 角色不会渲染，只剩光晕圈");
    assert.match(bg, /svg\/(idle|idle-look|thinking|conducting|building|error)\.webp/,
      "background-image 不是内置精灵图：" + bg);
  });

  check("精灵图 sheet 尺寸与动画步长一致（background-size = 帧数 × 帧宽）", () => {
    const size = els.sprite.style.backgroundSize;
    assert.ok(size, "background-size 未设置");
    assert.strictEqual(size, "5550px 150px", "idle 37 帧 × 150px 应为 5550px 150px，实际 " + size);
    assert.match(els.sprite.style.animation || "", /pet-steps 3s steps\(37\)/,
      "动画未按 37 帧设置：" + els.sprite.style.animation);
  });

  check("初始下发全窗鼠标穿透（否则透明窗挡住底下窗口、光标变抓手）", () => {
    const hit = petIpc.filter((c) => c[0] === "setIgnoreMouse");
    assert.ok(hit.length > 0, "初始化阶段从未调用 setIgnoreMouse —— 220×480 透明窗会一直吃鼠标");
    assert.strictEqual(hit[hit.length - 1][1], true,
      "最后一次 setIgnoreMouse 应为 true（初始全穿透），实际 " + hit[hit.length - 1][1]);
  });

  check("预加载了全部内置精灵图（切状态不白屏）", () => {
    for (const f of ["idle", "idle-look", "thinking", "conducting", "building", "error"]) {
      assert.ok(loadedImages.some((u) => String(u).includes("svg/" + f + ".webp")),
        "未预加载 svg/" + f + ".webp");
    }
  });

  // ---------- 2026-09-17 面板改版新增的静态契约 ----------

  check("脚本引用的每个 id 都在 HTML 里存在（面板元素变多后最容易漏）", () => {
    const ids = [...new Set(
      [...codeStripped.matchAll(/getElementById\(\s*"([^"]+)"\s*\)/g)].map((x) => x[1]))];
    assert.ok(ids.length >= 10, "只解析到 " + ids.length + " 个 id，正则可能失效了");
    const missing = ids.filter((id) => !html.includes('id="' + id + '"'));
    assert.deepStrictEqual(missing, [],
      "脚本取了这些 id 但 HTML 里没有对应元素 → 该处静默失效：" + missing.join(", "));
  });

  check("不对含图标子节点的元素直写 textContent（会把图标整个抹掉）", () => {
    // 这些元素内部现在有 <svg> 图标 / <span class="spin">，赋 textContent 会清空子节点。
    // 正确做法是写它们内部的纯文本节点（#sendLabel / #statusText / #pillText）。
    // （#liveLabel 已随 2026-09-26 的面板去重删除：启停并入引擎分段。）
    // 注意 #recent 不在此列：它的子节点本来就是按数据生成的，写 innerHTML 才是正确用法。
    const guarded = ["liveBtn", "sendBtn", "previewBtn", "statusEl", "pillEl"];
    const bad = [];
    for (const name of guarded) {
      const re = new RegExp("\\b" + name + "\\.(textContent|innerHTML)\\s*=");
      if (re.test(codeStripped)) bad.push(name);
    }
    assert.deepStrictEqual(bad, [],
      "这些变量指向的元素含图标子节点，直写 textContent/innerHTML 会清空它们：" + bad.join(", ") +
      "（应改写其内部文本节点）");
  });

  check("color-scheme 只声明在 #panel 上，没落到 :root / html（保透明窗）", () => {
    // (?<!prefers-) 是必需的：`prefers-color-scheme: light` 里含有同名字符串，
    // 不加这个后视断言会把媒体查询本身当成违规声明。
    const decls = cssStripped.split("\n")
      .filter((ln) => /(?<!prefers-)color-scheme\s*:\s*(dark|light|normal|only)/.test(ln));
    assert.ok(decls.length > 0, "找不到任何 color-scheme 声明 —— 原生下拉弹层在暗色下会刺眼");
    const stray = decls.filter((ln) => !ln.includes("#panel"));
    assert.deepStrictEqual(stray, [],
      "color-scheme 声明不在 #panel 规则里：" + stray.map((s) => s.trim()).join(" | ") +
      " —— 根元素设 color-scheme 会让 Chromium 画默认画布底色，破坏 transparent 窗");
  });

  check("面板与气泡是 border-box（204px 宽度预算，否则横向溢出被窗口裁掉）", () => {
    for (const sel of ["#panel", "#bubble"]) {
      const re = new RegExp(sel.replace("#", "\\#") + "\\s*\\{([^}]*)\\}", "g");
      const hits = [...cssStripped.matchAll(re)].map((x) => x[1]);
      assert.ok(hits.length > 0, "找不到 " + sel + " 规则");
      assert.ok(hits.some((body) => /box-sizing\s*:\s*border-box/.test(body)),
        sel + " 没有 box-sizing: border-box —— 加上 padding/边框后会超出 204px 可用宽");
    }
  });

  check("跟随系统主题：prefers-color-scheme: light 下有完整的亮色令牌覆盖", () => {
    // 定位「亮色令牌覆盖」那个媒体块 —— 判据是它内部直接声明了 :root 的令牌，
    // 而不是靠 includes 在文件尾部瞎撞（后续规则里的 var(--c-surface) 会伪造命中）。
    const re = /@media\s*\(prefers-color-scheme:\s*light\)\s*\{/g;
    let body = null;
    for (const mm of cssStripped.matchAll(re)) {
      const b = cssBlockBody(cssStripped, mm.index + mm[0].length - 1);
      if (/:root\s*\{/.test(b)) { body = b; break; }
    }
    assert.ok(body,
      "没有 prefers-color-scheme: light 下的 :root 亮色令牌覆盖块 —— " +
      "应用默认主题是「系统」（src/theme.ts 兜底 system），亮色系统下两边会分叉");
    for (const tok of ["--c-surface", "--c-border", "--c-accent", "--c-t-strong", "--glass"]) {
      assert.ok(new RegExp(tok + "\\s*:").test(body), "亮色令牌块缺少声明：" + tok);
    }
  });

  // ---------- 状态行的行为断言（真点按钮 / 真喂回调，不只看静态文本） ----------

  check("状态行：不带色调参数时也必须显示（原 setStatus(\"合成中…\") 会被 display:none 吞掉）", () => {
    const click = (els.preview && els.preview._on.click || [])[0];
    assert.ok(click, "试听按钮没注册 click 处理器");
    els.say.value = "测试一下";            // doPreview 对空文本直接 return
    click();
    assert.match(String(els.status.className), /\bshow\b/,
      "setStatus(\"合成中…\") 之后 #status 没拿到 .show → 这条提示永远看不见" +
      "（className=" + JSON.stringify(els.status.className) + "）");
    assert.strictEqual(els.statusText.textContent, "合成中…");
    assert.ok(petIpc.some((c) => c[0] === "previewText"), "没有下发 pet:preview");
  });

  check("状态行：ok/err 走 className，文案写进 #statusText（不碰 #status 的图标子节点）", () => {
    assert.strictEqual(typeof petCbs.preview, "function", "onPreviewResult 没被订阅");
    petCbs.preview({ ok: true, wav: "a.wav", url: "blob:x", duration_s: 3.2 });
    assert.strictEqual(els.status.className, "show ok", "合成成功应为 show ok");
    assert.match(els.statusText.textContent, /已合成 3\.2s/);
    // 待发产物就位后主按钮文案要跟着变
    assert.strictEqual(els.sendLabel.textContent, "发送试听",
      "试听成功后主按钮应变成「发送试听」，实际 " + JSON.stringify(els.sendLabel.textContent));
    petCbs.preview({ ok: false, error: "微信没开" });
    assert.strictEqual(els.status.className, "show err", "合成失败应为 show err");
    assert.match(els.statusText.textContent, /合成失败：微信没开/);
  });

  // ---------- 面板让位规则（回归点 12） ----------

  check("气泡显隐只有一个写入口：bubble.style.display 只出现在 setBubbleVisible 里", () => {
    const writes = [...codeStripped.matchAll(/bubble\.style\.display\s*=/g)];
    assert.strictEqual(writes.length, 1,
      "bubble.style.display 被写了 " + writes.length + " 次，应恰好 1 次（都在 setBubbleVisible 内）；" +
      "散着写的地方不会同步 #root.compact，那一处就会带着多余的 #recent 去和气泡抢高度");
    const fnIdx = codeStripped.indexOf("function setBubbleVisible");
    assert.ok(fnIdx > -1, "找不到 setBubbleVisible() —— 气泡显隐与让位标记没有统一入口");
    const body = cssBlockBody(codeStripped, codeStripped.indexOf("{", fnIdx));
    assert.ok(/bubble\.style\.display\s*=/.test(body),
      "唯一那处 bubble.style.display 不在 setBubbleVisible() 里");
    assert.ok(/classList\.(toggle|add|remove)\(\s*"compact"/.test(body),
      "setBubbleVisible() 没有同步 #root 上的 .compact —— 让位规则永远不会生效");
  });

  check("CSS 里有 #root.compact 收起 #recent 的规则（气泡在屏时面板让位）", () => {
    const hits = [...cssStripped.matchAll(/#root\.compact[^{]*\{([^}]*)\}/g)];
    assert.ok(hits.length > 0, "找不到任何 #root.compact 规则");
    const hidesRecent = hits.some((h) =>
      h[0].includes("#recent") && /display\s*:\s*none/.test(h[1]));
    assert.ok(hidesRecent,
      "没有「#root.compact 下 #recent 隐藏」的规则 —— 面板省不出高度，气泡一起来底部那行就会被切");
  });

  // ⚠️ 这一组只在在线模式跑：它会 setState/playGuide，把「离线」场景的既有状态冲掉
  // （药丸被改成「待机」），导致下面 OFFLINE 分支的断言误报。离线模式另有一组断言。
  if (!OFFLINE) {
    check("气泡上屏时 #root 挂 .compact，气泡收起时摘掉（setState 路径）", () => {
      assert.strictEqual(typeof petFns.setState, "function",
        "没从内联脚本里导出 setState（EXPORT_HOOK 可能没生效）");
      petFns.setState("think", "「你好」");
      assert.strictEqual(els.bubble.style.display, "block", "setState(think) 没把气泡放上屏");
      assert.ok(els.root.classList.contains("compact"),
        "气泡已上屏但 #root 没有 .compact → 面板不会让位，底部「最近发送」会被切");
      petFns.setState("idle");
      assert.strictEqual(els.bubble.style.display, "none", "setState(idle) 没把气泡收起来");
      assert.ok(!els.root.classList.contains("compact"),
        "气泡已收起但 .compact 还挂着 → 面板被白白精简，待机时看不到「最近发送」");
    });

    check("导览气泡走同一入口：playGuide 后 #root 挂上 .compact", () => {
      assert.strictEqual(typeof petFns.playGuide, "function", "没从内联脚本里导出 playGuide");
      petFns.playGuide({ title: "音色工坊", lines: ["一切从这里开始。", "丢进视频，我自动切片质检。"] });
      assert.strictEqual(els.bubble.className, "guide", "导览气泡的类名应为 guide");
      assert.strictEqual(els.bubble.style.display, "block", "导览气泡没上屏");
      assert.ok(els.root.classList.contains("compact"),
        "导览气泡在屏但面板没让位 —— guide 场景面板会被切 6px 并冒出内部滚动条");
    });

    // endGuide() 只负责清定时器并 `void tick()`，真正的重画在 tick 的 await 之后才发生，
    // 所以必须让出一轮事件循环再断言 —— 同步断言会读到「还没摘掉」的中间态。
    petFns.endGuide();
    await flush();

    check("导览结束后 .compact 被摘掉（面板恢复常态：行距复原、#recent 回来）", () => {
      assert.ok(!els.root.classList.contains("compact"),
        "导览结束后 .compact 还挂着 —— 面板会一直停在精简态");
      assert.strictEqual(els.bubble.style.display, "none",
        "导览结束后气泡应收起（状态机此时是 idle）");
    });
  }

  // ---------- 实时变声引擎：RVC 实时 / 千问变声（回归点 13）----------
  // 用户反馈：「这两个用户不确定到底用哪一种」「就没体现 rvc 的」「我感觉这个可以来回切换」。
  // 这里钉住四件事：① 两个引擎的端点各自独立；② 启停不走主进程 IPC（否则只能开 RVC）；
  // ③ 「选中」与「运行中」分开表达；④ 切换时先停再开（后端对同时开直接 409）。

  check("两个引擎的启停端点各自独立，没有都指向 RVC", () => {
    const map = codeStripped.match(/const ENGINES = \{[\s\S]*?\n\};/);
    assert.ok(map, "找不到 ENGINES 定义");
    const body = map[0];
    for (const s of ['start: "/api/rvc/live/start"', 'stop: "/api/rvc/live/stop"',
                     'start: "/api/cascade/start"', 'stop: "/api/cascade/stop"']) {
      assert.ok(body.includes(s), "ENGINES 里缺少 " + s + " —— 点千问会打到 RVC 的接口上");
    }
  });

  check("引擎启停不再走主进程 IPC，而是桌宠直连后端", () => {
    assert.ok(!/window\.pet\.liveToggle/.test(codeStripped),
      "还在用 window.pet.liveToggle —— 那条路只能开 RVC、开不了千问，" +
      "而且改主进程要重打 asar 才生效；桌宠直连后端才能只改 pet.html 就热替换");
    assert.ok(/method:\s*"POST"/.test(codeStripped), "找不到 POST 调用");
  });

  check("引擎选择持久化，默认 rvc；初始高亮跟着选中走", () => {
    assert.strictEqual(petFns.getEngine(), "rvc", "默认引擎应为 rvc");
    assert.ok(els.engRvc.classList.contains("on"), "默认应高亮 RVC 分段");
    assert.ok(!els.engQwen.classList.contains("on"), "默认不该高亮千问分段");
  });

  if (!OFFLINE) {
    // 都没在跑时点千问分段：只改选择，不发启停请求
    const beforePick = fetchCalls.length;
    (els.engQwen._on.click || [])[0]();
    await flush();

    check("没引擎在跑时点另一个引擎：只改选择 + 持久化，不发启停请求", () => {
      assert.strictEqual(petFns.getEngine(), "qwen", "选中态没切到千问");
      assert.ok(els.engQwen.classList.contains("on"), "千问分段没高亮");
      assert.ok(!els.engRvc.classList.contains("on"), "RVC 分段应取消高亮");
      assert.strictEqual(localStorageStore.get("pet_engine"), "qwen", "没写进 localStorage");
      const posts = fetchCalls.slice(beforePick).filter((c) => c.method === "POST");
      assert.deepStrictEqual(posts, [],
        "没在跑的时候点引擎不该发启停请求（免得点错一下就占 GPU 和 CABLE），实际发了：" +
        JSON.stringify(posts.map((p) => p.url)));
    });

    // 点已选中的引擎分段 = 启停当前引擎（2026-09-26 起「开 RVC」按钮已并入分段，
    // 那颗按钮与已选中项永远是同一个引擎，用户看着就是两个 RVC）→ POST /api/cascade/start
    const beforeStart = fetchCalls.length;
    (els.engQwen._on.click || [])[0]();
    await flush();

    check("点已选中的引擎分段 = 启停（选中千问 → /api/cascade/start）", () => {
      const posts = fetchCalls.slice(beforeStart).filter((c) => c.method === "POST").map((c) => c.url);
      assert.strictEqual(posts.length, 1, "应恰好一个 POST，实际 " + JSON.stringify(posts));
      assert.ok(posts[0].includes("/api/cascade/start"),
        "应打 /api/cascade/start，实际 " + posts[0]);
    });

    // 模拟「主界面把千问开起来了」：状态接口回 running=true
    cascadeRunning = true;
    await tickInterval()();
    await flush();

    check("引擎在跑时：药丸点名 + 分段亮呼吸点 + 气泡标题写出引擎名", () => {
      assert.strictEqual(els.pillText.textContent, "千问变声中",
        "药丸没写出引擎名 —— 这正是用户「不确定用的是哪一种」的根源");
      assert.ok(els.engQwen.classList.contains("running"), "运行中的千问分段没有呼吸点");
      assert.ok(!els.engRvc.classList.contains("running"), "RVC 没在跑却亮了呼吸点");
      assert.strictEqual(els.title.textContent, "千问变声",
        "气泡标题没写出引擎名 —— 不悬停面板时，气泡是唯一能看出「用的哪一种」的地方");
    });

    // 关键场景：选中 RVC、但跑的是千问。药丸必须说真话，不能跟着选中态走。
    // 用 setEngine()（纯改选择、不碰引擎）构造这个状态 —— 点分段做不到，
    // 点分段会触发 pickEngine 的「先停再开」。
    petFns.setEngine("rvc");
    await tickInterval()();
    await flush();

    check("选中态与运行态分开表达：选中 RVC 但千问在跑时，药丸仍说「千问变声中」", () => {
      assert.strictEqual(petFns.getEngine(), "rvc", "前置状态没构造出来：选中应为 rvc");
      assert.strictEqual(petFns.getRunningEngine(), "qwen", "前置状态没构造出来：在跑的应为 qwen");
      assert.strictEqual(els.pillText.textContent, "千问变声中",
        "药丸跟着「选中」走了 —— 用户选了 RVC 但实际跑的是千问时会被告知错的引擎");
      assert.ok(els.engRvc.classList.contains("on"), "RVC 应是选中态");
      assert.ok(!els.engRvc.classList.contains("running"), "RVC 没在跑，不该有呼吸点");
      assert.ok(els.engQwen.classList.contains("running"), "千问在跑，应有呼吸点");
    });

    // 回到「选中千问、千问在跑」，再点 RVC 分段 → 必须先停千问再开 RVC（后端对同时开会 409）
    petFns.setEngine("qwen");
    const beforeSwitch = fetchCalls.length;
    (els.engRvc._on.click || [])[0]();
    await flush();

    check("切到另一个引擎时自动「先停再开」，顺序不能反", () => {
      const posts = fetchCalls.slice(beforeSwitch)
        .filter((c) => c.method === "POST")
        .map((c) => c.url.replace(/^https?:\/\/[^/]+/, ""));
      assert.deepStrictEqual(posts, ["/api/cascade/stop", "/api/rvc/live/start"],
        "应先把千问停掉再开 RVC（后端同时开会 409「两者抢 GPU 且都占 CABLE」），实际顺序：" +
        JSON.stringify(posts));
      assert.strictEqual(petFns.getEngine(), "rvc", "选中态应切到 RVC");
    });
  }

  // ---------- 常驻遥控器（2026-09-29）：状态条 + 按住说话 ----------
  // 量的是「桌宠从装饰变成遥控器」这个改动本身。两个最容易静默腐烂的地方：
  //   ① 状态条上那三个数各自来自哪个源（药丸=实时状态 / 音色=下拉或后端 selected /
  //      耗时=实时日志尾部），源错了不会报错，只会显示一个看起来很像真的的假数字；
  //   ② 按住说话是**跨进程的状态机**（主进程持状态、这里只镜像）——
  //      最容易出的错是「手势发出去、状态没人接」，界面上表现为“按了没反应”。
  if (!OFFLINE) {
    // 先把音色清单拉进来：真实路径上这一步由 showPanel() → refreshPanel() 触发（悬停面板），
    // 而状态条的「当前音色」就是靠它把「主界面选中」那个空 value 换成真名字的。
    // 不先拉的话下面那条断言测到的是“还没拉到”的占位态，不是要守的行为。
    await petFns.loadVoices();
    (els.voiceSel._on.change || []).forEach((fn) => fn());
    // 让后端报「RVC 正在跑」：这时状态条三块同时在场（最挤的一帧）
    cascadeRunning = false;
    liveRunning = true;
    await tickInterval()();
    await flush();

    check("状态条：变声在跑时三块同屏 —— 引擎名 + 音色 + 实测推理耗时", () => {
      assert.strictEqual(els.pillText.textContent, "RVC 变声中");
      assert.strictEqual(els.rmVoice.textContent, "袋鼠骑士",
        "音色 chip 没显示出来（它是「当前音色」唯一的常驻入口）");
      assert.strictEqual(els.rmLat.hidden, false, "变声在跑，推理耗时的 chip 却没出现");
      assert.strictEqual(els.rmLat.textContent, "82ms",
        "耗时显示的不是后端给的值：" + JSON.stringify(els.rmLat.textContent));
      // 口径必须写清楚：这是单块推理耗时，不是端到端延迟（端到端还要加分块与音频驱动缓冲）
      assert.match(String(els.rmLat.title), /推理/);
      assert.match(String(els.rmLat.title), /端到端/);
    });

    // 停掉变声：日志尾部还留着上次的 82ms（后端接口就不再报了）——
    // 照旧显示的话，用户会以为变声还在跑（那正是“只读日志”最容易掉进去的坑）。
    liveRunning = false;
    await tickInterval()();
    await flush();

    check("状态条·耗时只在真的在跑时报（停了就清空，不留上次的数）", () => {
      assert.strictEqual(els.rmLat.hidden, true, "变声停了但耗时 chip 还在");
      assert.strictEqual(els.rmLat.textContent, "", "变声停了却还显示上一轮的耗时");
    });

    check("状态条·音色：下拉是「主界面选中」时用 /api/voices 的 selected 反查真名", () => {
      assert.strictEqual(els.voiceSel.value, "", "前置状态没构造出来：下拉应为「主界面选中」");
      assert.strictEqual(els.rmVoice.textContent, "袋鼠骑士",
        "下拉 value 为空时没去查 selected —— 用户看到的会是一句没用的「主界面选中」" +
        "（实际：" + JSON.stringify(els.rmVoice.textContent) + "）");
    });

    check("状态条·音色：下拉选了具体音色就显示那一个（与发送链路同源）", () => {
      els.voiceSel.value = "furina";
      (els.voiceSel._on.change || []).forEach((fn) => fn());
      assert.strictEqual(els.rmVoice.textContent, "芙宁娜",
        "chip 没跟着下拉走 —— 显示的音色和真发出去用的音色会分叉");
      els.voiceSel.value = "";
      (els.voiceSel._on.change || []).forEach((fn) => fn());
      assert.strictEqual(els.rmVoice.textContent, "袋鼠骑士");
    });

    // 药丸是个真按钮：点一下 = 启停**当前选中**的引擎（与分段里点已选中项同一件事）
    petFns.setEngine("rvc");
    const beforePill = fetchCalls.length;
    (els.pill._on.click || [])[0]();
    await flush();

    check("药丸点一下就启停当前引擎（打的是选中引擎的端点）", () => {
      const posts = fetchCalls.slice(beforePill).filter((c) => c.method === "POST").map((c) => c.url);
      assert.strictEqual(posts.length, 1, "应恰好一个 POST，实际 " + JSON.stringify(posts));
      assert.ok(posts[0].includes("/api/rvc/live/start"),
        "应打选中引擎（rvc）的启动端点，实际 " + posts[0]);
    });

    // ---------- 按住说话：手势 → IPC → 状态回传 ----------
    petIpc.length = 0;
    els.voiceSel.value = "kangaroo";
    (els.holdMic._on.pointerdown || [])[0]({ pointerId: 1 });

    check("按住说话：pointerdown → pet:mic-down，且带上面板选中的音色", () => {
      assert.deepStrictEqual(petIpc.filter((c) => c[0] === "micDown"), [["micDown", "kangaroo"]],
        "按下没有下发 pet:mic-down（或没带上音色 id）—— 主进程不知道用哪个音色换声");
      assert.strictEqual(petFns.getMicPhase(), "arming", "按下后应进入 arming（等 start 的响应）");
    });

    check("按住说话：pointerup → pet:mic-up（松手只表示「要停」，不停在本地）", () => {
      (els.holdMic._on.pointerup || [])[0]();
      assert.ok(petIpc.some((c) => c[0] === "micUp"), "松手没有下发 pet:mic-up —— 录音只能等 59s 上限自己停");
      assert.strictEqual(petFns.getMicPhase(), "arming",
        "松手不该直接把相位改成 idle —— 真正的下一个状态由主进程回传（换声可能要十几秒）");
    });

    check("按住说话：pointercancel 也算松手（系统抢走手势时不能挂着）", () => {
      const before = petIpc.filter((c) => c[0] === "micUp").length;
      (els.holdMic._on.pointercancel || [])[0]();
      assert.ok(petIpc.filter((c) => c[0] === "micUp").length > before);
    });

    check("mic-state 回传驱动按钮：录制中整块转红 + 秒数在跳", () => {
      assert.strictEqual(typeof petCbs.mic, "function", "onMicState 没被订阅（按钮状态就没人驱动了）");
      petCbs.mic({ phase: "recording", startedAt: Date.now() - 2300, maxSeconds: 59 });
      assert.strictEqual(petFns.getMicPhase(), "recording");
      assert.ok(els.holdMic.classList.contains("rec"), "录制中按钮没转红（分不清在不在录）");
      assert.match(String(els.micSec.textContent), /^2\.[0-9]s \/ 59s$/,
        "秒数没显示出来：" + JSON.stringify(els.micSec.textContent));
      petCbs.mic({ phase: "idle", startedAt: 0, maxSeconds: 59 });
      assert.strictEqual(petFns.getMicPhase(), "idle");
      assert.ok(!els.holdMic.classList.contains("rec"), "结束后按钮还红着");
      assert.strictEqual(els.micSec.textContent, "");
    });

    // 按住期间的两条集成级不变量。它们不属于「功能」，而是「手势能不能真的走完」：
    //   ① 窗口不能被设成鼠标穿透 —— 否则松手事件可能根本送不到页面；
    //   ② 面板不能在那 500ms 后自动收 —— 按钮一没了，指针捕获和秒数一起没了。
    petIpc.length = 0;
    petCbs.mic({ phase: "recording", startedAt: Date.now(), maxSeconds: 59 });
    (document._on.mousemove || [])[0]({ clientX: 5, clientY: 5, screenX: 5, screenY: 5 });

    check("按住期间不把窗口设成鼠标穿透（否则松手可能收不到）", () => {
      const last = petIpc.filter((c) => c[0] === "setIgnoreMouse").pop();
      assert.ok(last, "mousemove 根本没调 setIgnoreMouse —— 那套穿透逻辑被改掉了？");
      assert.strictEqual(last[1], false,
        "按住期间把窗口设成了穿透：光标一滑出面板，pointerup 就可能到不了页面");
      petCbs.mic({ phase: "idle", startedAt: 0, maxSeconds: 59 });
      (document._on.mousemove || [])[0]({ clientX: 5, clientY: 5, screenX: 5, screenY: 5 });
      const after = petIpc.filter((c) => c[0] === "setIgnoreMouse").pop();
      assert.strictEqual(after[1], true, "结束按住后应恢复全窗穿透（否则透明窗一直吃鼠标）");
    });

    check("按住期间面板不自动收起（panelTimer 不能被挂上）", () => {
      const leave = (els.panel._on.mouseleave || [])[0];
      assert.strictEqual(typeof leave, "function", "面板没有 mouseleave 处理器？");
      petCbs.mic({ phase: "recording", startedAt: Date.now(), maxSeconds: 59 });
      leave();
      assert.strictEqual(petFns.getPanelTimer(), null,
        "按住期间挂了自动收起定时器 —— 按钮会带着秒数一起消失，而录音还在继续");
      petCbs.mic({ phase: "idle", startedAt: 0, maxSeconds: 59 });
      leave();
      assert.notStrictEqual(petFns.getPanelTimer(), null,
        "没在按的时候面板应照旧自动收（常驻面板会挡住桌面）");
      clearTimeout(petFns.getPanelTimer());
    });

    check("旧主进程（preload 里没有 micDown）必须说出来，不能静默失败", () => {
      const saved = window.pet.micDown;
      delete window.pet.micDown;
      petIpc.length = 0;
      (els.holdMic._on.pointerdown || [])[0]({ pointerId: 3 });
      assert.match(String(els.statusText.textContent), /新版桌面端|asar/,
        "旧主进程时静默失败 —— 用户按半天会以为是自己没按住，而这他解决不了；" +
        "实际状态行：" + JSON.stringify(els.statusText.textContent));
      assert.strictEqual(petFns.getMicPhase(), "idle", "没发出去的按住不该让按钮停在“录制中”");
      window.pet.micDown = saved;
    });

    check("「按住说话」是第三个页签：切过去时显示页体、收起动作行（并记住这一页）", () => {
      (els.tabHold._on.click || [])[0]();
      assert.strictEqual(petFns.getActiveTab(), "hold");
      assert.ok(els.holdPane.classList.contains("on"), "按住页的页体没显示");
      assert.ok(!els.speakPane.classList.contains("on"), "说话页没有让位");
      assert.strictEqual(els.actRow.style.display, "none",
        "「按住」页里还留着「合成并发送/试听」—— 它们作用的是说话页的输入框，会让人以为还要先打字");
      assert.strictEqual(localStorageStore.get("pet_panel_tab"), "hold",
        "页签没记住 —— 固定用「按住」的人每次悬停都要重新点一次");
      // 这一页的入口同样受 hook.wechat 门控（整条链路终点是发微信）
      assert.ok(/hide\("tabHold"/.test(codeStripped) && /hide\("holdPane"/.test(codeStripped),
        "refreshCapVisibility 没有收起「按住说话」的页签/页体：关掉微信能力后它就是个一按必 404 的死页");
      (els.tabSpeak._on.click || [])[0]();
      assert.strictEqual(petFns.getActiveTab(), "speak");
      assert.strictEqual(els.actRow.style.display, "", "回到说话页应把动作行放回来");
    });

    // ---------- 发送前预检：微信开没开 / 会不会重启，要**按下之前**就说 ----------
    //
    // 这一组钉的是那条最贵的失败路径：录 0..59 秒 + 换声十几秒，然后才被告知「微信没开」。
    // 判据全部来自后端 /api/wechat/precheck（只读），前端只负责「把不同结论说成不同的话」。
    const holdPre = els.holdPre;
    const preFetches = () => fetchCalls.filter((c) => c.url.includes("/api/wechat/precheck")).length;

    // ① 常态：微信在跑、不重启
    precheckStub = null;
    (els.tabHold._on.click || [])[0]();     // 进页 → 强制拉一次（绕过节流）
    await flush();
    check("进「按住说话」页就把预检拉出来：常态写「微信已打开」", () => {
      assert.strictEqual(holdPre.className, "t-ok",
        "常态预检的色调不对（应 t-ok）：" + JSON.stringify(holdPre.className));
      assert.match(String(holdPre.textContent), /已打开|直接切麦克风/,
        "预检行没显示常态结论：" + JSON.stringify(holdPre.textContent));
    });

    // ② 注定失败（block_reason 非空）：这才是「松手之后才报错」要治的那件事
    precheckStub = { ok: true, status: 200, json: async () => ({
      ...PRECHECK_OK,
      wechat_running: false,
      block_reason: "微信没有在运行：请先打开微信并登录、进入聊天窗口，再重试发送",
    }) };
    await petFns.refreshWechatPrecheck(true);
    check("注定失败要在按下之前说，并转述后端的**分因**（没开 / 收托盘 / 登录页是三件事）", () => {
      assert.strictEqual(holdPre.className, "t-bad", "注定失败应是 t-bad：" + JSON.stringify(holdPre.className));
      assert.match(String(holdPre.textContent), /没有在运行/,
        "应转述后端分因的首句，而不是自己编一句「出错了」：" + JSON.stringify(holdPre.textContent));
      assert.strictEqual(holdPre.title, "微信没有在运行：请先打开微信并登录、进入聊天窗口，再重试发送",
        "完整原因（含下一步）必须进 title —— 单行条的省略号正好会切掉最该看的那半句");
    });

    check("预检只告警、不拦人：数据可能已过期，不许因此禁用按钮", () => {
      // 必须直接查 disabled：DOM 桩是直接调处理器，绕过了浏览器的「禁用按钮不发事件」——
      // 只靠下面那行行为断言的话，把按钮禁掉这种实现**测不出来**（变异测试实测过）。
      assert.ok(!els.holdMic.disabled,
        "预检说必败时把按钮禁掉了 —— 刚把微信打开的人会按不动，比不告警更糟");
      petIpc.length = 0;
      (els.holdMic._on.pointerdown || [])[0]({ pointerId: 4 });
      assert.strictEqual(petFns.getMicPhase(), "arming",
        "预检为 bad 时按不下去了 —— 刚把微信打开的人会按不动，比不告警更糟");
      assert.ok(petIpc.some((c) => c[0] === "micDown"), "预检存在时 micDown 没发给主进程");
      petCbs.mic({ phase: "idle", startedAt: 0, maxSeconds: 59 });
    });
    await flush();

    // ③ 会重启（VM_WECHAT_RESTART=auto/1 才出现；默认 0 时不会，所以这条只能靠桩喂）
    precheckStub = { ok: true, status: 200, json: async () => ({
      ...PRECHECK_OK,
      restart_needed: true,
      reason: "微信上次录音用的是「麦克风阵列 (Senary Audio)」，需重启让它重新枚举",
    }) };
    await petFns.refreshWechatPrecheck(true);
    check("会重启微信也要提前说，原因进 tooltip", () => {
      assert.strictEqual(holdPre.className, "t-warn", "会重启应是 t-warn（不是错误）：" + JSON.stringify(holdPre.className));
      assert.match(String(holdPre.textContent), /重启/);
      assert.match(String(holdPre.title), /重新枚举/, "重启的原因没进 tooltip");
    });

    // ④ 200 但字段缺失 = 前后端版本不一致（pet.html 是热替换的）
    precheckStub = { ok: true, status: 200, json: async () => ({ ok: true, hint: "旧版后端" }) };
    await petFns.refreshWechatPrecheck(true);
    check("预检字段缺失时说「没有结论」，绝不猜成「微信没开」", () => {
      assert.strictEqual(holdPre.className, "t-warn");
      assert.match(String(holdPre.textContent), /没有结论|后端版本/,
        "实际：" + JSON.stringify(holdPre.textContent));
      assert.doesNotMatch(String(holdPre.textContent), /没开|没在运行/,
        "把版本不一致说成「微信没开」会把用户指去查微信，方向完全错");
    });

    // ⑤ 预检接口不在（后端比桌宠旧 / 能力被关）与网络异常
    precheckStub = { ok: false, status: 404, json: async () => ({}) };
    await petFns.refreshWechatPrecheck(true);
    check("预检 404 不吓人：它是只读咨询，打不开不影响发送", () => {
      assert.strictEqual(holdPre.className, "t-warn", "预检打不开不该是红色警报：" + JSON.stringify(holdPre.className));
      assert.match(String(holdPre.textContent), /旧|不在/);
      assert.match(String(holdPre.title), /不影响发送/);
    });

    precheckStub = () => { throw new Error("后端没起来"); };
    await petFns.refreshWechatPrecheck(true);
    check("预检请求抛错时也要有个说法（不能停在上一句结论上）", () => {
      assert.strictEqual(holdPre.className, "t-warn");
      assert.match(String(holdPre.textContent), /打不开|没起来/,
        "实际：" + JSON.stringify(holdPre.textContent));
    });

    // ⑥ 节流：tick 每 1s 都会路过，但真请求 5s 才有一次
    precheckStub = null;
    await petFns.refreshWechatPrecheck(true);
    const afterForce = preFetches();
    await petFns.refreshWechatPrecheck(false);   // 刚拉过 → 5s 内不重复
    tickInterval()();                            // 就是 tick 那一帧（此刻正停在「按住」页）
    await flush();
    check("预检有节流：1s 轮询不会把它带着每秒枚举一次进程/窗口", () => {
      assert.strictEqual(preFetches(), afterForce,
        "多发了 " + (preFetches() - afterForce) + " 次预检请求");
    });

    (els.tabSpeak._on.click || [])[0]();   // 收尾：把页签还给说话页，别影响后面的用例
  }

  if (OFFLINE) {
    // ---------- 回归点 11：后端离线提示必须常驻 ----------
    check("离线：药丸切到「后端离线」(t-err)、气泡上屏、精灵图切 error", () => {
      assert.strictEqual(els.pillText.textContent, "后端离线",
        "离线时药丸文案不对：" + JSON.stringify(els.pillText.textContent));
      assert.strictEqual(els.pill.className, "pill t-err",
        "离线时药丸色调不对：" + JSON.stringify(els.pill.className));
      assert.strictEqual(els.bubble.style.display, "block", "离线气泡没上屏");
      assert.strictEqual(els.title.textContent, "后端离线");
      assert.match(String(els.sprite.style.backgroundImage), /error\.webp/,
        "离线时精灵图应切到 error.webp");
      assert.ok(els.root.classList.contains("compact"),
        "离线气泡在屏但面板没让位 —— offline 场景面板会被切 34px（实测）");
    });

    // 再驱动两轮轮询 —— 这正是原缺陷暴露的地方：
    // 第二帧会 fall through 到状态机的 else 分支，setState("idle") 把提示覆盖掉。
    const tickFn = tickInterval();
    for (let i = 0; i < 2; i += 1) {
      tickFn();
      await flush();
    }

    check("离线：连续几轮轮询后提示仍在（原实现 1s 后就被 setState(\"idle\") 覆盖）", () => {
      assert.strictEqual(els.pillText.textContent, "后端离线", "药丸被改回了「待机」");
      assert.strictEqual(els.pill.className, "pill t-err");
      assert.strictEqual(els.bubble.style.display, "block", "离线气泡被藏掉了");
      assert.match(String(els.sprite.style.backgroundImage), /error\.webp/);
      assert.ok(els.root.classList.contains("compact"),
        "离线期间 .compact 被摘掉了 —— 面板会带着多余的 #recent 去和气泡抢高度");
      // 离线那一帧必须把推理耗时抹掉：后端挂了它还挂着「82ms」，看着像变声还在跑。
      assert.strictEqual(els.rmLat.hidden, true, "离线时状态条还留着推理耗时");
      assert.strictEqual(els.rmLat.textContent, "");
    });
  } else {
    // 用子进程复跑本文件（VM_PET_OFFLINE=1）验证离线路径，
    // 避免把「在线」的桩改坏 —— 两套断言各跑各的。
    check("离线路径（子进程复跑）：后端全挂时提示常驻，不被状态机覆盖", () => {
      const r = spawnSync(process.execPath, [__filename], {
        env: Object.assign({}, process.env, { VM_PET_OFFLINE: "1" }),
        encoding: "utf-8",
      });
      assert.strictEqual(r.status, 0,
        "离线模式断言失败：\n" + String(r.stdout || "") + String(r.stderr || ""));
    });
  }

  // ---------- 回归点 13：长文分段发送（2026-09-23）下的结果播报口径 ----------
  // 后端会把长文切成 N 条依次发，「一次发送」因此变成「一批发送」。这里钉住三件
  // 会直接在界面上骗到用户的事：
  //   ① 单条属性（warning / duration_s）不能说成整批结论；
  //   ② 中途落败要说清「已发出几条 / 第几条挂的 / 剩的能不能重发」；
  //   ③ 看门狗不能「一看到新记录就收尾」—— 那会在第 1 条落地时就打绿勾，
  //      而此时后面的还在录；绿勾还会诱导用户去动键鼠，正好打断录制。

  check("EXPORT_HOOK 导出了 settleSendResult（否则分段口径无法在无 GUI 下验证）", () => {
    assert.strictEqual(typeof petFns.settleSendResult, "function",
      "内联脚本里找不到 settleSendResult() —— 被改名/删除了？测试会静默空转");
  });

  check("看门狗改成「凑齐才收尾」，且保留「连续不增长」兜底", () => {
    const idx = codeStripped.indexOf("async function watchSendOutcome");
    assert.ok(idx > -1, "找不到 watchSendOutcome()");
    const body = cssBlockBody(codeStripped, codeStripped.indexOf("{", idx));
    // ⚠️ 这里必须断言**判据本身**，不能只断言 "total_chunks 这个串出现过"：
    // watchSendOutcome 回传给 settleSendResult 的载荷里也有 `total_chunks: ...`，
    // 于是「把收尾判据里的总数读法删掉」这种回退，用 includes 检查照样绿
    // —— 变异测试实测过一次，所以才收紧到读取端与比较式。
    assert.ok(/last\.total_chunks/.test(body),
      "watchSendOutcome 没有从历史记录里读 total_chunks —— 收尾判据失去了「一共该有几条」，" +
      "会退化成第 1 条落地就打绿勾（此时后面的还在录）");
    assert.ok(/stableRounds\s*>=/.test(body),
      "「连续几轮不增长」的兜底判据没有真正参与比较（旧版后端不回传 total_chunks 时全靠它）");
    assert.ok(/countHistorySince\(/.test(body),
      "watchSendOutcome 没按「本批已发出几条」计数 —— 「出现新记录」≠「整批发完」");
    assert.strictEqual((body.match(/settleSendResult\(/g) || []).length, 1,
      "watchSendOutcome 里出现了多个 settleSendResult 出口 —— 收尾路径必须唯一，" +
      "否则总有一条会漏判分段是否结束");
  });

  if (typeof petFns.settleSendResult === "function") {
    // 单条：口径不能被分段逻辑带偏，旧的「已发出 3.2s」必须原样保留
    await petFns.settleSendResult({ ok: true, duration_s: 3.2, warning: "微信绑在物理麦上" });
    check("单条：仍按秒口径播报（分段逻辑不得污染单条路径）", () => {
      assert.strictEqual(els.statusText.textContent,
        "已发出 3.2s，但可能有静音：微信绑在物理麦上");
      assert.strictEqual(els.status.className, "show warn");
    });

    // 3 条全成：口径必须换成条数 —— 继续写「(12.3s)」会让人以为整段只有 12 秒
    await petFns.settleSendResult({ ok: true, total_chunks: 3, sent_chunks: 3 });
    check("整批成功：改成条数口径，不再拿单条时长冒充整段", () => {
      assert.strictEqual(els.statusText.textContent, "已发到微信 ✓（3 条语音）",
        "duration_s 只是批次里其中一条，不能当整段时长播报");
      assert.strictEqual(els.status.className, "show ok");
    });

    // ★ warning 是单条属性：批次里第 1 条告警、后两条正常时不能说成整批有问题
    await petFns.settleSendResult({ ok: true, total_chunks: 3, sent_chunks: 3, warning: "静音" });
    check("单条告警不否定整批：只说可能有静音，不说这批发砸了", () => {
      assert.strictEqual(els.statusText.textContent, "已发出 3 条语音，但可能有静音：静音");
      assert.strictEqual(els.status.className, "show warn");
    });

    // 中途落败：必须说清发出去几条、第几条挂的、剩下的可重发
    await petFns.settleSendResult({
      ok: true, outcome: "partial", total_chunks: 3, sent_chunks: 1,
      failed_index: 1, remaining_wavs: ["b.wav", "c.wav"],
    });
    check("中途落败：报「已发出 1/3 条」+ 第几条挂的 + 剩的可重发", () => {
      assert.strictEqual(els.statusText.textContent, "已发出 1/3 条（第 2 条失败），剩的可重发",
        "含糊成「结果：partial」的话，用户不知道前 1 条其实已经发出去了");
      assert.strictEqual(els.status.className, "show warn");
    });

    // 失败路径不能被分段字段带偏（坏消息要说得直接）
    await petFns.settleSendResult({ ok: false, error: "微信没开" });
    check("失败：仍走 err 色调与直白文案", () => {
      assert.strictEqual(els.statusText.textContent, "发送失败：微信没开");
      assert.strictEqual(els.status.className, "show err");
    });
  }

  console.log("===== " + (failures.length ? "FAIL" : "OK") + " =====");
  console.log(pass + " 通过, " + failures.length + " 失败");
  if (failures.length) {
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  process.exit(0);
})();

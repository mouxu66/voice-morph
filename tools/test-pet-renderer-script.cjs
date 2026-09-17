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

global.document = {
  getElementById: (id) => (els[id] = els[id] || mkEl(id)),
  createElement: () => mkEl("tmp"),
  querySelectorAll: () => [],
  addEventListener() {},
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
  },
  addEventListener() {},
};
global.location = { search: "" };
global.Image = class { set src(v) { loadedImages.push(v); } };
global.localStorage = { getItem: () => null, setItem() {} };
global.Audio = class { pause() {} play() { return Promise.resolve(); } };
global.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return 0; };   // 桩掉轮询定时器，进程才能自然退出
global.self = global;

// 后端桩：状态接口返回「什么都没跑」，皮肤接口返回内置芙宁娜
global.fetch = async (url) => {
  const u = String(url);
  // 离线模式：两个状态接口都失败 → 走 pet.html 自己的「后端离线」分支
  if (OFFLINE && (u.includes("/api/cascade/status") || u.includes("/api/rvc/live/status"))) {
    return { ok: false, status: 503, json: async () => ({}) };
  }
  let body = {};
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
// 显式挂一份。追加在末尾不影响上面按行号做的错误定位。
const EXPORT_HOOK = "\n;globalThis.__petFns = { setState, playGuide, endGuide, setBubbleVisible };\n";
let thrown = null;
try {
  (0, eval)(code + EXPORT_HOOK);
} catch (e) {
  thrown = e;
}
const petFns = globalThis.__petFns || {};

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
    // 正确做法是写它们内部的纯文本节点（#liveLabel / #sendLabel / #statusText / #pillText）。
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

  console.log("===== " + (failures.length ? "FAIL" : "OK") + " =====");
  console.log(pass + " 通过, " + failures.length + " 失败");
  if (failures.length) {
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  process.exit(0);
})();

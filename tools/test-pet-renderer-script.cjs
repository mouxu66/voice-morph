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
 */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

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
const htmlLines = html.split("\n");

// ---------------- 最小 DOM 桩（记录所有写入，供断言） ----------------
function mkEl(id) {
  const el = {
    id,
    style: {
      _props: {},
      setProperty(k, v) { this._props[k] = String(v); },
      removeProperty(k) { delete this._props[k]; },
    },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
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
  return el;
}
const els = {};
const petIpc = [];          // 渲染器下发给主进程的 IPC 调用记录
const loadedImages = [];    // new Image().src = ... 记录

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
  },
  addEventListener() {},
};
global.location = { search: "" };
global.Image = class { set src(v) { loadedImages.push(v); } };
global.localStorage = { getItem: () => null, setItem() {} };
global.Audio = class { pause() {} play() { return Promise.resolve(); } };
global.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
global.setInterval = () => 0;   // 桩掉轮询定时器，进程才能自然退出
global.self = global;

// 后端桩：状态接口返回「什么都没跑」，皮肤接口返回内置芙宁娜
global.fetch = async (url) => {
  const u = String(url);
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
let thrown = null;
try {
  (0, eval)(code);
} catch (e) {
  thrown = e;
}

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

  console.log("===== " + (failures.length ? "FAIL" : "OK") + " =====");
  console.log(pass + " 通过, " + failures.length + " 失败");
  if (failures.length) {
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  process.exit(0);
})();

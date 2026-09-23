#!/usr/bin/env node
/**
 * test-alt-hint-renderer.cjs —— 置顶引导横幅 alt-hint.html 内联脚本的守卫。
 *
 * 运行：node tools/test-alt-hint-renderer.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 背景（2026-09-23 重设计）：
 *   「微信发语音时屏幕上方那条黑框太丑」——旧版是 slate 蓝黑卡 + 2px 天蓝描边 +
 *   外发光 + 30~38px 大标题 + emoji 图标，视觉重量等同系统报错弹窗，而它只是
 *   十几秒的引导。重做成中性玻璃卡片：阶段色只落在 36px 图标底与 3px 进度条，
 *   主标题退回 16px，动作指令（「别动键鼠」）留在副文，图标换成内联 SVG。
 *
 *   这次改动**只碰渲染层**（alt-hint.html 是磁盘文件，安装版从
 *   resources/backend/web/electron/pet/ 直接读），所以窗口尺寸/位置/鼠标穿透
 *   全在 alt-hint.cjs 里没动 —— 本测试也不该去断言那些。
 *
 * 起不了真 Electron GUI（沙箱里 Chromium GPU 进程必崩），所以喂一套最小 DOM 桩
 * 跑**真实内联脚本**，再驱动各个阶段做行为断言。
 *
 * 覆盖的回归点：
 *   1. 内联脚本必须能从头执行到尾（顶层抛异常 = 整块横幅变成一张空白透明窗）
 *   2. 脚本引用的每个 id 都必须真在 HTML 里存在（漏一个就是静默失效）
 *   3. 四个阶段（prep/press/release/done）都要能渲染，且 className 写成 stage-x
 *      —— 用直接赋值而非 classList.add，才能保证旧阶段类不会残留
 *   4. 图标必须是内联 SVG，且不得再用系统 emoji
 *      —— emoji 随系统字体变形、质感廉价，且 Windows 与 macOS 长得完全不同
 *   5. 副文里的 `<b>Alt</b>` 要升级成键帽 `<kbd>Alt</kbd>`
 *      —— 主进程那侧还在用 <b> 标按键，在渲染层收口是为了「改长相不必重打 asar」
 *   6. 按住说话阶段要主动补一句「按 Esc 取消」，且已提到 Esc 时不重复补
 *      —— Esc 热键确实已注册（alt-hint.cjs armCancelKey），旧版从没说出口过
 *   7. 倒计时口径必须区分：press 是真递减（「还剩 Ns」），prep 只推一次不递减（「约 Ns」）
 *      —— prep 那侧永远显示同一个数字，写成「还剩」就是在骗人
 *   8. progress 三态：-1 → indeterminate 动画；0~1 → 宽度百分比；越界值要 clamp
 *   9. 窗口是 hide/show 复用而非重载，所以 visibilitychange 必须重播入场动画
 *  10. 亮色主题必须独立成块（prefers-color-scheme: light）
 *      —— 同一组透明度在深底是「提亮」、浅底是「加深」，合成一套值亮色就会糊
 *  11. 不得使用 backdrop-filter —— Windows 透明窗拿不到真模糊（糊不到下层窗口），
 *      只在部分 GPU 驱动上多一层黑底风险，收益为零
 */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/** 剥掉 JS 注释：静态断言必须先做这步，否则最先命中的是「解释为什么禁止」的注释本身。 */
function stripJsComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/[^\n]*/g, "$1");
}
/** 剥掉 CSS 块注释。 */
function stripCssComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "");
}

const HTML = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "..", "web", "electron", "pet", "alt-hint.html");

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

// ---------------- 载入并抽取内联脚本 / 样式 ----------------
const html = fs.readFileSync(HTML, "utf-8");
const scriptM = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptM, "alt-hint.html 里没找到内联 <script>");
const code = scriptM[1];
const codeStripped = stripJsComments(code);
const styleM = html.match(/<style>([\s\S]*?)<\/style>/);
assert.ok(styleM, "alt-hint.html 里没找到内联 <style>");
const css = stripCssComments(styleM[1]);

// ---------------- 最小 DOM 桩 ----------------
function mkEl(id) {
  const el = {
    id,
    _cls: new Set(),
    style: { _props: {} },
    addEventListener() {},
    removeEventListener() {},
    textContent: "",
    innerHTML: "",
    offsetWidth: 0,
  };
  Object.defineProperty(el, "className", {
    get() { return [...el._cls].join(" "); },
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  el.classList = {
    add(...c) { c.forEach((x) => el._cls.add(x)); },
    remove(...c) { c.forEach((x) => el._cls.delete(x)); },
    contains: (c) => el._cls.has(c),
  };
  return el;
}
const els = {};
const docListeners = {};
const doc = {
  hidden: false,
  addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  getElementById(id) { return (els[id] = els[id] || mkEl(id)); },
  // 桩也要能顶住 querySelector：旧版 alt-hint.html 用它取 `#bar > i`，
  // 缺了它会让「对照旧版本」的变异验证报出假 FAIL（失败原因是桩不兼容而非旧版有缺陷）。
  querySelector(sel) { return (els[sel] = els[sel] || mkEl(sel)); },
  querySelectorAll() { return []; },
};

let onHintCb = null;
const sandbox = { document: doc, console };
sandbox.window = {
  altHint: { onHint(cb) { onHintCb = cb; } },
};

// ---------------- 跑真实内联脚本 ----------------
let bootError = null;
try {
  vm.runInNewContext(code, vm.createContext(sandbox), { filename: "alt-hint.html<script>" });
} catch (e) {
  bootError = e;
}
/** 脚本必须已订阅；没订阅就说明在订阅之前的某句就抛了。 */
const push = (payload) => { onHintCb(payload); };
const el = (id) => els[id];

check("内联脚本能从头执行到尾", () => {
  assert.strictEqual(bootError, null, "顶层抛异常：" + (bootError && bootError.message));
});
check("脚本已订阅 alt-hint:update", () => {
  assert.strictEqual(typeof onHintCb, "function");
});

check("脚本引用的每个 id 都在 HTML 里存在", () => {
  const refs = [...codeStripped.matchAll(/getElementById\(\s*"([^"]+)"\s*\)/g)].map((m) => m[1]);
  assert.ok(refs.length >= 5, "getElementById 引用太少，脚本可能没跑起来");
  const missing = refs.filter((id) => !new RegExp('id="' + id + '"').test(html));
  assert.deepStrictEqual(missing, [], "HTML 里缺这些 id：" + missing.join(", "));
});

check("空 payload / 无 stage 的 payload 直接忽略，不写入", () => {
  const before = el("title").innerHTML;
  push(null);
  push({});
  assert.strictEqual(el("title").innerHTML, before);
});

check("prep：中性卡片 + 品牌紫阶段类 + 倒计时是「约」而非「还剩」", () => {
  push({ stage: "prep", sub: "正在准备变声，马上就好，请先切到微信聊天窗口", remainS: 3 });
  assert.strictEqual(el("card").className, "stage-prep");
  assert.ok(/<svg[\s\S]*<\/svg>/.test(el("iconBox").innerHTML), "图标必须是内联 SVG");
  assert.ok(el("sub").innerHTML.includes("正在准备变声"));
  assert.strictEqual(el("countdown").textContent, "约 3s");
  assert.strictEqual(el("countdown").style.display, "block");
});

check("prep + progress:-1 → 进度条走 indeterminate 不确定态", () => {
  push({ stage: "prep", sub: "合成 + 换声中…", remainS: null, progress: -1 });
  assert.ok(el("bar").classList.contains("indeterminate"), "应挂 indeterminate");
  assert.strictEqual(el("countdown").style.display, "none", "remainS 为 null 时不该显示倒计时");
});

check("press：标题里的 Alt 是键帽，副文剥掉与标题重复的「现在按住 Alt」", () => {
  push({ stage: "press", sub: "现在按住 <b>Alt</b>，对着麦克风说话，说完松开即发送", remainS: 12, progress: 0.2 });
  assert.strictEqual(el("card").className, "stage-press");
  assert.ok(el("title").innerHTML.includes("<kbd>Alt</kbd>"), "标题里的 Alt 应是键帽，实际：" + el("title").innerHTML);
  // 前缀剥掉才装得下单行；留着就会把「Esc 取消」挤到第二行
  assert.ok(!/现在按住/.test(el("sub").innerHTML), "副文不该重复标题的「现在按住」，实际：" + el("sub").innerHTML);
  assert.ok(el("sub").innerHTML.includes("对着麦克风说话"), "剥前缀不能连正文一起吃掉");
  assert.ok(/Esc/.test(el("sub").innerHTML), "应主动补一句 Esc 可取消");
  assert.strictEqual(el("countdown").textContent, "还剩 12s");
  assert.ok(!el("bar").classList.contains("indeterminate"), "确定进度时不应留 indeterminate");
  assert.strictEqual(el("bar").style.width, "20.0%");
});

check("press：副文里别处的 <b>Alt</b> 也升级成键帽", () => {
  push({ stage: "press", sub: "按住 <b>Alt</b> 说话", remainS: 5, progress: 0.1 });
  assert.ok(el("sub").innerHTML.includes("<kbd>Alt</kbd>"), "应升级成键帽，实际：" + el("sub").innerHTML);
  assert.ok(!/<b>\s*Alt\s*<\/b>/i.test(el("sub").innerHTML), "不该残留 <b>Alt</b>");
});

check("press：前缀只在真匹配时才剥，不误伤其它文案", () => {
  push({ stage: "press", sub: "现在按住 Ctrl 说话", remainS: 5, progress: 0.1 });
  assert.ok(el("sub").innerHTML.includes("现在按住 Ctrl"), "不是 Alt 就不该剥，实际：" + el("sub").innerHTML);
});

check("press：副文已提到 Esc 时不重复补", () => {
  push({ stage: "press", sub: "按住 <b>Alt</b> 说话（按 Esc 取消）", remainS: 9, progress: 0.1 });
  const hits = (el("sub").innerHTML.match(/Esc/g) || []).length;
  assert.strictEqual(hits, 1, "Esc 出现了 " + hits + " 次，应只 1 次");
});

check("press：副文里的「别动键鼠」这类普通 <b> 保留为强调", () => {
  push({ stage: "press", sub: "说话时<b>别动键鼠</b>", remainS: 5, progress: 0.5 });
  assert.ok(el("sub").innerHTML.includes("<b>别动键鼠</b>"), "普通 <b> 应原样保留");
});

check("release / done：走绿色阶段类，且旧阶段类被清掉", () => {
  push({ stage: "release", sub: "声卡会自动还原，可继续下一条", progress: 1 });
  assert.strictEqual(el("card").className, "stage-release", "className 应整体替换，不留 stage-press");
  push({ stage: "done", sub: "引导结束，可再按需重发", progress: 1 });
  assert.strictEqual(el("card").className, "stage-done");
  assert.strictEqual(el("bar").style.width, "100.0%");
});

check("未知 stage 回退到 prep，不产生 stage-undefined", () => {
  push({ stage: "wat", sub: "x" });
  assert.strictEqual(el("card").className, "stage-prep");
});

check("progress 越界值被 clamp 到 0~100%", () => {
  push({ stage: "prep", sub: "x", progress: 7 });
  assert.strictEqual(el("bar").style.width, "100.0%");
  push({ stage: "prep", sub: "x", progress: -0.5 });
  assert.strictEqual(el("bar").style.width, "0.0%");
});

check("sub 里的换行转成 <br>，空 sub 则隐藏该行", () => {
  push({ stage: "prep", sub: "第一行\n第二行" });
  assert.ok(el("sub").innerHTML.includes("<br>"), "换行应转 <br>");
  push({ stage: "prep", sub: "" });
  assert.strictEqual(el("sub").style.display, "none");
});

check("visibilitychange 重播入场动画（窗口是 hide/show 复用，不重载页面）", () => {
  const fns = docListeners.visibilitychange || [];
  assert.strictEqual(fns.length, 1, "应恰好注册一个 visibilitychange 监听");
  doc.hidden = true;
  fns[0]();                                     // 隐藏时不该动动画
  assert.strictEqual(el("wrap").style.animation, undefined);
  doc.hidden = false;
  fns[0]();                                     // 重新显示 → 先置 none 再清空，触发重播
  assert.strictEqual(el("wrap").style.animation, "");
});

// ---------------- 静态检查（CSS / 标记层面） ----------------
check("图标不再使用系统 emoji", () => {
  // 旧版的 🎤 / ✅ 是「不像这个产品」的一半原因，且跨系统长得完全不一样
  const emoji = html.match(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu);
  assert.strictEqual(emoji, null, "不应出现 emoji，实际：" + (emoji || []).join(""));
});

check("亮色主题独立成块（prefers-color-scheme: light）", () => {
  const i = css.indexOf("prefers-color-scheme: light");
  assert.ok(i >= 0, "缺少亮色主题块");
  const body = css.slice(i, css.indexOf("}", css.indexOf("{", i)) + 1);
  for (const token of ["--card-bg", "--t-strong", "--t-muted", "--st-brand"]) {
    assert.ok(body.includes(token), "亮色块应重新定义 " + token);
  }
});

check("尊重「减少动态」偏好", () => {
  assert.ok(css.includes("prefers-reduced-motion"), "缺少 prefers-reduced-motion 处理");
});

check("不使用 backdrop-filter（Windows 透明窗拿不到真模糊）", () => {
  assert.ok(!/backdrop-filter/i.test(css), "透明窗里 backdrop-filter 糊不到下层窗口，只会多一层风险");
});

check("进度条 3px、不抢视线", () => {
  assert.ok(/\.track\s*\{[^}]*height:\s*3px/.test(css), "进度条轨道应是 3px");
});

check("JS 写的每个类名在 CSS 里都有定义", () => {
  // stage-x 与 indeterminate 都是「JS 写、CSS 读」的契约，写错一个就是静默失效
  const declared = new Set([...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1]));
  const written = new Set([...codeStripped.matchAll(/"stage-"\s*\+\s*stage/g)].map(() => "stage-prep"));
  const stageClasses = ["stage-prep", "stage-press", "stage-release", "stage-done"];
  const missingStage = stageClasses.filter((c) => !declared.has(c));
  assert.deepStrictEqual(missingStage, [], "CSS 缺阶段类：" + missingStage.join(", "));
  assert.ok(declared.has("indeterminate"), "CSS 缺 .indeterminate");
  assert.strictEqual(written.size, 1, "应通过 \"stage-\" + stage 拼接，而不是散写类名");
});

// ---------------- 汇总 ----------------
console.log("");
if (failures.length) {
  console.log("FAILED " + failures.length + " / " + (pass + failures.length));
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log("PASS " + pass + " / " + pass);

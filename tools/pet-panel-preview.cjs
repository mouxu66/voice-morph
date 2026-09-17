#!/usr/bin/env node
/**
 * pet-panel-preview.cjs —— 把 pet.html 用真实 Chromium 渲染成 PNG，用来「看」面板长什么样。
 *
 * 为什么需要它：桌宠的 Electron GUI 在本机沙箱里起不来（Chromium GPU 进程必崩，
 * web/electron/smoke-pet-render.cjs 的文件头也记了这条），所以改完面板样式没法直接看。
 * 但 Playwright 缓存里的 chromium-headless-shell 是标准 Chromium，支持 --screenshot，
 * 于是可以离线把 pet.html 渲染出来做视觉验收 —— 不必让用户反复重启桌面端来当我的眼睛。
 *
 * 做法：
 *   1. 把 pet.html 与 svg/ 复制到临时目录（保持 svg/*.webp 的相对路径）
 *   2. 在主脚本【之前】注入 fetch 桩，喂假的音色/历史/状态数据（后端没起也能出真实内容）
 *   3. 在主脚本【之后】注入一小段，强制展开面板并写入状态行文案
 *   4. headless shell 截图（2x 缩放，便于看清 1px 边框与圆角）
 *
 * 用法：
 *   node tools/pet-panel-preview.cjs                  # 出全部场景到 outputs/pet-preview/
 *   node tools/pet-panel-preview.cjs --scene ok-dark  # 只出一个
 *   VM_CHROME=/path/to/chrome.exe node tools/...      # 手动指定 Chromium
 *
 * 场景命名：<state>-<theme>，state ∈ ok|err|live|guide|busy，theme ∈ dark|light
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");

const ROOT = path.join(__dirname, "..");
const PET_DIR = path.join(ROOT, "web", "electron", "pet");
const OUT_DIR = path.join(ROOT, "outputs", "pet-preview");
const SCENES = ["ok-dark", "ok-light", "offline-dark", "live-dark", "think-dark", "guide-dark", "busy-dark"];

/** 找 Playwright 缓存里的 chromium-headless-shell。 */
function findChrome() {
  if (process.env.VM_CHROME && fs.existsSync(process.env.VM_CHROME)) return process.env.VM_CHROME;
  const base = path.join(os.homedir(), "AppData", "Local", "ms-playwright");
  if (!fs.existsSync(base)) return null;
  const hits = fs.readdirSync(base)
    .filter((d) => d.startsWith("chromium_headless_shell-"))
    .sort()
    .map((d) => path.join(base, d, "chrome-headless-shell-win64", "chrome-headless-shell.exe"))
    .filter((p) => fs.existsSync(p));
  return hits.length ? hits[hits.length - 1] : null;
}

/**
 * 主脚本之前注入：① 假的 preload 桥 ② 假后端。
 *
 * ① 是必需的，不是可选的：普通浏览器里 HTML 的 `id="pet"` 会通过「具名元素访问」
 *    让 window.pet 指向那个 <div>，于是脚本末尾 `window.pet && window.pet.setIgnoreMouse(true)`
 *    真值判断通过、调用却抛 TypeError，**整个脚本在这里断掉** —— tick() 不会执行，
 *    表现为精灵图不渲染、气泡不出现、状态药丸停在初始值。
 *    Electron 里 contextBridge 先定义了 window.pet（真桥），所以线上不会这样。
 *    预览台不模拟 preload 就会渲染出一张「坏掉」的图，白排查一场。
 *
 * ② 假后端：后端没起也要能出「有内容」的面板（否则 select 空、recent 只有占位）。
 */
function preScript(state) {
  return `<script>
(function () {
  window.pet = {
    setIgnoreMouse: function () {}, dragStart: function () {}, dragMove: function () {},
    dragEnd: function () {}, onGuide: function () {}, onPreviewResult: function () {},
    onSendResult: function () {}, sendLast: function () {}, sendText: function () {},
    sendWav: function () {}, previewText: function () {}, liveToggle: function () {},
    showBackendLog: function () { return Promise.resolve(); }
  };
  const SCEN = ${JSON.stringify(state)};
  const FAKE = {
    "/api/voices": { voices: [
      { id: "furina",   display_name: "芙宁娜",         has_reference: true },
      { id: "kangaroo", display_name: "袋鼠骑士",       has_reference: true },
      { id: "noref",    display_name: "无参考(应被过滤)", has_reference: false }
    ] },
    "/api/wechat/history": { items: [
      { wav: "w1.wav", ts: Math.floor(Date.now() / 1000) - 25,    duration_s: 3.2 },
      { wav: "w2.wav", ts: Math.floor(Date.now() / 1000) - 420,   duration_s: 5.1 },
      { wav: "w3.wav", ts: Math.floor(Date.now() / 1000) - 90000, duration_s: 2.4 }
    ] },
    "/api/health": { ok: true }
  };
  window.fetch = async (url) => {
    const u = String(url);
    // offline 场景：两个状态接口都失败 → 走 pet.html 自己的「后端离线」分支
    // （药丸/精灵图/气泡全由产品代码接管，不在预览台里 setPill 硬塞 ——
    //   硬塞会被 tick() 覆盖，之前 err 场景截出来药丸还是「待机」就是这么来的）
    if (SCEN === "offline" && (u.includes("/api/cascade/status") || u.includes("/api/rvc/live/status"))) {
      return { ok: false, status: 503, json: async () => ({}) };
    }
    // 默认「什么都没跑」= 待机：面板本身才是这几张图的主角，
    // 气泡/精灵图的状态另由 live / think / guide 三个场景覆盖。
    if (u.includes("/api/cascade/status")) {
      return { ok: true, status: 200, json: async () => ({ running: false }) };
    }
    if (u.includes("/api/rvc/live/status")) {
      return { ok: true, status: 200, json: async () => ({ live_running: SCEN === "live" }) };
    }
    for (const k of Object.keys(FAKE)) {
      if (u.includes(k)) return { ok: true, status: 200, json: async () => FAKE[k] };
    }
    return { ok: false, status: 503, json: async () => ({}) };
  };
})();
</script>`;
}

/** 主脚本之后注入：展开面板 + 按场景写状态行/药丸。 */
function postScript(state) {
  return `<script>
window.addEventListener("load", function () {
  var SCEN = ${JSON.stringify(state)};
  showPanel();
  if (SCEN === "ok")   setStatus("已合成 3.2s，试听中…满意就点「发送试听」", "ok");
  if (SCEN === "offline") setStatus("后端没起来，先开主程序", "err");
  if (SCEN === "live") setStatus("变声已开启，微信把 CABLE Output 当麦克风", "ok");
  if (SCEN === "busy") { setStatus("合成中… 完成后自动发到微信，全程别动键鼠", "ok");
                         setBusy(sendBtn, true); }
  if (SCEN === "think") { setState("think", "「你好呀」");
                          setStatus("听懂啦，正在合成…", ""); }
  if (SCEN === "guide") playGuide({ title: "音色工坊",
    lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
    action: "build", motion: "pop", duration: 60000 });
});
</script>`;
}

/**
 * 量测模式：把关键布局数字打到 console，由 --enable-logging=stderr 带出来。
 * 为什么需要：204px 宽度是硬预算，「按钮文字被切」这种事肉眼只能猜，
 * scrollWidth > clientWidth 才是证据。
 */
function measureScript(state) {
  return `<script>
window.addEventListener("load", function () {
  var SCEN = ${JSON.stringify(state)};
  showPanel();
  if (SCEN === "busy") setBusy(sendBtn, true);
  if (SCEN === "offline") setStatus("后端没起来，先开主程序", "err");
  if (SCEN === "ok")   setStatus("已合成 3.2s，试听中…满意就点「发送试听」", "ok");
  if (SCEN === "think") { setState("think", "「你好呀」"); setStatus("听懂啦，正在合成…", ""); }
  if (SCEN === "guide") playGuide({ title: "音色工坊",
    lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
    action: "build", motion: "pop", duration: 60000 });
  setTimeout(function () {
    var R = function (el) { var r = el.getBoundingClientRect();
      return { w: +r.width.toFixed(1), h: +r.height.toFixed(1), top: +r.top.toFixed(1) }; };
    var steps = document.getElementById("steps");
    var M = {
      scen: SCEN,
      theme: getComputedStyle(document.documentElement).getPropertyValue("--c-surface").trim(),
      panelBg: getComputedStyle(panel).backgroundColor,
      panelOpacity: getComputedStyle(panel).opacity,
      win: { w: innerWidth, h: innerHeight },
      rootScrollH: document.getElementById("root").scrollHeight,
      panel: R(panel),
      panelOverflow: panel.scrollHeight > panel.clientHeight,
      pill: { txt: pillText.textContent, w: R(pillEl).w, cls: pillEl.className },
      bubbleVisible: getComputedStyle(bubble).display !== "none",
      bubble: bubble.offsetHeight,
      petTop: Math.round(petBox.getBoundingClientRect().top),
      petH: petBox.offsetHeight,
      spriteBg: (sprite.style.backgroundImage || "").replace(/^url\\("?|"?\\)$/g, "").split("/").pop(),
      voiceSel: voiceSel.options[voiceSel.selectedIndex] ? voiceSel.options[voiceSel.selectedIndex].text : null,
      recentChips: recentEl.querySelectorAll(".hitem").length,
      statusCls: statusEl.className,
      statusText: statusText.textContent.slice(0, 24),
      stepsDots: steps ? steps.children.length : 0,
      stepsOnIdx: steps ? Array.prototype.findIndex.call(steps.children, function (el) {
        return el.className.indexOf("on") >= 0; }) : -1,
      buttons: [["send", sendBtn], ["preview", previewBtn], ["live", liveBtn]].map(function (p) {
        var b = p[1];
        return { id: p[0], w: R(b).w, scrollW: b.scrollWidth, clientW: b.clientWidth,
                 clipped: b.scrollWidth > b.clientWidth + 1, txt: b.textContent.trim() };
      }),
    };
    console.log("MEASURE " + JSON.stringify(M));
  }, 400);
});
</script>`;
}

/**
 * 注入桌面壁纸：桌宠浮在桌面上，面板的 backdrop-filter 必须对着东西才有意义。
 * 同时注入 <base>：让 pet.html 里的相对路径 svg/*.webp 直接解析到源目录，
 * 于是完全不必把 2.5MB 精灵图拷进临时目录 —— 本机沙箱里 fs.cpSync 拷这批文件
 * 会把 node 进程直接杀掉（无异常、无输出、退出码 127），这个坑踩过一次。
 */
function headInjection(theme) {
  const bg = theme === "light"
    ? "linear-gradient(140deg, #cfd8e6 0%, #e8edf5 45%, #b9c6d8 100%)"
    : "linear-gradient(140deg, #223047 0%, #131a26 45%, #2c2438 100%)";
  const base = "file:///" + PET_DIR.replace(/\\/g, "/") + "/";
  return `<base href="${base}">\n<style>html, body { background: ${bg} !important; }</style>`;
}

/**
 * 强制暗色主题。
 *
 * headless Chromium 的 prefers-color-scheme **恒为 light**（试过 --force-dark-mode、
 * --force-prefers-color-scheme 都不改），所以「dark 场景」如果不做处理，
 * 渲染出来的其实是亮色 —— 量测时 ok-dark 与 ok-light 的数字一模一样，就是这么来的。
 * 与其赌未公开的命令行开关，不如确定性地把亮色令牌块从样式里摘掉：
 * 剩下的 :root 暗色令牌就是唯一生效的一套。
 * （媒体查询「接线」本身由 tools/test-pet-renderer-script.cjs 的静态断言覆盖。）
 */
function forceDark(html) {
  const re = /@media\s*\(prefers-color-scheme:\s*light\)\s*\{/g;
  let out = html;
  for (;;) {
    re.lastIndex = 0;
    const mm = re.exec(out);
    if (!mm) break;
    const open = mm.index + mm[0].length - 1;
    let depth = 0, end = -1;
    for (let i = open; i < out.length; i += 1) {
      if (out[i] === "{") depth += 1;
      else if (out[i] === "}") { depth -= 1; if (depth === 0) { end = i; break; } }
    }
    if (end < 0) break;
    out = out.slice(0, mm.index) + out.slice(end + 1);
  }
  return out;
}

/**
 * 冻结动画。默认注入，两个原因：
 *   1. 截图会撞在入场动画中途（panel-in 的 scale(.97)/opacity 未收敛），出图发虚、
 *      量测也会量到 197.9 而不是 204 —— 这是纯粹的自欺。
 *   2. 各场景的 fetch 重试会消耗 --virtual-time-budget，动画停在哪一帧不确定。
 * 想看动效时用 --anim 关掉本注入。
 */
function freezeStyle() {
  return `<style>*,*::before,*::after{animation:none !important;transition:none !important}</style>`;
}

function buildPage(scene, mode, keepAnim) {
  const [state, theme] = scene.split("-");
  let html = fs.readFileSync(path.join(PET_DIR, "pet.html"), "utf-8");
  if (theme === "dark") html = forceDark(html);
  const head = headInjection(theme) + (keepAnim ? "" : "\n" + freezeStyle());
  html = html.replace("</head>", head + "\n</head>");
  const at = html.indexOf("<script>");
  html = html.slice(0, at) + preScript(state) + "\n" + html.slice(at);
  const tail = mode === "measure" ? measureScript(state) : postScript(state);
  html = html.replace("</body>", tail + "\n</body>");
  return html;
}

function main() {
  const i = process.argv.indexOf("--scene");
  const wanted = i > -1 ? [process.argv[i + 1]] : SCENES;
  const measure = process.argv.includes("--measure");
  const keepAnim = process.argv.includes("--anim");
  const chrome = findChrome();
  if (!chrome) {
    console.error("找不到 chromium-headless-shell。装 Playwright，或设 VM_CHROME 指向 chrome.exe。");
    process.exit(2);
  }
  console.log("chromium: " + chrome);
  console.log("mode: " + (measure ? "measure（只出数字，不存图）" : "screenshot"));

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pet-preview-"));
  fs.mkdirSync(OUT_DIR, { recursive: true });
  console.log("tmp: " + tmp);

  let ok = 0;
  for (const scene of wanted) {
    const file = path.join(tmp, "pet-" + scene + ".html");
    fs.writeFileSync(file, buildPage(scene, measure ? "measure" : "shot", keepAnim), "utf-8");
    const png = path.join(OUT_DIR, "panel-" + scene + ".png");
    if (fs.existsSync(png)) fs.rmSync(png);
    // 每次给独立 profile 目录：同一个 user-data-dir 被并发/连续复用会让 Chromium 抢锁失败
    const profile = path.join(tmp, "profile-" + scene);
    const args = [
      "--no-sandbox",              // 本机沙箱里 Chromium 再开自己的沙箱会起不来
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--user-data-dir=" + profile,
      "--hide-scrollbars",
      "--allow-file-access-from-files",
      "--force-device-scale-factor=2",
      "--window-size=220,480",
      "--virtual-time-budget=6000",
      "--enable-logging=stderr",
      "--log-level=0",
    ];
    if (measure) args.push("--screenshot=" + path.join(tmp, "_measure.png"));
    else args.push("--screenshot=" + png);
    args.push("file:///" + file.replace(/\\/g, "/"));

    const r = spawnSync(chrome, args, { encoding: "utf-8", timeout: 120000 });
    const err = String(r.stderr || "");
    if (measure) {
      const lines = err.split("\n").filter((l) => l.includes("MEASURE "));
      if (lines.length) {
        ok += 1;
        const json = lines[lines.length - 1].slice(lines[lines.length - 1].indexOf("MEASURE ") + 8);
        console.log("  " + scene + "  " + json.trim());
      } else {
        console.log("  FAIL " + scene + " 没拿到量测输出");
      }
    } else if (fs.existsSync(png)) {
      ok += 1;
      console.log("  ok  " + scene + " -> " + path.relative(ROOT, png) +
        "  (" + Math.round(fs.statSync(png).size / 1024) + " KB)");
    } else {
      console.log("  FAIL " + scene +
        "  status=" + r.status + " signal=" + r.signal +
        " error=" + (r.error && r.error.message));
      console.log("        " + err.split("\n").slice(-6).join("\n        "));
    }
  }
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log("\n" + ok + "/" + wanted.length + (measure ? " 个场景量测成功" : " 张 -> " + path.relative(ROOT, OUT_DIR)));
  if (!ok) process.exit(1);
}

if (require.main === module) main();

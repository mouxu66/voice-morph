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
 *   1. 用 `<base href="file:///…/web/electron/pet/">` 让 svg/*.webp 指回源目录
 *      （曾经是 fs.cpSync 拷一份，但本机沙箱里拷大目录会被静默杀掉：node 退出码 127、零输出）
 *   2. 在主脚本【之前】注入 fetch 桩，喂假的音色/历史/状态数据（后端没起也能出真实内容）
 *      —— 以及一个 no-op 的 window.pet 桥，否则普通浏览器里 `id="pet"` 的具名元素访问
 *      会让 `window.pet` 变成那个 div，页面脚本一调 setIgnoreMouse 就整段死掉
 *   3. 在主脚本【之后】注入一小段，强制展开面板并写入状态行文案
 *   4. headless shell 截图（2x 缩放，便于看清 1px 边框与圆角）或量测（--measure）
 *
 * 用法：
 *   node tools/pet-panel-preview.cjs                        # 出全部场景到 outputs/pet-preview/
 *   node tools/pet-panel-preview.cjs --scene ok-dark        # 只出一个
 *   node tools/pet-panel-preview.cjs --scene a --scene b    # 可重复，也支持 --scene a,b
 *   node tools/pet-panel-preview.cjs --measure              # 只打布局数字，不存图
 *   VM_CHROME=/path/to/chrome.exe node tools/...            # 手动指定 Chromium
 *   VM_PET_HTML=/tmp/patched.html node tools/... --measure  # 换一份 pet.html（变异测试）
 *
 * 场景命名：<state>-<theme>，state 只能是小写字母（qwenidle 而不是 qwen-idle ——
 * 带连字符会被 split("-") 切成两段，theme 拿到 "idle" 从而静默不套暗色）。
 *
 * 作为模块：`require("./pet-panel-preview.cjs").measureScenes(["guide-dark"])`，
 * 供 tools/test-pet-panel-layout.cjs 复用（同一个渲染路径，避免两套实现漂移）。
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");

const ROOT = path.join(__dirname, "..");
const PET_DIR = path.join(ROOT, "web", "electron", "pet");
const OUT_DIR = path.join(ROOT, "outputs", "pet-preview");
const SCENES = ["ok-dark", "ok-light", "offline-dark", "live-dark", "qwen-dark", "qwenidle-dark",
                "think-dark", "guide-dark", "busy-dark", "fx-dark", "fxpick-dark",
                "hold-dark", "holdrec-dark", "holdwarn-dark"];

/** 找 Playwright 缓存里的 chromium-headless-shell。找不到返回 null（调用方负责报错/跳过）。 */
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
    showBackendLog: function () { return Promise.resolve(); },
    // 按住说话（2026-09-29）：主进程与渲染进程之间的两条 IPC + 一条状态回传。
    // 预览台只留一个回传句柄 —— 「录制中」那张图靠它从真状态推出来（页面自己的
    // onMicState 回调把按钮文案/颜色/秒数全接管了），不是在截图脚本里硬塞文案。
    micDown: function () {}, micUp: function () {},
    onMicState: function (cb) { window.__petMicCb = cb; }
  };
  const SCEN = ${JSON.stringify(state)};
  const FAKE = {
    // selected 是 2026-09-29 加的：桌宠状态条要拿它反查「主界面选中」的真名字
    // （下拉默认项的 value 是空串，光看下拉不知道那是谁）。
    "/api/voices": { selected: "kangaroo", voices: [
      { id: "furina",   display_name: "芙宁娜",         has_reference: true },
      { id: "kangaroo", display_name: "袋鼠骑士",       has_reference: true },
      { id: "noref",    display_name: "无参考(应被过滤)", has_reference: false }
    ] },
    "/api/wechat/history": { items: [
      { wav: "w1.wav", ts: Math.floor(Date.now() / 1000) - 25,    duration_s: 3.2 },
      { wav: "w2.wav", ts: Math.floor(Date.now() / 1000) - 420,   duration_s: 5.1 },
      { wav: "w3.wav", ts: Math.floor(Date.now() / 1000) - 90000, duration_s: 2.4 }
    ] },
    "/api/health": { ok: true },
    // 音效声板（fx/fxpick 场景用）：9 个样本正好铺满 3 列 × 3 行网格，
    // 让 max-height 的内滚边界也被量到。
    "/api/soundboard/catalog": { items: [
      { id: "s1", name: "鼓掌",     icon: "👏", duration_s: 1.8, count: 3 },
      { id: "s2", name: "欢呼",     icon: "🎉", duration_s: 2.4, count: 2 },
      { id: "s3", name: "称号老搭", icon: "😂", duration_s: 3.1, count: 5 },
      { id: "s4", name: "惊讶",     icon: "😮", duration_s: 0.9, count: 1 },
      { id: "s5", name: "哀嚎",     icon: "😭", duration_s: 2.2, count: 4 },
      { id: "s6", name: "铃铛",     icon: "🔔", duration_s: 1.1, count: 2 },
      { id: "s7", name: "警报",     icon: "🚨", duration_s: 4.5, count: 1 },
      { id: "s8", name: "完结撒花", icon: "🌸", duration_s: 3.8, count: 6 },
      { id: "s9", name: "木鱼",     icon: "🪵", duration_s: 0.6, count: 9 }
    ] }
  };
  window.fetch = async (url, opts) => {
    const u = String(url);
    const method = String((opts && opts.method) || "GET").toUpperCase();
    // offline 场景：两个状态接口都失败 → 走 pet.html 自己的「后端离线」分支
    // （药丸/精灵图/气泡全由产品代码接管，不在预览台里 setPill 硬塞 ——
    //   硬塞会被 tick() 覆盖，之前 err 场景截出来药丸还是「待机」就是这么来的）
    if (SCEN === "offline" && (u.includes("/api/cascade/status") || u.includes("/api/rvc/live/status"))) {
      return { ok: false, status: 503, json: async () => ({}) };
    }
    // 启停请求（桌宠直接 POST 后端，见 pet.html 的 apiPost）：一律回 ok，
    // 预览台不需要真启停 —— 状态由下面的 status 桩决定。
    if (method === "POST") return { ok: true, status: 200, json: async () => ({ ok: true }) };
    // 两个引擎互斥，所以一次只会有一个 running=true：
    //   live  → RVC 实时在跑（分段里 RVC 亮呼吸点、药丸「RVC 变声中」）
    //   qwen  → 千问变声在跑（分段里千问亮呼吸点、药丸「千问变声中」）
    //   qwen-idle → 千问被选中但没启动（只高亮、不亮呼吸点；验证「选中 ≠ 已开启」）
    if (u.includes("/api/cascade/status")) {
      return { ok: true, status: 200,
        json: async () => (SCEN === "qwen"
          ? { running: true, stage: "capturing", last_text: "今天天气不错", queued_s: 0, child_error: "" }
          : { running: false }) };
    }
    if (u.includes("/api/rvc/live/status")) {
      // infer_ms/block_ms 是 2026-09-29 加的（后端 tail realtime_gui.log 实测）：
      // live 场景要让状态条上的耗时 chip 真的出现（那是全条最挤的形态：
      // 「RVC 变声中」+ 音色 + 耗时三块同时在位，宽度预算就靠它量）。
      return { ok: true, status: 200,
        json: async () => (SCEN === "live"
          ? { live_running: true, infer_ms: 82.4, infer_ms_p95: 130.2, infer_samples: 100, block_ms: 40 }
          : { live_running: false }) };
    }
    // 发送前预检（2026-09-29）：常态回「微信已打开」；holdwarn 场景回「注定失败」——
    // 那一条的文案最长（分因首句 + ⚠），专门用来量「文案变长会不会把面板顶高」：
    // 预检行钉死了单行 + 省略号，所以两个场景的余量必须**一样**。
    if (u.includes("/api/wechat/precheck")) {
      const warn = SCEN === "holdwarn";
      return { ok: true, status: 200, json: async () => ({
        ok: true, restart_mode: "0", restart_needed: false,
        reason: "VM_WECHAT_RESTART=0 已关闭重启",
        wechat_running: !warn,
        block_reason: warn
          ? "微信没有在运行：请先打开微信并登录、进入聊天窗口，再重试发送"
          : "",
      }) };
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
  if (SCEN === "live") setStatus("RVC 实时 已开启，微信把 CABLE Output 当麦克风", "ok");
  if (SCEN === "qwen") setStatus("千问变声 已开启，识别→合成→换嗓", "ok");
  // 只切选中、不启动：验证「选中 ≠ 已开启」（分段高亮但没有呼吸点）
  if (SCEN === "qwenidle") {
    setEngine("qwen");
    setStatus("引擎已切到「千问变声」，再点一下它启动", "");
  }
  if (SCEN === "busy") { setStatus("合成中… 完成后自动发到微信，全程别动键鼠", "ok");
                         setBusy(sendBtn, true); }
  if (SCEN === "think") { setState("think", "「你好呀」");
                          setStatus("听懂啦，正在合成…", ""); }
  // 音效页：实时播放模式（点格子立即出声）
  if (SCEN === "fx") { setTab("fx"); setStatus("点一个音效立即播放（再点一下停）", ""); }
  // 音效页 + 预混勾选态：这是「音效页最挤」的形态 —— 网格满 + 队列 2 颗 chip，
  // 布局门禁的 fxpick-dark 钉的就是它（网格/队列 max-height 的边界都在场）。
  if (SCEN === "fxpick") {
    setTab("fx");
    sfxMode = "premix"; refreshFxModeSeg();
    sfxPicks.push({ id: "s1", name: "鼓掌", icon: "👏", mode: "layer", at_s: 0 },
                  { id: "s2", name: "欢呼", icon: "🎉", mode: "append", at_s: 0 });
    renderFxGrid(); renderFxPicks(); refreshFxActions();
    setStatus("已勾 2 个音效，点「发送」混进最近一条语音", "ok");
  }
  if (SCEN === "guide") playGuide({ title: "音色工坊",
    lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
    action: "build", motion: "pop", duration: 60000 });
  // ⚠ 这里和 measureScript 里各有一份同样的切页条件（截图模式不等 setTimeout，
  //   量测模式要等布局稳定）—— 加新场景时**两处都要改**，只改一处的话：
  //   量测数字对、截出来的图是另一页（2026-09-29 加 holdwarn 时真踩过）。
  if (SCEN === "hold" || SCEN === "holdrec" || SCEN === "holdwarn") setTab("hold");
  if (SCEN === "holdrec" && window.__petMicCb) {
    window.__petMicCb({ phase: "recording", startedAt: Date.now() - 2300, maxSeconds: 59 });
  }
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
  var SWEEP = !!(SCEN && SCEN.sweep === true);   // 预算扫描模式：把所有页 × 两种气泡量一遍
  showPanel();
  if (SCEN === "busy") setBusy(sendBtn, true);
  if (SCEN === "offline") setStatus("后端没起来，先开主程序", "err");
  if (SCEN === "ok")   setStatus("已合成 3.2s，试听中…满意就点「发送试听」", "ok");
  if (SCEN === "live") setStatus("RVC 实时 已开启，微信把 CABLE Output 当麦克风", "ok");
  if (SCEN === "qwen") setStatus("千问变声 已开启，识别→合成→换嗓", "ok");
  if (SCEN === "qwenidle") { setEngine("qwen"); setStatus("引擎已切到「千问变声」，再点一下它启动", ""); }
  if (SCEN === "think") { setState("think", "「你好呀」"); setStatus("听懂啦，正在合成…", ""); }
  if (SCEN === "fx") { setTab("fx"); setStatus("点一个音效立即播放（再点一下停）", ""); }
  if (SCEN === "fxpick") {
    setTab("fx");
    sfxMode = "premix"; refreshFxModeSeg();
    sfxPicks.push({ id: "s1", name: "鼓掌", icon: "👏", mode: "layer", at_s: 0 },
                  { id: "s2", name: "欢呼", icon: "🎉", mode: "append", at_s: 0 });
    renderFxGrid(); renderFxPicks(); refreshFxActions();
    setStatus("已勾 2 个音效，点「发送」混进最近一条语音", "ok");
  }
  if (SCEN === "guide") playGuide({ title: "音色工坊",
    lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
    action: "build", motion: "pop", duration: 60000 });
  // 预算扫描的代表状态：与 ok 场景同一段文案 —— 余量是**跟基线比**的，
  // 所以每次扫描里的文案必须固定，否则量到的是文案长度差而不是布局差。
  if (SWEEP) setStatus("已合成 3.2s，试听中…满意就点「发送试听」", "ok");
  setTimeout(function () {
    // 切页必须放在这个 timeout 里：页面的 bootstrap 是 async（await 能力清单），
    // 它结尾还会调一次 setTab(activeTab, false)；在这里切才能保证量到的是目标页。
    if (SCEN === "hold" || SCEN === "holdrec" || SCEN === "holdwarn") setTab("hold");
    if (SCEN === "holdrec" && window.__petMicCb) {
      window.__petMicCb({ phase: "recording", startedAt: Date.now() - 2300, maxSeconds: 59 });
    }
    var R = function (el) { var r = el.getBoundingClientRect();
      return { w: +r.width.toFixed(1), h: +r.height.toFixed(1), top: +r.top.toFixed(1) }; };
    /**
     * 「文字有没有被裁」的共用量法（按钮与状态条 chip 都用它）。
     * 判据不能用 scrollWidth > clientWidth —— 对带 overflow:hidden 的元素，
     * scrollWidth 被钳到 clientWidth，溢出再多也报「正好」；而 overflow:hidden
     * 恰恰是 ellipsis 生效的前提，所以那个判据对**所有会省略号的元素**都是瞎的。
     * 改用 Range.getClientRects() 取文字的自然宽度：Range 给的是**布局矩形**，
     * 裁剪只发生在绘制阶段，不影响它；再跟内容盒宽度（clientWidth 去掉左右 padding）比。
     */
    var clip = function (name, b) {
      // display:none 的不测：clientWidth=0 而 Range 仍量得出自然宽，必假红
      // （说话页场景里 fxMode 两颗按钮、fx 场景里 preview / 整个 holdPane 都是不在场的）。
      if (!b || b.offsetParent === null) return null;
      var cs2 = getComputedStyle(b);
      var padL = parseFloat(cs2.paddingLeft) || 0;
      var padR = parseFloat(cs2.paddingRight) || 0;
      var avail = b.clientWidth - padL - padR;
      var natural = 0;
      try {
        var rng = document.createRange();
        rng.selectNodeContents(b);
        var rects = rng.getClientRects();
        for (var ri = 0; ri < rects.length; ri++) {
          if (rects[ri].width > natural) natural = rects[ri].width;
        }
      } catch (e) { natural = -1; }
      return { id: name, w: R(b).w, scrollW: b.scrollWidth, clientW: b.clientWidth,
               textW: Math.round(natural * 100) / 100, availW: Math.round(avail * 100) / 100,
               clipped: natural > avail + 1, txt: b.textContent.trim() };
    };
    // ---------- 纵向预算：快照工具（2026-09-29）----------
    // 为什么要 budget：面板纵向预算是**死的**（max-height 300px），而只看
    // 「装不装得下」（scrollH > clientH）时，新增一个控件可以把余量吃到 0
    // 却依然「不溢出」—— 下一个控件才炸，而那时没人知道是谁吃掉的。
    // 所以把**余量本身**当不变量（基线见 tools/pet-panel-layout-budget.json）。
    /** 控件的稳定名字（报告要能指名道姓）：优先 id，其次 tag.class。 */
    var ctlName = function (el) {
      if (el.id) return el.id;
      var cls = (el.className || "").toString().trim().split(/\\s+/)[0] || "";
      return el.tagName.toLowerCase() + (cls ? "." + cls : "");
    };
    /**
     * 自动枚举面板里所有**会显示**的可交互控件，逐个量文字有没有被裁。
     * 为什么要自动枚举而不是维护一张清单：手写清单必然漏 —— 2026-09-29 之前
     * 「按住说话」那颗按钮与状态条三个 chip 都是靠人记得才进来的；漏掉的那个被挤窄时
     * 只会安静地变成「…」，没有任何信号。
     * 只收 button/input：select 元素的 Range 量不出有意义的自然宽（option 不参与布局），
     * 量它只会得到 0 而假装「没被裁」。
     */
    var controlsOf = function () {
      var out = [];
      Array.prototype.forEach.call(panel.querySelectorAll("button, input"), function (el) {
        if (!el || el.offsetParent === null) return;  // display:none / 隐藏页里的不量（会假红）
        var r = clip(ctlName(el), el);
        if (r) out.push(r);
      });
      // 状态条那三个 chip 是 <span>，控件名单盖不到 —— 显式补上（它们是最挤的一行）
      [["pill", pillEl], ["rmVoice", rmVoiceEl], ["rmLat", rmLatEl]].forEach(function (p) {
        var r = clip(p[0], p[1]);
        if (r) out.push(r);
      });
      return out;
    };
    /** 功能页页签（从 DOM 枚举 —— 加第四页时不需要有人回来改这里）。 */
    var tabsInDom = function () {
      var row = document.getElementById("tabRow");
      if (!row) return [];
      return Array.prototype.map.call(row.querySelectorAll("button[data-tab]"), function (b) {
        return b.dataset.tab;
      });
    };
    var activeTabName = function () {
      var row = document.getElementById("tabRow");
      if (!row) return null;
      var on = Array.prototype.filter.call(row.querySelectorAll("button[data-tab]"), function (b) {
        return b.classList.contains("on") || b.getAttribute("aria-selected") === "true";
      });
      return on.length ? on[0].dataset.tab : null;
    };
    /**
     * 面板**最多能拿到多高**：临时把它顶到 1000px，看布局与 max-height 把它钳到哪里，
     * 马上恢复。为什么要这么量（而不是用公式算）：
     *   · 面板是 height:auto + max-height:300px，内容不超时 clientHeight 就等于内容高，
     *     所以「可用高」根本无法从正常状态读出来（这也正是以前把“余量”误当成
     *     clientHeight - scrollHeight 的原因：它恒为 0，只在被切时才是负数）；
     *   · 公式算（480 - 宠物 - 气泡 - 间距）看着简单，但它把 flex 的实际行为复制了一份，
     *     改一处 CSS 就会静默漂。实测不会有这个问题。
     * ⚠️ 它**会临时改变布局**，所以调用方必须在读完 scrollHeight 之后再调它，
     * 而且之后不能拿这次快照里的其它几何值当准。
     */
    var capacityOf = function () {
      var prevH = panel.style.height;
      panel.style.height = "1000px";
      var cap = panel.clientHeight;
      panel.style.height = prevH;
      return cap;
    };
    /**
     * 当前**可见功能页**的直接子块（带 pane/ 前缀）。
     * 为什么不能只看面板的直接子块：面板的子块是「页签/动作行/状态行」这些容器，
     * 新加一个控件时它们只会“整块长高” —— 报告只会说「speakPane 114→122px」，
     * 而这并不能告诉你是**哪个控件**吃掉的。往下多量一层，报告才能指名。
     * 只取一层（:scope > *）：再深就是布局细节，基线会碎得没法维护。
     */
    var paneChildren = function () {
      var pane = document.querySelector("#panel .pane.on");
      return pane ? pane.querySelectorAll(":scope > *") : [];
    };
    /**
     * 把一批元素量成 [{id,h,disp,mb}]。没有 id 的用 class 链/tag 命名，**重名自动加 #n**。
     * 重名那一条不能省：说话页里三个无 id 的 label 都叫 class="field"，
     * 若都以 “pane/field” 为键，后一个会把前一个盖掉 —— 基线里就只剩最后一个，
     * 前两个改矮/改高都看不见（那种“基线自己丢了证据”是最难查的）。
     */
    var blockList = function (els, prefix) {
      var seen = {};
      return Array.prototype.map.call(els, function (el) {
        var cls = (el.className || "").toString().trim().split(/\\s+/).filter(Boolean).join(".");
        var base = prefix + (el.id || cls || el.tagName.toLowerCase());
        seen[base] = (seen[base] || 0) + 1;
        var cs = getComputedStyle(el);
        return { id: seen[base] > 1 ? base + "#" + seen[base] : base,
                 h: el.offsetHeight, disp: cs.display, mb: cs.marginBottom };
      });
    };
    /** 面板里所有带 id 的元素（含隐藏页里的）—— 用于「基线里的 id 还在不在」盘点。 */
    var paneIds = function () {
      return Array.prototype.map.call(panel.querySelectorAll("[id]"), function (e) { return e.id; });
    };
    var steps = document.getElementById("steps");
    /**
     * 一次布局快照。**所有**断言都读它 —— 场景模式与预算扫描模式共用同一个函数，
     * 否则两边各有一份「什么算量到了」，迟早漂成两套口径。
     */
    var snap = function (scen) {
      return {
      scen: scen,
      theme: getComputedStyle(document.documentElement).getPropertyValue("--c-surface").trim(),
      panelBg: getComputedStyle(panel).backgroundColor,
      panelOpacity: getComputedStyle(panel).opacity,
      win: { w: innerWidth, h: innerHeight },
      rootScrollH: document.getElementById("root").scrollHeight,
      panel: R(panel),
      panelOverflow: panel.scrollHeight > panel.clientHeight,
      // 面板被 flex 压扁时，光看 panelOverflow 只知道「在滚」，不知道「差多少、谁占的」。
      // 把 scrollHeight / clientHeight 与每个直接子块的高度都打出来，才能定出该精简谁。
      panelScrollH: panel.scrollHeight,
      panelClientH: panel.clientHeight,
      blocks: blockList(panel.children, "")
        .concat(blockList(paneChildren(), "pane/")),   // 可见页的直接子块，带 pane/ 前缀（见上面注释）
      pill: { txt: pillText.textContent, w: R(pillEl).w, cls: pillEl.className },
      // 引擎分段的状态：选中（.on）与运行中（.running）必须分开 ——
      // 把「选中」当「已开启」是这次要防的核心错误。
      engine: {
        sel: engine,
        running: runningEngine,
        on: [engRvcBtn, engQwenBtn].filter(function (b) { return b && b.classList.contains("on"); })
              .map(function (b) { return b.dataset.eng; }),
        runningMark: [engRvcBtn, engQwenBtn].filter(function (b) { return b && b.classList.contains("running"); })
              .map(function (b) { return b.dataset.eng; }),
        // 2026-09-26「开 RVC」按钮已并入引擎分段（点已选中项 = 启停），liveLabel 不复存在。
      },
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
      // 引擎分段的两个按钮也要进这个列表：它们有 text-overflow: ellipsis，
      // 被挤窄时会静默变成「RVC 实…」。
      //
      // 判据不能用 scrollWidth > clientWidth —— 对带 overflow:hidden 的元素，
      // scrollWidth 被钳到 clientWidth，溢出再多也报「正好」。而 overflow:hidden
      // 恰恰是 ellipsis 生效的前提，所以这个判据对**所有会省略号的按钮**都是瞎的。
      // 改用 Range.getClientRects() 取文字的自然宽度：Range 给的是**布局矩形**，
      // 裁剪只发生在绘制阶段，不影响它；再跟内容盒宽度（clientWidth 去掉左右 padding）比。
      buttons: [["send", sendBtn], ["preview", previewBtn],
                ["engRvc", engRvcBtn], ["engQwen", engQwenBtn],
                ["tabSpeak", tabSpeakBtn], ["tabHold", tabHoldBtn], ["tabFx", tabFxBtn],
                ["holdMic", holdMicEl],
                ["fxModeLive", fxModeLiveBtn], ["fxModePremix", fxModePremixBtn]].map(function (p) {
        return clip(p[0], p[1]);
      }).filter(Boolean),
      // 状态条（2026-09-29）的三个 chip 走同一套判据。
      // 必须量它们：头部那一行现在是「药丸(引擎名) + 音色 + 耗时」三块挤 184px，
      // 是全面板最挤的地方之一，而 chip 是 <span> —— 上面那套“按钮”清单永远盖不到，
      // 一旦超宽只会安静地变成省略号（或把右边的 chip 顶出窗口）。
      strip: [["pill", pillEl], ["rmVoice", rmVoiceEl], ["rmLat", rmLatEl]].map(function (p) {
        return clip(p[0], p[1]);
      }).filter(Boolean),
      // 自动枚举的控件（替代手写 buttons 清单，见 controlsOf 的注释）
      controls: controlsOf(),
      // —— 纵向预算台账（2026-09-29）——
      // need = 内容需要多高；capacity = 布局最多给多高；reserve = 两者之差。
      // ★ 不变量是 reserve：**只能变多，不能变少** —— 这才是「新增控件吃掉了余量」
      //   的直接信号（只看溢出的话，余量被吃到 0 也算“没问题”，下一个人再动手就爆）。
      budget: (function () {
        var need = panel.scrollHeight;    // 必须在 capacityOf() 前读（它会临时改布局）
        var clientH = panel.clientHeight;
        var cap = capacityOf();
        return {
          maxH: getComputedStyle(panel).maxHeight,   // CSS 上的上限（300px）
          clientH: clientH,
          scrollH: need,
          capacity: cap,
          reserve: cap - need,                       // ★ 余量（负数 = 底部被切）
          slack: clientH - need,                     // 兼容旧字段：被切多少（健康时恒为 0）
        };
      })(),
      tab: activeTabName(),
      tabs: tabsInDom(),
      paneIds: paneIds(),
      };
    };
    var emit = function (scen) { console.log("MEASURE " + JSON.stringify(snap(scen))); };
    // ---------- 预算扫描（sweep）：一次页面加载，把所有页 × 两种气泡都量一遍 ----------
    // 为什么要扫「所有页」而不是继续手写场景：手写的场景清单漏了某一页时，
    // 那一页可以无限长而门禁看不见。页签从 DOM 枚举（tabsInDom），所以加第四页
    // 会自动被扫到 —— 若它的余量变少/溢出，门禁当场红。
    // 两种气泡：气泡在屏时面板被压扁（#root.compact 让位），这是另一个约束，
    // 而且每页的最挤形态不同（按住页那颗 46px 按钮就在按住页里）。
    if (SWEEP) {
      // 阶段 0 强制「无气泡」：不赌 setStatus 恰好没弹气泡（那会让两个阶段量到同一件事）
      setBubbleVisible(false);
      var tabs = tabsInDom();
      var phase = 0;   // 0 = 无气泡，1 = 大头气泡（guide 82px，最挤）
      var ti = 0;
      var next = function () {
        if (phase === 0 && ti >= tabs.length) {
          phase = 1;
          ti = 0;
          // 用与 guide 场景同一段文案：气泡高固定，两次测量才可比
          playGuide({ title: "音色工坊", lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
            action: "build", motion: "pop", duration: 60000 });
          setTimeout(next, 260);
          return;
        }
        if (ti >= tabs.length) {
          console.log("SWEEP-DONE " + tabs.length + " 页 × 2 种气泡");
          return;
        }
        setTab(tabs[ti], false);
        setTimeout(function () {
          emit("sweep:" + tabs[ti] + ":bubble" + phase);
          ti += 1;
          next();
        }, 80);
      };
      next();
      return;
    }
    emit(SCEN);
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
  // 场景名必须严格是 <state>-<theme>。加这条校验是因为真踩过：
  // 取名叫 `qwen-idle-dark`，split("-") 得到 state="qwen"、theme="idle" →
  // theme 判不出 "dark" → forceDark() 不生效 → 静默出成亮色图，
  // 而量测里的 theme 字段又只打令牌值，看起来一切正常。宁可当场报错。
  const [state, theme] = scene.split("-");
  if (!/^[a-z]+$/.test(state) || (theme !== "dark" && theme !== "light")) {
    throw new Error("场景名必须形如 <state>-<dark|light>（state 只能是小写字母、不能含连字符），收到："
      + JSON.stringify(scene));
  }
  // VM_PET_HTML 指向别处的 pet.html：给变异测试用（把按钮改窄、确认 clipped 判据真的会报警），
  // 这样不必去动仓库里的真文件，也就不存在「测完忘了改回来」的风险。
  const htmlPath = process.env.VM_PET_HTML || path.join(PET_DIR, "pet.html");
  let html = fs.readFileSync(htmlPath, "utf-8");
  if (theme === "dark") html = forceDark(html);
  const head = headInjection(theme) + (keepAnim ? "" : "\n" + freezeStyle());
  html = html.replace("</head>", head + "\n</head>");
  const at = html.indexOf("<script>");
  html = html.slice(0, at) + preScript(state) + "\n" + html.slice(at);
  // sweep：一次页面加载把所有功能页 × 两种气泡都量一遍（预算扫描，见 sweep 那段注释）。
  // 状态传对象而不是字符串，页面里所有 `SCEN === "xxx"` 分支自然全不命中 ——
  // 不用为它另写一套 setup。
  const tail = mode === "measure" ? measureScript(state)
    : mode === "sweep" ? measureScript({ sweep: true })
    : postScript(state);
  html = html.replace("</body>", tail + "\n</body>");
  return html;
}

/**
 * 从 Chromium 的 CONSOLE 日志行里抠出 MEASURE 后面的 JSON 对象。
 *
 * 不能简单 `slice(indexOf("MEASURE ") + 8)` —— 那行实际长这样：
 *   [0917/191657.455:INFO:CONSOLE:1366] "MEASURE {...}", source: file:///… (1366)
 * 尾巴上的 `", source: …` 会让 JSON.parse 直接抛。所以做花括号配对扫描，
 * 并且要跳过字符串内部的花括号（文案里将来出现 `{}` 也不会误判）。
 */
function extractMeasureJson(line) {
  const at = line.indexOf("MEASURE ");
  if (at < 0) return null;
  const start = line.indexOf("{", at);
  if (start < 0) return null;
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < line.length; i += 1) {
    const c = line[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') { inStr = true; continue; }
    if (c === "{") depth += 1;
    else if (c === "}") {
      depth -= 1;
      if (depth === 0) {
        try { return JSON.parse(line.slice(start, i + 1)); } catch (_) { return null; }
      }
    }
  }
  return null;
}

/**
 * 渲染一个场景。
 * @returns {{ok:boolean, json:object|null, png:string, status:number|null, signal:string|null, err:string}}
 *   mode="measure" 时 json 为页面里 MEASURE 那行解析出来的对象。
 */
function runScene(scene, opts) {
  const mode = opts.mode || "shot";
  const chrome = opts.chrome || findChrome();
  const tmp = opts.tmp || fs.mkdtempSync(path.join(os.tmpdir(), "pet-preview-"));
  const file = path.join(tmp, "pet-" + scene + ".html");
  fs.writeFileSync(file, buildPage(scene, mode, !!opts.keepAnim), "utf-8");
  const png = opts.pngDir === null ? path.join(tmp, "_" + scene + ".png")
                                   : path.join(opts.pngDir || OUT_DIR, "panel-" + scene + ".png");
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
    "--screenshot=" + png,
    "file:///" + file.replace(/\\/g, "/"),
  ];
  const r = spawnSync(chrome, args, { encoding: "utf-8", timeout: 120000 });
  const err = String(r.stderr || "");
  let json = null;
  let all = [];
  if (mode !== "shot") {
    // 一个页面可能打多行 MEASURE（sweep 模式：每页 × 每种气泡一行）——
    // 只留最后一行会把前面的页静默丢掉（看起来还挺成功）。全部收下来。
    const lines = err.split("\n").filter((l) => l.includes("MEASURE "));
    all = lines.map(extractMeasureJson).filter(Boolean);
    if (all.length) json = all[all.length - 1];
  }
  return { ok: mode === "measure" ? !!json : mode === "sweep" ? all.length > 0 : fs.existsSync(png),
           json, all, png, status: r.status, signal: r.signal, err };
}

/**
 * 量测若干场景（供测试复用）。
 * @returns {{chrome:string|null, scenes:Object<string, object>, raw:Object<string, object>}}
 */
function measureScenes(scenes, opts) {
  const chrome = findChrome();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pet-measure-"));
  const out = {};
  try {
    for (const s of scenes) {
      const r = runScene(s, { mode: "measure", chrome, tmp, pngDir: null, keepAnim: (opts || {}).keepAnim });
      out[s] = r.json;
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  return { chrome, scenes: out };
}

/**
 * 预算扫描：一次页面加载，把所有功能页 × {无气泡, 大头气泡} 量一遍。
 * 供 `test-pet-panel-layout.cjs` 使用 —— 覆盖不靠手写场景清单，靠 DOM 里真实存在的页签。
 */
function measureSweep(opts) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pet-sweep-"));
  try {
    return runScene("sweep-dark", { mode: "sweep", tmp, pngDir: null, keepAnim: (opts || {}).keepAnim });
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function main() {
  // --scene 可重复出现，也支持逗号分隔：早先只读 argv[i+1]，
  // 写 `--scene a --scene b` 只会量到 a，另外几个被静默丢掉（"1/1 个场景量测成功"
  // 看着还挺成功）。现在全都收进来，并对未知场景名直接报错，不再静默。
  const wanted = [];
  for (let i = 0; i < process.argv.length; i++) {
    if (process.argv[i] === "--scene" || process.argv[i] === "--scenes") {
      const v = process.argv[i + 1];
      if (!v || v.startsWith("--")) {
        console.error("--scene 后面要跟场景名（可逗号分隔，也可重复多次）");
        process.exit(2);
      }
      for (const s of v.split(",")) if (s.trim()) wanted.push(s.trim());
      i += 1;
    }
  }
  const unknown = wanted.filter((s) => !SCENES.includes(s));
  if (unknown.length) {
    console.error("未知场景：" + unknown.join(", ") + "\n可选：" + SCENES.join(", "));
    process.exit(2);
  }
  const list = wanted.length ? wanted : SCENES;
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
  for (const scene of list) {
    const r = runScene(scene, { mode: measure ? "measure" : "shot", chrome, tmp, keepAnim });
    if (r.ok) {
      ok += 1;
      if (measure) console.log("  " + scene + "  " + JSON.stringify(r.json));
      else console.log("  ok  " + scene + " -> " + path.relative(ROOT, r.png) +
        "  (" + Math.round(fs.statSync(r.png).size / 1024) + " KB)");
    } else {
      console.log("  FAIL " + scene + "  status=" + r.status + " signal=" + r.signal);
      console.log("        " + r.err.split("\n").slice(-6).join("\n        "));
    }
  }
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log("\n" + ok + "/" + list.length + (measure ? " 个场景量测成功" : " 张 -> " + path.relative(ROOT, OUT_DIR)));
  if (!ok) process.exit(1);
}

module.exports = { findChrome, measureScenes, measureSweep, runScene, extractMeasureJson, buildPage,
                   SCENES, ROOT, PET_DIR, OUT_DIR };

if (require.main === module) main();

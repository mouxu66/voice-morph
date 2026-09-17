// 应用退出守卫（纯静态、只读源码文本，不依赖 electron / node_modules）：
//   锁住「关掉主窗后进程必须真的退干净」这件事。
//
// 为什么需要它：Electron 的 window-all-closed 要「一个窗口都不剩」才触发。
// 只要漏掉任何一个窗口（哪怕是 hide 掉的置顶横幅），它就永不触发 → app.quit()
// 永不执行 → 应用无声无息留在后台：主窗关了、桌宠也销毁了（右下角什么都不剩）、
// 任务栏也没图标，用户只能开任务管理器杀。2026-09-17 用户实测踩到。
//
// 这类 bug 不报错、不崩溃，只在「我以为关掉了」时暴露，所以必须用静态断言钉死。
//
// 运行：node tools/test-electron-exit.cjs —— 退出码 0 = 通过，非 0 = 失败。
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

/**
 * 剥掉注释 —— **不剥就会出现假信心断言**。
 *
 * 实测踩到：断言「closed 处理器里有 app.quit()」时，匹配到的其实是
 * 我自己写在上方注释里的「window-all-closed 永不触发 → app.quit() 永不执行」；
 * 于是把真正的 `app.quit()` 调用删掉，测试**照样全绿**。
 * 同理，把 `altHint.destroyAltHint()` 注释掉（而不是删掉）也测不出来。
 *
 * 所以：凡是断言「某段代码存在」，必须先剥注释再匹配。
 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")        // 块注释
    .replace(/(^|[^:'"`\\])\/\/[^\n]*/g, "$1"); // 行注释（避开 :// 之类的 URL）
}

const ALT_HINT = stripComments(read("web/electron/alt-hint.cjs"));
const MAIN = stripComments(read("web/electron/main.cjs"));
const PET_ACTIONS = stripComments(read("web/electron/pet-actions.cjs"));

let pass = 0;
let fail = 0;
function t(name, fn) {
  try {
    fn();
    pass++;
    console.log("  ok   " + name);
  } catch (e) {
    fail++;
    console.log("  FAIL " + name + "\n         " + e.message);
  }
}

/**
 * 取 `win.on("closed", …)` 处理器**本体**（到它自己的 `});` 为止）。
 * 早先写死 900 字符，结果越出处理器、把后面的无关代码（和注释）也圈进来，
 * 断言就可能被别处的同名代码满足 —— 必须按结构切，不能按长度切。
 */
function closedHandlerBlock(src) {
  const i = src.indexOf('win.on("closed"');
  assert.ok(i > -1, "main.cjs 里找不到 win.on(\"closed\") 处理器");
  const end = src.indexOf("\n  });", i);
  assert.ok(end > -1, "win.on(\"closed\") 处理器找不到结束的 });");
  return src.slice(i, end + 6);
}

// ------------------------------------------------------------ alt-hint 侧
t("alt-hint.cjs 导出 destroyAltHint", () => {
  assert.ok(
    /module\.exports\s*=\s*\{[^}]*\bdestroyAltHint\b/s.test(ALT_HINT),
    "alt-hint.cjs 必须导出 destroyAltHint（主窗关闭时靠它销毁横幅窗口）",
  );
});

// 只 hide() 不够：窗口对象还活着，window-all-closed 就永不触发。必须是 destroy()。
t("destroyAltHint 真的销毁窗口（destroy），不是只 hide", () => {
  const i = ALT_HINT.indexOf("function destroyAltHint");
  assert.ok(i > -1, "找不到 destroyAltHint 函数体");
  const body = ALT_HINT.slice(i, i + 700);
  assert.ok(/\.destroy\(\)/.test(body), "destroyAltHint 必须调用 .destroy()");
  assert.ok(/altHintWin\s*=\s*null/.test(body), "销毁后要把 altHintWin 置空，避免悬垂引用");
});

// -------------------------------------------------------------- main 侧
t("main.cjs 引入了 alt-hint 模块", () => {
  assert.ok(
    /require\(\s*["']\.\/alt-hint\.cjs["']\s*\)/.test(MAIN),
    "main.cjs 必须 require(\"./alt-hint.cjs\")，否则无处调用 destroyAltHint",
  );
});

t("主窗关闭时同时销毁桌宠与置顶横幅（否则进程退不掉）", () => {
  const blk = closedHandlerBlock(MAIN);
  assert.ok(/pet\.destroyPet\(\)/.test(blk), "closed 处理器里应销毁桌宠");
  assert.ok(
    /altHint\.destroyAltHint\(\)/.test(blk),
    "closed 处理器里必须销毁置顶横幅 —— 漏了它，window-all-closed 永不触发，应用会留在后台",
  );
});

t("主窗关闭还有兜底：无窗口残留时直接 app.quit()", () => {
  const blk = closedHandlerBlock(MAIN);
  assert.ok(
    /BrowserWindow\.getAllWindows\(\)\.length\s*===\s*0/.test(blk) && /app\.quit\(\)/.test(blk),
    "光靠 window-all-closed 太脆弱：应有「确认无窗口残留则直接 quit」的兜底",
  );
});

t("before-quit 兜底销毁横幅（任何退出路径都不留窗口）", () => {
  const i = MAIN.indexOf('app.on("before-quit"');
  assert.ok(i > -1, "main.cjs 里找不到 before-quit 处理器");
  const blk = MAIN.slice(i, i + 600);
  assert.ok(/altHint\.destroyAltHint\(\)/.test(blk), "before-quit 里也要销毁横幅");
});

// -------------------------------------------------- 合成语音必须全自动
// 「同一条音频：第一次发自动、重发却要人按 Alt」是自相矛盾的，用户 2026-09-17 实测踩到。
t("桌宠发送已合成语音走全自动 send_voice，不再调 play_to_cable", () => {
  assert.ok(
    /backendPost\(\s*["']\/api\/wechat\/send_voice["']/.test(PET_ACTIONS),
    "应调用 /api/wechat/send_voice（全自动）",
  );
  assert.ok(
    !/backendPost\(\s*["']\/api\/wechat\/play_to_cable["']/.test(PET_ACTIONS),
    "不应再调用 /api/wechat/play_to_cable（那是半自动，还要用户自己按住 Alt）",
  );
});

t("已废弃的 runAltHintCountdown 不再被引用（按住 Alt 那条倒计时）", () => {
  assert.ok(
    !/runAltHintCountdown\s*\(/.test(PET_ACTIONS),
    "pet-actions.cjs 不应再调用 runAltHintCountdown",
  );
  assert.ok(
    !/function runAltHintCountdown/.test(ALT_HINT),
    "alt-hint.cjs 里 runAltHintCountdown 应已删除，避免有人再接回去",
  );
});

console.log(`\n${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);

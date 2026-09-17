#!/usr/bin/env node
/**
 * smoke-pet-render.cjs —— 桌宠渲染器**真 Electron 渲染**冒烟：角色到底画没画出来。
 *
 * 运行：cd web && npx electron electron/smoke-pet-render.cjs [--out <png>]
 * 退出码 0 = 角色已渲染；非 0 = 只有光晕/空白（2026-09-17 用户报障的「透明玻璃球」）。
 *
 * ── 为什么放在 web/electron/ 而不是 tools/ ────────────────────────────────
 * 本脚本要真 Electron 运行时（`require("electron")` 拿 app/BrowserWindow）。
 * 而 tools/check.py 的 nodetest 步会**复刻 CI 的"无 npm 依赖"环境**（把 web/node_modules
 * 临时挪走），并静态拦截任何 `require("electron")` 的入口脚本 —— 所以它不能进
 * tools/（会直接把自检判红）。放 web/electron/ 才能解析到 electron 模块。
 *
 * ── 与 tools/test-pet-renderer-script.cjs 的分工 ─────────────────────────
 *   · tools/test-pet-renderer-script.cjs（进 nodetest，每次自检都跑）：
 *     在 DOM 桩上跑真实内联脚本，断言脚本不崩、background-image/尺寸变量写对、
 *     初始穿透已下发。抓的是「脚本有没有跑完整」。
 *   · 本脚本（手动 / 交付前目视，**不在门禁里**）：
 *     真 Chromium 渲染 + capturePage 抓像素。抓的是「图片解码成功没、合成结果里
 *     到底有没有角色」——DOM 桩证明不了这一层。
 *   本次 bug 的表象恰恰是「只剩 CSS ::before 的径向渐变光晕」：脚本层与像素层
 *   都得看，缺一个都可能在"看着绿"的情况下漏掉。
 *
 * ── 判定依据 ──────────────────────────────────────────────────────────────
 * 芙宁娜精灵图含大量深色描边（RGB 明显低于 200）。
 *   正常渲染 → 画面里有成片深色像素；
 *   只剩光晕 → 全图都是 240+ 的浅色（用户截图实测最暗像素 rgb(205,214,224)、
 *             最大饱和度仅 20）。该判据与用户截图完全对应，能抓住同一类回归。
 */
"use strict";
const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

// 环境自检：ELECTRON_RUN_AS_NODE=1 时 Electron 会退化成纯 Node，
// require("electron") 只返回一个 exe 路径字符串 → 下面 destructure 出来的 app 是 undefined，
// 报错会是一句很难懂的 "Cannot read properties of undefined"。这里提前给一句人话。
if (!app || !BrowserWindow) {
  console.error(
    "[smoke-pet-render] 没拿到 Electron 运行时（app 为空）。\n" +
    "  最常见原因：环境里设了 ELECTRON_RUN_AS_NODE=1，Electron 退化成了纯 Node。\n" +
    "  处理：清掉该变量再跑，例如  env -u ELECTRON_RUN_AS_NODE npx electron electron/smoke-pet-render.cjs\n" +
    "  或直接跑 npm run test:pet-render（在干净 shell 里）。",
  );
  process.exit(2);
}

// __dirname = web/electron → 项目根是上两级
const ROOT = path.join(__dirname, "..", "..");
const PET_HTML = path.join(__dirname, "pet", "pet.html");
const outArg = process.argv.indexOf("--out");
const OUT = outArg > -1 && process.argv[outArg + 1]
  ? path.resolve(process.argv[outArg + 1])
  : path.join(ROOT, "outputs", "pet_render_smoke.png");

// 深色像素阈值：芙宁娜的描边/深蓝服装远低于此；光晕圈最暗也才 205
const DARK_CUTOFF = 200;
const DARK_MIN_COUNT = 200;   // 150×150 的角色深色像素上千；留个宽松下限防噪点

app.disableHardwareAcceleration();   // 无 GPU / 远程桌面下也能出图

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 220, height: 480,
    show: false,
    transparent: true, frame: false,
    webPreferences: { contextIsolation: true },
  });
  // mock=1：自动轮播状态，不依赖后端；桌宠会自己调 setSprite → 精灵图上屏
  await win.loadFile(PET_HTML, { query: { mock: "1" } });
  await new Promise((r) => setTimeout(r, 1500));   // 等精灵图解码 + 首帧动画落位

  const info = await win.webContents.executeJavaScript(`(() => {
    const sp = document.getElementById("sprite");
    const cs = getComputedStyle(sp);
    const r = sp.getBoundingClientRect();
    return {
      bgImage: sp.style.backgroundImage,
      bgSize: sp.style.backgroundSize,
      animation: sp.style.animation,
      w: r.width, h: r.height,
      opacity: cs.opacity,
      visibility: cs.visibility,
      ignoreMouseSent: true,
    };
  })()`);

  const img = await win.webContents.capturePage();
  const { width, height } = img.getSize();
  const bmp = img.toBitmap();   // BGRA
  let dark = 0, minLum = 255, minPx = null;
  for (let i = 0; i < bmp.length; i += 4) {
    const b = bmp[i], g = bmp[i + 1], r = bmp[i + 2];
    const lum = (r * 299 + g * 587 + b * 114) / 1000;
    if (lum < minLum) { minLum = lum; minPx = [r, g, b]; }
    if (lum < DARK_CUTOFF) dark += 1;
  }

  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, img.toPNG());

  const lines = [
    "===== smoke-pet-render =====",
    "sprite.background-image : " + info.bgImage,
    "sprite.background-size  : " + info.bgSize,
    "sprite.animation        : " + info.animation,
    "sprite 盒子尺寸          : " + info.w + " x " + info.h,
    "抓图尺寸                : " + width + " x " + height,
    "最暗像素                : lum=" + minLum.toFixed(1) + " rgb=" + JSON.stringify(minPx),
    "深色像素数 (<" + DARK_CUTOFF + ")     : " + dark,
    "抓图                    : " + OUT,
  ];

  const problems = [];
  if (!/svg\/.*\.webp/.test(info.bgImage || "")) problems.push("sprite 没有指向本地精灵图：" + info.bgImage);
  if (!info.bgSize) problems.push("sprite 没有 background-size（动画步长会错）");
  if (Number(info.w) < 100) problems.push("sprite 盒子尺寸异常：" + info.w);
  if (dark < DARK_MIN_COUNT) {
    problems.push("画面里几乎没有深色像素（" + dark + " < " + DARK_MIN_COUNT +
      "）——角色没画出来，只剩 CSS ::before 光晕圈（2026-09-17 报障现象）");
  }
  lines.push(problems.length ? "===== FAIL =====" : "===== OK =====");
  problems.forEach((p) => lines.push("  - " + p));
  if (!problems.length) lines.push("角色已真实渲染（深色像素 " + dark + " 个）");

  // Windows 上 Electron 是 GUI 子系统进程，console.log 不会回到父控制台，
  // 所以结果必须落盘：--out 的 .png 旁边写同名 .json / .log
  const report = {
    ok: problems.length === 0,
    problems,
    sprite: info,
    capture: { width, height },
    minLum, minPx, darkPixels: dark, darkCutoff: DARK_CUTOFF, darkMinCount: DARK_MIN_COUNT,
    png: OUT,
  };
  fs.writeFileSync(OUT.replace(/\.png$/i, ".json"), JSON.stringify(report, null, 2), "utf-8");
  fs.writeFileSync(OUT.replace(/\.png$/i, ".log"), lines.join("\n") + "\n", "utf-8");
  console.log(lines.join("\n"));

  app.exit(problems.length ? 1 : 0);
}).catch((e) => {
  const msg = "冒烟异常：" + (e && e.stack || e);
  try {
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT.replace(/\.png$/i, ".log"), msg + "\n", "utf-8");
  } catch {}
  console.error(msg);
  app.exit(2);
});

#!/usr/bin/env node
/**
 * test-pet-panel-layout.cjs —— 桌宠快捷面板「在 220×480 透明窗里放得下」的布局守卫。
 *
 * 运行：node tools/test-pet-panel-layout.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 为什么需要它（2026-09-17）：
 *   气泡与面板共用同一个 480px 高的窗口，可用高 = 480 - 18(#root 留白) - 150(角色)
 *   - 6(面板下边距) - 气泡 - 6(气泡下边距)。气泡一起来面板就被 flex 压扁，
 *   底部「最近发送」被切掉一截并冒出内部滚动条。实测（未修时）：
 *     guide   气泡 82 → 面板只剩 217.8，自然高 224 → 切 6px
 *     think   气泡 60 → 面板只剩 240，  自然高 256 → 切 16px
 *     offline 气泡 78 → 面板只剩 222，  自然高 256 → 切 34px
 *   修法是让面板在气泡在屏时主动让位（#root.compact，见 pet.html 里的注释）。
 *
 * 这个测试跑**真实 Chromium**（复用 tools/pet-panel-preview.cjs 的渲染路径，
 * 不另起一套实现，避免两边漂移），把每个场景的布局数字量出来做断言。
 * 「看起来没问题」不是证据：`scrollHeight > clientHeight` 才是被切的证据。
 *
 * ⚠️ 需要 Playwright 缓存里的 chromium-headless-shell（或 VM_CHROME 指定）。
 * 找不到时**大声跳过**（退出码 0，但会打醒目横幅）—— CI 机器不一定装浏览器，
 * 不能因此把整条 check 判红。要让「缺浏览器」也算失败，设 VM_PET_LAYOUT_REQUIRE=1。
 */
"use strict";
const assert = require("node:assert");
const path = require("node:path");
const preview = require("./pet-panel-preview.cjs");

/**
 * 默认只量三个「把不变量钉住」的场景，而不是全部 7 个 —— 每个场景要起一次
 * headless Chromium（约 1.5s），全量会让 pre-commit 明显变慢。
 *   ok-dark      ：无气泡 → 面板应为完整态（#recent 可见），且不能溢出
 *   guide-dark   ：最大的气泡（82px）→ 最容易溢出，也是这次报障的场景
 *   offline-dark ：修之前切得最多（34px）的场景
 * 想全量：VM_PET_LAYOUT_ALL=1；想指定：命令行传场景名。
 */
const DEFAULT_SCENES = ["ok-dark", "guide-dark", "offline-dark"];
const ALL_SCENES = preview.SCENES;

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

console.log("===== test-pet-panel-layout =====");

const chrome = preview.findChrome();
if (!chrome) {
  console.log("");
  console.log("  **************************************************************");
  console.log("  [skip] 找不到 chromium-headless-shell，布局断言**没有执行**。");
  console.log("         面板溢出这类问题在本机**未被验证** —— 别把这条 skip 当通过。");
  console.log("         装 Playwright，或设 VM_CHROME 指向 chrome.exe 后重跑。");
  console.log("  **************************************************************");
  console.log("");
  if (process.env.VM_PET_LAYOUT_REQUIRE === "1") {
    console.log("VM_PET_LAYOUT_REQUIRE=1，缺浏览器视为失败。");
    process.exit(1);
  }
  console.log("===== SKIP =====");
  process.exit(0);
}
console.log("chromium: " + chrome);

const argvScenes = process.argv.slice(2).filter((a) => !a.startsWith("-"));
const scenes = argvScenes.length ? argvScenes
  : (process.env.VM_PET_LAYOUT_ALL === "1" ? ALL_SCENES : DEFAULT_SCENES);
console.log("scenes: " + scenes.join(", "));

const { scenes: got } = preview.measureScenes(scenes);

for (const s of scenes) {
  const m = got[s];
  if (!m) {
    check(s + "：能渲染并回传量测数据", () => {
      throw new Error("没有拿到该场景的 MEASURE 输出（页面脚本可能抛异常了）");
    });
    continue;
  }

  // 1) 不变量：面板内容必须装得下（这才是「被切」的直接证据）
  check(s + "：面板内容不溢出（scrollHeight ≤ clientHeight）", () => {
    assert.strictEqual(m.panelOverflow, false,
      "面板内容 " + m.panelScrollH + "px 装进 " + m.panelClientH + "px —— " +
      "底部会被切掉 " + (m.panelScrollH - m.panelClientH) + "px 并冒出内部滚动条。" +
      "气泡在屏时应由 #root.compact 让位（收起 #recent），检查 setBubbleVisible 是否同步了 .compact");
  });

  // 2) 不变量：没有任何东西被挤出窗口
  check(s + "：没有元素被挤出窗口（root.scrollHeight ≤ 窗高）", () => {
    assert.ok(m.rootScrollH <= m.win.h,
      "root.scrollHeight=" + m.rootScrollH + " > 窗高 " + m.win.h);
  });

  // 3) 不变量：按钮文字不许被裁（204px 宽度是硬预算）
  check(s + "：按钮文字都没有被裁（scrollWidth ≤ clientWidth）", () => {
    const bad = m.buttons.filter((b) => b.clipped);
    assert.deepStrictEqual(bad, [],
      "被裁的按钮：" + bad.map((b) => b.id + "(" + b.txt + " " + b.scrollW + ">" + b.clientW + ")").join(", "));
  });

  // 4) 不变量：入场动画结束后面板必须完全不透明（否则截图/肉眼会误判成「颜色发灰」）
  check(s + "：面板动画结束后 opacity = 1", () => {
    assert.strictEqual(m.panelOpacity, "1", "opacity=" + m.panelOpacity);
  });

  // 5) 让位规则真的生效：有气泡时 #recent 收起，没气泡时它在
  const recent = (m.blocks || []).find((b) => b.id === "recent");
  if (recent) {
    check(s + "：让位规则与气泡显隐一致（有气泡→#recent 收起；无气泡→可见）", () => {
      if (m.bubble > 0) {
        assert.strictEqual(recent.disp, "none",
          "气泡在屏（" + m.bubble + "px）但 #recent 仍占 " + recent.h + "px —— 面板没让位");
      } else {
        assert.notStrictEqual(recent.disp, "none",
          "没有气泡，面板是完整态，#recent 不该被藏起来");
      }
    });
  }
}

// 6) 各场景的角色位置必须一致 —— 面板让位不该把角色顶走
const tops = scenes.map((s) => got[s] && got[s].petTop).filter((v) => typeof v === "number");
if (tops.length > 1) {
  check("各场景角色位置一致（让位只动面板，不该顶走角色）", () => {
    const uniq = [...new Set(tops)];
    assert.strictEqual(uniq.length, 1, "各场景 petTop 不一致：" +
      scenes.map((s) => s + "=" + (got[s] && got[s].petTop)).join(", "));
  });
}

console.log("===== " + (failures.length ? "FAIL" : "OK") + " =====");
console.log(pass + " 通过, " + failures.length + " 失败");
if (failures.length) {
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
process.exit(0);

#!/usr/bin/env node
/**
 * test-pet-visibility.cjs —— 桌宠显隐决策矩阵单测。
 *
 * 运行：node tools/test-pet-visibility.cjs —— 退出码 0 = 通过，非 0 = 失败。
 *
 * 为什么需要它（2026-09-14「日常 / 游戏分开」）：
 *   显隐规则从「模式 + 运行态」升级为「模式 + 运行态 + 性能档」后，分支组合变成
 *   2(模式) × 2(用户隐藏) × 3(档位) × 2(运行) × 2(导览) 量级。这些组合塞在
 *   pet.cjs 的 setInterval 闭包里，只能起真 Electron GUI 手测——沙箱起不了 GUI
 *   （Chromium GPU 进程必崩），所以把决策抽成 pet-visibility.cjs 纯函数在这里穷举。
 *
 * 本测试**只验决策**，不验窗口真的显示/隐藏（那要真 GUI）。显隐执行分支由
 *   pet.cjs 里「按 shouldShowPet 的结果 show/hide」这段薄胶水承担。
 *
 * 覆盖的回归点：
 *   - 游戏档必须压过「常驻显示」（这是本次改动的核心，漏了就白改）
 *   - 后端离线/字段异常时退化为「未运行 + 无档位」= 原有行为，不能永久隐身
 *   - 未知档位（如后端将来加了档）不能误触发隐藏
 */
const assert = require("node:assert");
const {
  PERF_GAME,
  shouldShowPet,
  parseLiveStatus,
} = require("../web/electron/pet-visibility.cjs");

// 默认入参：仅变声时显示 + 未隐藏 + 均衡档 + 什么都没跑
const BASE = {
  mode: "cascade",
  userHidden: false,
  perfProfile: "balanced",
  running: false,
  guideActive: false,
};

/** 在默认入参上覆盖若干字段 */
const S = (over) => shouldShowPet({ ...BASE, ...over });

const CASES = [
  // ---- shouldShowPet：基本矩阵 ----
  ["仅变声时显示 + 无运行无导览 → 不显示", () => assert.strictEqual(S({}), false)],
  ["常驻显示 → 显示", () => assert.strictEqual(S({ mode: "always" }), true)],
  ["仅变声时显示 + 实时变声运行中 → 显示（挂字幕）", () => assert.strictEqual(S({ running: true }), true)],
  ["仅变声时显示 + 页面导览进行中 → 显示", () => assert.strictEqual(S({ guideActive: true }), true)],

  // ---- 用户手动隐藏优先级最高 ----
  ["用户隐藏 压过 常驻显示", () => assert.strictEqual(S({ mode: "always", userHidden: true }), false)],
  ["用户隐藏 压过 运行中", () => assert.strictEqual(S({ running: true, userHidden: true }), false)],
  ["用户隐藏 压过 导览", () => assert.strictEqual(S({ guideActive: true, userHidden: true }), false)],

  // ---- 游戏档：本次改动的核心，必须压过一切「该显示」的理由 ----
  ["游戏档 压过 常驻显示", () => assert.strictEqual(S({ mode: "always", perfProfile: PERF_GAME }), false)],
  ["游戏档 压过 实时变声运行中", () => assert.strictEqual(S({ running: true, perfProfile: PERF_GAME }), false)],
  ["游戏档 压过 页面导览", () => assert.strictEqual(S({ guideActive: true, perfProfile: PERF_GAME }), false)],
  ["游戏档 同时常驻+运行+导览 → 仍不显示", () => assert.strictEqual(
    S({ mode: "always", running: true, guideActive: true, perfProfile: PERF_GAME }), false)],
  ["游戏档 + 用户未隐藏 → 不显示（不依赖 userHidden 兜底）", () => assert.strictEqual(
    S({ mode: "always", userHidden: false, perfProfile: PERF_GAME }), false)],

  // ---- 未知/异常档位不能误隐藏 ----
  ["未知档位 ultra + 常驻 → 照常显示", () => assert.strictEqual(
    S({ mode: "always", perfProfile: "ultra" }), true)],
  ["空档位 + 常驻 → 照常显示", () => assert.strictEqual(
    S({ mode: "always", perfProfile: "" }), true)],
  ["无参调用不抛且不显示", () => assert.strictEqual(shouldShowPet(), false)],
  ["undefined 入参不抛", () => assert.strictEqual(shouldShowPet(undefined), false)],

  // ---- parseLiveStatus：后端离线 / 响应异常时的退化 ----
  ["null → 未运行 + 无档位", () => assert.deepStrictEqual(parseLiveStatus(null),
    { running: false, perfProfile: "" })],
  ["空对象 → 未运行 + 无档位", () => assert.deepStrictEqual(parseLiveStatus({}),
    { running: false, perfProfile: "" })],
  ["非对象（后端返回纯文本）→ 未运行 + 无档位", () => assert.deepStrictEqual(parseLiveStatus("oops"),
    { running: false, perfProfile: "" })],
  ["正常响应 → 原样带出", () => assert.deepStrictEqual(
    parseLiveStatus({ live_running: true, perf_profile: "game" }),
    { running: true, perfProfile: "game" })],
  ["live_running 真值归一为布尔", () => assert.strictEqual(
    parseLiveStatus({ live_running: 1 }).running, true)],
  ["perf_profile 类型不对（数字）→ 退化为空档位", () => assert.deepStrictEqual(
    parseLiveStatus({ live_running: true, perf_profile: 123 }),
    { running: true, perfProfile: "" })],
  ["live_running 缺失但档位为 game → 不显示（档位单独生效）", () => assert.strictEqual(
    shouldShowPet({ ...BASE, mode: "always", perfProfile: parseLiveStatus({ perf_profile: "game" }).perfProfile }),
    false)],
];

function main() {
  console.log("");
  console.log("===== test-pet-visibility =====");
  console.log("（纯决策函数穷举；不验窗口真实显隐，那需真 GUI）");
  console.log("");

  let failed = 0;
  for (const [name, fn] of CASES) {
    try {
      fn();
      console.log(`[PASS] ${name}`);
    } catch (e) {
      failed++;
      console.log(`[FAIL] ${name}`);
      console.log(`       ${e.message.split("\n")[0]}`);
    }
  }

  console.log("");
  console.log(`合计 ${CASES.length} 条：${CASES.length - failed} 通过，${failed} 失败`);
  if (failed) {
    console.log("RESULT: FAIL");
    return 1;
  }
  console.log("RESULT: PASS");
  return 0;
}

if (require.main === module) {
  process.exit(main());
}

module.exports = { CASES };

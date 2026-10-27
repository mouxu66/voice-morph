#!/usr/bin/env node
/**
 * test-pet-geometry.cjs —— 桌宠窗「换身材」的锚点与夹边。
 *
 * 为什么单独测这个：这段逻辑只在真 Electron 里生效，而沙箱里起不了 GUI。算错的表现
 * 是「点了『怎么回』，桌宠窜到屏幕外」——用户看到的是 bug，代码里一行报错都没有。
 * 所以锚点必须能被断言。
 *
 * 钉住的四件事：
 *   ① 放大锚右下角（桌宠的脚原地不动，面板向上向左长）
 *   ② 缩回用存档坐标（用户可能把桌宠拖过，不能按公式反推回"默认位置"）
 *   ③ 放大越界就夹回工作区（桌宠贴左边缘时不把面板顶出屏幕）
 *   ④ 缩回不夹（回到用户自己放过的地方，不该被挪）
 */
"use strict";
const assert = require("node:assert");
const g = require("../web/electron/pet-geometry.cjs");

let pass = 0;
const failures = [];
function check(name, fn) {
  try { fn(); pass += 1; console.log("  ok   " + name); }
  catch (e) { failures.push(name + " —— " + e.message); console.log("  FAIL " + name + "\n       " + e.message); }
}

const WORK = { x: 0, y: 0, width: 1920, height: 1040 };
// 一个典型的「右下角」桌宠：右边距 24、下边距 8（pet.cjs 建窗时用的同一组数字）
const CORNER = { x: 1920 - 220 - 24, y: 1040 - 480 - 8, width: 220, height: 480 };

console.log("pet-geometry");

check("放大：右下角原地不动", () => {
  const b = g.nextBounds(CORNER, "big", { workArea: WORK });
  assert.strictEqual(b.width, 440);
  assert.strictEqual(b.height, 640);
  assert.strictEqual(b.x + b.width, CORNER.x + CORNER.width, "右边缘动了");
  assert.strictEqual(b.y + b.height, CORNER.y + CORNER.height, "下边缘动了");
});

check("放大：长出来的部分向左、向上（不是向右、向下）", () => {
  const b = g.nextBounds(CORNER, "big", { workArea: WORK });
  assert.strictEqual(b.x, CORNER.x - 220, "没向左长 220");
  assert.strictEqual(b.y, CORNER.y - 160, "没向上长 160");
});

check("缩回：用存档坐标，不用公式反推", () => {
  // 用户在放大前把桌宠拖到了一个「公式推不出来」的位置；放大后又在别处挪了一下。
  // 缩回必须回**放大前**那一点，而不是按右下角不动反推。
  const saved = { x: 500, y: 300, width: 220, height: 480 };
  const moved = { x: 300, y: 200, width: 440, height: 640 };
  const back = g.nextBounds(moved, "small", { saved, workArea: WORK });
  assert.deepStrictEqual(back, { x: 500, y: 300, width: 220, height: 480 });
  // 反证：公式会给出 300+220=520 / 200+160=360，和存档差 20/60 —— 两者必须可区分，
  // 否则这条用例只是在重复前一条。
  assert.notDeepStrictEqual(back, { x: 520, y: 360, width: 220, height: 480 });
});

check("缩回：没有存档就按「右下角不动」反推", () => {
  const big = { x: 100, y: 100, width: 440, height: 640 };
  const back = g.nextBounds(big, "small", { saved: null, workArea: WORK });
  assert.strictEqual(back.x + back.width, big.x + big.width, "右边缘动了");
  assert.strictEqual(back.y + back.height, big.y + big.height, "下边缘动了");
  assert.deepStrictEqual({ w: back.width, h: back.height }, { w: 220, h: 480 });
});

check("放大越界：贴左边时夹回工作区，面板不许有一半在屏幕外", () => {
  const atLeft = { x: 0, y: 1040 - 480 - 8, width: 220, height: 480 };
  const b = g.nextBounds(atLeft, "big", { workArea: WORK });
  assert.ok(b.x >= WORK.x, "左边越界了：" + b.x);
  assert.ok(b.x + b.width <= WORK.x + WORK.width, "右边越界了");
});

check("放大越界：贴上边时同理", () => {
  const atTop = { x: 1920 - 244, y: 0, width: 220, height: 480 };
  const b = g.nextBounds(atTop, "big", { workArea: WORK });
  assert.ok(b.y >= WORK.y, "上边越界了：" + b.y);
  assert.ok(b.y + b.height <= WORK.y + WORK.height, "下边越界了");
});

check("缩回不夹：用户放在工作区外也不想被系统悄悄挪走", () => {
  const saved = { x: -50, y: -30, width: 220, height: 480 };
  const back = g.nextBounds({ x: 0, y: 0, width: 440, height: 640 }, "small", { saved, workArea: WORK });
  assert.deepStrictEqual(back, saved, "缩回时被夹了 —— 用户会觉得桌宠自己跑了");
});

check("二次放大是幂等的（cur 已经是 big 时再放大不该继续长）", () => {
  const big = g.nextBounds(CORNER, "big", { workArea: WORK });
  const again = g.nextBounds(big, "big", { workArea: WORK });
  assert.deepStrictEqual(again, big, "重复放大又长了 220×160");
});

check("屏幕比窗口还小：先缩尺寸再夹原点，不给出负坐标", () => {
  const tiny = { x: 0, y: 0, width: 400, height: 300 };
  const b = g.clampToWorkArea({ x: -200, y: -200, width: 440, height: 640 }, tiny);
  assert.deepStrictEqual(b, { x: 0, y: 0, width: 400, height: 300 });
});

console.log("\n" + pass + " 项通过" + (failures.length ? "，" + failures.length + " 项失败" : ""));
if (failures.length) {
  for (const f of failures) console.log("  ✗ " + f);
  process.exit(1);
}

/**
 * test-pet-msg-grouping.cjs —— 桌宠分析面板「同一人连发只显示一次昵称」的守卫。
 *
 * 运行：node tools/test-pet-msg-grouping.cjs —— 退出码 0 = 通过，非 0 = 失败。
 * （tools/check.py 的 nodetest 步按 tools/test-*.cjs glob 自动收网，无需登记。）
 *
 * 为什么：群里 20 条常常只有 3 个人在说，每条都带一格昵称会让左栏看起来全是名字，
 * 正文被挤成一条缝，「换人了没有」反而更难看出来。规则写在 pet.html 的
 * `anaMsgRows`：同一人的连续消息只有第一条带「谁」，时间行结束这一轮。
 *
 * 这里不跑整脚本（完整 DOM 桩在 test-pet-renderer-script.cjs 里），只把
 * anaMsgRow / anaMsgRows / anaWho 三个函数原文抠出来跑，断言渲染结果。
 */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "web", "electron", "pet", "pet.html"), "utf8");
const from = html.indexOf("function anaMsgRow(");
const to = html.indexOf("function anaMarkNamesHint(");
if (from < 0 || to <= from) {
  console.log("✗ 找不到 anaMsgRow / anaMarkNamesHint —— pet.html 结构变了，测试要跟着改");
  process.exit(1);
}
const src = html.slice(from, to);

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const ana = new Function(
  "esc",
  "ANA_BARE_PLACEHOLDERS",
  "ANA_KIND_LABEL",
  src + "\nreturn { anaMsgRow, anaMsgRows, anaWho };",
)(esc, new Set(["图片", "视频"]), { text: "文字" });

const countWho = (s) => (s.match(/class="who/g) || []).length;
let failed = 0;
const check = (name, cond) => {
  console.log(`${cond ? "✓" : "✗"} ${name}`);
  if (!cond) failed += 1;
};

const m = (text, speaker, extra = {}) => ({ kind: "text", text, speaker, ...extra });

// 1. 同一人连发三条：只有第一条带昵称，后两条是 cont
const same = ana.anaMsgRows([m("在吗", 3), m("今晚开黑", 3), m("九点", 3)]);
check("同人连发 3 条只出现 1 次昵称", countWho(same) === 1);
check("后两条标记为 cont", (same.match(/amsg cont/g) || []).length === 2);

// 2. 换人就要重新写：说话人变了，昵称数跟着变
const diff = ana.anaMsgRows([m("在吗", 3), m("我来", 9)]);
check("换人后两行都有昵称", countWho(diff) === 2);

// 3. 时间行结束这一轮：隔一段时间后再说，要重新提示是谁
const gap = ana.anaMsgRows([m("在吗", 3), { kind: "time", text: "23:10" }, m("还没睡", 3)]);
check("时间行之后重新显示昵称", countWho(gap) === 2);

// 4. 「我」连发同理
const me = ana.anaMsgRows([
  m("收到", 0, { side: "right" }),
  m("马上来", 0, { side: "right" }),
]);
check("我的连发只出现 1 次「我」", (me.match(/class="who me/g) || []).length === 1);

// 5. 同名不同 id 不合并：显示两行名字，而不是把两个人算成一个
const sameName = ana.anaMsgRows([
  m("你好", 3, { name: "巴黎罗" }),
  m("我也叫这个", 9, { name: "巴黎罗" }),
]);
check("同名不同 id 仍是两个人", countWho(sameName) === 2);

// 6. 没有归属（id=0、没有名字）的消息不制造连续关系
const noWho = ana.anaMsgRows([m("图片", 0), m("图片", 0)]);
check("无归属消息不出昵称格", countWho(noWho) === 0);

if (failed) {
  console.log(`\n失败 ${failed} 项`);
  process.exit(1);
}
console.log("\n全部通过");

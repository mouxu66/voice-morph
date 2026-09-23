#!/usr/bin/env node
/**
 * test-pet-send-result.cjs —— 守住「合成并发送」必须把结果回传给桌宠面板。
 *
 * 为什么需要它（2026-09-18 事故）：
 *   `pet-actions.cjs` 里两条发送路径不对称 —— `sendWechatWav()`（发试听产物）会
 *   发 `pet:send-result`，而 `doSendTextToWechat()`（打字直发）**从不回传**。
 *   面板 `pet.html` 只靠这个事件收尾，于是打字直发后状态栏永远停在「合成中…」。
 *   用户实测原话："不是已经合成完了吗？为什么下面还有一行小字写着合成中"。
 *
 * 这类 bug 的特征是「功能其实正常、只是结果没人接」：语音真发出去了，所以任何
 * "点一下看看"的手工验证都容易漏掉 —— 必须靠断言钉住。
 *
 * 设计要点：
 *   1. **先剥注释再断言**：踩过坑（犯错指南 §3.23）—— 直接搜文本会匹配到注释里的
 *      同名串，于是把代码删掉测试照样绿。这里剥掉 `//` 与块注释再找。
 *   2. 断言**成功与失败两个分支都要回传**：只回传成功的话，409 预检拦截
 *      （微信没开）同样让面板卡在"合成中"。
 *   3. 零依赖、纯文本，退出码 0 = PASS / 1 = FAIL。
 *
 * 用法：node tools/test-pet-send-result.cjs
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const TARGET = path.join(ROOT, "web", "electron", "pet-actions.cjs");
const FN = "doSendTextToWechat";

/** 剥掉行注释与块注释（字符串字面量里的 // 不处理，够用且不引依赖）。 */
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

/** 从 `function <name>(` 起按大括号配平，取出函数体（不含签名）。 */
function extractFunctionBody(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) return null;
  const open = src.indexOf("{", start);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return src.slice(open + 1, i);
    }
  }
  return null;
}

function main() {
  const failures = [];

  if (!fs.existsSync(TARGET)) {
    console.error(`FAIL: 找不到 ${path.relative(ROOT, TARGET)}`);
    process.exit(1);
  }
  const raw = fs.readFileSync(TARGET, "utf8");
  const body = extractFunctionBody(raw, FN);
  if (!body) {
    console.error(`FAIL: 在 pet-actions.cjs 里找不到 ${FN}() —— 函数被改名/删除了？`);
    process.exit(1);
  }
  const code = stripComments(body);          // 关键：剥注释，避免匹配到说明文字

  const hits = (code.match(/pet:send-result/g) || []).length;
  if (hits === 0) {
    failures.push(
      `${FN}() 里没有 pet:send-result —— 打字直发的结果没人回传，` +
      `桌宠面板会永远停在「合成中…」`);
  } else if (hits < 2) {
    failures.push(
      `${FN}() 只回传了 ${hits} 次；成功与失败（如 409 预检拦截）都要回传，否则` +
      `失败时面板同样卡在「合成中…」`);
  }

  if (!code.includes("getPetWin")) {
    failures.push(`${FN}() 里没有 getPetWin() —— 拿不到面板窗口就无法回传`);
  }

  // ---- 2026-09-23 长文分段发送：载荷必须带上「本批几条 / 已发几条 / 第几条挂的」----
  // 面板靠这几个字段判断「整批发完了没」（不能在第 1 条落地时就打绿勾收尾）与
  // 「剩下的能不能重发」。少一个字段就是静默降级：面板只能含糊说一句「失败了」，
  // 而用户实际已经在微信里收到前两条 —— 他会以为一条都没发出去。
  const payloads = [...code.matchAll(
    /petWin\.webContents\.send\(\s*"pet:send-result"\s*,\s*\{([\s\S]*?)\}\s*\)/g)].map((mm) => mm[1]);
  if (payloads.length === 0) {
    failures.push(`${FN}() 里找不到 pet:send-result 的载荷对象 —— 正则失效或调用被改写`);
  } else {
    const joined = payloads.join("\n");
    const missing = ["total_chunks", "sent_chunks", "failed_index", "remaining_wavs"]
      .filter((f) => !new RegExp("\\b" + f + "\\s*:").test(joined));
    if (missing.length) {
      failures.push(
        `pet:send-result 载荷缺少分段字段：${missing.join(", ")} —— ` +
        `长文被切成 N 条后，面板既判断不了整批是否结束，也提示不了可重发`);
    }
  }
  if (!/data\.failed_index/.test(code)) {
    failures.push(`${FN}() 没有转发 data.failed_index —— 中途落败时说不清是第几条挂的`);
  }
  if (!code.includes("条失败")) {
    failures.push(
      `${FN}() 的失败文案里没有「第 N/M 条失败」口径 —— 只报「结果：partial」的话，` +
      `用户不知道前面几条其实已经发出去了`);
  }

  // ---- 超时必须放宽到 600s（分段发送的代价）----
  // 每条 = N 次 TTS 调用 + N 轮「切卡/录音/等浮层消失」。旧值 180s 只够 1 条，
  // 3 条以上会**假超时**：主进程放弃等待但后端仍在录 —— 用户看到「失败了」，
  // 却在微信里收到语音，还可能再点一次 → 两批语音叠在一起，更乱。
  const tmo = code.match(/,\s*(\d{4,})\s*\)\s*;\s*$/);
  if (!tmo) {
    failures.push(
      `${FN}() 里找不到 backendPost 的超时实参（形如 \`, 600000);\` 收尾）—— ` +
      `可能被改成变量了，这条断言会静默失效`);
  } else if (Number(tmo[1]) < 600000) {
    failures.push(
      `${FN}() 超时是 ${tmo[1]}ms（<600000）—— 长文拆 N 条依次录制，` +
      `180s 只够 1 条；假超时会让用户以为失败，随后在微信里收到语音，再去点第二次`);
  }

  // 另一条路径本就该回传，顺手守住（防止有人"统一重构"时把它删了）
  const wavBody = extractFunctionBody(raw, "sendWechatWav");
  if (!wavBody || !stripComments(wavBody).includes("pet:send-result")) {
    failures.push("sendWechatWav() 丢了 pet:send-result —— 发试听产物的路径也会卡住");
  }

  if (failures.length) {
    console.error("FAIL: 桌宠发送结果回传链路被破坏");
    for (const f of failures) console.error("  - " + f);
    process.exit(1);
  }
  console.log(`PASS: ${FN}() 回传 ${hits} 处（成功+失败），sendWechatWav() 也在回传`);
}

main();

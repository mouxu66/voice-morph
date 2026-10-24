// backend.cjs / pipe-guard.cjs 的 EPIPE 根治专项测试。
//
// 运行：node tools/test-backend.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 背景（2026-10-24，用户实测"错误框一直在弹"）：
//   主进程 stdout 是「启动者给的管道」，启动者退出后每写一次 console.log 就
//   EPIPE；Electron 无兜底 → 弹模态框，后端持续打日志 → 框一个接一个弹。
//   根治三环：① pipe-guard.cjs 全局静默管道类错误；② pumpBackendLine 在
//   stdout 已死时只落盘不镜像；③ 日志流自身 error 不升级成未捕获异常。
//   最后一条用例是端到端复现：父进程真的关掉子进程管道读端，子进程继续
//   console.log，必须静默活到自然退出。
const assert = require("node:assert");
const { installElectronStub } = require("./electron-stub.cjs");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

// electron 桩：userData 指向临时目录（BACKEND_LOG/PID_FILE 在 require 时求值，
// 必须在 require backend.cjs 之前装好）。目录预建——createWriteStream 不会建目录。
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-backend-userdata-"));
installElectronStub({
  app: {
    isPackaged: true,
    getPath: (name) => (name === "userData" ? userDataDir : userDataDir),
    quit: () => {},
    on: () => {},
    whenReady: () => Promise.resolve(),
  },
  ipcMain: { handle: () => {}, on: () => {} },
  dialog: { showErrorBox: () => {} },
  shell: {},
  BrowserWindow: function () {},
});

const pipeGuard = require("../web/electron/pipe-guard.cjs");
const backend = require("../web/electron/backend.cjs");

const BACKEND_LOG = path.join(userDataDir, "backend.log");

// ---------- 夹具 ----------
function clearLog() {
  fs.writeFileSync(BACKEND_LOG, "", "utf-8");
}
function readLog() {
  return fs.existsSync(BACKEND_LOG) ? fs.readFileSync(BACKEND_LOG, "utf-8") : "";
}
/** 假流：记录 write 调用；error 事件可手动触发 */
function makeFakeStream() {
  const { EventEmitter } = require("node:events");
  const s = new EventEmitter();
  s.writes = [];
  s.write = (chunk) => { s.writes.push(String(chunk)); return true; };
  return s;
}
/** 临时替换 process.stdout，finally 还原（绝不能真 destroy 测试进程的 stdout） */
function withStdout(fake, fn) {
  const real = process.stdout;
  Object.defineProperty(process, "stdout", { value: fake, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "stdout", { value: real, configurable: true });
  }
}

const checks = [];
function t(name, fn) { checks.push({ name, fn }); }

// =====================================================================
// Part 1: pipe-guard.cjs（纯函数 + 事件行为）
// =====================================================================
t("isPipeError: 管道类错误码识别，其余一律不静默", () => {
  for (const code of ["EPIPE", "ERR_STREAM_DESTROYED", "ERR_STREAM_WRITE_AFTER_END"]) {
    assert.strictEqual(pipeGuard.isPipeError({ code }), true, code);
  }
  // 不是管道问题的一律不静默——兜底不能变成掩蔽真错误的黑洞
  assert.strictEqual(pipeGuard.isPipeError({ code: "ECONNRESET" }), false);
  assert.strictEqual(pipeGuard.isPipeError({ code: "EACCES" }), false);
  assert.strictEqual(pipeGuard.isPipeError(null), false);
  assert.strictEqual(pipeGuard.isPipeError(undefined), false);
  assert.strictEqual(pipeGuard.isPipeError("EPIPE"), false, "只认带 code 的错误对象");
});

t("writableStream: 已死/关闭的 stdout 判定为不可写", () => {
  assert.strictEqual(pipeGuard.writableStream({ destroyed: false, writable: true }), true);
  assert.strictEqual(pipeGuard.writableStream({ destroyed: false }), true, "writable 缺省视为可写");
  assert.strictEqual(pipeGuard.writableStream({ destroyed: true }), false);
  assert.strictEqual(pipeGuard.writableStream({ writable: false }), false);
  assert.strictEqual(pipeGuard.writableStream(null), false);
  assert.strictEqual(pipeGuard.writableStream(undefined), false);
});

t("installPipeGuard: stdout EPIPE 事件静默，不升级成未捕获异常", () => {
  const fake = makeFakeStream();
  pipeGuard.installPipeGuard([fake]);
  // emit 是同步的：handler 若抛出会直接从这里炸出，测试挂掉
  fake.emit("error", Object.assign(new Error("broken pipe"), { code: "EPIPE" }));
});

t("installPipeGuard: 未知错误仍然抛出（兜底不掩蔽真问题）", () => {
  const fake = makeFakeStream();
  pipeGuard.installPipeGuard([fake]);
  assert.throws(
    () => fake.emit("error", Object.assign(new Error("permission denied"), { code: "EACCES" })),
    /permission denied/,
  );
});

t("installPipeGuard: uncaughtException 的 EPIPE 放行、非 EPIPE 抛出", () => {
  pipeGuard.installPipeGuard([]); // 只装 process 级兜底
  process.emit("uncaughtException", Object.assign(new Error("bp"), { code: "EPIPE" }));
  assert.throws(() => process.emit("uncaughtException", new Error("boom")), /boom/);
});

// =====================================================================
// Part 2: backend.cjs 泵送与日志落盘
// =====================================================================
t("pumpBackendLine: stdout 活着 → 镜像 + 落盘（开发态终端看日志的场景）", () => {
  const fake = makeFakeStream();
  backend._setBackendLogStreamForTest(fake);
  let mirrored = null;
  const realLog = console.log;
  console.log = (...args) => { mirrored = args; };
  try {
    backend.pumpBackendLine("", "hello from backend\n");
  } finally {
    console.log = realLog;
  }
  assert.ok(mirrored && mirrored[0] === "[backend]", "stdout 可写时要镜像");
  assert.ok(mirrored[1].includes("hello from backend"), "镜像内容要 trim 过");
  assert.deepStrictEqual(fake.writes, ["hello from backend\n"], "同时必须落盘");
});

t("pumpBackendLine: stdout 已死（启动者关了管道）→ 只落盘不镜像", () => {
  const fake = makeFakeStream();
  backend._setBackendLogStreamForTest(fake);
  let logCalls = 0;
  const realLog = console.log;
  console.log = () => { logCalls += 1; };
  try {
    withStdout({ destroyed: true, writable: false }, () => {
      backend.pumpBackendLine(":err", "traceback line\n");
    });
  } finally {
    console.log = realLog;
  }
  assert.strictEqual(logCalls, 0, "stdout 死了还镜像 = 给 EPIPE 模态框攒弹药");
  assert.deepStrictEqual(fake.writes, ["traceback line\n"], "不镜像不等于不落盘");
});

t("pumpBackendLine: stdout writable=false（管道关闭的另一种形态）→ 同样只落盘", () => {
  const fake = makeFakeStream();
  backend._setBackendLogStreamForTest(fake);
  let logCalls = 0;
  const realLog = console.log;
  console.log = () => { logCalls += 1; };
  try {
    withStdout({ destroyed: false, writable: false }, () => {
      backend.pumpBackendLine("", "another line\n");
    });
  } finally {
    console.log = realLog;
  }
  assert.strictEqual(logCalls, 0);
  assert.deepStrictEqual(fake.writes, ["another line\n"]);
});

t("appendBackendLog: 日志流 error 后停写，且绝不向上抛", () => {
  clearLog();
  backend._setBackendLogStreamForTest(null);
  // 走 ensureBackendLogStream 建真流（生产路径：error handler 在此挂上），
  // 再把它的 write 换成假的，避免真写文件
  const s = backend.ensureBackendLogStream();
  s.writes = [];
  s.write = (chunk) => { s.writes.push(String(chunk)); return true; };
  backend.appendBackendLog("before-error\n");
  assert.deepStrictEqual(s.writes, ["before-error\n"]);
  // 模拟磁盘满/文件被占：流 emit error。handler 已挂 → 设 flag 静默，不抛
  s.emit("error", Object.assign(new Error("EBADF"), { code: "EBADF" }));
  backend.appendBackendLog("after-error\n"); // 不抛
  assert.deepStrictEqual(s.writes, ["before-error\n"], "流坏掉后必须停写，不能每行都抛");
});

t("appendBackendLog: write 同步抛（磁盘满的另一副面孔）→ 记 broken 停写", () => {
  const fake = makeFakeStream();
  fake.write = () => { throw new Error("ENOSPC: no space left"); };
  backend._setBackendLogStreamForTest(fake);
  backend.appendBackendLog("x\n"); // 同步抛要被吞
  backend.appendBackendLog("y\n"); // 第二次直接跳过，连抛的机会都不给
  assert.strictEqual(fake.writes.length, 0);
});

t("ensureBackendLogStream: 建真实流、可写、幂等", async () => {
  backend._setBackendLogStreamForTest(null);
  clearLog();
  const s1 = backend.ensureBackendLogStream();
  assert.ok(s1 && typeof s1.write === "function", "要返回可写流");
  assert.strictEqual(backend.ensureBackendLogStream(), s1, "幂等：不能建两个流抢同一个文件");
  // createWriteStream 的落盘是异步 flush 的，用 write callback 等真写进去再断言
  await new Promise((resolve, reject) => {
    s1.write("ensure-test\n", (err) => (err ? reject(err) : resolve()));
  });
  assert.ok(readLog().includes("ensure-test"), "流建好后必须能写");
});

// =====================================================================
// Part 3: 端到端——父进程真的关掉子进程管道读端（用户场景复现）
// =====================================================================
t("接线检查: main.cjs 入口装 pipe guard、backend.cjs 镜像前查 stdout 存活", () => {
  const mainSrc = fs.readFileSync(path.join(__dirname, "..", "web", "electron", "main.cjs"), "utf-8");
  assert.ok(
    mainSrc.includes('require("./pipe-guard.cjs").installPipeGuard()'),
    "main.cjs 入口必须装管道兜底（重构时不许把它弄丢）",
  );
  const backendSrc = fs.readFileSync(path.join(__dirname, "..", "web", "electron", "backend.cjs"), "utf-8");
  assert.ok(
    /pumpBackendLine\(tag, text\)\s*\{[\s\S]*writableStream\(process\.stdout\)/.test(backendSrc),
    "pumpBackendLine 镜像前必须查 stdout 存活",
  );
});

t("E2E: 管道读端被关后，持续 console.log 不崩、自然退出", async () => {
  // 子进程脚本：与 main.cjs 入口同一动作（installPipeGuard）→ 持续 console.log。
  // 父进程关掉其 stdout 读端后，若无兜底子进程会 uncaughtException 崩掉
  // （退出码非 0），有兜底则活到自然退出。
  // 为什么不直接 require main.cjs：whenReady 链会真 spawn python 后端、建窗、
  // 弹首启引导——那是 GUI 集成测试的活，这里只验管道静默这一件事。
  const childScript = path.join(os.tmpdir(), `vm-epipe-child-${process.pid}.cjs`);
  const guardPath = path.join(__dirname, "..", "web", "electron", "pipe-guard.cjs");
  fs.writeFileSync(
    childScript,
    `
const { installPipeGuard } = require(${JSON.stringify(guardPath)});
installPipeGuard();
process.stdout.write("READY\\n");
let n = 0;
const timer = setInterval(() => { console.log("tick", ++n); }, 20);
setTimeout(() => { clearInterval(timer); process.stdout.write("DONE\\n"); process.exit(0); }, 1500);
`,
    "utf-8",
  );

  await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [childScript], { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    let pipeClosed = false;
    const timer = setTimeout(() => {
      try { child.kill(); } catch {}
      reject(new Error("E2E 超时：子进程 8s 未退出"));
    }, 8000);

    child.stdout.on("data", (d) => {
      if (!pipeClosed && String(d).includes("READY")) {
        // 用户场景：启动者（父进程）退出/关闭管道读端
        child.stdout.destroy();
        pipeClosed = true;
      }
    });
    child.stderr.on("data", (d) => { stderr += String(d); });
    child.on("error", (e) => { clearTimeout(timer); reject(e); });
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      try { fs.unlinkSync(childScript); } catch {}
      assert.ok(pipeClosed, "测试自身问题：没有成功关掉管道读端");
      assert.strictEqual(
        code, 0,
        `子进程应静默活到自然退出（实际 code=${code} signal=${signal} stderr=${stderr.slice(0, 300)}）—— ` +
        "EPIPE 又升级成未捕获异常了",
      );
      assert.ok(!/EPIPE/.test(stderr), `stderr 不应出现未捕获的 EPIPE：${stderr.slice(0, 300)}`);
      resolve();
    });
  });
});

// ---------- 收尾 ----------
(async function main() {
  let pass = 0, fail = 0;
  for (const { name, fn } of checks) {
    try {
      await fn();
      pass += 1;
      process.stdout.write(`  ✓ ${name}\n`);
    } catch (e) {
      fail += 1;
      process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`);
    }
  }
  process.stdout.write(`\n[test-backend] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();

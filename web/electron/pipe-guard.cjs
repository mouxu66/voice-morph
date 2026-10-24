// 坏管道（EPIPE）静默 + stdout 存活判断。
//
// 背景（2026-10-24，用户实测"错误框一直在弹"）：
// 主进程的 stdout/stderr 是「启动者给的管道」。当启动者退出或关闭管道读端
// （从终端 / 启动器 / 其他工具里拉起本应用后再结束，就会走到这条分支；
//   双击 exe 启动时 stdout 为空管道，不会犯），主进程每写一次 console.log
// 都会拿到 `EPIPE: broken pipe, write`。Electron 主进程没有任何默认兜底，
// 会把每次写失败弹成「A JavaScript error occurred in the main process」
// 模态框 —— 而 Python 后端持续打日志（backend.cjs 逐行转发到 console.log），
// 框就会一个接一个弹个不停。日志文件（BACKEND_LOG）与功能本身都不受影响，
// 所以管道类错误一律静默；未知错误仍然抛出，不掩蔽真问题。
"use strict";

const PIPE_ERROR_CODES = new Set([
  "EPIPE", // 管道读端已关：没人看主进程日志，不是错误
  "ERR_STREAM_DESTROYED", // 流已销毁（含管道关闭后的连带状态）
  "ERR_STREAM_WRITE_AFTER_END", // 流已结束还写：同上，日志场景无害
]);

function isPipeError(err) {
  return !!err && PIPE_ERROR_CODES.has(err.code);
}

/**
 * 给主进程装管道兜底：
 *  1. stdout/stderr 的 'error' 事件 —— 不监听时 Node 会把它抛成未捕获异常；
 *  2. process 的 'uncaughtException' —— 兜住漏网的同步 throw。
 * 两者都只静默管道类错误，其余原样抛出（交给默认崩溃路径/上层日志）。
 *
 * 幂等：重复调用会重复挂监听，但 handler 无副作用（仅按 code 放行），可接受；
 * 生产上只在 main.cjs 入口调一次。
 */
function installPipeGuard(streams) {
  const targets = streams || [process.stdout, process.stderr];
  for (const s of targets) {
    if (s && typeof s.on === "function") {
      s.on("error", (err) => {
        if (isPipeError(err)) return;
        throw err;
      });
    }
  }
  process.on("uncaughtException", (err) => {
    if (isPipeError(err)) return;
    throw err;
  });
}

/**
 * stdout 是否还值得写。管道关闭后 `destroyed` 为真、`writable` 为 false；
 * 安装版形态下没人读主进程 stdout，此时镜像日志纯属给 EPIPE 攒弹窗。
 */
function writableStream(s) {
  return !!s && !s.destroyed && s.writable !== false;
}

module.exports = { isPipeError, installPipeGuard, writableStream, PIPE_ERROR_CODES };

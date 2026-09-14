// electron 桩装配器（测试公共件，不是主进程模块；没有任何顶层副作用）
//
// 为什么需要它
// ------------
// `web/electron/*.cjs` 在顶层 `require("electron")`。要在纯 Node 里加载它们、并断言
// 真实行为（不只是"能不能 require"），就得让 "electron" 解析到我们的桩。
//
// 历史写法是 `require.resolve("electron", { paths: [webRoot] })` 拿到真实入口再顶替
// `require.cache[entry]`。这有个**环境依赖**：本机必须装过 `web/node_modules`
// （即跑过 `npm install`）。于是：
//   · CI 的 backend job 不装 npm 包 → 这些 node 脚本在 CI 上根本跑不起来
//   · 结果就是它们**长期只在本机凭自觉跑**，烂了也没人知道
//     （2026-09-14 接入 check.py 时当场发现 test-setup-ipc.cjs 里 3 条已经红了）
//
// 本模块改成**完全不做磁盘解析**：给桩注册一个合成 id，再把 `Module._resolveFilename`
// 对 "electron" 这一次请求重定向过去。没装 electron 的机器上照样能用 ——
// 门禁跑得起来的门禁才是门禁。
//
// 为什么放在 `web/electron/` 而不是 `tools/`
// ------------------------------------------
// `smoke-loadpath.cjs` 与受测模块在同一个目录，用同目录相对路径引用最稳；而且
// `web/electron/**` 会**打进 app.asar**，若桩放在 asar 外（`tools/` 只进
// `extraResources`），打包后那条 require 就是死路径。`tools/electron-stub.cjs`
// 保留为薄转发，供 `tools/test-*.cjs` 使用。
//
// 用法（必须在 require 任何 `web/electron/*.cjs` **之前**调用）：
//
//     const { installElectronStub } = require("./electron-stub.cjs");
//     installElectronStub({ app: { ... }, dialog: { ... } });
const Module = require("module");

/** 合成模块 id：故意不是合法路径，避免与真实文件撞车 */
const STUB_ID = "__vm_electron_stub__";

let installed = false;
let prevResolve = null;

/**
 * 装入 electron 桩。
 *
 * @param {object} exportsObj 桩要导出的对象（app / dialog / ipcMain / shell / …）
 *   可传一个**对象**，也可传返回对象的函数（需要每次调用生成新桩时用）。
 * @returns {string} 合成模块 id（一般用不到，调试时看 require.cache 用）
 * @throws {Error} 重复调用且传了不同桩 —— 覆盖已有桩会让"测的是什么"变得含糊
 */
function installElectronStub(exportsObj) {
  const make = typeof exportsObj === "function" ? exportsObj : () => exportsObj || {};
  if (installed) {
    require.cache[STUB_ID].exports = make();
    return STUB_ID;
  }
  require.cache[STUB_ID] = {
    id: STUB_ID,
    filename: STUB_ID,
    loaded: true,
    exports: make(),
  };
  prevResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    if (request === "electron") return STUB_ID;
    return prevResolve.call(this, request, ...rest);
  };
  installed = true;
  return STUB_ID;
}

/** 取回桩对象，供测试中途改行为（如切换 dialog 的返回值） */
function stubExports() {
  if (!installed) throw new Error("electron 桩还没装 —— 先调 installElectronStub()");
  return require.cache[STUB_ID].exports;
}

/** 卸掉桩并还原解析函数（测试自清理用；进程退出前不调也无妨） */
function uninstallElectronStub() {
  if (!installed) return;
  Module._resolveFilename = prevResolve;
  delete require.cache[STUB_ID];
  installed = false;
  prevResolve = null;
}

module.exports = { STUB_ID, installElectronStub, stubExports, uninstallElectronStub };

// 薄转发：实现放在 `web/electron/electron-stub.cjs`（与受测模块同目录，且打包后路径有效）。
//
// 这里保留这个入口是为了让 `tools/test-*.cjs` 的引用路径保持稳定
// （`require("./electron-stub.cjs")`），同时避免出现两份实现漂移。
//
// 背景与用法见 `web/electron/electron-stub.cjs` 的文件头。
module.exports = require("../web/electron/electron-stub.cjs");

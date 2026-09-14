// electron-builder.nosign.cjs —— 仅供本地自测：跳过代码签名。
//
// 由 `scripts/release.ps1 -SkipSign` 使用；正式打包仍走 package.json 的 build 字段。
//
// 为什么需要单独一份配置：electron-builder 判定"要不要签名"用的是
//   `chooseNotNull(signtoolOptions?.certificateFile, certificateFile) != null`
// —— 只看**是不是 null**，不看真假。所以：
//   `-c.win.certificateFile=`   → 空串被当成证书路径 → `ENOENT: open ''`
//   `-c.win.certificateFile=null` → CLI 不做 JSON 解析 → 被当成文件名 "null"
// 两条 CLI 路都走不通，只能从配置层面把该字段置成真正的 null。
const pkg = require("./package.json");

module.exports = {
  ...pkg.build,
  win: { ...pkg.build.win, certificateFile: null },
};

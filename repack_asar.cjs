// 临时脚本：把 .asar_tmp 重新打包为 app.asar
const asar = require("D:/变声/web/node_modules/@electron/asar");
asar.createPackage("D:/变声/.asar_tmp", "D:/变声/voice-morph-desktop/resources/app.asar")
  .then(() => console.log("repacked OK"))
  .catch((e) => { console.error("repack failed:", e); process.exit(1); });

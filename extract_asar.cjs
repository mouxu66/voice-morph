// 临时脚本：解包 app.asar 到 .asar_tmp
const asar = require("D:/变声/web/node_modules/@electron/asar");
asar.extractAll("D:/变声/voice-morph-desktop/resources/app.asar", "D:/变声/.asar_tmp");
console.log("extracted to .asar_tmp");

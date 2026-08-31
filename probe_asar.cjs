const asar = require("D:/变声/web/node_modules/@electron/asar");
const files = asar.listPackage("D:/变声/voice-morph-desktop/resources/app.asar");
console.log("total files:", files.length);
for (const f of files) {
  if (!f.startsWith("\\node_modules")) console.log(f);
}

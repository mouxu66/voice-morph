// 校验重打包后的 app.asar：动作词汇 + 小剧场 + 面板 + 根顺序
const asar = require("D:/变声/web/node_modules/@electron/asar");
const ASAR = "D:/变声/voice-morph-desktop/resources/app.asar";
const m = asar.extractFile(ASAR, "electron\\main.cjs").toString();
const p = asar.extractFile(ASAR, "electron\\pet\\pet.html").toString();
const pl = asar.extractFile(ASAR, "electron\\pet\\preload.cjs").toString();
const NEW_MOTIONS = ["shake", "float", "wiggle", "flip", "bounce", "dance"];
console.log("root_order(D:\\变声 before resources/backend):",
  m.indexOf('"D:\\\\变声"') < m.indexOf('path.join(res, "backend")'));
console.log("petGuideSent 随机庆祝动作:", m.includes("moves[Math.floor(Math.random() * moves.length)]"));
console.log("pet.html 新动作 CSS:", NEW_MOTIONS.every((x) => p.includes("m-" + x) && p.includes("@keyframes m-" + x)));
console.log("pet.html 新动作注册:", p.includes('"shake", "float", "wiggle", "flip", "bounce", "dance"'));
console.log("pet.html 待机小剧场:", p.includes("IDLE_SKITS") && p.includes("nextSkitAt"));
console.log("pet.html 面板:", p.includes('id="panel"'), "| 单击发送:", pl.includes("sendLast"));

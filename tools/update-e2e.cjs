// 更新链路端到端自检（无 UI）：真实请求清单 → 比较版本 → 下载安装包 → sha256 校验。
// 日常内测桌面端自动更新全链路用。退出码 0 = 全链路可用；非 0 = 不可用。
//
// 用法：
//   1) 先起静态目录（提供 exe + latest.json）：
//        python -m http.server 9000 --directory web\release2
//   2) 再跑本脚本（VM_UPDATE_URL 指向上面那个源）：
//        set VM_UPDATE_URL=http://127.0.0.1:9000/latest.json && node tools/update-e2e.cjs
//
// 说明：桩 electron（纯 Node 可加载 updater.cjs），currentVersion 模拟为 0.2.0，
// 用于验证"从 0.2.0 检到 0.2.1"的升级判定与下载校验，不触碰真实安装。
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "vm-update-e2e-"));
// 桩注入必须以 web 侧 node_modules 的 electron 入口为 key（updater.cjs 从 web/electron 解析，
// 会命中同一个 realpath；在 tools/ 上下文 require.resolve("electron") 会解析不到）。
const webRoot = path.join(__dirname, "..", "web");
const electronEntry = require.resolve("electron", { paths: [webRoot] });
require.cache[electronEntry] = {
  id: electronEntry, filename: electronEntry, loaded: true,
  exports: { app: { getPath: () => userDataDir, getVersion: () => "0.2.0" } },
};
const updater = require("../web/electron/updater.cjs");

(async () => {
  const r = await updater.checkForUpdates();
  if (!r.ok) throw new Error(`checkForUpdates 失败: ${JSON.stringify(r)}`);
  if (!r.configured) throw new Error("未配置更新源（需要 VM_UPDATE_URL）");
  if (!r.hasUpdate) throw new Error(`应检测到新版本（current=${r.current}, latest=${r.latest && r.latest.version}）`);
  if (updater.compareVersion(r.latest.version, r.current) <= 0) {
    throw new Error(`latest(${r.latest.version}) 应高于 current(${r.current})`);
  }
  if (!r.latest.sha256 || !/^[0-9a-f]{64}$/.test(r.latest.sha256)) {
    throw new Error("清单必须带 64 位 sha256（安全策略）");
  }

  const dl = await updater.downloadUpdate(r.latest, () => {});
  if (!dl.ok) throw new Error(`下载安装包失败: ${dl.reason}`);
  if (!fs.existsSync(dl.file)) throw new Error(`安装包未落盘: ${dl.file}`);
  const size = fs.statSync(dl.file).size;
  if (size < 10 * 1024 * 1024) throw new Error(`安装包不应这么小（${size} 字节）`);

  process.stdout.write(
    `[update-e2e] 通过 ✓\n` +
    `  版本判定: ${r.current} -> ${r.latest.version}\n` +
    `  清单来源: ${process.env.VM_UPDATE_URL}\n` +
    `  下载校验: ${path.basename(dl.file)} (${size} B, sha256 匹配)\n`);
})().catch((e) => {
  console.error("[update-e2e] 失败:", e.message);
  process.exit(1);
});
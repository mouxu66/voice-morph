// 外部资源「自动发现 + 下载指引」专项测试
//
// 运行：node tools/test-model-scan.cjs —— 退出码 0 = 通过，非 0 = 失败。
//
// 覆盖：
//   A. 指引数据完整性（它是 UI 的唯一信息源，缺字段就是 UI 缺一块）
//   B. 扫描正确性：找得到 / 不误报 / 排序与推荐
//   C. 扫描边界：预算、剪枝、黑名单、kinds 过滤、显式 roots
//
// 手法：造真临时目录树 + 真 fs，不 mock。所有扫描都用 `roots` 把范围钉在临时目录里
// （否则会去扫本机盘符，慢且不确定）。
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const guides = require("../web/electron/model-guides.cjs");
const modelSetup = require("../web/electron/model-setup.cjs");
const scan = require("../web/electron/model-scan.cjs");

const checks = [];
function t(name, fn) { checks.push({ name, fn }); }

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function mkdirs(...dirs) {
  for (const d of dirs) fs.mkdirSync(d, { recursive: true });
}

function write(p, body = "x") {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, body, "utf-8");
}

// ---------- 夹具 ----------

/** 一棵"完备"的 RVC 整合包（对齐官方 release 的目录形态） */
function makeRvc(root) {
  const r = path.join(root, "RVC");
  mkdirs(
    path.join(r, "infer"), path.join(r, "logs"), path.join(r, "assets", "weights"),
    path.join(r, "assets", "pretrained_v2"), path.join(r, "configs"), path.join(r, "i18n"),
  );
  write(path.join(r, "go-webui.bat"));
  return r;
}

/** TTS 模型目录（两个子目录名必须逐字对齐 config.py 的推导） */
function makeTtsModels(root) {
  const m = path.join(root, "tts_models");
  mkdirs(path.join(m, "qwen3-tts-1.7b-base"), path.join(m, "qwen3-tts-tokenizer-12hz"));
  write(path.join(m, "qwen3-tts-1.7b-base", "config.json"), "{}");
  write(path.join(m, "qwen3-tts-1.7b-base", "model.safetensors"));
  return m;
}

/** 与 tts_models 同项目根的 venv312（约定布局），可选装 qwen_tts / torch */
function makeVenv(projectRoot, { qwen = true, torch = true } = {}) {
  const venv = path.join(projectRoot, "tts_trial", "venv312");
  const site = path.join(venv, "Lib", "site-packages");
  mkdirs(path.join(venv, "Scripts"), site);
  write(path.join(venv, "Scripts", "python.exe"));
  if (qwen) mkdirs(path.join(site, "qwen_tts-0.1.1.dist-info"));
  if (torch) mkdirs(path.join(site, "torch"));
  return path.join(venv, "Scripts", "python.exe");
}

// ============================================================
// A. 指引数据完整性
// ============================================================

t("指引: 每个可选路径项都有对应指引（跨模块一致性）", () => {
  const setupIpc = require("../web/electron/setup-ipc.cjs");
  const pickKeys = Object.keys(setupIpc.PICK_SPECS).sort();
  const guideKeys = Object.keys(guides.GUIDES).sort();
  assert.deepStrictEqual(
    guideKeys, pickKeys,
    "PICK_SPECS 与 GUIDES 的键必须逐个对齐 —— 少一个就是 UI 上有个项没有下载指引",
  );
});

t("指引: 每条链接都是 https 且 label 非空（同一项内不重复）", () => {
  for (const g of Object.values(guides.GUIDES)) {
    assert.ok(g.links.length > 0, `${g.key} 一条链接都没有`);
    const seen = new Set();
    for (const l of g.links) {
      assert.ok(l.label && l.label.trim(), `${g.key} 有条链接没有 label`);
      assert.ok(/^https:\/\//.test(l.url), `${g.key} 的链接不是 https：${l.url}`);
      assert.ok(!seen.has(l.url), `${g.key} 内链接重复：${l.url}`);
      seen.add(l.url);
    }
  }
  // 跨项共用同一条官方链接是**合理**的（Qwen3-TTS 仓库同时是模型与解释器的出处），
  // 不做全局去重 —— 那会逼着我们为了通过测试而删掉该有的链接。
});

t("指引: 每项都要有体积、用途、目标布局、步骤", () => {
  for (const g of Object.values(guides.GUIDES)) {
    assert.ok(g.sizeText, `${g.key} 缺体积说明`);
    assert.ok(g.why, `${g.key} 缺用途说明`);
    assert.ok(g.layout.length > 0, `${g.key} 缺目标目录布局`);
    assert.ok(g.steps.length > 0, `${g.key} 缺操作步骤`);
  }
});

t("指引: TTS 模型的 local_dir 用的是本应用要求的目录名，不是上游仓库名", () => {
  // 这是最容易让用户"下完了却仍报未配置"的一处：上游叫 Qwen3-TTS-12Hz-1.7B-Base，
  // 本应用找 qwen3-tts-1.7b-base。命令里必须写后者。
  const g = guides.guideFor("tts_models");
  const cmd = g.steps.map((s) => s.command || "").join("\n");
  assert.ok(cmd.includes(guides.QWEN_BASE_DIR), "命令里要出现本地目录名 qwen3-tts-1.7b-base");
  assert.ok(cmd.includes(guides.QWEN_TOK_DIR), "命令里要出现本地目录名 qwen3-tts-tokenizer-12hz");
  assert.ok(
    cmd.includes(guides.QWEN_BASE_REPO) && cmd.includes(guides.QWEN_TOK_REPO),
    "命令里要出现上游仓库名",
  );
  assert.ok(!cmd.includes(`local_dir <项目根>\\tts_models\\${guides.QWEN_BASE_REPO.split("/")[1]}`),
    "不能把上游仓库名当成本地目录名");
});

t("resolveLink: 只认常量链接，越界/未知 kind 一律 null（渲染层无法打开任意地址）", () => {
  const ok = guides.resolveLink("tts_models", 0);
  assert.ok(ok && ok.url.startsWith("https://"));
  assert.strictEqual(guides.resolveLink("tts_models", 999), null);
  assert.strictEqual(guides.resolveLink("tts_models", -1), null);
  assert.strictEqual(guides.resolveLink("tts_models", 1.5), null);
  assert.strictEqual(guides.resolveLink("tts_models", "0x0"), null);
  assert.strictEqual(guides.resolveLink("不存在", 0), null);
});

t("allGuides: 返回副本，调用方改动不会污染模块常量", () => {
  const a = guides.allGuides();
  a[0].links[0].url = "https://evil.example.com";
  a[0].label = "被改了";
  const b = guides.allGuides();
  assert.notStrictEqual(b[0].links[0].url, "https://evil.example.com");
  assert.notStrictEqual(b[0].label, "被改了");
});

// ============================================================
// B. 扫描正确性
// ============================================================

t("scan: 三项都能在构造的目录树里找到", async () => {
  const root = tmp("vm-scan-all-");
  const rvc = makeRvc(root);
  const models = makeTtsModels(root);
  const py = makeVenv(root);

  const r = await scan.scan({ roots: [root] });
  assert.deepStrictEqual(r.candidates.rvc_root.map((c) => c.path), [rvc]);
  assert.deepStrictEqual(r.candidates.tts_models.map((c) => c.path), [models]);
  assert.deepStrictEqual(r.candidates.tts_venv.map((c) => c.path), [py]);
});

t("scan: 首选候选带 recommended，且同 kind 内只有一个", async () => {
  const root = tmp("vm-scan-rec-");
  makeRvc(path.join(root, "a"));
  makeRvc(path.join(root, "b"));
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  const rec = r.candidates.rvc_root.filter((c) => c.recommended);
  assert.strictEqual(rec.length, 1, "推荐位只能有一个");
  assert.strictEqual(rec[0].path, r.candidates.rvc_root[0].path);
});

t("scan: 得分高的排在前面（用它当推荐）", async () => {
  const root = tmp("vm-scan-rank-");
  // 弱：infer/ + logs/ = 5 分，刚好过线（真实世界里"像但不完整"的那种）
  const weak = path.join(root, "weak");
  mkdirs(path.join(weak, "infer"), path.join(weak, "logs"));
  // 强：完整整合包
  const strong = makeRvc(root);
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.ok(r.candidates.rvc_root.length >= 2, "弱但合格的候选也应列出，让用户自己判断");
  assert.strictEqual(r.candidates.rvc_root[0].path, strong);
  assert.ok(r.candidates.rvc_root[0].score > r.candidates.rvc_root[1].score);
});

t("scan: 只有 tools/ 的普通项目目录不算 RVC（不误报）", async () => {
  const root = tmp("vm-scan-fp-");
  const proj = path.join(root, "某个项目");
  mkdirs(path.join(proj, "tools"), path.join(proj, "infer")); // 连 infer 都有，但不够
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.ok(
    !r.candidates.rvc_root.some((c) => c.path === proj),
    "含 tools/ + infer/ 但没有整合包特征的目录不该被推荐（会误导用户）",
  );
});

t("scan: 弱命中不剪枝 —— 含 tools/ 的目录仍要往下找（回归 2026-09-14）", async () => {
  // 真踩过的坑：`D:\变声` 因为含 tools/ + .venv 被 checkRvcRoot 放行，
  // 当时按"命中即剪枝"把它的子树整个剪掉，于是同一台机器上的
  // `D:\变声\tts_models` 永远扫不出来。这条钉住"弱命中不剪枝"。
  const root = tmp("vm-scan-prune-");
  const project = path.join(root, "变声");
  mkdirs(path.join(project, "tools"), path.join(project, ".venv", "Scripts"));
  write(path.join(project, ".venv", "Scripts", "python.exe"));
  const models = makeTtsModels(project);

  const r = await scan.scan({ roots: [root] });
  assert.deepStrictEqual(
    r.candidates.tts_models.map((c) => c.path), [models],
    "弱命中（RVC 得分不过线）决不能阻止继续下探",
  );
});

t("scan: 强命中的子树不再下探（RVC 里上万文件，探进去是白烧预算）", async () => {
  const root = tmp("vm-scan-cut-");
  const rvc = makeRvc(root);
  // 在 RVC 内部埋一个"陷阱"：看起来像另一个 RVC 的目录
  const trap = path.join(rvc, "assets", "deep", "AnotherRVC");
  makeRvc(path.join(rvc, "assets", "deep"));
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.ok(
    !r.candidates.rvc_root.some((c) => c.path === trap),
    "整合包内部不该再被当作候选的父级继续下探",
  );
  assert.strictEqual(r.candidates.rvc_root[0].path, rvc);
});

t("scan: node_modules / 系统目录等黑名单不进入扫描范围", async () => {
  const root = tmp("vm-scan-skip-");
  const rvc = makeRvc(path.join(root, "node_modules")); // 藏在 node_modules 里
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.ok(
    !r.candidates.rvc_root.some((c) => c.path === rvc),
    "node_modules 下的目录不扫（否则每个前端项目的依赖树都要走一遍）",
  );
});

t("scan: 没有 qwen-tts 的裸 venv 不作为 TTS 解释器候选", async () => {
  const root = tmp("vm-scan-barevenv-");
  const bare = makeVenv(root, { qwen: false, torch: false });
  const r = await scan.scan({ roots: [root], kinds: ["tts_venv"] });
  assert.deepStrictEqual(r.candidates.tts_venv, [], "没装 qwen-tts 的环境推荐给用户是误导");
  void bare;
});

t("scan: 只扫「解释器」时也能靠 tts_models 的约定布局推出来", async () => {
  // 模型目录存在的副作用是"旁边的解释器可以推出来"。只勾解释器时若不走这条路，
  // 用户会被告知扫不到 —— 而它其实就躺在隔壁。
  const root = tmp("vm-scan-derive-");
  makeTtsModels(root);
  const py = makeVenv(root);
  const r = await scan.scan({ roots: [root], kinds: ["tts_venv"] });
  assert.strictEqual(r.candidates.tts_venv.length, 1);
  assert.strictEqual(r.candidates.tts_venv[0].path, py);
  assert.ok(
    r.candidates.tts_venv[0].reasons.some((x) => x.includes("约定布局")),
    "要说明这是怎么推导出来的，用户才知道能不能信",
  );
});

t("scan: kinds 过滤生效（只要 RVC 就不去翻 TTS）", async () => {
  const root = tmp("vm-scan-kinds-");
  makeRvc(root);
  makeTtsModels(root);
  makeVenv(root);
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.deepStrictEqual(Object.keys(r.candidates), ["rvc_root"]);
});

// ============================================================
// C. 扫描边界
// ============================================================

t("scan: 目录数预算触顶时标 truncated，但仍返回已找到的", async () => {
  const root = tmp("vm-scan-budget-");
  for (let i = 0; i < 40; i += 1) mkdirs(path.join(root, `dir${String(i).padStart(2, "0")}`));
  makeRvc(path.join(root, "dir39"));
  const r = await scan.scan({ roots: [root], maxDirs: 5, kinds: ["rvc_root"] });
  assert.strictEqual(r.stats.truncated, true, "触预算必须如实上报，UI 才会说'可能不全'");
  assert.ok(r.stats.dirsVisited <= 5);
});

t("scan: 时间预算触顶也标 truncated（注入 now，不真等）", async () => {
  const root = tmp("vm-scan-time-");
  for (let i = 0; i < 30; i += 1) mkdirs(path.join(root, `d${i}`));
  let clock = 0;
  const r = await scan.scan({
    roots: [root], kinds: ["rvc_root"], timeBudgetMs: 1,
    now: () => { clock += 500; return clock; },
  });
  assert.strictEqual(r.stats.truncated, true);
});

t("scan: 空目录/不存在的根不抛异常，返回空结果", async () => {
  const root = tmp("vm-scan-empty-");
  const r = await scan.scan({ roots: [root, path.join(root, "不存在")] });
  assert.deepStrictEqual(r.candidates.tts_models, []);
  assert.deepStrictEqual(r.candidates.rvc_root, []);
  assert.deepStrictEqual(r.candidates.tts_venv, []);
  assert.strictEqual(r.stats.truncated, false);
});

t("scan: 每个 kind 最多回传 MAX_PER_KIND 个候选", async () => {
  const root = tmp("vm-scan-cap-");
  for (let i = 0; i < scan.MAX_PER_KIND + 4; i += 1) {
    makeRvc(path.join(root, `pkg${String(i).padStart(2, "0")}`));
  }
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.strictEqual(r.candidates.rvc_root.length, scan.MAX_PER_KIND);
});

t("scan: 不跟符号链接（junction 能成环，也可能牵出整棵网络盘）", async () => {
  const outside = tmp("vm-scan-target-");
  const outsideRvc = makeRvc(outside);
  const root = tmp("vm-scan-link-");
  let linked = true;
  try {
    fs.symlinkSync(outside, path.join(root, "link"), "junction");
  } catch {
    linked = false; // 权限不足（非管理员）——跳过这条断言，别让它变成假红
  }
  if (!linked) return;
  const r = await scan.scan({ roots: [root], kinds: ["rvc_root"] });
  assert.ok(
    !r.candidates.rvc_root.some((c) => c.path.includes("link")),
    "符号链接/junction 不跟随",
  );
  void outsideRvc;
});

t("candidateRoots: 去重、只回真存在的目录、extras 的父级也算", () => {
  const root = tmp("vm-scan-roots-");
  const deep = path.join(root, "a", "b", "tts_models");
  mkdirs(deep);
  const roots = scan.candidateRoots([deep]);
  assert.strictEqual(new Set(roots).size, roots.length, "根目录列表不能有重复");
  for (const p of roots) assert.ok(fs.statSync(p).isDirectory(), `${p} 应是目录`);
  assert.ok(roots.includes(deep));
  assert.ok(roots.includes(path.dirname(deep)), "父目录也要试 —— 用户可能挪过一层");
  const again = scan.candidateRoots([deep]);
  assert.deepStrictEqual(roots, again, "顺序要稳定，否则推荐结果每次都不一样");
});

t("rank: 名字像目标的排前面（预算有限时先走像的）", () => {
  assert.ok(scan.rank("D:\\RVC") > scan.rank("D:\\WeGameApps"));
  assert.ok(scan.rank("/x/tts_models") > scan.rank("/x/random"));
  assert.strictEqual(scan.rank("/x/random"), 0);
});

t("scan: 空目录/不存在路径不产生候选（不拿错误路径骗用户）", async () => {
  const root = tmp("vm-scan-bad-");
  const fake = path.join(root, "tts_models"); // 名字对，但里面什么都没有
  mkdirs(fake);
  const r = await scan.scan({ roots: [root], kinds: ["tts_models"] });
  assert.deepStrictEqual(r.candidates.tts_models, []);
  assert.ok(!modelSetup.checkTtsModels(fake).ok, "与面板判据一致：名字对但缺文件不算配好");
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
  process.stdout.write(`\n[test-model-scan] ${pass} 通过, ${fail} 失败\n`);
  process.exit(fail === 0 ? 0 : 1);
})();

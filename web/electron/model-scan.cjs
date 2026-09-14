// 外部资源自动发现：在本机找出可用的 TTS 模型 / TTS 解释器 / RVC 整合包
//
// 为什么要它：首启引导原先只会弹一个"请选择目录"的空选择器。用户知道要选"RVC
// 整合包"，但不知道它在哪 —— 尤其当模型早就躺在本机、只是没配过的时候。让人工在
// 十几个盘符里翻目录，是把系统的活推给人。
//
// 设计要点
// --------
// 1. **不阻塞界面**：全程 `fs.promises`，靠 await 让出事件循环。同步递归扫描会冻住
//    主进程窗口，用户看到的是"点了一下就卡住"。绝不 fs.readdirSync。
// 2. **判定不与面板分叉**：最终"这个目录到底行不行"一律交给 `model-setup.cjs` 的
//    校验函数。本模块只负责"哪些目录值得拿出来验"，同一份判据只有一个来源。
//    （目录名预筛 → 官方校验确认，两级；预筛错了顶多漏一个候选，不会产生假候选。）
// 3. **预算硬约束**：目录数 + 墙上时间双上限。宁可少找到几个，也不能让首启卡住。
//    命中即剪枝（RVC 整合包里有上万个文件，探进去纯属浪费）。
// 4. **优先队列而非朴素 BFS**：同层里名字像目标的先走。这点很关键 —— 预算有限时，
//    `D:\RVC` 应该排在 `D:\WeGameApps` 前面。
// 5. **不跟符号链接**：junction 能成环，也可能牵出整棵网络盘/备份盘。
const fs = require("fs");
const path = require("path");
const os = require("os");

const modelSetup = require("./model-setup.cjs");
const guides = require("./model-guides.cjs");

// ---------------------------------------------------------------------------
// 扫描黑名单：这些目录名**不下探**（系统目录 / 包管理器缓存 / 与目标无关的大树）
// 注意：venv 类目录**不在**这里 —— 它们本身可能就是候选，要靠"访问它"才能判定；
// 只是不下探它们的子目录（见 classify 后的剪枝）。
// ---------------------------------------------------------------------------
const SKIP_NAMES = new Set([
  // Windows 系统与备份
  "$recycle.bin", "system volume information", "windows", "winsxs", "recovery",
  "perflogs", "program files", "program files (x86)", "programdata", "msocache",
  "$windows.~bt", "$windows.~ws", "$winreagent", "config.msi", "intel", "amd",
  "nvidia corporation", "drivers",
  // 开发无关的大树
  "node_modules", ".git", ".svn", ".hg", "__pycache__", ".idea", ".vscode",
  "site-packages", "dist-packages", ".pytest_cache", ".mypy_cache", ".ruff_cache",
  ".next", ".nuxt", ".turbo", ".parcel-cache", "target", "vendor",
  // 包管理器 / 语言运行时缓存
  ".cache", ".nuget", ".gradle", ".m2", ".cargo", ".npm", ".conda", ".ollama",
  "anaconda3", "miniconda3", "miniforge3", "scoop", "pip", "wheels",
  // 容器 / 虚拟机 / 临时
  "wsl", "docker", "vmware", "virtualbox", "temp", "tmp",
]);

/** 名字命中这些片段时，同层优先下探（预算有限，先走像的） */
const HINT_RE = /(rvc|voice|tts|qwen|varlet|模型|音色|变声|语音|声音|下载|download|ai[-_]?tool)/i;

/** venv 目录名形态（用于识别"这可能是一个 Python 环境"） */
const VENV_NAME_RE = /^(\.?venv\d*|venv\d+|env\d*|py3\d{1,2}|python-?3\.\d{1,2})$/i;

const MAX_DEPTH = 3;
const DEFAULT_MAX_DIRS = 3000;
const DEFAULT_TIME_BUDGET_MS = 3000;

/** 候选低于这个分就不值得展示（避免把"恰好有个 tools/ 的项目目录"当成 RVC） */
const MIN_SCORE = { tts_models: 4, tts_venv: 4, rvc_root: 5 };
/** 每个 kind 最多回传多少个候选（UI 是折叠列表，不需要更多） */
const MAX_PER_KIND = 8;

const KIND_LABEL = { tts_models: "Qwen3-TTS 模型", tts_venv: "TTS 专用解释器", rvc_root: "RVC 整合包" };

function isDirSync(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function isFileSync(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/** 读一个目录的条目名（失败返回 null，不抛）。 */
async function readNames(dir) {
  try {
    const entries = await fs.promises.readdir(dir, { withFileTypes: true });
    const dirs = new Set();
    const files = new Set();
    for (const e of entries) {
      // 符号链接一律不进（成环 + 可能牵出网络盘）
      if (e.isSymbolicLink()) continue;
      if (e.isDirectory()) dirs.add(e.name);
      else if (e.isFile()) files.add(e.name);
    }
    return { dirs, files };
  } catch {
    return null; // 无权限 / 不存在 / 是文件
  }
}

/**
 * 候选根目录：盘符根 + 用户常用目录 + 已知约定位置 + 调用方给的位置。
 * 顺序即"下探优先级"（同层内先排这些），盘符根放最后 —— 它们最杂。
 */
function candidateRoots(extra = []) {
  const out = [];
  const add = (p) => {
    if (typeof p !== "string" || !p.trim()) return;
    const norm = path.resolve(p.trim().replace(/^"(.*)"$/, "$1"));
    if (!out.includes(norm)) out.push(norm);
  };

  const home = os.homedir();
  // 1) 已知约定位置：成本最低、命中率最高
  for (const p of ["D:\\RVC", "D:\\变声", "C:\\RVC", path.join(home, "RVC")]) add(p);
  // 2) 调用方给的（已配置路径 + 环境变量），连同其上一两层 —— 用户可能挪过目录
  for (const p of extra) {
    add(p);
    add(path.dirname(p));
    add(path.dirname(path.dirname(p)));
  }
  // 3) 用户目录与最常见的落盘位置
  add(home);
  for (const sub of [
    "Downloads", "Desktop", "Documents", "Documents\\Downloads",
    "下载", "桌面", "文档",
  ]) add(path.join(home, sub));
  // 4) 盘符根
  if (process.platform === "win32") {
    for (let c = 0x43; c <= 0x5a; c++) add(`${String.fromCharCode(c)}:\\`); // C: → Z:
  } else {
    add("/");
    add("/opt");
    add("/home");
  }
  add("D:\\");
  add("E:\\");

  return out.filter(isDirSync);
}

/**
 * 目录名 → RVC 整合包得分。只在 `checkRvcRoot` 已通过的前提下调用。
 * 对 `assets/` 最多多做一次 readdir（候选数量极少，可忽略）。
 */
async function scoreRvcRoot(dir, names) {
  let score = 0;
  const reasons = [];
  const bump = (n, why) => { score += n; reasons.push(why); };

  if (names.dirs.has("infer")) bump(3, "含 infer/");
  if (names.dirs.has("rvc")) bump(3, "含 rvc/");
  if (names.dirs.has("logs")) bump(2, "含 logs/");
  if (names.dirs.has("tools")) bump(1, "含 tools/");
  if (names.dirs.has("configs")) bump(1, "含 configs/");
  if (names.dirs.has("i18n")) bump(1, "含 i18n/");
  if (names.dirs.has("dataset_raw")) bump(1, "含 dataset_raw/");
  if (names.files.has("go-webui.bat") || names.files.has("go-realtime_gui.bat")) {
    bump(3, "含启动脚本");
  }
  if (names.dirs.has("assets")) {
    const a = await readNames(path.join(dir, "assets"));
    if (a) {
      if (a.dirs.has("weights")) bump(3, "含 assets/weights/（音色权重目录）");
      if (a.dirs.has("pretrained_v2") || a.dirs.has("pretrained")) bump(3, "含自带底模");
      if (a.dirs.has("hubert_base")) bump(2, "含 hubert 特征提取底模");
    }
  }
  const hasRuntime =
    isFileSync(path.join(dir, "runtime", "python.exe")) ||
    isFileSync(path.join(dir, ".venv", "Scripts", "python.exe"));
  if (hasRuntime) bump(2, "含自带 Python 运行时");

  return { score, reasons };
}

/** 目录名 → TTS 模型目录得分。只在 `checkTtsModels` 已通过的前提下调用。 */
function scoreTtsModels(dir, names) {
  let score = 4; // 通过校验本身已是最强证据（目录名 + config.json 都对）
  const reasons = ["含 qwen3-tts-1.7b-base 与分词器"];

  if (isFileSync(path.join(dir, guides.QWEN_BASE_DIR, "model.safetensors"))) {
    score += 2;
    reasons.push("权重文件已就位");
  }
  if (path.basename(dir).toLowerCase() === "tts_models") {
    score += 1;
    reasons.push("目录名符合约定 tts_models/");
  }
  const chain = modelSetup.deriveTtsChain(dir);
  if (chain.found) {
    score += 3;
    reasons.push("同项目根下有 TTS 解释器");
  }
  if (names.dirs.has("ref")) {
    score += 1;
    reasons.push("含 ref/ 兜底参考音目录");
  }
  return { score, reasons };
}

/**
 * Python 环境 → TTS 解释器得分。只判"像不像能跑 Qwen3-TTS 的环境"：
 * 装了 qwen-tts 是最强证据；只有 torch 或什么都没有的 venv 不应被推荐。
 */
async function scoreTtsVenv(venvDir, names, bonus = 0, bonusReason = "") {
  let score = bonus;
  const reasons = bonusReason ? [bonusReason] : [];

  if (!names.dirs.has("Scripts")) return null;
  const py = path.join(venvDir, "Scripts", "python.exe");
  if (!isFileSync(py)) return null;

  if (VENV_NAME_RE.test(path.basename(venvDir))) {
    score += 1;
    reasons.push("目录名是虚拟环境");
  }
  const site = await readNames(path.join(venvDir, "Lib", "site-packages"));
  if (site) {
    const installed = (prefix) => [...site.dirs].some((n) => n.toLowerCase().startsWith(prefix));
    if (installed("qwen_tts")) {
      score += 4;
      reasons.push("已装 qwen-tts");
    }
    if (installed("torch")) {
      score += 2;
      reasons.push("已装 torch");
    }
  }
  return { score, reasons, pythonPath: py };
}

/** 把候选塞进结果表：同路径合并（取高分、并集理由） */
function merge(list, cand) {
  const hit = list.find((c) => c.path === cand.path);
  if (!hit) {
    list.push(cand);
    return;
  }
  if (cand.score > hit.score) hit.score = cand.score;
  for (const r of cand.reasons) if (!hit.reasons.includes(r)) hit.reasons.push(r);
}

/**
 * 扫描本机。
 *
 * @param {object} [opts]
 * @param {Array<"tts_models"|"tts_venv"|"rvc_root">} [opts.kinds] 只找这些（默认全部）
 * @param {string[]} [opts.roots] **显式指定根目录**（给了就用它，不再猜）；测试用
 * @param {string[]} [opts.extraRoots] 额外根目录（通常传已配置路径，让旧配置能被重新找到）
 * @param {number} [opts.maxDirs] 访问目录数上限
 * @param {number} [opts.timeBudgetMs] 墙上时间上限
 * @param {number} [opts.maxDepth]
 * @param {() => number} [opts.now] 时间源（测试注入）
 * @returns {Promise<{candidates: object, stats: object}>}
 */
async function scan(opts = {}) {
  const kinds = opts.kinds && opts.kinds.length ? opts.kinds.slice() : Object.keys(KIND_LABEL);
  const maxDirs = opts.maxDirs || DEFAULT_MAX_DIRS;
  const budget = opts.timeBudgetMs || DEFAULT_TIME_BUDGET_MS;
  const maxDepth = opts.maxDepth == null ? MAX_DEPTH : opts.maxDepth;
  const now = opts.now || Date.now;
  const started = now();

  const candidates = Object.fromEntries(kinds.map((k) => [k, []]));
  const visited = new Set();
  let dirsVisited = 0;
  let truncated = false;

  const wantTts = kinds.includes("tts_models");
  const wantRvc = kinds.includes("rvc_root");
  const wantVenv = kinds.includes("tts_venv");

  // roots 显式给定时完全接管 —— 测试靠这条把扫描关进临时目录树，
  // 否则会真去扫本机 C:/D: 盘，既慢又不确定。
  const roots = opts.roots && opts.roots.length
    ? opts.roots.filter(isDirSync)
    : candidateRoots(opts.extraRoots);
  // 队列按"层"推进：每层只在进入时排一次序（同层优先命中 HINT 的），
  // 而不是每弹一个元素就全排 —— 后者在 3000 次的量级上是白烧的 CPU。
  let frontier = roots.map((dir) => ({ dir, depth: 0 }));
  let stop = false;

  while (frontier.length && !stop) {
    frontier.sort((a, b) => rank(b.dir) - rank(a.dir));
    const next = [];
    for (const { dir, depth } of frontier) {
      if (dirsVisited >= maxDirs || now() - started > budget) {
        truncated = true;
        stop = true;
        break;
      }
      if (visited.has(dir)) continue;
      visited.add(dir);

      const names = await readNames(dir);
      if (!names) continue;
      dirsVisited += 1;

      const base = path.basename(dir).toLowerCase();
      // decisive：命中且证据**决定性**时才剪枝（RVC 整合包里上万文件，探进去纯属浪费）。
      // 实测踩过：`D:\变声` 因为含 `tools/` + `.venv` 也被 checkRvcRoot 放行（得分刚好过线），
      // 若按"命中即剪"就会把它的子树整个剪掉，于是同一台机器上 `D:\变声\tts_models`
      // 永远扫不出来 —— 弱命中宁可多探一层，也不要漏。
      let decisive = false;

      // ---- 判定本目录 ----
      // 注意 `(wantTts || wantVenv)`：TTS 模型目录存在的**副作用**是"同项目根的
      // 解释器可以按约定布局推出来"。只勾了「解释器」时也要走这条路，否则用户
      // 会得到"扫不到解释器"，而它其实就躺在旁边。
      const looksTtsDir = names.dirs.has(guides.QWEN_BASE_DIR) && names.dirs.has(guides.QWEN_TOK_DIR);
      if (looksTtsDir && (wantTts || wantVenv) && modelSetup.checkTtsModels(dir).ok) {
        if (wantTts) {
          const { score, reasons } = scoreTtsModels(dir, names);
          merge(candidates.tts_models, { kind: "tts_models", path: dir, score, reasons });
        }
        decisive = true; // 子目录名逐字对齐，不存在误判
        // 顺带把约定布局里推导出的解释器也算进来，用户不用再找一遍
        if (wantVenv) {
          const chain = modelSetup.deriveTtsChain(dir);
          if (chain.found) {
            const venvDir = path.dirname(path.dirname(chain.venvPy)); // .../venv312
            const vnames = await readNames(venvDir);
            const v = vnames
              ? await scoreTtsVenv(venvDir, vnames, 3, "与 TTS 模型同项目根（约定布局）")
              : null;
            if (v && v.score >= MIN_SCORE.tts_venv) {
              merge(candidates.tts_venv, {
                kind: "tts_venv", path: v.pythonPath, score: v.score, reasons: v.reasons,
              });
            }
          }
        }
      }

      if (wantRvc && ["infer", "rvc", "logs", "assets", "tools"].some((n) => names.dirs.has(n))) {
        if (modelSetup.checkRvcRoot(dir).ok) {
          const { score, reasons } = await scoreRvcRoot(dir, names);
          if (score >= MIN_SCORE.rvc_root) {
            merge(candidates.rvc_root, { kind: "rvc_root", path: dir, score, reasons });
            // 决定性证据：只有真整合包才有这些，普通项目目录不会有
            decisive =
              names.dirs.has("infer") ||
              names.files.has("go-webui.bat") ||
              names.files.has("go-realtime_gui.bat");
          }
        }
      }

      if (wantVenv && VENV_NAME_RE.test(base) && names.dirs.has("Scripts")) {
        const v = await scoreTtsVenv(dir, names);
        if (v && v.score >= MIN_SCORE.tts_venv) {
          merge(candidates.tts_venv, {
            kind: "tts_venv", path: v.pythonPath, score: v.score, reasons: v.reasons,
          });
        }
        decisive = true; // 环境内部没有我们要找的东西，别再往里探
      }

      // ---- 决定下探 ----
      if (depth >= maxDepth || decisive) continue;
      for (const child of names.dirs) {
        const lc = child.toLowerCase();
        if (SKIP_NAMES.has(lc) || lc.startsWith("$")) continue;
        const full = path.join(dir, child);
        if (!visited.has(full)) next.push({ dir: full, depth: depth + 1 });
      }
    }
    frontier = next;
  }

  // ---- 排序 + 截断 + 标推荐 ----
  const out = {};
  for (const kind of kinds) {
    const sorted = candidates[kind]
      .sort((a, b) => b.score - a.score || a.path.length - b.path.length || a.path.localeCompare(b.path))
      .slice(0, MAX_PER_KIND);
    sorted.forEach((c, i) => { c.recommended = i === 0; });
    out[kind] = sorted;
  }

  return {
    candidates: out,
    stats: {
      dirsVisited,
      truncated,
      elapsedMs: Math.max(0, now() - started),
      maxDirs,
      roots: roots.length,
    },
  };
}

/** 同层优先级：命中提示片段 +1，已知约定路径额外加权 */
function rank(dir) {
  const base = path.basename(dir);
  let r = HINT_RE.test(base) ? 1 : 0;
  const norm = dir.replace(/\\/g, "/").toLowerCase();
  if (/(^|\/)rvc$/.test(norm)) r += 3;
  if (/(^|\/)(变声|voice-?morph)$/.test(norm)) r += 3;
  if (/(^|\/)tts_models$/.test(norm)) r += 2;
  if (/(^|\/)tts_trial$/.test(norm)) r += 2;
  return r;
}

module.exports = {
  SKIP_NAMES,
  HINT_RE,
  VENV_NAME_RE,
  MAX_DEPTH,
  MIN_SCORE,
  MAX_PER_KIND,
  DEFAULT_MAX_DIRS,
  DEFAULT_TIME_BUDGET_MS,
  KIND_LABEL,
  candidateRoots,
  rank,
  scoreRvcRoot,
  scoreTtsModels,
  scoreTtsVenv,
  scan,
};

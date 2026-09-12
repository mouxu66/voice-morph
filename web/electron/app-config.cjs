// 应用级持久化配置：userData/config.json
//
// 存在意义（干净机器首启引导）：安装包不带模型（4.9G 留在包外），换一台没有
// D:\变声 / D:\RVC 的机器时，用户通过界面选定模型目录 → 落盘到这里 → 下次 spawn
// 后端时注入 VM_* 环境变量。详见 model-setup.cjs 与 backend.cjs externalResourceEnv。
//
// 设计约束：
// - **绝不抛异常**：文件缺失/损坏/JSON 解析失败一律回落默认值。首启引导本身是
//   "救火"链路，读取配置再炸掉会让应用彻底打不开，代价不对称。
// - **原子写**：先写 config.json.tmp 再 renameSync 覆盖，避免写一半断电留坏档。
// - 懒加载 + 每次都重新读磁盘：配置可能被用户在别处改（手动编辑 / 重新选目录），
//   缓存会让"改完不生效"变成新的坑。
const fs = require("fs");
const path = require("path");
const { app } = require("electron");

const FILE_NAME = "config.json";

/** 配置默认值（空字符串 = 未配置，交给后端 config.py 的默认推导） */
const DEFAULTS = {
  version: 1,
  ttsModelsDir: "",
  ttsVenvPy: "",
  rvcRoot: "",
  // 用户是否已处理过首启引导（点过"去配置"或"稍后配置"）。
  // 置 true 后不再自动弹窗，避免每次启动都骚扰；UI 侧仍有常驻降级提示。
  setupSeen: false,
};

/** 用户可配置的路径字段白名单（save 只接受这些键，防止写入任意垃圾） */
const PATH_KEYS = ["ttsModelsDir", "ttsVenvPy", "rvcRoot"];

function configPath() {
  return path.join(app.getPath("userData"), FILE_NAME);
}

/** 把任意来源的对象规整成合法配置：类型不对的字段回落默认值，未知键丢弃。 */
function normalize(raw) {
  const out = { ...DEFAULTS };
  if (!raw || typeof raw !== "object") return out;
  for (const key of PATH_KEYS) {
    const v = raw[key];
    if (typeof v === "string" && v.trim()) {
      // 去掉首尾空白与包裹引号（用户从资源管理器复制路径常带引号）
      out[key] = v.trim().replace(/^"(.*)"$/, "$1");
    }
  }
  out.setupSeen = raw.setupSeen === true;
  out.version = Number.isInteger(raw.version) ? raw.version : DEFAULTS.version;
  return out;
}

/** 读取配置；文件不存在或损坏时返回默认值（不抛、不写回）。 */
function load() {
  try {
    const text = fs.readFileSync(configPath(), "utf-8");
    return normalize(JSON.parse(text));
  } catch {
    return { ...DEFAULTS };
  }
}

/** 写入配置（原子：tmp + rename）。返回 true 表示落盘成功。 */
function save(patch) {
  const merged = normalize({ ...load(), ...(patch || {}) });
  const file = configPath();
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(merged, null, 2), "utf-8");
    fs.renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}

/** 清空所有路径配置（保留 setupSeen），用于"重置引导"。 */
function reset() {
  return save({ ...DEFAULTS, setupSeen: load().setupSeen });
}

/** 是否已配置至少一项路径（UI 据此决定是否显示"未配置"入口）。 */
function anyConfigured(config = null) {
  const c = config || load();
  return PATH_KEYS.some((k) => Boolean(c[k]));
}

module.exports = {
  FILE_NAME,
  DEFAULTS,
  PATH_KEYS,
  configPath,
  normalize,
  load,
  save,
  reset,
  anyConfigured,
};

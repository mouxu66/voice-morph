// 模型/引擎环境检测 + 路径推导（纯逻辑，不碰 electron API，便于测试）
//
// 干净机器首启引导的核心判定层：给定一份配置，判断 TTS 模型 / TTS 专用解释器 /
// RVC 整合包是否真的可用，并给出每项缺失原因。
//
// 为什么不能只判"目录存在"：用户很可能把 D:\变声 整个选进来，或选了 tts_models 的
// 上一级。只判 exists 会放进一个"看着配好了、一合成就崩"的假状态，比不配更糟。
// 因此每项都按后端真实消费的结构校验（与 system_api.py /diagnose 判据对齐）。
const fs = require("fs");
const path = require("path");

/** 目录是否包含任一子目录名 */
function hasAnyDir(root, names) {
  return names.some((n) => fs.existsSync(path.join(root, n)));
}

function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** TTS 模型目录校验：必须同时具备 Qwen3-TTS 基座（含 config.json）与 12hz 分词器。
 *  与 config.py 的 QWEN_MODEL_DIR / QWEN_TOKENIZER_DIR 推导严格一致。 */
function checkTtsModels(dir) {
  if (!dir) return { ok: false, reason: "未配置" };
  if (!isDir(dir)) return { ok: false, reason: `目录不存在：${dir}` };
  const model = path.join(dir, "qwen3-tts-1.7b-base");
  const tok = path.join(dir, "qwen3-tts-tokenizer-12hz");
  const missing = [];
  if (!isFile(path.join(model, "config.json"))) missing.push("qwen3-tts-1.7b-base/config.json");
  if (!isDir(tok)) missing.push("qwen3-tts-tokenizer-12hz/");
  if (missing.length) {
    return { ok: false, reason: `缺少 ${missing.join("、")}（请选择包含这两项的 tts_models 目录）` };
  }
  return { ok: true, reason: "" };
}

/** TTS 专用解释器校验（tts_trial/venv312 里的 python.exe）。 */
function checkTtsVenv(py) {
  if (!py) return { ok: false, reason: "未配置" };
  if (isFile(py)) return { ok: true, reason: "" };
  return { ok: false, reason: `解释器不存在：${py}` };
}

/** RVC 整合包校验：目录存在且含典型子目录（与 /diagnose 的 rvc_root 判据一致）。 */
function checkRvcRoot(root) {
  if (!root) return { ok: false, reason: "未配置" };
  if (!isDir(root)) return { ok: false, reason: `目录不存在：${root}` };
  if (!hasAnyDir(root, ["rvc", "infer", "logs", "tools"])) {
    return { ok: false, reason: "不像 RVC 整合包（缺 rvc/ infer/ logs/ tools/ 等子目录）" };
  }
  return { ok: true, reason: "" };
}

/**
 * 从「TTS 模型目录」推导整条 TTS 链所需路径。
 *
 * 用户只需要选一次模型目录 —— venv 解释器由约定结构推导，避免让用户单独去
 * 定位 venv312\Scripts\python.exe 这种反人类路径。
 * 布局（本机实测 D:\变声）：
 *     <root>/tts_models/                        ← 用户选的
 *     <root>/tts_trial/venv312/Scripts/python.exe
 * 因此若所选目录名为 tts_models，其父目录即项目根。
 *
 * @returns {{ projectRoot: string, venvPy: string, found: boolean }}
 */
function deriveTtsChain(modelsDir) {
  if (!modelsDir) return { projectRoot: "", venvPy: "", found: false };
  const projectRoot = path.basename(modelsDir).toLowerCase() === "tts_models"
    ? path.dirname(modelsDir)
    : modelsDir; // 用户直接选了项目根
  const venvPy = path.join(projectRoot, "tts_trial", "venv312", "Scripts", "python.exe");
  return { projectRoot, venvPy, found: isFile(venvPy) };
}

/**
 * 生效配置解析：用户显式配置 > 推导补全。
 * 返回用于注入 VM_* 的最终值（可能为空 = 没配置且推不出来）。
 */
function resolveConfig(config) {
  const c = config || {};
  const chain = deriveTtsChain(c.ttsModelsDir || "");
  return {
    ttsModelsDir: c.ttsModelsDir || "",
    // 用户手填优先；没填就从模型目录推
    ttsVenvPy: c.ttsVenvPy || (chain.found ? chain.venvPy : ""),
    // venv312 所在项目根 → qwen3_tts.py 的 VM_PROJECT_ROOT（拼 worker 脚本/解释器路径用）
    projectRoot: chain.found ? chain.projectRoot : "",
    rvcRoot: c.rvcRoot || "",
  };
}

/**
 * 全面检测（供首启引导与 UI 面板共用）。
 * @param {object} config app-config 读出的配置
 * @param {object} [env] 用于对照的进程环境变量（默认 process.env）——用户显式设了
 *   VM_* 环境变量时，即使 config.json 为空也算"已配置"，不能误报缺失。
 */
function checkModelEnv(config, env = process.env) {
  const resolved = resolveConfig(config);
  const envTts = env.VM_TTS_MODELS_DIR || "";
  const envVenv = env.VM_TTS_VENV_PY || "";
  const envRvc = env.VM_RVC_ROOT || "";

  const items = [];

  const ttsDir = resolved.ttsModelsDir || envTts;
  const ttsCheck = checkTtsModels(ttsDir);
  items.push({
    key: "tts_models",
    label: "Qwen3-TTS 模型",
    ok: ttsCheck.ok,
    path: ttsDir,
    source: resolved.ttsModelsDir ? "config" : envTts ? "env" : "none",
    reason: ttsCheck.reason,
  });

  const venvPy = resolved.ttsVenvPy || envVenv;
  const venvCheck = checkTtsVenv(venvPy);
  items.push({
    key: "tts_venv",
    label: "TTS 专用解释器",
    ok: venvCheck.ok,
    path: venvPy,
    source: resolved.ttsVenvPy ? "derived" : envVenv ? "env" : "none",
    reason: venvCheck.reason,
  });

  const rvc = resolved.rvcRoot || envRvc;
  const rvcCheck = checkRvcRoot(rvc);
  items.push({
    key: "rvc_root",
    label: "RVC 整合包",
    ok: rvcCheck.ok,
    path: rvc,
    source: resolved.rvcRoot ? "config" : envRvc ? "env" : "none",
    reason: rvcCheck.reason,
  });

  // 「TTS 未配置」的判定口径：模型与解释器任一不可用 → TTS 不可用。
  // RVC 单独一档，因为两者可以独立缺失（只有 TTS 也能用离线合成）。
  const ttsOk = ttsCheck.ok && venvCheck.ok;
  return {
    items,
    ttsOk,
    rvcOk: rvcCheck.ok,
    // 需要引导的最小集合（缺模型或缺解释器都算，缺 RVC 也算 —— 干净机器通常两个都缺）
    missing: items.filter((i) => !i.ok).map((i) => i.key),
    allOk: items.every((i) => i.ok),
    resolved,
  };
}

module.exports = {
  isFile,
  isDir,
  checkTtsModels,
  checkTtsVenv,
  checkRvcRoot,
  deriveTtsChain,
  resolveConfig,
  checkModelEnv,
};

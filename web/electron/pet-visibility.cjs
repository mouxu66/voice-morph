// 桌宠显隐决策（纯逻辑，零 electron 依赖 → node 可直接单测）。
//
// 为什么单独一个文件：显隐规则从「模式 + 运行态」升级为「模式 + 运行态 + 性能档」之后，
// 分支组合变多（游戏档必须压过「常驻显示」），塞在 pet.cjs 的 setInterval 闭包里
// 只能靠真 GUI 手测。抽成纯函数后 tools/test-pet-visibility.cjs 能穷举整个矩阵。
//
// 规则（自上而下，先命中先返回）：
//   1. 用户手动隐藏              → 不显示（右键「隐藏桌宠」/ 设置面板关掉）
//   2. 游戏档（perf_profile=game）→ 不显示，且**压过常驻模式**
//   3. 常驻模式（mode=always）    → 显示
//   4. 级联/实时变声运行中        → 显示（挂实时字幕）
//   5. 页面导览进行中             → 显示（讲完自动退场）
const PERF_GAME = "game";

function shouldShowPet({ mode, userHidden, perfProfile, running, guideActive } = {}) {
  if (userHidden) return false;
  if (perfProfile === PERF_GAME) return false;
  if (mode === "always") return true;
  return Boolean(running) || Boolean(guideActive);
}

/**
 * 归一化 /api/rvc/live/status 的响应体。
 * 后端离线、返回非 JSON、字段缺失或类型不对时一律退化为「未运行 + 无性能档」，
 * 绝不让桌宠因为一次解析异常而卡在常驻或永不出现。
 */
function parseLiveStatus(payload) {
  const obj = payload && typeof payload === "object" ? payload : {};
  return {
    running: Boolean(obj.live_running),
    perfProfile: typeof obj.perf_profile === "string" ? obj.perf_profile : "",
  };
}

module.exports = { PERF_GAME, shouldShowPet, parseLiveStatus };

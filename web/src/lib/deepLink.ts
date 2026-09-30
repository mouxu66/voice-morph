/**
 * 首页两个动作各自的目标地址。
 *
 * 为什么值得单独一个模块：**这两个地址本身就是产品决策**，而且是那种
 * "写错一个字符不会有任何报错"的东西 ——
 *
 *   · 「录一句听听」→ `/offlinevc?voice=<id>`
 *     离线变声：录一句、几秒出结果。第一次体验该走这条（只要麦克风权限）。
 *   · 「开麦」→ `/live?tab=rvc&voice=<id>`
 *     实时变声：要先配虚拟声卡、把麦克风接进链路，是熟练之后的事。
 *
 * 一旦两者对调，页面看起来完全正常 —— 只是用户"点了之后要做的事"悄悄变难了，
 * 而这正是最难被发现的回归。放在这里是为了让 `useOfflineVc.test.tsx` 能直接钉住它们。
 */

/** 「录一句听听」：离线变声页 + 预选音色。 */
export function offlineVcDeepLink(voiceId: string): string {
  return `/offlinevc?voice=${encodeURIComponent(voiceId)}`
}

/** 「开麦」：实时变声页（rvc 标签） + 预选音色。 */
export function liveDeepLink(voiceId: string): string {
  return `/live?tab=rvc&voice=${encodeURIComponent(voiceId)}`
}

/**
 * 「使用模式」（套餐）与**逐项开关**之间的破坏性关系，收在这一处。
 *
 * 为什么需要这个模块 —— 2026-10-08 用户报障「在能力管理里把 17 项全开了，
 * 点立即重启，回来一看又全被关了」。查下来是一串**每一步都合理、合起来吃人**的行为：
 *
 *   1. 能力面板逐项开启 = 只动那一项（`POST /api/plugins/{id}/enable`），写进
 *      `outputs/plugins.json` 的 `disabled` 列表；
 *   2. 点「立即重启」= 重启后端 + 整页重载；
 *   3. 重载后 `ModeChooser` 重新挂载 —— 它**每次启动都弹**（设计如此：问「今天怎么用」），
 *      于是用户刚做完设置，迎面就是一个全屏「选择一个使用方式」；
 *   4. 用户随手点一个场景卡片 = `applyPluginPreset(id)` = **整套重设**：
 *      不在该套餐里的能力**全部写进 `disabled`**。刚开的 17 项当场清零。
 *
 * 前半段（第 1、2 步）与后半段（第 4 步）各自都是对的 —— 套餐本来就叫「套餐」，
 * 套用就是重设。错的是**用户看不见这两件事是同一份状态**：能力面板里那 17 个开关
 * 看起来是「我的设置」，模式卡片看起来是「我要用哪种模式」，点下去后者吃掉前者。
 * 后端注释里那句「真实事故：用户在 preserve 模式下怎么开都留不住」就是这件事的另一面。
 *
 * ---------------------------------------------------------------- 2026-10-15：语义已改
 *
 * 2026-10-08 的修法是「**不改语义、只补知情权**」（下面那两件事）。10-15 用户明确：
 * 知情权不够 —— 原话「切换模式会把我自己手动打开的功能给关掉 …… 不能这么搞」。
 *
 * 现在：
 *   · **默认切换 = 只增不减**（后端 `apply_preset` 默认 `strict=False`）：套餐只负责
 *     「补齐」，你手动开的一项也不会掉，所以点模式不需要确认；
 *   · **收窄变成显式动作**：想「只剩这一个套餐」要点专门的「严格套用（会关掉 N 项）」，
 *     那条路上才需要把要关的几项点名 —— `capabilitiesLostBy()` 现在**只服务它**。
 *
 * 本模块保留两件事：
 *   · `capabilitiesLostBy()`  —— 算出严格套用会关掉哪几项（可执行、可断言）；
 *   · `suppressModeChooserOnce()` —— 让「改完开关的重启」不再撞上模式选择页。
 *
 * ★ 判据一律用后端 **expand 过**的 `preset.plugins`（`plugin_manifest.catalog()` 里
 *   `sorted(preset_ids(name))`，已含 `core.*` 与 `requires` 闭包），前端**不重算依赖** ——
 *   依赖只该有一份判据，前端再写一遍就会随清单一改而漂（同 `CapabilityPanel` 头注释里
 *   「守卫只写在后端」那条）。
 */

import type { PluginCatalog, PluginEntry } from "@/types"

/**
 * 「这一次重载别再弹模式选择页」的一次性标记。
 *
 * 键名与 `ModeChooser` 内部常量**必须一致**：那边是消费方，这里是生产方，
 * 两边各写一份字面量就等于埋一个改一处忘一处的坑 —— 所以消费方也 import 本常量。
 * `sessionStorage` 而非 `localStorage`：语义是「这次重载」，关掉应用就该失效。
 */
export const MODE_SUPPRESS_ONCE_KEY = "vm.modechooser.suppress-once"

/**
 * 告诉 `ModeChooser`：**这一次**重载不是「用户刚点完模式」，别再弹选择页。
 *
 * 用在两处：① `ModeChooser` 自己选完模式（它要跳目标页，不能被自己再拦一次）；
 * ② 能力面板/侧栏的「立即重启」—— 用户刚在设置里逐项调完，重载后立刻被问
 * 「今天怎么用」，随手一点就会把刚调好的开关整套覆盖（就是本模块头注释那个事故）。
 *
 * 吞掉异常：`sessionStorage` 在隐私模式/受限环境可能直接抛（Safari 老版本、
 * 部分 WebView）。这里失败只意味着「选择页可能还会弹」，不该让重启本身失败。
 */
export function suppressModeChooserOnce(): void {
  try {
    sessionStorage.setItem(MODE_SUPPRESS_ONCE_KEY, "1")
  } catch {
    /* 没有 storage 就算了：宁可多弹一次，也不能让重启按钮炸掉 */
  }
}

/**
 * **严格套用** `targetId` 这套餐会**关掉**哪几项用户当前正开着的能力。
 *
 * ⚠️ 2026-10-15 起这只服务「严格套用」那一个**显式动作** —— 默认切换（只增不减）
 * 永远不关东西，所以默认路径**不该**调它（调了只会平白弹一条确认）。
 *
 * 返回空数组 = 这次严格套用不夺走任何东西（可以不加确认直接执行）。
 * 返回非空 = 这些能力会被后端写进 `disabled`（重启后真的不加载），**值得先问一句**。
 *
 * 为什么是精确的而不是估算：切套餐后 `off = 全部 - keep`，而 `keep` 已是闭包
 * （含 core 与 requires），所以读侧 `enabled_ids()` 的「被依赖而复活」不会再补出
 * 新成员 —— `当前开着 ∩ 不在套餐里` 恰好就是要消失的那批。
 *
 * `custom`（用户逐项改过）时这个集合通常很大，正是最该拦一下的场景。
 * 参数允许 `null`（catalog 还没加载完）：那种情况下答「不知道」= 空数组，
 * 不拿一个猜出来的结论去吓用户。
 */
export function capabilitiesLostBy(
  catalog: PluginCatalog | null,
  targetId: string,
): PluginEntry[] {
  const preset = catalog?.presets.find((p) => p.id === targetId)
  if (!catalog || !preset) return []
  const keep = new Set(preset.plugins)
  // `core.*` 不可关（后端会直接拒绝），即便清单写漏了也不该出现在「将被关闭」名单里
  return catalog.plugins.filter((p) => p.enabled && !p.core && !keep.has(p.id))
}

/**
 * 把「将被关闭」的清单说成一句人能读懂的话。
 *
 * 名字最多点 6 个再加「等 N 项」：确认条只有一行宽，列全了反而看不清重点 ——
 * 用户此刻只需要知道「**会掉东西**」以及「掉的是不是我想要的」。
 */
export function describeLost(names: string[]): string {
  const head = names.slice(0, 6).join("、")
  return names.length > 6 ? `${head} 等 ${names.length} 项` : head
}

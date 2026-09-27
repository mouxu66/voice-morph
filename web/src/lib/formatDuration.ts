/**
 * 时长格式化（`3:07` / `1:02:05`）。
 *
 * 为什么单独抽一个文件：翻唱页与在线扒歌页各写过一遍**逐字相同**的表达式，
 * 而那个表达式有个共同的错 —— 先 `Math.floor` 分、再对余数独立取整：
 *
 * ```ts
 * `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`
 * ```
 *
 * 当 `s % 60` 落在 `[59.5, 60)` 时，`Math.round` 把它抬成 **60**，
 * 于是出现 `3:60` 这种不存在的时刻（真实数据里很容易撞上：后端给的
 * `duration_s` 是浮点，`179.7` 就会显示成 `2:60`）。
 *
 * 正确做法是**先把总秒数取整到秒**，再从那个整数上切分与求余 ——
 * 分与秒来自同一个整数，余数天然 `< 60`，不可能进位到 60。
 */

/**
 * 把秒数格式化成 `分:秒`（超过一小时给 `时:分:秒`）。
 *
 * @param seconds 时长（秒）。负数与非有限值都当作 `0` —— 它只用于展示，
 *                为它抛错或显示 `NaN:NaN` 都不如显示 `0:00` 有用。
 * @param opts.forceHours 强制带小时位（`0:03:07`）。用于同一列表里对齐宽度。
 */
export function formatDuration(
  seconds: number,
  opts: { forceHours?: boolean } = {},
): string {
  // ★ 先归一到"整秒"，后面所有切分都基于它 —— 这是修掉 `3:60` 的关键一步。
  const total =
    Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds) : 0

  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  // 余数来自同一个整数 `total`，所以恒在 [0, 59]；`% 60` 只是消掉 h 的进位
  const s = total % 60

  const mm = String(m).padStart(h > 0 || opts.forceHours ? 2 : 1, "0")
  const ss = String(s).padStart(2, "0")

  if (h > 0 || opts.forceHours) return `${h}:${mm}:${ss}`
  return `${mm}:${ss}`
}

/**
 * 时长格式化的边界。
 *
 * 存在的理由：翻唱页与在线扒歌页各写过一遍**逐字相同**的表达式，两处都有同一个
 * bug —— 先 `Math.floor(s/60)` 分成、再对余数独立 `Math.round`。当余数落在
 * `[59.5, 60)` 时会进位成 60，显示 `3:60`（真实数据里很常见：后端 `duration_s`
 * 是浮点，`179.7` 就显示成 `2:60`）。
 *
 * 关键那条就是 `59.5`、`59.6`、`179.7` 这几个 —— 它们专打"余数独立取整"的写法。
 */
import { describe, expect, it } from "vitest"

import { formatDuration } from "@/lib/formatDuration"

describe("formatDuration", () => {
  it("常规值", () => {
    expect(formatDuration(0)).toBe("0:00")
    expect(formatDuration(7)).toBe("0:07")
    expect(formatDuration(59)).toBe("0:59")
    expect(formatDuration(60)).toBe("1:00")
    expect(formatDuration(125)).toBe("2:05")
  })

  it("★ 余数不许进位成 60（原实现显示 3:60 的那批值）", () => {
    // 逐条都对应用户真实会遇到的浮点：215.6s、179.7s、119.5s
    expect(formatDuration(59.5)).toBe("1:00")
    expect(formatDuration(59.6)).toBe("1:00")
    expect(formatDuration(119.5)).toBe("2:00") // 旧的写法是 1:60
    expect(formatDuration(179.7)).toBe("3:00") // 旧的写法是 2:60
    expect(formatDuration(215.6)).toBe("3:36")
  })

  it("★ 任何值都不产生 `:60`（全量扫一遍，别只挑几个点）", () => {
    for (let i = 0; i <= 4000; i += 1) {
      const s = i + 0.5 // 最容易触发进位的小数
      const got = formatDuration(s)
      expect(got).not.toMatch(/:60$/)
      expect(got).not.toMatch(/:60:/)
    }
  })

  it("整秒取整是四舍五入，不是截断（59.6 → 1:00 而不是 0:59）", () => {
    expect(formatDuration(59.4)).toBe("0:59")
    expect(formatDuration(59.6)).toBe("1:00")
  })

  it("超过一小时给时:分:秒", () => {
    expect(formatDuration(3600)).toBe("1:00:00")
    expect(formatDuration(3725)).toBe("1:02:05")
    expect(formatDuration(3599)).toBe("59:59")
  })

  it("forceHours 补齐宽度（同列表对齐用）", () => {
    expect(formatDuration(125, { forceHours: true })).toBe("0:02:05")
    expect(formatDuration(3725, { forceHours: true })).toBe("1:02:05")
  })

  it("垃圾输入显示 0:00，不显示 NaN", () => {
    for (const v of [NaN, Infinity, -Infinity, -1, -0.4]) {
      expect(formatDuration(v)).toBe("0:00")
    }
    // undefined/null 走调用方的 `r.duration_s ? ... : ""`，这里只管数字
    expect(formatDuration(0.4)).toBe("0:00")
  })
})

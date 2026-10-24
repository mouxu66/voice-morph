/**
 * `/autopilot` 页的三条纯函数门控。
 *
 * 为什么这些要在前端钉一遍（后端已经有 `normalize` 的校验）：
 * 托管是**唯一会替用户给别人发消息**的能力，而「能不能点开始」这件事
 * 前端说了算 —— 按钮亮着但后端 403/400，用户会以为是功能坏了；
 * 按钮该亮却不亮，用户以为授权没生效。所以三类判据必须可断言：
 *
 *  1. 白名单只能从会话列表里点选，重复点同一条不能堆出两份；
 *  2. 取消白名单时**必须连带清掉群聊标记** —— 否则重新加回来时
 *     「群聊」这个更严格的规则会莫名复活（白名单里已没有它，但 groups 里还在）；
 *  3. 「开始托管」要同时满足：已授权 + 白名单非空 + 当前 off。
 */
import { describe, expect, it } from "vitest"
import type { AutopilotConfig, AutopilotState } from "@/api/client"
import { canStart, withChat, withGroup } from "./useAutopilot"

function cfg(patch: Partial<AutopilotConfig> = {}): AutopilotConfig {
  return {
    whitelist: [],
    groups: [],
    me_names: [],
    question_only: true,
    skip_muted: true,
    poll_s: 20,
    min_gap_s: 45,
    max_per_chat_per_hour: 3,
    max_per_run: 20,
    quiet_hours: [23, 7],
    history_pages: 0,
    reply_max_chars: 30,
    ...patch,
  }
}

function state(status: AutopilotState["status"]): AutopilotState {
  return {
    ok: true,
    server_time: 0,
    status,
    stats: { ticks: 0, sent: 0, skipped: 0, errors: 0 },
    run_sent: 0,
    last_error: "",
    last_tick_at: 0,
    config: cfg(),
    recent: [],
    wechat: {},
    advisor: {},
  }
}

describe("withChat", () => {
  it("加进白名单；重复点同一条不会出现两份", () => {
    const once = withChat(cfg(), "117", true)
    expect(once.whitelist).toEqual(["117"])
    expect(withChat(once, "117", true).whitelist).toEqual(["117"])
  })

  it("取消白名单时**连带清掉群聊标记**（否则重新加回来时群聊规则会莫名复活）", () => {
    const on = withGroup(withChat(cfg(), "117", true), "117", true)
    expect(on.groups).toEqual(["117"])

    const off = withChat(on, "117", false)
    expect(off.whitelist).toEqual([])
    expect(off.groups).toEqual([])
  })

  it("不动别的会话与其它字段", () => {
    const before = cfg({ whitelist: ["a", "b"], groups: ["b"], reply_max_chars: 12 })
    const after = withChat(before, "c", true)
    expect(after.whitelist).toEqual(["a", "b", "c"])
    expect(after.groups).toEqual(["b"])
    expect(after.reply_max_chars).toBe(12)
    // 纯函数：不改入参
    expect(before.whitelist).toEqual(["a", "b"])
  })
})

describe("withGroup", () => {
  it("只有已在白名单里的会话才允许标记群聊", () => {
    expect(withGroup(cfg(), "117", true).groups).toEqual([])
    const on = withGroup(withChat(cfg(), "117", true), "117", true)
    expect(on.groups).toEqual(["117"])
    // 取消标记但仍在白名单里
    expect(withGroup(on, "117", false)).toMatchObject({ whitelist: ["117"], groups: [] })
  })
})

describe("canStart", () => {
  const ready = cfg({ whitelist: ["117"] })

  it("已授权 + 白名单非空 + 当前 off 才可点", () => {
    expect(canStart(state("off"), true, ready)).toBe(true)
  })

  it("没勾授权 → 不可点（后端会 403，前端不该先把按钮点亮）", () => {
    expect(canStart(state("off"), false, ready)).toBe(false)
  })

  it("白名单为空 → 不可点（后端会 400）", () => {
    expect(canStart(state("off"), true, cfg())).toBe(false)
  })

  it("已经在跑 / 暂停 / 停止中 → 不可点（避免重复点出第二条线程）", () => {
    for (const s of ["running", "paused", "stopping"] as const) {
      expect(canStart(state(s), true, ready), s).toBe(false)
    }
  })

  it("状态或草稿还没加载出来 → 不可点，不猜", () => {
    expect(canStart(null, true, ready)).toBe(false)
    expect(canStart(state("off"), true, null)).toBe(false)
  })
})

/**
 * 侧栏底部「能力」入口 —— 守的是**常驻**这件事本身。
 *
 * 为什么值得单开一个文件：插件化改造（`docs/插件化设计.md` 步 1–7）落的全是**骨架**
 * ——后端按清单挂路由、前端路由与侧栏由 `/api/plugins` 驱动、桌宠认能力清单。
 * 用户侧唯一摸得到的出口就是这一屏。而它曾经**条件渲染**：`closedNavCount > 0` 才出现。
 * 对"从没关过任何能力"的用户（也就是绝大多数），等于这个入口根本不存在 ——
 * 19 项能力的开关、套餐预设、关掉省多少空间，全在设置抽屉第二屏里隐身。
 * 2026-09-23 用户反馈「没感受到一切皆插件的思想」，根因就在这里。
 *
 * 所以下面第 1 条不是顺手补的边界，**它就是这次修复本身**：
 * 零异常（19 项全 ok）时入口必须仍然在，并且写着总项数。
 *
 * 另外钉住三条口径：
 * 1. 计数取自 `catalog.counts`，与「能力管理」面板头同源（别在底栏重算一遍，
 *    两处各算一套是典型的派生数据双流水线）；
 * 2. **只有「未加载」上红色** —— 已关闭是用户自己关的，只是陈述，不该天天报警；
 * 3. 未加载 > 已关闭 的优先级，与 `CapabilityPanel.rowStateOf` 同一条规则。
 */
import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router-dom"
import type { PluginCatalog } from "@/types"

const h = vi.hoisted(() => ({
  /** 「能力清单」这个 hook 的当前返回值，由每个用例改写 */
  catalog: { status: "loading" } as unknown,
}))

vi.mock("@/lib/pluginRoutes", () => ({
  usePluginCatalog: () => ({ state: h.catalog, reload: () => {} }),
}))

vi.mock("@/api/client", () => ({
  rvcLiveReset: vi.fn(async () => ({ ok: true })),
}))

// 侧栏导航与设置抽屉都不是被测对象：桩掉它们，断言才能只盯着底栏那一条入口。
vi.mock("@/components/voice-studio/StudioNav", () => ({
  StudioNav: () => <nav data-testid="studio-nav" />,
}))
vi.mock("@/components/layout/SettingsPanel", () => ({ SettingsPanel: () => null }))

import { AppChrome, capabilityEntryCounts } from "./AppChrome"

function catalog(counts: Partial<PluginCatalog["counts"]> = {}): PluginCatalog {
  return {
    ok: true,
    counts: { total: 19, ok: 19, broken: 0, disabled: 0, ...counts },
    plugins: [],
    loaders: { routers: 26, loaded: 26, broken: [] },
    restartRequired: true,
    presets: [],
    preset: "standard",
  }
}

const onOpenCapabilities = vi.fn()

function renderChrome() {
  return render(
    <MemoryRouter initialEntries={["/home"]}>
      <AppChrome
        petGuideEnabled
        onTogglePetGuide={() => {}}
        simpleMode={false}
        onToggleSimple={() => {}}
        onOpenEnv={() => {}}
        onOpenModel={() => {}}
        onOpenStorage={() => {}}
        onOpenLicenses={() => {}}
        onOpenUpdate={() => {}}
        onOpenCapabilities={onOpenCapabilities}
        onOpenChain={() => {}}
        version="0.2.4"
        canCheckUpdate={false}
      />
    </MemoryRouter>,
  )
}

/**
 * 底栏那条入口。用「文本以『能力』开头」定位而不是按可访问名 ——
 * 它的名字会随状态变（能力19 项 / 能力3 项已关闭…），拿名字当选择器会天天改。
 */
function capEntries(): HTMLElement[] {
  return screen.getAllByRole("button").filter((b) => b.textContent?.startsWith("能力"))
}

function capEntry(): HTMLElement {
  const found = capEntries()
  if (found.length !== 1) throw new Error(`期望恰好 1 个「能力」入口，实际 ${found.length} 个`)
  return found[0]
}

describe("AppChrome · 侧栏「能力」入口", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  /** ★ 本次修复本身：零异常时入口必须还在。 */
  it("★ 什么都没关、什么都没坏，入口仍然常驻并报总项数", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    expect(capEntry().textContent).toContain("19 项")
    // 全 ok 不上色 —— 沉默即正常，跟面板里的徽章策略是同一条
    expect(capEntry().className).not.toContain("text-red")
  })

  it("有已关闭时改报关闭数，且**不上红色**（用户自己关的，只是陈述）", () => {
    h.catalog = { status: "ready", catalog: catalog({ disabled: 3, ok: 16 }) }
    renderChrome()

    expect(capEntry().textContent).toContain("3 项已关闭")
    expect(capEntry().className).not.toContain("text-red")
  })

  it("有未加载时上红色，文案是「未加载」而不是「已关闭」", () => {
    h.catalog = { status: "ready", catalog: catalog({ broken: 2, ok: 17 }) }
    renderChrome()

    expect(capEntry().textContent).toContain("2 项未加载")
    expect(capEntry().className).toContain("text-red")
  })

  it("两种异常同时存在时「未加载」优先——少了块功能却不知道原因，比你自己关的更急", () => {
    h.catalog = { status: "ready", catalog: catalog({ broken: 1, disabled: 3, ok: 15 }) }
    renderChrome()

    expect(capEntry().textContent).toContain("1 项未加载")
    expect(capEntry().textContent).not.toContain("已关闭")
  })

  it("清单还没读到时入口也在，只是暂时不报数字", () => {
    h.catalog = { status: "loading" }
    renderChrome()

    expect(capEntry()).toBeInTheDocument()
    expect(capEntry().textContent).toBe("能力")
  })

  it("点它调到「能力管理」（窄屏抽屉里那份也调同一个）", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    fireEvent.click(capEntry())
    expect(onOpenCapabilities).toHaveBeenCalledTimes(1)
  })

  it("窄屏抽屉打开后入口出现两份——桌面侧栏与抽屉共用同一段 sidebarBody", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    expect(capEntries()).toHaveLength(1)
    fireEvent.click(screen.getByRole("button", { name: "打开导航" }))
    expect(capEntries()).toHaveLength(2)
  })
})

describe("capabilityEntryCounts", () => {
  it("三档文案与色调", () => {
    expect(capabilityEntryCounts({ total: 19, ok: 19, broken: 0, disabled: 0 })).toEqual({
      tone: "muted",
      label: "19 项",
    })
    expect(capabilityEntryCounts({ total: 19, ok: 16, broken: 0, disabled: 3 })).toEqual({
      tone: "muted",
      label: "3 项已关闭",
    })
    expect(capabilityEntryCounts({ total: 19, ok: 18, broken: 1, disabled: 0 })).toEqual({
      tone: "danger",
      label: "1 项未加载",
    })
  })

  it("★ 只有 broken 配得上 danger —— 加一档就别再往 disabled 上加颜色", () => {
    const tones = [
      capabilityEntryCounts({ total: 19, ok: 19, broken: 0, disabled: 0 }).tone,
      capabilityEntryCounts({ total: 19, ok: 16, broken: 0, disabled: 3 }).tone,
    ]
    expect(tones).toEqual(["muted", "muted"])
  })
})

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

function renderChrome(path = "/home") {
  return render(
    <MemoryRouter initialEntries={[path]}>
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

/**
 * 去掉的重复入口 —— 2026-09-25 用户点名「首页又有一个，这些肯定不需要」后清理的。
 *
 * 这几条测试的意义是**反向的**：它们断言某个东西**不在**。通常不该这么写测试
 * （"不存在"很容易被当成漏测），但这里成立，因为删掉的是**已经开花的重复**——
 * 每个都对应界面上一处真实的重复入口，而"删了又被人手滑加回来"是这个仓库
 * 反复出现过的模式（见 docs/犯错档案-工程.md 的"半新半旧"类教训）。
 *
 * 若不写这几条：将来谁想让 logo 重新可点（"别的应用都这样"）会一路绿灯，
 * 而用户当时明确说过这处多余。
 */
describe("AppChrome · 已删除的重复入口不得回归", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("★ 桌面侧栏品牌区不是链接——导航里的「首页」是唯一入口", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    const { container } = renderChrome()

    // 侧栏里唯一该指向 /home 的是 StudioNav（已桩成 nav），品牌区不能是 <a href="/home">。
    // 用「文本含变声工坊」定位品牌区而非数 <a>：窄屏顶栏那个 logo **是**链接
    // （见下一条），数总数会把两件事混成一个。
    const brandText = Array.from(container.querySelectorAll("a")).filter((a) =>
      (a.textContent ?? "").includes("变声工坊"),
    )
    expect(brandText).toHaveLength(0)
  })

  it("窄屏顶栏 logo 保留可点但不再带「变声工坊」字样——抽屉关上时它是唯一回首页的路", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    // 去掉字样不等于去掉入口：这是有意保留的，别误删
    const homeLinks = screen.getAllByRole("link", { name: "回首页" })
    expect(homeLinks).toHaveLength(1)
    expect(homeLinks[0]).toHaveAttribute("href", "/home")
    expect(homeLinks[0].textContent).not.toContain("变声工坊")
  })

  it("★ 侧栏底部不再有「一键恢复音频」——恢复动作只在自检弹窗里，那里会先说清当前设备", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    expect(screen.queryByText("一键恢复音频")).not.toBeInTheDocument()
  })

  it("★ 顶栏不再有「发送链路自检」图标——首页链路状态条是唯一入口", () => {
    h.catalog = { status: "ready", catalog: catalog() }
    renderChrome()

    expect(screen.queryByRole("button", { name: "发送链路自检" })).not.toBeInTheDocument()
  })
})

/**
 * 顶栏标题 —— 2026-09-27 修复：原来是手写的 `pageTitles` 表，「翻唱」「在线扒歌」
 * 两个新页面上线后顶栏一直显示"首页"（表里没这两条路径，静默回退），且没有测试守着。
 *
 * 现在标题从能力清单的 `nav.label` 派生。这几条钉住三件事：
 *   1. 有 nav 的路由 → 显示清单里的标签（新页面对此的回归保护）；
 *   2. `/tools` 在清单里**故意没有 nav**（它是侧栏底部的索引入口，不是导航项）
 *      → 走 NAVLESS_TITLES 覆盖，不能掉进"变声工坊"兜底；
 *   3. 未知路径 / 清单没到位 → 退回产品名，**绝不再显示一个错的页面名**。
 */
describe("AppChrome · 顶栏标题取自能力清单", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  /** 只放本块用到的四条路由（含两条没有 nav 的），其余字段喂满类型即可 */
  function catalogWithRoutes(): PluginCatalog {
    return {
      ...catalog(),
      plugins: [
        {
          id: "test.routes",
          name: "test.routes",
          kind: "builtin",
          category: "core",
          order: 0,
          summary: "",
          core: true,
          state: "ok",
          enabled: true,
          blockedBy: [],
          reasons: [],
          requires: [],
          routers: [],
          routes: [
            { path: "/home", module: "Home", export: "HomeRoute", nav: { label: "首页", icon: "Home", group: "start", order: 10 } },
            { path: "/cover", module: "Cover", export: "CoverRoute", nav: { label: "翻唱", icon: "Music4", group: "vc", order: 30 } },
            { path: "/ytdlp", module: "Ytdlp", export: "YtdlpRoute", nav: { label: "在线扒歌", icon: "CloudDownload", group: "vc", order: 35 } },
            // /tools 故意不带 nav —— 见 NAVLESS_TITLES 的注释
            { path: "/tools", module: "CapabilityIndex", export: "CapabilityIndexRoute" },
          ],
          legacyRoutes: [],
          extras: {},
          health: null,
          healthProbe: null,
          disableNote: "",
        },
      ],
    }
  }

  function title(): string {
    return screen.getByRole("heading", { level: 1 }).textContent ?? ""
  }

  it("★ 新页面也显示清单标签：/cover → 翻唱、/ytdlp → 在线扒歌（曾经的\"首页\"回归）", () => {
    h.catalog = { status: "ready", catalog: catalogWithRoutes() }

    const a = renderChrome("/cover")
    expect(title()).toBe("翻唱")
    a.unmount()

    renderChrome("/ytdlp")
    expect(title()).toBe("在线扒歌")
  })

  it("有 nav 的老页面不变：/home → 首页", () => {
    h.catalog = { status: "ready", catalog: catalogWithRoutes() }
    renderChrome("/home")
    expect(title()).toBe("首页")
  })

  it("/tools 在清单里没有 nav —— 走覆盖值\"能做的事\"，不掉进产品名兜底", () => {
    h.catalog = { status: "ready", catalog: catalogWithRoutes() }
    renderChrome("/tools")
    expect(title()).toBe("能做的事")
  })

  it("清单没有的路径 → 产品名，而不是一个错的页面名", () => {
    h.catalog = { status: "ready", catalog: catalogWithRoutes() }
    renderChrome("/no-such-page")
    expect(title()).toBe("变声工坊")
  })

  it("清单还没到位时也显示产品名（加载只有一瞬间，不能闪成一个错名字）", () => {
    h.catalog = { status: "loading" }
    renderChrome("/cover")
    expect(title()).toBe("变声工坊")
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

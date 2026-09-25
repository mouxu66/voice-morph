import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { USES, normalizeUseCase, profileOf } from "@/lib/useProfile"

/**
 * 「用途档案 + 首屏裁剪」的约定（P0-②，渐进式披露的入口）。
 *
 * 理论依据见 `docs/调研-用户需求与界面精简-2026-09-25.md`：
 * 用户抱怨的「功能冗余」是功能平铺得太早，行业解法是渐进式披露，
 * 最有效的一招是"首次启动问一句用途，然后按答案裁剪"（Canva 那种做法）。
 *
 * ★ 四条必须钉住的约定
 * -------------------
 *
 * ① **`null`（没定过）与 `"skip"`（选了"我还没想好"）是两件事**。
 *    用途本身用 `USE_KEY`、是否问过用 `USE_ASKED_KEY`，**分开存**。
 *    混成一个值 → 用户选了"先逛逛"下次启动又被弹一次（这是最容易犯的错）。
 *
 * ② **只改默认排序，不删东西**。调研明确写了「渐进式披露 ≠ 藏信息」，
 *    且功能少同样差评。所以用途只提供 `aim`（把哪条路提前），
 *    不存在"这个用途看不到某项功能"的表达。
 *
 * ③ **两个模态框必须串行**。首次启动会先弹用途问答、再弹意图引导
 *    （`FirstLaunchGuide`），后者靠 `ready` 门等前者结束 ——
 *    叠在一起是最糟的第一印象。
 *
 * ④ **门控**：`ChainStatusBar` 挂在核心首页上，却打两个可关能力的端点
 *    （`hook.wechat` / `sound.tts`）。被关掉的那格必须显示"该能力已关闭"
 *    而不是探成"链路断了"。这是本仓老坑（核心页裸渲染可关组件），
 *    门禁 `tools/audit_endpoint_ownership.py --check` 会直接报红。
 *
 * 为什么是源码断言：与 `crossPluginGate.test.ts`、`sfxMarkBar.test.ts` 同手法 ——
 * 要防的是"有人把条件改成无条件/把两处状态合并"，造后端桩的收益远低于成本。
 */

/** 统一成 LF：仓库里部分 .tsx 是 CRLF，多行断言不做归一化会莫名其妙地红。 */
function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8").replace(/\r\n/g, "\n")
}

const libSrc = readRel(path.join("lib", "useProfile.ts"))
const pickerSrc = readRel(path.join("components", "UseCasePicker.tsx"))
const guideSrc = readRel(path.join("components", "FirstLaunchGuide.tsx"))
const appSrc = readRel(path.join("App.tsx"))
const homeSrc = readRel(path.join("pages", "Home", "HomePage.tsx"))
const barSrc = readRel(path.join("components", "ChainStatusBar.tsx"))
const advSrc = readRel(path.join("components", "AdvancedSection.tsx"))
const setSrc = readRel(path.join("components", "layout", "SettingsPanel.tsx"))

describe("用途档案：两个 key 必须分开存（约定 ①）", () => {
  it("用途与「问过了吗」是两个独立的 key", () => {
    expect(libSrc).toContain('export const USE_KEY = "vm-use-profile"')
    expect(libSrc).toContain('export const USE_ASKED_KEY = "vm-use-asked"')
  })

  it("setUseCase 同时落两个 key —— 否则选完下次还弹", () => {
    // 同时出现两行 setItem 才说明两个都写了
    expect(libSrc).toMatch(/setItem\(USE_KEY[\s\S]{0,120}setItem\(USE_ASKED_KEY/)
  })

  it("`null` 与 `skip` 语义不同：normalize 把非法值归一成 null，不吞掉 skip", () => {
    expect(normalizeUseCase(null)).toBeNull()
    expect(normalizeUseCase("")).toBeNull()
    expect(normalizeUseCase("乱写的")).toBeNull()
    // skip 是合法值，不能被归一成 null —— 否则"我还没想好"会被当成"没问过"
    expect(normalizeUseCase("skip")).toBe("skip")
    expect(normalizeUseCase("game")).toBe("game")
  })
})

describe("用途档案：只改排序不删功能（约定 ②）", () => {
  it("每个用途只声明 aim（推到前面的是谁），没有「看不到什么」的表达", () => {
    for (const u of USES) {
      expect(u).toHaveProperty("aim")
      // 不该有 hidden / exclude / disabled 这类"藏起来"的字段
      expect(Object.keys(u)).not.toContain("hidden")
      expect(Object.keys(u)).not.toContain("exclude")
      expect(Object.keys(u)).not.toContain("disabled")
    }
  })

  it("「我还没想好」不裁剪（aim 为 null）", () => {
    expect(profileOf("skip")?.aim).toBeNull()
  })

  it("三个真实用途都有 aim —— 不然选了等于没选", () => {
    for (const id of ["game", "wechat", "dubbing"] as const) {
      expect(profileOf(id)?.aim, `${id} 缺 aim`).toBeTruthy()
    }
  })

  it("首页按用途换 hero 副标题，且没定过时回退到通用文案", () => {
    expect(homeSrc).toContain("profile?.lead ??")
    expect(homeSrc).toContain("profileOf(getUseCase())")
  })

  it("首页主次按钮按用途对调（改默认排序，不是删按钮）", () => {
    // 两个按钮都在，只是有 profile 时第二个变成"直接去<用途>"
    expect(homeSrc).toContain('{profile ? "去挑音色" : "先听一个"}')
    expect(homeSrc).toContain("直接去{profile.name}")
    // 没定过时"我想做自己的"仍在 —— 不能因为加了裁剪就把它弄丢
    expect(homeSrc).toContain("我想做自己的")
  })
})

describe("两个模态框串行（约定 ③）", () => {
  it("意图引导有 ready 门，用途问答结束前不挂载", () => {
    expect(guideSrc).toContain("if (!ready && !replayed) return null")
    expect(guideSrc).toContain("ready = true")
  })

  it("App 里 ready 由「用途问过没」驱动，且初值直接读存储（老用户不迟到一帧）", () => {
    expect(appSrc).toContain("useCaseAnswered")
    expect(appSrc).toMatch(/useState\(\(\) => hasBeenAsked\(\)\)/)
    expect(appSrc).toContain("<FirstLaunchGuide ready={useCaseAnswered} />")
  })

  it("重播路径不受 ready 约束（用户主动点的，不该被吃掉）", () => {
    expect(guideSrc).toContain("setReplayed(true)")
  })
})

describe("链路状态条的门控（约定 ④）", () => {
  it("★ 自己门控两个可关能力 —— 首页是核心页，裸打可关端点会 404", () => {
    expect(barSrc).toContain('pluginVisible(catalog, "hook.wechat")')
    expect(barSrc).toContain('pluginVisible(catalog, "sound.tts")')
  })

  it("被关掉时不发请求，给「该能力已关闭」而不是「链路断了」", () => {
    expect(barSrc).toContain("wechatOn ? sendChainCheck() : Promise.reject")
    expect(barSrc).toContain("ttsOn ? ttsChainCheck() : Promise.reject")
    expect(barSrc).toContain("该能力已关闭")
  })

  it("「未启用」不算故障 —— 用户自己关的不该被报成断链", () => {
    // badCount 只数 bad，unknown 不计入
    expect(barSrc).toContain('steps.filter((s) => s.state === "bad")')
    expect(barSrc).toMatch(/只有 "bad" 才算问题/)
  })

  it("探测失败不炸页面（Promise.allSettled，不是 all）", () => {
    expect(barSrc).toContain("Promise.allSettled")
    expect(barSrc).not.toContain("Promise.all([")
  })

  it("cuda 为 null 不是故障（types.ts 明确了：那是没装 torch）", () => {
    expect(barSrc).toContain("h.cuda === null")
    expect(barSrc).toContain("未装 torch")
  })

  it("★ 出口不是死链 —— 状态条长在首页上，指向 /home 等于原地跳", () => {
    // 2026-09-25：此前这里写 `to="/home"`，而本组件就在首页里渲染，
    // 点了页面纹丝不动 —— 用户点名的那类"重复且没用"。
    // 现在派发事件，由 App.tsx 打开发送链路自检弹窗（详情 + 一键修复）。
    //
    // ⚠️ 断言要连 `<Link` 一起匹配：注释里也提到了 `to="/home"`（就是解释为什么删它），
    // 只查 `to="/home"` 会被自己的注释绊倒 —— 本仓踩过的"注释带坏门禁"类型。
    expect(barSrc).not.toMatch(/<Link\b[^>]*to="\/home"/)
    expect(barSrc).toContain('new Event("open-send-chain")')
  })

  it("事件有接收方 —— 发出去没人听也是死链", () => {
    // App.tsx 必须挂监听，否则这条路径依然断
    expect(appSrc).toContain('"open-send-chain"')
    expect(appSrc).toContain("setChainOpen(true)")
  })
})

describe("进阶折叠区：收起不是删除", () => {
  it("摘要行永远可见，内容才受 open 控制", () => {
    expect(advSrc).toContain("summary")
    // 内容用条件渲染（而非 CSS hidden）—— 藏起来的东西不该照旧发请求
    expect(advSrc).toContain("{open && ")
    expect(advSrc).not.toContain("hidden={")
  })

  it("首页把效果阶梯 + 术语表收进进阶区，且两块都还在", () => {
    expect(homeSrc).toContain("<AdvancedSection")
    expect(homeSrc).toContain("<EffectLadderCard")
    expect(homeSrc).toContain("GLOSSARY.map")
  })
})

describe("设置里必须留出口（猜错的人不能被困住）", () => {
  it("设置面板能重选用途、也能清空回到不裁剪", () => {
    expect(setSrc).toContain('replay("replay-use-picker")')
    expect(setSrc).toContain("clearUseCase()")
    expect(setSrc).toContain("清空用途")
  })

  it("清空后提示用户（否则会以为没生效）", () => {
    expect(setSrc).toContain("不再裁剪")
  })
})

describe("用途问答本身的可跳过性（调研点名：强制教程推高流失）", () => {
  it("Esc 能关，且关闭等价于选了 skip（同样落盘）", () => {
    expect(pickerSrc).toContain('e.key === "Escape"')
    expect(pickerSrc).toContain('close("skip")')
  })

  it("有明确的「先逛逛」按钮，不强制走完", () => {
    expect(pickerSrc).toContain("先逛逛")
  })

  it("选完不替用户跳页（跳页是意图引导的事）", () => {
    // 组件里不该出现 navigate
    expect(pickerSrc).not.toContain("navigate(")
  })
})

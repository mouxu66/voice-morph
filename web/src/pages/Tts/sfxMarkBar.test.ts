import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * 「插音效」工具栏（`SfxMarkBar.tsx`）的门控与约定。
 *
 * 为什么需要这个文件：`SfxMarkBar` 是全仓**第二个**渲染音效素材的前端组件
 * （第一个是 `SoundboardPanel`）。两处都打 `sound.fx-board` 的端点，所以两处
 * 都踩同一类坑 —— 而这个文件把"两处写法必须一致"钉住，免得第二个组件各写各的。
 *
 * ★ 两条约定（都是 `SoundboardPanel` 已经付出的学费）
 * ---------------------------------------------------
 *
 * ① **门控写在自己身上**（`pluginVisible(catalog, "sound.fx-board")` + `return null`）。
 *    宿主页是 `sound.tts`（单段合成页）/ `hook.wechat`，**不是** `sound.fx-board` ——
 *    「宿主开着、声板关着」完全可达，此时那块照旧渲染就是一路 404。
 *    见 `docs/犯错指南.md` 速查表 74 / `docs/犯错档案-工程.md` §8.40。
 *
 * ② **不许自己调 `useSoundboard`**，必须由调用方把路由层那**一个**实例传进来。
 *    hook 一调就预热常驻播放器 + 拉目录；组件里再调一次 = 两回预热、两回目录请求，
 *    而且两处"正在播"高亮会各说各话。`crossPluginGate.test.ts` 对
 *    `SoundboardPanel` 断言了"两处面板吃同一实例"，这里对 `SfxMarkBar` 断言同一条。
 *
 * 为什么是源码断言而不是渲染测试：与 `crossPluginGate.test.ts`、
 * `pages/Tts/index.gate.test.ts` 同一手法 —— 要防的回归是"有人把条件渲染改回
 * 无条件"，而造后端桩的成本远高于收益。
 */

/** 统一成 LF：仓库里部分 .tsx 是 CRLF，多行断言不做归一化会莫名其妙地红。 */
function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8").replace(/\r\n/g, "\n")
}

const barSrc = readRel(path.join("pages", "Tts", "SfxMarkBar.tsx"))
const pageSrc = readRel(path.join("pages", "Tts", "TtsPage.tsx"))
const routeSrc = readRel(path.join("pages", "Tts", "index.tsx"))
const hookSrc = readRel(path.join("pages", "Tts", "useTts.ts"))

describe("SfxMarkBar 的插件门控", () => {
  it("★ 自己也门控（宿主页属 sound.tts，不是 sound.fx-board）", () => {
    expect(barSrc).toContain('pluginVisible(catalog, "sound.fx-board")')
    expect(barSrc).toContain("if (!on || !sb) return null")
  })

  it("★ 不许自己调 useSoundboard —— 必须吃路由层那一个实例", () => {
    // 反面：`const sb = useSoundboard()` 会让每次渲染多一次预热 + 一次目录请求
    expect(barSrc).not.toMatch(/useSoundboard\(/)
    // 正面：`sb` 是 props，类型从 hook 的返回值取（不手抄一份接口）
    expect(barSrc).toContain("ReturnType<typeof useSoundboard>")
    expect(barSrc).toContain("sb?: ReturnType<typeof useSoundboard>")
  })

  it("没有素材时不渲染（宁可不显示，也不要一个空壳工具栏）", () => {
    expect(barSrc).toContain("if (marks.length === 0) return null")
  })
})

describe("SfxMarkBar 的接线（三处缺一不可）", () => {
  it("路由层把实例传给单段合成页（与微信页共用同一个）", () => {
    expect(routeSrc).toContain("<TtsPage {...tts} soundboard={soundboard} />")
  })

  it("页面把它转给工具栏，且 `sb` 是可选的（老后端/没插件时也要能渲染）", () => {
    expect(pageSrc).toContain("<SfxMarkBar sb={p.soundboard}")
    expect(pageSrc).toContain("soundboard?: Parameters<typeof SfxMarkBar>[0][\"sb\"]")
  })

  it("textarea 有 ref（插完要能把光标移到片段之后）", () => {
    expect(pageSrc).toContain("ref={textArea}")
    expect(pageSrc).toContain("setSelectionRange")
    // ⚠️ 读 value 必须等一帧：insertMark 走 state，同帧读到的是旧值
    expect(pageSrc).toContain("requestAnimationFrame")
  })
})

describe("标记插入本身（useTts.insertMark）", () => {
  it("插在偏移处并向返回值里导出", () => {
    expect(hookSrc).toContain("const insertMark = useCallback(")
    expect(hookSrc).toContain("insertMark,")
  })

  it("★ 偏移越界要夹住 —— offset 来自渲染层，而 text 可能已经变了", () => {
    // 直接 `slice(0, offset)` 在 offset 过大时会把片段插到意外位置
    expect(hookSrc).toContain("offset < 0 || offset > current.length")
  })

  it("前面已有空白就不补前导空格（免得留出双空格）", () => {
    expect(hookSrc).toContain("/\\s$/.test(before)")
  })
})

describe("与后端的语法口径一致（不抄第二份常量表）", () => {
  it("按钮点出的片段形状就是 `[名字]`", () => {
    expect(barSrc).toContain("onInsert(`[${it.name}]`, -1)")
  })

  it("提示里判「写过标记了没」的正则与后端 `sfx_mark._MARK` 同形", () => {
    // 后端：re.compile(r"\[([^\[\]]{1,24})\]")
    expect(barSrc).toContain("/\\[[^[\\]]{1,24}\\]/")
  })
})

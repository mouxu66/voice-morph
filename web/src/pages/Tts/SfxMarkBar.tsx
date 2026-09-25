import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import type { useSoundboard } from "@/pages/Tts/useSoundboard"

/**
 * 「在文字里插音效」的一行工具栏 —— 点一下把 `[爆炸]` 插进输入框。
 *
 * 为什么必须有这个（而不是只写进文档）
 * ------------------------------------
 * `[爆炸]` 是**纯字面语法**：后端 `sfx_mark.py` 认它，但界面上一点痕迹都没有。
 * 用户没有任何办法知道：
 *   · 有这功能（不知道就不会用，等于做了个没人用的功能）；
 *   · 该按**声板上的名字**写（写 `[爆炸声]` 会 400 报错 —— 报得清楚，但本不该踩）；
 *   · 有哪几条能用（声板可能装了音效包，变成 12 条）。
 *
 * 所以这里把"能用的名字"直接从后端目录里取（与声板格子**同一份数据**），
 * 点一下 = 插到光标处。好处是：
 *   · 名字永远与声板一致（前端不抄第二份常量表）；
 *   · 装了音效包这里跟着多出来，不用改前端；
 *   · 用户想手打也行 —— 他至少已经看见语法长什么样了。
 *
 * ★ `sb` 必须由**调用方**传进来（`pages/Tts/index.tsx` 的那一个实例）
 * ---------------------------------------------------------------
 * `useSoundboard` 在本仓的约定是"**只在路由层调一次**再往下传"：它一调就会
 * 预热常驻播放器 + 拉一次目录。在组件里再调一次 = 两回预热/两回目录请求，
 * 而且两处的"正在播"高亮会各说各话（`SoundboardPanel` 的 docstring 里写明了这条，
 * `crossPluginGate.test.ts` 也断言了"两处面板吃同一个实例"）。
 *
 * 所以本组件的门控写法与 `SoundboardPanel` 一致：**自己判** `sound.fx-board`
 * （无论被谁塞进哪个宿主页都安全），但**不自己调 hook**。
 * 也因此 `sb` 是可选的 —— 宿主页没传（比如 ③ 手动档那种没有合成产物的地方）
 * 就整块不渲染，而不是崩掉。
 *
 * 插入落在**光标处**（不是末尾）：`offset` 由调用方从 `textarea.selectionStart`
 * 取，插完把光标移到片段之后（用户接着打字就在音效后面）。本组件只管"插什么"，
 * 不持有那个 ref。
 */
export function SfxMarkBar({
  sb,
  text,
  onInsert,
}: {
  /** 路由层那一个 `useSoundboard` 实例（**不要**在本组件里新调一个）。 */
  sb?: ReturnType<typeof useSoundboard>
  /** 当前输入框内容 —— 只用来判"是不是已经写过标记了"（写过就不再啰嗦提示）。 */
  text: string
  /** 把 `snippet` 插到 `offset` 处（`-1` = 末尾）。调用方负责移光标并聚焦。 */
  onInsert: (snippet: string, offset: number) => void
}) {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const on = pluginVisible(catalog, "sound.fx-board")
  if (!on || !sb) return null

  const marks = sb.items
  if (marks.length === 0) return null

  const hasMark = /\[[^[\]]{1,24}\]/.test(text)

  return (
    <div className="mt-3 rounded-md border border-border bg-background/60 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="text-xs font-medium text-card-foreground">
          插音效
          <span className="ml-1.5 font-normal text-muted-foreground">
            点一下写进文字，合成时会拌进语音（不会被念出来）
          </span>
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          {marks.map((it) => (
            <button
              key={it.id}
              type="button"
              title={`在光标处插入 [${it.name}] · ${it.duration_s}s`}
              // 名字取后端下发的显示名 —— 与 `sfx_mark._name_index()` 的匹配键同源
              onClick={() => onInsert(`[${it.name}]`, -1)}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              <span className="text-sm leading-none">{it.icon || "🎧"}</span>
              {it.name}
            </button>
          ))}
        </div>
      </div>
      {/* 「还有这么用的」——只在用户还没写标记时提示，写过了就不再啰嗦 */}
      {!hasMark && (
        <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
          例：<code className="rounded bg-muted px-1 font-mono">我今天去那个 [爆炸] 那个地方</code>
          {" "}—— 音效接在它<strong className="font-medium text-card-foreground">前面那段话</strong>之后。
          写错名字会直接报错（不会悄悄不生效）。
        </p>
      )}
    </div>
  )
}

import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { useCascade } from "@/pages/Cascade/useCascade"
import { CascadePage } from "@/pages/Cascade/CascadePage"
import { useSoundboard } from "@/pages/Tts/useSoundboard"

/**
 * ⚠️ **这个模块不在任何插件清单里**（`CascadeRoute` 全仓无引用）：`/cascade` 现在由
 * `sound.rvc-live` 的 `legacy_routes` 重定向到 `/live?tab=qwen`，真正的实现是
 * `pages/Live/index.tsx`。它只是 `import.meta.glob` 会多匹配到的那些 `index.tsx` 之一
 * （`lib/pluginRoutes.tsx` 顶部的注释按名字列了这批），所以改 `CascadePage` 时会被它绊一下。
 *
 * 留着的唯一理由是别让「glob 多匹配」那段说明跟着失效；这里照同一套规矩接声板
 * （按 `sound.fx-board` 门控、hook 由路由层调一次）。
 */
export function CascadeRoute() {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const fxBoardOn = pluginVisible(catalog, "sound.fx-board")

  const cascade = useCascade()
  const soundboard = useSoundboard(fxBoardOn)

  return <CascadePage {...cascade} soundboard={soundboard} />
}

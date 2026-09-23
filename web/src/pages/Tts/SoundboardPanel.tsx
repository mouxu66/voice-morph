import { Square } from "lucide-react"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import type { useSoundboard } from "@/pages/Tts/useSoundboard"

/** 出厂音效的图标（按 id；用户导入的走兜底）。 */
const ICONS: Record<string, string> = {
  boom: "💥",
  applause: "👏",
  alarm: "⚠️",
  riser: "🎵",
  ding: "🔔",
  weird: "👻",
}

/**
 * 特效声板格子面板。
 *
 * **自门控**：整块 UI 归 `sound.fx-board` 插件，关掉它时后端 router 已卸载、
 * 端点全 404，所以这里必须 `return null` —— 这正是"宿主页托管别的插件"那一类
 * 缺口（`docs/犯错指南.md` 速查表 74 / `docs/犯错档案-工程.md` §8.40）。
 * 门控写在组件自己身上（而不是调用方），是为了让它无论被谁塞进哪个页面都安全。
 *
 * 两处使用点（都在微信发送页，宿主是 `hook.wechat`）：
 *   · ② 半自动 —— TTS 播到声卡、你按住 Alt 录制的**那几秒里**点格子；
 *   · ③ 手动  —— 实时变声说话时点格子（音效与人声一起被录进去）。
 */
export function SoundboardPanel({
  sb,
  hint,
}: {
  // hook 由路由层调一次（`pages/Tts/index.tsx`）往下传 —— 本组件在本页出现两回，
  // 各调一次 hook 就是两回预热/两回目录请求，而且两块的"正在播"高亮会各说各话。
  sb: ReturnType<typeof useSoundboard>
  hint?: string
}) {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const on = pluginVisible(catalog, "sound.fx-board")

  if (!on) return null

  return (
    <div className="mt-3 rounded-md border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-card-foreground">
          音效声板
          <span className="ml-2 font-normal text-muted-foreground">
            {hint ?? "点一下即出声，与人声一起被微信录走"}
          </span>
        </p>
        {sb.playing && (
          <button
            type="button"
            onClick={() => void sb.stop()}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            <Square className="h-3 w-3" />停止
          </button>
        )}
      </div>

      <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-6">
        {sb.items.length === 0 && (
          <p className="col-span-3 text-[11px] text-muted-foreground sm:col-span-6">
            还没有音效素材（`sound.fx-board` 插件自带的 6 条出厂音效应在此列出）。
          </p>
        )}
        {sb.items.map((it) => {
          const active = sb.playing === it.id
          return (
            <button
              key={it.id}
              type="button"
              disabled={!sb.ready}
              title={`${it.name} · ${it.duration_s}s${it.count ? ` · 用过 ${it.count} 次` : ""}`}
              onClick={() => void sb.play(it.id)}
              className={`flex flex-col items-center gap-1 rounded-lg border px-2 py-2.5 text-[11px] transition disabled:pointer-events-none disabled:opacity-50 ${
                active
                  ? "border-primary bg-primary/15 text-primary"
                  : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary"
              }`}
            >
              <span className="text-lg leading-none">{ICONS[it.id] ?? "🎧"}</span>
              <span className="max-w-full truncate">{it.name}</span>
            </button>
          )
        })}
      </div>

      {sb.errorMessage && (
        <p className="mt-2 text-[11px] text-destructive">{sb.errorMessage}</p>
      )}
    </div>
  )
}

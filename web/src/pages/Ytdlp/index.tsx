import { useYtdlp } from "@/pages/Ytdlp/useYtdlp"
import { YtdlpPage } from "@/pages/Ytdlp/YtdlpPage"

/**
 * 「在线扒歌」页路由出口。
 *
 * 本页不需要门控：它自己就是 `sound.ytdlp` 这个能力 —— 被关掉时整条路由消失
 * （后端 `/ytdlp/*` 也一并不再挂载，前后端同一条边界）。
 *
 * 为什么单独成页而不是并进「翻唱」：见 `useYtdlp.ts` 顶部注释 —— 核心是让
 * "关掉这个可选的扒歌能力"和"翻唱仍然完全可用"这两件事在结构上就成立。
 */
export function YtdlpRoute() {
  const ytdlp = useYtdlp()
  return <YtdlpPage {...ytdlp} />
}

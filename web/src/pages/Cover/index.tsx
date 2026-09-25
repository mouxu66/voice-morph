import { useCover } from "@/pages/Cover/useCover"
import { CoverPage } from "@/pages/Cover/CoverPage"

/**
 * 「翻唱」页路由出口。
 *
 * 独立成页而不是并进工具箱的三个理由：
 *   1. **链路不同**：工具箱里的离线变声是"单条音频换声"（一条人声进、一条出），
 *      翻唱要先**分离**、再换声、最后**合回伴奏** —— 多两步且输入是整首歌。
 *   2. **参数不同**：翻唱的变调是主角（男女声互转 ±12），离线变声的 pitch 是微调；
 *      放同一页用户会分不清该动哪个滑块。
 *   3. **耗时不同**：翻唱动辄几分钟，塞进工具箱的 tab 里会让"工具箱很快"的印象崩掉。
 *
 * 本页不需要门控：它自己就是 `sound.cover` 这个能力，被关掉时整条路由消失。
 */
export function CoverRoute() {
  const cover = useCover()
  return <CoverPage {...cover} />
}

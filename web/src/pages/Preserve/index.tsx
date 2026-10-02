import { PreservePage } from "@/pages/Preserve/PreservePage"
import { usePreserve } from "@/pages/Preserve/usePreserve"

/**
 * 声音保存（`sound.preserve`）。
 *
 * 这一页是**独立路由**、不是别的页的 tab —— 理由：
 *   - 它是一个完整的三步流程（同意 → 录音 → 体检），塞进别的页会让那一页更长，
 *     而项目正在治的正是「页内东西全摊开」（见 `docs/待办盘点-2026-10-01.md` §四/§五）。
 *   - 它有自己的能力开关价值：只要变声、不需要声音保存的用户可以关掉它。
 *
 * ⚠️ 它与 `media/voicebank/`（RVC 音色的参考音库）**不是一回事**：
 * 那边是给变声用的素材，这边是**人的声音档案**（带知情同意、分档参考音、操作日志）。
 *
 * 本页**不实现合成**：复现走现有的 TTS / 发送链路。所以这里没有能力门控 ——
 * 后端 `requires` 是空的，页面也不直接打别的插件的端点。
 */
export function PreserveRoute() {
  const p = usePreserve()
  return <PreservePage p={p} />
}

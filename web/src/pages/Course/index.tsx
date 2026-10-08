import { CoursePage } from "@/pages/Course/CoursePage"
import { useCourse } from "@/pages/Course/useCourse"

/**
 * 讲稿录课（`sound.course`）。
 *
 * 独立路由、不是别的页的 tab —— 与 Preserve 同款理由：
 * 它是完整的三步流程（传课件 → 逐页合成 → 导出成片），塞进输字变声页会把那页撑爆。
 *
 * ⚠️ 2026-10-08 事故：本桥接文件当初交付时漏建，`plugin.json` 的 `routes` 也是空的 ——
 * 后端/组件/测试全在，但界面上永远没有入口（用户三轮"开了又没"其实一轮都没出现过）。
 * 路由与导航完全由清单的 `routes[].nav` 驱动（`lib/pluginRoutes.tsx`），这里补上最后一米。
 */
export function CourseRoute() {
  const s = useCourse()
  return <CoursePage {...s} />
}

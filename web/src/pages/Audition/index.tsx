import { AuditionPage } from "@/pages/Audition/AuditionPage"

/**
 * 试音间：一个声音试音多个音色。
 *
 * 与其它页面的关系：本页只做「挑 + 比」的编排，不复制任何引擎；
 * 想精调某个音色，用结果卡上的「拿去微调」把音频交到离线变声页，
 * 或用页面里的链接去实时变声 / 输字变声页。
 */
export function AuditionRoute() {
  return <AuditionPage />
}

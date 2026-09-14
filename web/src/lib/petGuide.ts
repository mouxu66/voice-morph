import { petGuide, type PetGuidePayload } from "@/lib/electron"

/**
 * 桌宠页面导览：每个页面配一段口播文案 + 一组动作。
 *
 * action = 精灵图（pet/svg 下的 webp 序列帧），motion = 叠在外层的 CSS 动作，
 * 两者组合出「这一页在干什么」的观感，避免每个页面长得一个样。
 *
 * 行文约束：页面内气泡更宽，但单句仍控制在 15 个全角字符以内，避免折行难看。
 */
export type PageGuide = {
  title: string
  lines: string[]
  action: PetGuidePayload["action"]
  motion: PetGuidePayload["motion"]
}

export const GREETING = "嗨，我是你的导览小助手，下面带你熟悉这一页~"

export const GUIDES: Record<string, PageGuide> = {
  "/home": {
    title: "首页 · 先玩再定制",
    lines: ["第一次来？跟我走。", "先挑个音色试听一下，", "再输字让它替你说。"],
    action: "build",
    motion: "wave",
  },
  "/workshop": {
    title: "音色工坊",
    lines: ["一切从这里开始。", "丢进视频，我自动切片质检。", "挑够半分钟干净人声。"],
    action: "build",
    motion: "pop",
  },
  "/voices": {
    title: "音色库",
    lines: ["这里是你的声音仓库。", "能自动挖掘候选音色，", "试听满意就存下来。"],
    action: "idle",
    motion: "wave",
  },
  // /discover、/ft 已并入「音色工坊」，/market 已并入「音色库」，不再单独介绍。
  "/tts": {
    title: "语音合成",
    lines: ["选个音色念一段话。", "长文稿切「有声书」模式，", "按句合成自动拼接。"],
    action: "play",
    motion: "nod",
  },
  "/live": {
    title: "实时变声",
    lines: ["两个模式随你挑。", "RVC 低延迟像换嗓，", "级联换人味更足。"],
    action: "play",
    motion: "bounce",
  },
  "/offlinevc": {
    title: "离线工坊",
    lines: ["录好的音频交给我。", "先「离线变声」换嗓，", "再「效果器」加混响电音。"],
    action: "build",
    motion: "wiggle",
  },
}

/** 单句停留时长，与 pet.html 内的 lineMs 保持一致 */
export function lineMs(line: string): number {
  return Math.min(4200, Math.max(2200, 1500 + line.length * 110))
}

function build(page: string, g: PageGuide, lines: string[]): PetGuidePayload {
  return {
    page,
    title: g.title,
    lines,
    action: g.action,
    motion: g.motion,
    duration: lines.reduce((sum, l) => sum + lineMs(l), 0) + 1000,
  }
}

let lastPage = ""
let lastAt = 0

/**
 * 切页时通知桌宠介绍这一页。
 * 同一页面本次会话内第二次进入只讲首句（已经是熟面孔了，别啰嗦）。
 *
 * @param seen 由调用方持有的「本次会话已介绍过的页面」集合
 */
export function announcePage(pathname: string, seen: Set<string>): void {
  const g = GUIDES[pathname]
  if (!g) return
  // 去重：StrictMode 会重复执行 effect，2s 内对同一页的重复请求直接吞掉，
  // 否则首屏会出现「讲完整版 → 立刻被短版覆盖」的抖动。
  const now = Date.now()
  if (pathname === lastPage && now - lastAt < 2000) return
  lastPage = pathname
  lastAt = now

  const short = seen.has(pathname)
  seen.add(pathname)
  petGuide(build(pathname, g, short ? g.lines.slice(0, 1) : g.lines))
}

/** 桌宠右键「再讲一遍本页」之外的主动重播（例如设置里点「重新介绍」） */
export function replayPage(pathname: string): void {
  const g = GUIDES[pathname]
  if (!g) return
  petGuide(build(pathname, g, g.lines))
}

/**
 * 「用途档案」—— 首次启动问一句用途，之后整个首屏按这个答案裁剪。
 *
 * 为什么加这个
 * ------------
 * 行业调研（`docs/调研-用户需求与界面精简-2026-09-25.md`）的结论是：
 * 用户感受到的「功能冗余」往往不是功能多，而是**功能平铺得太早**。
 * 行业标准解法叫「渐进式披露」，最有效的一招是 Canva 那种做法 ——
 * 注册时只问一句「你打算用它做什么」，然后按答案裁剪体验。
 *
 * 三个候选用途不是拍脑袋定的，来自测评里出现频次最高的三个场景
 * （游戏开黑连麦 > 微信/QQ 语音 > 短视频配音）。
 *
 * 三条设计原则（都别破坏）
 * ------------------------
 * 1. **`null` 是合法状态，且必须与"跳过"区分开**。
 *    `null` = 还没问过（要弹窗）；`"skip"` = 问过且用户选了「先逛逛」（不弹了，
 *    首屏也不裁剪）。把两者混成一个值会让「跳过」在下一次启动时又被弹一次。
 * 2. **只影响"先给你看什么"，不影响"你能不能找到"**。
 *    被降权的区块仍然留在页面上（折叠或后移），绝不删除 —— 调研里明确指出
 *    「渐进式披露 ≠ 藏信息」，且功能少同样会被差评。
 * 3. **随时可改、不锁死**。用户可以重选用途，也可以回到"不裁剪"。
 *    没有这个出口，猜错的人就被困在一个不合适的主页上。
 */

/** 用途 id。加新用途时同步 `USES` 与 `use-tests`，否则门禁会红。 */
export type UseCase = "game" | "wechat" | "dubbing" | "skip"

export interface UseProfile {
  id: UseCase
  name: string
  /** 一句话说明"为什么这么用" —— 给选择卡当副标题 */
  hint: string
  /**
   * 首屏把哪个区块推到最前。
   * `null` = 不特别推（用于「先逛逛」和暂未归类的用途）。
   */
  aim: string | null
  /** 这个用途下，首页该先说什么话（hero 副标题）。`null` = 用默认文案。 */
  lead: string | null
}

/**
 * 四个选项。前三 + 一个"我还没想好"。
 *
 * ⚠️ 「我还没想好」不等于「跳过」：前者是**一个合法的答案**（不裁剪但也不该被
 * 当成未回答），后者是**不回答**。这里合并成一个 `skip`，因为两者对首屏的
 * 影响相同（都不裁剪），区分它们只会多一个没人用的状态。
 */
export const USES: UseProfile[] = [
  {
    id: "game",
    name: "打游戏开麦",
    hint: "开黑时换一副声音，队友听不出来",
    aim: "/live",
    lead: "开麦就能换声音。手机连上电脑，挑一个音色，进游戏说话就行。",
  },
  {
    id: "wechat",
    name: "发微信语音",
    hint: "打字 → 合成 → 自动发进微信，不用按住说话",
    aim: "/tts",
    lead: "打字它就说，还能一键发进微信。先听听像不像，再决定要不要练专属的。",
  },
  {
    id: "dubbing",
    name: "做配音 / 短视频",
    hint: "把录好的整段音频变成目标音色",
    aim: "/offlinevc",
    lead: "整段音频一次性变成目标音色，适合配音和剪辑后期。",
  },
  {
    id: "skip",
    name: "我还没想好",
    hint: "先随便逛，之后在设置里随时能定",
    aim: null,
    lead: null,
  },
]

/** 存/取的 key。改这个 key 会让老用户的用途档案失效，别乱改。 */
export const USE_KEY = "vm-use-profile"

/** 用途选择弹窗的"问过了吗"标记（与用途本身分开存，见原则 1）。 */
export const USE_ASKED_KEY = "vm-use-asked"

const VALID: readonly string[] = USES.map((u) => u.id)

/** 校验并归一化 —— 存储里可能有旧值 / 手工改坏的值。 */
export function normalizeUseCase(raw: string | null | undefined): UseCase | null {
  if (!raw) return null
  return VALID.includes(raw) ? (raw as UseCase) : null
}

/** 按 id 取用途档案。未知 id 返回 `null`（调用方按"没定"处理）。 */
export function profileOf(id: UseCase | null): UseProfile | null {
  if (!id) return null
  return USES.find((u) => u.id === id) ?? null
}

/**
 * 读当前用途。**故意不区分读写失败**：localStorage 在隐私模式下会抛，
 * 那就当"没定"处理 —— 一个读不到的偏好不该让首页崩掉。
 */
export function getUseCase(): UseCase | null {
  try {
    return normalizeUseCase(localStorage.getItem(USE_KEY))
  } catch {
    return null
  }
}

/** 问过了吗。用途与"问过标记"是两件事，都要落盘（见原则 1）。 */
export function hasBeenAsked(): boolean {
  try {
    return localStorage.getItem(USE_ASKED_KEY) === "1"
  } catch {
    return true // 读不到就当问过：宁可不多弹一次
  }
}

/**
 * 写入用途。**同时落 `USE_ASKED_KEY`** —— 否则用户选完用途、下次启动
 * 因为"问过标记"没写而再被弹一次。
 */
export function setUseCase(id: UseCase): void {
  try {
    localStorage.setItem(USE_KEY, id)
    localStorage.setItem(USE_ASKED_KEY, "1")
  } catch {
    /* 写不了就只在本次会话生效，不报错 */
  }
}

/** 清空用途（回到"没定"，首屏不裁剪）。给设置里的「重选用途」用。 */
export function clearUseCase(): void {
  try {
    localStorage.removeItem(USE_KEY)
    localStorage.removeItem(USE_ASKED_KEY)
  } catch {
    /* 同上 */
  }
}

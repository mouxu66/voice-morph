/**
 * P2-1 诊断结论的**前端唯一真相源**：判定码 → 配色 + 图标语义 + 兜底文案。
 *
 * 为什么要有这张表，而不是在页面里写 `if (verdict === "noisy") className="text-yellow-600"`：
 *
 *   1. **判定码是跨端契约**。它们定义在 `m2_server/quality_verdict.py` 的
 *      `VERDICT_*` 常量里，后端随时可能加一个（比如以后加 `wrong_language`）。
 *      散在页面里的 if 链漏掉新码时**不会报错** —— 只会落进 `else` 分支显示成
 *      中性色，于是"说好的红叉"变成了灰色提示，用户以为没事。
 *   2. 所以这里做两件事：把配色集中到一处；并且**导出一份码清单**，
 *      由 `qualityVerdict.test.ts` 与后端源码逐条对账（少一个就红）。
 *
 * 配色口径（与项目既有约定一致）：
 *   - `ok`          绿 —— 可以继续，但**仍然给下一步**，否则用户会在这步发呆
 *   - `too_short`   琥珀 —— 能补录解决，不是错
 *   - `noisy`       琥珀 —— 换素材或调参数，也不是"坏了"
 *   - `too_loud`    红 —— 削波不可逆，必须换素材
 *   - `wrong_speaker` 红 —— 音色会被带偏，是最贵的一种错（训练完才发现）
 *   - `empty`       红 —— 白跑一趟
 *   - `unknown`     灰 —— 兜底，不许静默变中性
 */
export type VerdictCode =
  | "ok"
  | "too_short"
  | "wrong_speaker"
  | "noisy"
  | "too_loud"
  | "empty"
  | "unknown"

export type VerdictMeta = {
  /** 卡片左边框与图标色 */
  tone: "ok" | "warn" | "bad" | "muted"
  /** 结果区里那行小标题 */
  label: string
}

export const VERDICT_META: Record<VerdictCode, VerdictMeta> = {
  ok: { tone: "ok", label: "素材可用" },
  too_short: { tone: "warn", label: "素材偏少" },
  noisy: { tone: "warn", label: "噪声偏重" },
  too_loud: { tone: "bad", label: "爆音需换素材" },
  wrong_speaker: { tone: "bad", label: "以他人声为主" },
  empty: { tone: "bad", label: "没有可用人声" },
  unknown: { tone: "muted", label: "需要人工判断" },
}

/** 后端 `quality_verdict.VERDICT_*` 的全集（对账用；改动必须与后端同批）。 */
export const KNOWN_VERDICT_CODES: VerdictCode[] = [
  "ok",
  "too_short",
  "wrong_speaker",
  "noisy",
  "too_loud",
  "empty",
  "unknown",
]

/** 未知判定码 → 兜底元数据。**绝不返回 undefined**：调用方直接取属性会崩。 */
export function verdictMeta(code: string | undefined | null): VerdictMeta {
  if (!code) return VERDICT_META.unknown
  return VERDICT_META[code as VerdictCode] ?? VERDICT_META.unknown
}

/** tone → Tailwind 类名（卡片边框 + 文字 + 图标）。集中在这里，页面只读结果。 */
export const TONE_CLASS: Record<VerdictMeta["tone"], { box: string; title: string; icon: string }> = {
  ok: {
    box: "border-emerald-500/40 bg-emerald-500/10",
    title: "text-emerald-600",
    icon: "text-emerald-600",
  },
  warn: {
    box: "border-yellow-500/40 bg-yellow-500/10",
    title: "text-yellow-600",
    icon: "text-yellow-600",
  },
  bad: {
    box: "border-destructive/40 bg-destructive/10",
    title: "text-destructive",
    icon: "text-destructive",
  },
  muted: {
    box: "border-border bg-muted/40",
    title: "text-muted-foreground",
    icon: "text-muted-foreground",
  },
}

/** 一句给"人"看的汇总，用于结果区标题右侧（不带判定码的原始术语）。 */
export function verdictSummary(v: {
  total?: number
  ok?: number
  ok_ratio?: number
}): string {
  const total = Number(v.total ?? 0)
  const ok = Number(v.ok ?? 0)
  if (!total) return "没有切出切片"
  const pct = Math.round(Number(v.ok_ratio ?? ok / total) * 100)
  return `${total} 条切片 · 可用 ${ok} 条（${pct}%）`
}

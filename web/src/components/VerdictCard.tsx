/**
 * P2-1 诊断卡：流水线跑完后，把「为什么不行 + 下一步做什么」摊开给用户。
 *
 * 为什么单独一个组件文件：它是 P2-1 唯一的**渲染侧**出口，而渲染侧的错误
 * 最容易悄悄退化 —— 少渲染一条 action、兜底色用错，都不会报错，只是用户
 * 看不到那行字。放成独立组件才能用 RTL 直接断言"这行话真的出现在页面上了"。
 *
 * 与 `ErrorPanel` 的分工（别把两者混起来）：
 *   · `ErrorPanel` 只装**抛出来的**异常原文（ffmpeg 挂了、网络 404），一律红色；
 *   · `VerdictCard` 装**跑完了但结果不对**的结论（纯伴奏切出 0 条、40 条里 34 条
 *     D 级），按判定等级取三种颜色。
 * 全用红会让「素材偏少」这种还能补录救回来的情况看起来像崩了 —— 用户的
 * 第一反应会是"这软件坏了"，而不是"我再去录一段"。
 */
import { AlertTriangle, ArrowRight, CircleAlert, CircleCheck, X } from "lucide-react"
import type { PipelineVerdict } from "@/api/client"
import { TONE_CLASS, verdictMeta, verdictSummary } from "@/lib/qualityVerdict"

export function VerdictCard({
  v,
  onDismiss,
  className = "",
}: {
  v: PipelineVerdict
  onDismiss?: () => void
  className?: string
}) {
  const meta = verdictMeta(v.verdict)
  const style = TONE_CLASS[meta.tone]
  // 三档图标：好 / 待判断 / 有问题。`muted` 用感叹号而不是问号 —— 后端给 muted
  // 的场景是"数据不足，说不清"，那也是一种提醒，不是"纯疑问"。
  const Icon = meta.tone === "ok" ? CircleCheck : meta.tone === "muted" ? CircleAlert : AlertTriangle
  const actions = v.actions ?? []
  return (
    <div
      data-verdict={v.verdict}
      data-tone={meta.tone}
      className={`mt-4 rounded-lg border px-4 py-3.5 animate-in fade-in slide-in-from-top-2 duration-300 ${style.box} ${className}`}
    >
      <div className="flex items-start gap-2.5">
        <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${style.icon}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className={`text-sm font-semibold ${style.title}`}>{v.title}</p>
            <span className={`rounded-full bg-background/60 px-2 py-0.5 font-mono text-[10px] ${style.title}`}>
              {meta.label}
            </span>
            {/* 数字汇总只在真的有切片时出现 —— total=0 时 verdictSummary 会说
                "没有切出切片"，而标题已经写过同一件事了，重复反而像话没说完 */}
            {Number(v.total) > 0 && (
              <span className="font-mono text-[10px] text-muted-foreground">{verdictSummary(v)}</span>
            )}
          </div>
          {v.detail ? (
            <p className="mt-1.5 text-xs leading-5 text-card-foreground/90">{v.detail}</p>
          ) : null}
          {/* 「下一步」是这个卡片的**主要价值** —— 用户看完结论最想知道的就是这个。
              所以它不折叠、不截断、一条不省。 */}
          {actions.length > 0 && (
            <div className="mt-2.5 space-y-1 border-t border-border/50 pt-2.5">
              <p className="text-[10px] font-medium text-muted-foreground">下一步</p>
              <ul className="space-y-1">
                {actions.map((a, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-xs leading-5 text-muted-foreground">
                    <ArrowRight className="mt-1 h-3 w-3 shrink-0" />
                    <span>{a}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            aria-label="收起这条结论"
            className={`shrink-0 rounded-md p-1 transition hover:bg-background/60 ${style.icon}`}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  )
}

import { useState } from "react"
import { ChevronLeft, ChevronRight, Palette, Sparkles, X } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * 人偶市场首进步骤引导：首次进入自动弹一个换装小指南（挑皮肤 → 一键安装 → 换肤应用），
 * 可跳过/完成后不再自动弹；之后右下角留一个小按钮可随时重开。
 *
 * 只依赖 localStorage，不碰后端；层级 z-[35]，低于详情抽屉(z-50)与安装托盘(z-40)。
 */

const GUIDE_KEY = "pet-market-guide-seen"

const STEPS = [
  {
    title: "挑一个喜欢的皮肤",
    body: "从「皮肤库」的卡片里挑顺眼的，顶部的分类（二次元 / 卡通 / 像素萌宠）能帮你缩小范围。",
  },
  {
    title: "点「一键安装」",
    body: "没装过的皮肤上有蓝色「一键安装」按钮，点它下载素材，装好会进皮肤库。",
  },
  {
    title: "点「换肤应用」",
    body: "装好后卡片或详情里会出现「换肤应用」，点一下，桌面人偶几秒后换上新外观。",
  },
]

export function PetMarketGuide() {
  // 首次进入自动展示（localStorage 无标记），其后只留右下角小按钮可重开
  const [showCard, setShowCard] = useState(() => localStorage.getItem(GUIDE_KEY) !== "1")
  const [step, setStep] = useState(0)

  const close = () => {
    setShowCard(false)
    localStorage.setItem(GUIDE_KEY, "1")
  }

  if (showCard) {
    const s = STEPS[step]
    const last = step === STEPS.length - 1
    return (
      <div className="fixed bottom-24 right-4 z-[35] w-[300px] rounded-2xl border border-border bg-card/95 p-4 shadow-xl backdrop-blur-md sm:right-6">
        <div className="flex items-start justify-between gap-2">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
            <Palette className="h-4 w-4 text-primary" />
            换装小指南
          </p>
          <button
            type="button"
            onClick={close}
            aria-label="跳过引导"
            className="rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          第 {step + 1} / {STEPS.length} 步
        </p>
        <h3 className="mt-2 text-sm font-medium text-card-foreground">{s.title}</h3>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{s.body}</p>
        <div className="mt-3 flex items-center justify-between">
          <div className="flex gap-1.5">
            {STEPS.map((_, i) => (
              <span
                key={i}
                className={cn(
                  "h-1.5 w-1.5 rounded-full transition-colors",
                  i === step ? "bg-primary" : "bg-primary/25",
                )}
              />
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            {step > 0 && (
              <button
                type="button"
                onClick={() => setStep((st) => st - 1)}
                className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground"
              >
                <ChevronLeft className="h-3 w-3" />
                上一步
              </button>
            )}
            {!last ? (
              <button
                type="button"
                onClick={() => setStep((st) => st + 1)}
                className="inline-flex items-center gap-1 rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition hover:bg-blue-500"
              >
                下一步
                <ChevronRight className="h-3 w-3" />
              </button>
            ) : (
              <button
                type="button"
                onClick={close}
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition hover:bg-blue-500"
              >
                <Sparkles className="h-3 w-3" />
                完成
              </button>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <button
      type="button"
      onClick={() => {
        setShowCard(true)
        setStep(0)
      }}
      className="fixed bottom-6 right-4 z-[35] flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card/95 text-muted-foreground shadow-lg backdrop-blur-md transition hover:border-primary/40 hover:text-primary sm:right-6"
      title="再学一遍换装引导"
      aria-label="再学一遍换装引导"
    >
      <Sparkles className="h-4 w-4" />
    </button>
  )
}
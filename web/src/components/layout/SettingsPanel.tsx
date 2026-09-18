import { Check, Compass, Download, Eye, EyeOff, FolderOpen, HardDrive, LayoutGrid, Moon, Monitor, Palette, Scale, Sparkles, Sun, X } from "lucide-react"
import { cn } from "@/lib/utils"
import type { ThemeMode } from "@/theme"

/**
 * 设置抽屉。
 *
 * 此前这些项塞在顶栏一个 192px 宽的小下拉里（五组内容一路贴到底），
 * 既放不下说明文字，也没法一眼看清当前值。改成右侧抽屉后每组有标题、
 * 说明与足够宽的控件，主题也从三个小方块换成可读的分段控件。
 */
export function SettingsPanel({
  open,
  onClose,
  theme,
  onThemeChange,
  simpleMode,
  onToggleSimple,
  petGuideEnabled,
  onTogglePetGuide,
  version,
  canCheckUpdate,
  onOpenModel,
  onOpenStorage,
  onOpenLicenses,
  onOpenUpdate,
}: {
  open: boolean
  onClose: () => void
  theme: ThemeMode
  onThemeChange: (mode: ThemeMode) => void
  simpleMode: boolean
  onToggleSimple: () => void
  petGuideEnabled: boolean
  onTogglePetGuide: () => void
  version: string | null
  canCheckUpdate: boolean
  onOpenModel: () => void
  onOpenStorage: () => void
  onOpenLicenses: () => void
  onOpenUpdate: () => void
}) {
  if (!open) return null

  const replay = (event: string) => {
    window.dispatchEvent(new CustomEvent(event))
    onClose()
  }

  const jump = (fn: () => void) => {
    fn()
    onClose()
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/60" onClick={onClose}>
      <aside
        className="flex h-full w-full max-w-[400px] flex-col overflow-hidden border-l border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="设置"
      >
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-sm font-semibold text-foreground">设置</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-5">
          <Group title="外观" hint="深色是为长时间盯屏幕调过的默认值。">
            <div className="grid grid-cols-3 gap-1 rounded-lg border border-border bg-background/60 p-1">
              {([["dark", "暗色", Moon], ["light", "亮色", Sun], ["system", "跟随系统", Monitor]] as [ThemeMode, string, typeof Moon][]).map(
                ([mode, label, Icon]) => (
                  <button
                    type="button"
                    key={mode}
                    onClick={() => onThemeChange(mode)}
                    className={cn(
                      "flex items-center justify-center gap-1.5 rounded-md px-2 py-2 text-xs transition",
                      theme === mode
                        ? "bg-primary text-primary-foreground shadow-sm"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {label}
                  </button>
                ),
              )}
            </div>

            <Toggle
              icon={<LayoutGrid className="h-4 w-4" />}
              label="极简模式"
              hint="只留首页与三条主路径，进阶入口收进「更多功能」。"
              on={simpleMode}
              onClick={onToggleSimple}
            />
          </Group>

          <Group title="桌宠" hint="页面边角那个会说话的小人偶。">
            <Toggle
              icon={petGuideEnabled ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
              label="切页时介绍当前页面"
              on={petGuideEnabled}
              onClick={onTogglePetGuide}
            />
            <Row icon={<Sparkles className="h-4 w-4" />} label="让桌宠再讲一遍本页" onClick={() => replay("replay-pet-guide")} />
            <Row icon={<Palette className="h-4 w-4" />} label="重播换装引导" onClick={() => replay("replay-pet-onboarding")} />
            <Row icon={<Compass className="h-4 w-4" />} label="重播新手引导" onClick={() => replay("replay-first-launch")} />
          </Group>

          <Group title="维护" hint="模型、引擎与磁盘占用都在本机，不上传。">
            <Row icon={<FolderOpen className="h-4 w-4" />} label="模型与引擎配置" onClick={() => jump(onOpenModel)} />
            <Row icon={<HardDrive className="h-4 w-4" />} label="存储占用与清理" onClick={() => jump(onOpenStorage)} />
          </Group>

          <Group title="关于">
            <Row icon={<Scale className="h-4 w-4" />} label="开源许可" onClick={() => jump(onOpenLicenses)} />
            {canCheckUpdate && (
              <Row
                icon={<Download className="h-4 w-4" />}
                label="检查更新"
                trailing={version ? `v${version}` : undefined}
                onClick={() => jump(onOpenUpdate)}
              />
            )}
          </Group>
        </div>
      </aside>
    </div>
  )
}

function Group({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{title}</h3>
      {hint && <p className="mt-1 text-xs leading-5 text-muted-foreground/80">{hint}</p>}
      <div className="mt-3 space-y-1.5">{children}</div>
    </section>
  )
}

function Row({
  icon,
  label,
  trailing,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  trailing?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-2.5 rounded-lg border border-border bg-background/50 px-3 py-2.5 text-left text-[13px] text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
    >
      <span className="text-muted-foreground/80">{icon}</span>
      <span className="min-w-0 flex-1">{label}</span>
      {trailing && <span className="font-mono text-[11px] opacity-70">{trailing}</span>}
    </button>
  )
}

function Toggle({
  icon,
  label,
  hint,
  on,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  hint?: string
  on: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      className="flex w-full items-start gap-2.5 rounded-lg border border-border bg-background/50 px-3 py-2.5 text-left transition hover:border-primary/40"
    >
      <span className="mt-0.5 text-muted-foreground/80">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] text-foreground">{label}</span>
        {hint && <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">{hint}</span>}
      </span>
      <span
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border transition",
          on ? "border-primary bg-primary text-primary-foreground" : "border-slate-600 bg-transparent",
        )}
      >
        {on && <Check className="h-3 w-3" />}
      </span>
    </button>
  )
}

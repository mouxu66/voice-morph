import { Activity, Radio } from "lucide-react"

type StudioHeaderProps = {
  eyebrow: string
  title: string
  description: string
  backendUp: boolean
  action?: React.ReactNode
}

export function StudioHeader({ eyebrow, title, description, backendUp, action }: StudioHeaderProps) {
  return (
    <header className="relative overflow-hidden border-b border-border/70 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary/10 via-background to-card" />
      <div className="relative mx-auto flex max-w-7xl flex-col gap-8 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="mb-4 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-primary">
            <Radio className="h-3.5 w-3.5" />
            {eyebrow}
          </div>
          <h1 className="font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">{title}</h1>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 rounded-full border border-border bg-card/80 px-3 py-2 text-xs text-muted-foreground shadow-md backdrop-blur-sm">
            <span className={`h-2 w-2 rounded-full ${backendUp ? "bg-primary animate-pulse" : "bg-destructive"}`} />
            <Activity className="h-3.5 w-3.5" />
            {backendUp ? "本地服务在线" : "本地服务离线"}
          </div>
          {action}
        </div>
      </div>
    </header>
  )
}

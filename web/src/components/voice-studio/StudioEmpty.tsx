import { FileAudio, RefreshCw } from "lucide-react"

type StudioEmptyProps = { title: string; description: string; onRetry?: () => void }

export function StudioEmpty({ title, description, onRetry }: StudioEmptyProps) {
  return (
    <div className="rounded-2xl border border-dashed border-border bg-card/60 px-6 py-16 text-center shadow-md">
      <FileAudio className="mx-auto h-9 w-9 text-muted-foreground" />
      <p className="mt-4 text-sm font-medium text-card-foreground">{title}</p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{description}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-5 inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          <RefreshCw className="h-3.5 w-3.5" />重试加载
        </button>
      )}
    </div>
  )
}

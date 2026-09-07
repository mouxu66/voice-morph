import type { MarketTask } from "@/api/client"

/** 安装任务当前百分比：优先安装段（阶段百分比>0 时），否则按下载字节数（下载阶段 install.percent 恒为 0，须回退字节进度）。 */
export function pctOf(t: MarketTask | null): number {
  if (!t) return 0
  const instPct = t.install?.percent
  if (instPct != null && instPct > 0) return Math.min(100, Math.max(0, instPct))
  if (t.total && t.total > 0) return Math.min(100, Math.round(((t.done ?? 0) / t.total) * 1000) / 10)
  return 0
}

/** 进行中阶段的短文本（安装 5 段 + 裸下载兜底）。 */
export function ACTIVE_PHASE_TEXT(phase: string): string {
  const map: Record<string, string> = {
    downloading_pth: "下载权重与索引",
    downloading_index: "下载索引",
    staging: "注册音色",
    downloading: "下载中",
  }
  return map[phase] ?? (phase || "进行中")
}

/** 字节数 → 人类可读（KB/MB/GB）。 */
export function fmtBytes(n: number | undefined | null): string {
  if (!n || n <= 0) return "-"
  const units = ["B", "KB", "MB", "GB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}
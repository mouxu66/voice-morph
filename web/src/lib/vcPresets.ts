// 变声参数预设：本机 localStorage 持久化，离线变声页保存/套用/删除。
// 预设记录完整的转换参数组合（音色 + 变调 + 检索强度 + 降噪）。
export type VcPreset = {
  id: string
  name: string
  voiceId: string
  pitch: number
  indexRate: number
  denoise: boolean
  createdAt: number
}

const KEY = "vc-presets"
const LIMIT = 20

export function loadPresets(): VcPreset[] {
  try {
    const raw = localStorage.getItem(KEY)
    const arr = raw ? (JSON.parse(raw) as VcPreset[]) : []
    return Array.isArray(arr) ? arr.slice(0, LIMIT) : []
  } catch {
    return []
  }
}

function persist(presets: VcPreset[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(presets.slice(0, LIMIT)))
  } catch {
    /* 存储满等异常忽略 */
  }
}

export function addPreset(p: Omit<VcPreset, "id" | "createdAt">): VcPreset[] {
  const item: VcPreset = { ...p, id: `${Date.now()}`, createdAt: Date.now() }
  const next = [item, ...loadPresets().filter((x) => x.name !== item.name)]
  persist(next)
  return next
}

export function removePreset(id: string): VcPreset[] {
  const next = loadPresets().filter((x) => x.id !== id)
  persist(next)
  return next
}

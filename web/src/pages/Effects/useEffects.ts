import { useCallback, useEffect, useRef, useState } from "react"
import { backendPrefix } from "../../api/client"

/**
 * 效果器工作台状态机：选音频 → 叠效果链 → A/B 试听 → 下载。
 *
 * 后端 /api/effects/* 直接 POST multipart（不走 client.ts 的 jsonFetch 封装，
 * 但复用它的 backendPrefix() 判定基址）。DSP 全在本地 CPU 线程池跑，不占 GPU，
 * 与实时链路互不干扰。
 */

// 与后端 effects.py CATALOG 对应的类型
export type FxParam = {
  key: string
  label: string
  min: number
  max: number
  step: number
  default: number
}

export type FxMeta = {
  type: string
  name: string
  icon: string
  desc: string
  params: FxParam[]
}

export type FxStep = {
  type: string
  params: Record<string, number>
}

type Feedback = { tone: "ok" | "error" | "info"; text: string }

// /api 基址：与 client.ts 共用同一处判定，避免"各 hook 各写一份"（2026-09-13 收敛）
const BASE = backendPrefix() + "/api"

export function useEffects() {
  const [catalog, setCatalog] = useState<FxMeta[]>([])
  const [chain, setChain] = useState<FxStep[]>([])
  const [sourceFile, setSourceFile] = useState<File | null>(null)
  const [sourceUrl, setSourceUrl] = useState<string | null>(null)
  const [resultUrl, setResultUrl] = useState<string | null>(null)
  const [processing, setProcessing] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [skipped, setSkipped] = useState<string[]>([])
  // 预设持久化：链保存到 localStorage（与离线变声的 vcPresets 同一模式）
  const [presets, setPresets] = useState<{ name: string; chain: FxStep[] }[]>([])

  const objUrlRef = useRef<string | null>(null)
  const resUrlRef = useRef<string | null>(null)

  useEffect(() => {
    try {
      const raw = localStorage.getItem("vm-fx-presets")
      const arr = raw ? (JSON.parse(raw) as { name: string; chain: FxStep[] }[]) : []
      if (Array.isArray(arr)) setPresets(arr.slice(0, 12))
    } catch {
      /* 忽略损坏的存储 */
    }
  }, [])

  // 目录加载（一次即可，后端目录静态）
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const r = await fetch(BASE + "/effects/catalog")
        const j = (await r.json()) as { effects?: FxMeta[] }
        if (alive && j.effects) setCatalog(j.effects)
      } catch {
        /* 服务未启动时静默，页面顶部有状态指示 */
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  // 释放 objectURL，防内存泄漏
  const _swapSource = useCallback((f: File | null) => {
    if (objUrlRef.current) URL.revokeObjectURL(objUrlRef.current)
    if (resUrlRef.current) {
      URL.revokeObjectURL(resUrlRef.current)
      resUrlRef.current = null
    }
    setResultUrl(null)
    setSkipped([])
    if (f) {
      const u = URL.createObjectURL(f)
      objUrlRef.current = u
      setSourceFile(f)
      setSourceUrl(u)
    } else {
      objUrlRef.current = null
      setSourceFile(null)
      setSourceUrl(null)
    }
  }, [])

  const addFx = useCallback((meta: FxMeta) => {
    setChain((prev) => {
      if (prev.some((s) => s.type === meta.type)) return prev // 同效果不重复
      const params: Record<string, number> = {}
      for (const p of meta.params) params[p.key] = p.default
      return [...prev, { type: meta.type, params }]
    })
    setResultUrl(null)
  }, [])

  const removeFx = useCallback((idx: number) => {
    setChain((prev) => prev.filter((_, i) => i !== idx))
    setResultUrl(null)
  }, [])

  const moveFx = useCallback((idx: number, dir: -1 | 1) => {
    setChain((prev) => {
      const to = idx + dir
      if (to < 0 || to >= prev.length) return prev
      const next = [...prev]
      ;[next[idx], next[to]] = [next[to], next[idx]]
      return next
    })
    setResultUrl(null)
  }, [])

  const setParam = useCallback((idx: number, key: string, value: number) => {
    setChain((prev) =>
      prev.map((s, i) => (i === idx ? { ...s, params: { ...s.params, [key]: value } } : s)),
    )
  }, [])

  const apply = useCallback(async () => {
    if (!sourceFile) return
    setProcessing(true)
    setFeedback(null)
    try {
      const fd = new FormData()
      fd.append("file", sourceFile, sourceFile.name)
      fd.append("chain", JSON.stringify(chain))
      const r = await fetch(BASE + "/effects/apply", { method: "POST", body: fd })
      if (!r.ok) {
        const detail = await r.text()
        throw new Error(`HTTP ${r.status}: ${detail.slice(0, 120)}`)
      }
      if (resUrlRef.current) URL.revokeObjectURL(resUrlRef.current)
      const u = URL.createObjectURL(await r.blob())
      resUrlRef.current = u
      setResultUrl(u)
      const skippedRaw = r.headers.get("X-Fx-Skipped") || "0"
      const skippedList = skippedRaw === "0" ? [] : skippedRaw.split("; ").filter(Boolean)
      setSkipped(skippedList)
      setFeedback({
        tone: "ok",
        text: skippedList.length
          ? `已生成。${skippedList.length} 个效果被跳过：${skippedList.join("；")}（不影响其余效果）`
          : "效果已应用，可对比试听。",
      })
    } catch (error) {
      setFeedback({
        tone: "error",
        text: error instanceof Error ? error.message : "处理失败",
      })
    } finally {
      setProcessing(false)
    }
  }, [sourceFile, chain])

  const savePreset = useCallback(
    (name: string) => {
      const trimmed = name.trim()
      if (!trimmed) return
      setPresets((prev) => {
        const next = [{ name: trimmed, chain }, ...prev.filter((p) => p.name !== trimmed)].slice(0, 12)
        try {
          localStorage.setItem("vm-fx-presets", JSON.stringify(next))
        } catch {
          /* 存储满忽略 */
        }
        return next
      })
    },
    [chain],
  )

  const loadPreset = useCallback((name: string) => {
    setPresets((prev) => {
      const hit = prev.find((p) => p.name === name)
      if (hit) setChain(hit.chain.map((s) => ({ ...s, params: { ...s.params } })))
      return prev
    })
    setResultUrl(null)
  }, [])

  const removePreset = useCallback((name: string) => {
    setPresets((prev) => {
      const next = prev.filter((p) => p.name !== name)
      try {
        localStorage.setItem("vm-fx-presets", JSON.stringify(next))
      } catch {
        /* 忽略 */
      }
      return next
    })
  }, [])

  return {
    catalog,
    chain,
    sourceFile,
    sourceUrl,
    resultUrl,
    processing,
    feedback,
    skipped,
    presets,
    setSource: _swapSource,
    addFx,
    removeFx,
    moveFx,
    setParam,
    apply,
    savePreset,
    loadPreset,
    removePreset,
    clearChain: useCallback(() => {
      setChain([])
      setResultUrl(null)
    }, []),
  }
}

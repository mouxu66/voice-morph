import { useCallback, useEffect, useRef, useState } from "react"
import { Mic, MicOff, Radio } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * 麦克风输入电平表 / 滚动波形。
 *
 * 用浏览器 Web Audio 直接采麦克风，不经过后端 —— 目的是回答用户最关心的问题：
 * "它到底有没有在收我的声音？"。实时变声的输出走虚拟声卡，浏览器采不到，
 * 所以这里诚实地标注为「输入电平」，输出侧由链路图的状态表达。
 */
type LiveLevelMeterProps = {
  /** 实时变声正在运行：配色转为激活态，并提示"正在送进 RVC" */
  active: boolean
  className?: string
}

const HISTORY = 56          // 波形柱数量
const SAMPLE_MS = 50        // 采样间隔（20fps，够顺滑且不烧 CPU）
const DB_FLOOR = -60        // 显示下限，低于此视为静音

export function LiveLevelMeter({ active, className }: LiveLevelMeterProps) {
  const [history, setHistory] = useState<number[]>(() => new Array(HISTORY).fill(0))
  const [peak, setPeak] = useState(0)
  const [db, setDb] = useState<number | null>(null)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState("")

  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const bufRef = useRef<Uint8Array<ArrayBuffer> | null>(null)
  const timerRef = useRef<number | null>(null)

  const teardown = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    analyserRef.current = null
    bufRef.current = null
    void ctxRef.current?.close().catch(() => { /* 关闭失败可忽略 */ })
    ctxRef.current = null
    setListening(false)
    setDb(null)
    setHistory(new Array(HISTORY).fill(0))
    setPeak(0)
  }, [])

  useEffect(() => teardown, [teardown])

  const start = useCallback(async () => {
    setError("")
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        setError("当前环境不支持麦克风采集（需 https 或 Electron 本地页面）")
        return
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      })
      streamRef.current = stream
      const ctx = new AudioContext()
      ctxRef.current = ctx
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 1024
      analyser.smoothingTimeConstant = 0.6
      analyserRef.current = analyser
      bufRef.current = new Uint8Array(analyser.fftSize)
      ctx.createMediaStreamSource(stream).connect(analyser)

      setListening(true)
      timerRef.current = window.setInterval(() => {
        const node = analyserRef.current
        const buf = bufRef.current
        if (!node || !buf) return
        node.getByteTimeDomainData(buf)
        let sum = 0
        for (let i = 0; i < buf.length; i += 1) {
          const v = (buf[i] - 128) / 128
          sum += v * v
        }
        const rms = Math.sqrt(sum / buf.length)
        const cur = rms > 0 ? 20 * Math.log10(rms) : -100
        const norm = cur <= DB_FLOOR ? 0 : Math.min(1, (cur - DB_FLOOR) / -DB_FLOOR)
        setHistory((h) => [...h.slice(1), norm])
        setPeak((p) => Math.max(norm, p * 0.94))
        setDb(cur > -100 && cur > DB_FLOOR ? cur : null)
      }, SAMPLE_MS)
    } catch (e) {
      const name = e instanceof Error ? e.name : ""
      setError(
        name === "NotAllowedError"
          ? "麦克风权限被拒绝，请在系统设置里允许本应用使用麦克风"
          : name === "NotFoundError"
            ? "没有检测到可用的麦克风设备"
            : `无法采集麦克风：${e instanceof Error ? e.message : String(e)}`,
      )
    }
  }, [])

  const toggle = useCallback(() => {
    if (listening) teardown()
    else void start()
  }, [listening, start, teardown])

  const tone = listening ? (active ? "text-primary" : "text-foreground") : "text-muted-foreground"

  return (
    <div className={cn("rounded-xl border border-border bg-background/60 p-4", className)}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <Radio className={cn("h-3.5 w-3.5", active ? "animate-pulse text-primary" : "text-muted-foreground")} />
            麦克风输入电平
          </p>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {listening ? (active ? "正在采集 → 送进 RVC 变声" : "正在采集（实时变声未启动）") : "开启后可确认麦克风是否在收声"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className={cn("font-mono text-xs tabular-nums", tone)}>
            {db === null ? "—" : `${db.toFixed(0)} dB`}
          </span>
          <button
            type="button"
            onClick={toggle}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
              listening
                ? "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20"
                : "border-border text-muted-foreground hover:border-primary hover:text-primary",
            )}
          >
            {listening ? <Mic className="h-3.5 w-3.5" /> : <MicOff className="h-3.5 w-3.5" />}
            {listening ? "停止监听" : "开启电平"}
          </button>
        </div>
      </div>

      <div className="mt-3 flex h-16 items-end gap-[2px]">
        {history.map((level, i) => {
          // 让每根柱子有最小可见高度，全静音时仍是一条整齐的基线而非空白
          const height = Math.max(4, level * 100)
          const hot = level > 0.85
          return (
            <span
              key={i}
              className={cn(
                "flex-1 rounded-sm transition-[height] duration-75",
                listening ? (hot ? "bg-destructive" : active ? "bg-primary/80" : "bg-foreground/45") : "bg-muted-foreground/20",
              )}
              style={{ height: `${height}%` }}
            />
          )
        })}
      </div>

      <div className="mt-2 flex items-center justify-between text-[10px] text-muted-foreground">
        <span>静音</span>
        <span className="font-mono">峰值 {Math.round(peak * 100)}%</span>
        <span>过载</span>
      </div>

      {error && (
        <p className="mt-2 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-[11px] leading-4 text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}

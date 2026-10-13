import { useRef, useState } from "react"
import { Loader2, Music4, Play, Square, Wand2 } from "lucide-react"
import { bgmGain, bgmPremix, bgmStart, bgmStop, bgmUpload } from "@/api/client"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { friendlyError } from "@/lib/errors"

/**
 * BGM 垫底面板（「边有歌声、边能说话」的 A/C 两档，同一块 UI 两种用法）。
 *
 *   · **premix**（A 档）：选一首歌 → 全程垫进一条合成产物（RMS 自动配平 + 淡入淡出）
 *     产出 `bgmmix_*.wav`，发送目标换成它。混音发生在合成侧，发送链路零改动。
 *   · **layer**（C 档）：选一首歌 → 往 CABLE 实时垫一层，与说话/变声/one-shot 音效
 *     并行不抢占（走 board_worker 的独立音频流）。边说边唱用这档。
 *
 * **自门控**：端点全属 `sound.fx-board`（与音效声板同一家），插件关掉时 router 已卸载，
 * 这里必须 `return null`（门控写在组件自己身上，塞进哪个页面都安全 —— 同 SoundboardPanel）。
 *
 * 16kHz 预期管理（两个模式共用）：微信语音按 16kHz 人声编码，8kHz 以上的高频会被
 * 截掉 —— 垫进去的歌比原曲「闷一点」。这是平台编码特性，只能缓解（选 BGM 能量集中
 * 在中低频的歌）不能消除，所以文案要提前说出来，别让用户以为程序坏了。
 */
export function BgmPanel({
  mode,
  wav,
  sourceSeconds,
  onPremixed,
}: {
  /** premix = 垫进一条合成产物（A）；layer = 实时垫进 CABLE（C）。 */
  mode: "premix" | "layer"
  /** premix 模式的垫底源：要混的那条合成产物（文件名）。缺省时按钮置灰并说明原因。 */
  wav?: string
  /** 垫底源的人声时长（秒）—— 只用来展示，帮助用户判断歌该从哪一秒开始垫。 */
  sourceSeconds?: number
  /** 预混成功回调（调用方把发送目标换成返回的 wav）。 */
  onPremixed?: (r: { wav: string; seconds: number; notes: string[] }) => void
}) {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const on = pluginVisible(catalog, "sound.fx-board")
  const [song, setSong] = useState<{ name: string; duration_s: number } | null>(null)
  const [uploading, setUploading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [leadDb, setLeadDb] = useState(10)
  const [startS, setStartS] = useState(0)
  const [gain, setGain] = useState(0.3)
  const [loop, setLoop] = useState(true)
  const [error, setError] = useState("")
  const [notes, setNotes] = useState<string[]>([])
  const [done, setDone] = useState("")
  const fileInput = useRef<HTMLInputElement | null>(null)

  if (!on) return null

  // 没有垫底源（premix 模式还没合成过语音）时说明缺什么，而不是静默置灰。
  const premixReady = mode === "premix" ? Boolean(wav) : true
  const reason = mode === "premix" && !wav ? "先在上面合成一条语音，才能垫歌" : ""

  const pickSong = async (f: File | null) => {
    if (!f) return
    setError("")
    setNotes([])
    setDone("")
    setUploading(true)
    try {
      const r = await bgmUpload(f)
      setSong({ name: r.name, duration_s: r.duration_s })
    } catch (e) {
      setSong(null)
      setError(friendlyError(e, "这首歌读不出来"))
    } finally {
      setUploading(false)
    }
  }

  /** A 档：上传 → 预混 → 把发送目标交给调用方。 */
  const runPremix = async () => {
    if (!song) return
    setBusy(true)
    setError("")
    setNotes([])
    try {
      const r = await bgmPremix({ wav, bgm: song.name, lead_db: leadDb, start_s: startS })
      setNotes(r.notes ?? [])
      setDone(r.wav)
      onPremixed?.({ wav: r.wav, seconds: r.seconds, notes: r.notes ?? [] })
    } catch (e) {
      setError(friendlyError(e, "垫底混音失败"))
    } finally {
      setBusy(false)
    }
  }

  /** C 档：开始/停止实时垫层。 */
  const runLayer = async (start: boolean) => {
    if (!song) return
    setBusy(true)
    setError("")
    try {
      if (start) {
        await bgmStart(song.name, gain, loop)
        setPlaying(true)
      } else {
        await bgmStop()
        setPlaying(false)
      }
    } catch (e) {
      setError(friendlyError(e, start ? "垫歌失败" : "停止失败"))
    } finally {
      setBusy(false)
    }
  }

  /** 垫歌进行中拖音量滑块 = 实时调（流里读的是同一个变量，不用重开）。 */
  const adjustGain = async (v: number) => {
    setGain(v)
    if (!playing) return
    try {
      await bgmGain(v)
    } catch (e) {
      setError(friendlyError(e, "调音量失败"))
    }
  }

  return (
    <div className="mt-3 rounded-md border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-card-foreground">
          {mode === "premix" ? "歌声垫底" : "实时垫歌"}
          <span className="ml-2 font-normal text-muted-foreground">
            {mode === "premix"
              ? "选一首歌垫在语音下面（自动配平 + 淡入淡出），走正常发送"
              : "边说边唱：歌实时垫进虚拟声卡，与人声混在一起被录走"}
          </span>
        </p>
        <button
          type="button"
          disabled={uploading}
          onClick={() => fileInput.current?.click()}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
        >
          {uploading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Music4 className="h-3 w-3" />}
          {song ? "换一首歌" : "选一首歌"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="audio/*,.mp3,.wav,.m4a,.flac,.aac,.ogg"
          hidden
          onChange={(e) => {
            void pickSong(e.target.files?.[0] ?? null)
            e.target.value = "" // 清掉才能连续选同一个文件
          }}
        />
      </div>

      {song ? (
        <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">
          已选：{song.name}（{song.duration_s.toFixed(1)}s）
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          {reason || "还没选歌 —— 支持整首 mp3 / wav / m4a / flac，退出应用会自动清掉"}
        </p>
      )}

      {mode === "premix" ? (
        <div className="mt-2 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
              歌比人声低
              <input
                type="range"
                min={2}
                max={20}
                step={1}
                value={leadDb}
                onChange={(e) => setLeadDb(Number(e.target.value))}
                className="h-1.5 w-32 accent-primary"
                aria-label="歌比人声低的分贝数"
              />
              <span className="w-14 font-mono text-card-foreground">{leadDb} dB</span>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
              从第
              <input
                type="number"
                min={0}
                step={0.5}
                value={startS}
                onChange={(e) => setStartS(Math.max(0, Number(e.target.value)))}
                title="从歌的哪一秒开始垫（跳过前奏直取副歌）"
                className="w-14 rounded border border-border bg-background px-1 py-0 text-[11px] text-card-foreground"
                aria-label="从歌的第几秒开始垫"
              />
              秒起
            </label>
            <button
              type="button"
              disabled={!song || busy || uploading || !premixReady}
              title={reason || "混好后发送目标自动换成它"}
              onClick={() => void runPremix()}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[11px] font-medium text-primary-foreground transition hover:scale-[1.03] disabled:pointer-events-none disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
              {busy ? "混音中…" : "垫进这条语音"}
            </button>
            {done && !busy && (
              <span className="font-mono text-[11px] text-muted-foreground">已混好 {done}</span>
            )}
          </div>
          {sourceSeconds !== undefined && (
            <p className="text-[11px] text-muted-foreground">
              这条语音 {sourceSeconds.toFixed(1)}s —— 垫底不改长度，歌更长会被截断、更短会循环补满。
            </p>
          )}
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={!song || busy || uploading || playing}
            onClick={() => void runLayer(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[11px] font-medium text-primary-foreground transition hover:scale-[1.03] disabled:pointer-events-none disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />
            开始垫歌
          </button>
          <button
            type="button"
            disabled={!playing || busy}
            onClick={() => void runLayer(false)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
          >
            <Square className="h-3.5 w-3.5" />
            停止垫歌
          </button>
          <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
            歌的音量
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={gain}
              onChange={(e) => void adjustGain(Number(e.target.value))}
              className="h-1.5 w-32 accent-primary"
              aria-label="垫底歌的音量"
            />
            <span className="w-8 font-mono text-card-foreground">{gain.toFixed(2)}</span>
          </label>
          <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
            <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} className="accent-primary" />
            歌短时循环
          </label>
          {playing && (
            <span className="text-[11px] text-primary">垫歌进行中 —— 照常说话即可，也可以点上面的音效格子</span>
          )}
        </div>
      )}

      <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
        微信语音按 16kHz 人声编码，8kHz 以上的高频会被截掉 —— 歌会比原曲闷一点，选伴奏不炸耳的歌听感最好。
      </p>
      {notes.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[11px] text-yellow-600">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}

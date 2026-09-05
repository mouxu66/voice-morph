import { useEffect, useState } from "react"
import { AudioLines, CheckCircle2, Clock3, Crown, Database, Download, FileAudio, FolderInput, Gauge, HardDriveDownload, Loader2, Mic, Play, Plus, RefreshCw, Save, Search, Trash2, WandSparkles, XCircle } from "lucide-react"
import type { useVoices } from "@/pages/Voices/useVoices"
import { importVoicePack, listRvcVoices, mediaUrl, voicePackUrl } from "@/api/client"
import type { VoiceQc } from "@/types"
import { AbCompareCard } from "@/components/voice-studio/AbCompareCard"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { StudioEmpty } from "@/components/voice-studio/StudioEmpty"
import { ErrorPanel } from "@/components/ErrorPanel"

// 质检分项中文名（与 tools/voice_qc.py 的 items 键对应）
const QC_ITEM_LABELS: Record<string, string> = {
  duration_ratio: "时长比",
  f0_shift: "f0偏移",
  asr_overlap: "ASR重合",
  emb_sim: "声纹相似",
}

// 质检徽标：分数等级配色 + 失败时醒目红标；点开弹出面板展示「中断于哪一步 / 原因 / 排查」与分项明细
function QcBadge({ qc }: { qc: VoiceQc }) {
  const [open, setOpen] = useState(false)
  const score = qc.score ?? null
  const dsErr = typeof qc.dataset?.error === "string" ? qc.dataset.error : null
  const dsStage = typeof qc.dataset?.error_stage === "string" ? qc.dataset.error_stage : null
  const voiceErr = qc.voice?.error || dsErr || qc.error || null
  const voiceStage = qc.voice?.error_stage || dsStage || qc.error_stage || null
  const hint = qc.voice?.hint || qc.hint || null

  // 既没分也没错 → 不渲染（例如尚未质检）
  if (score == null && !voiceErr) return null

  const items = qc.voice?.items ?? {}
  const failed = Object.values(items).filter((v) => !v.pass)
  const s = score ?? 0
  const tone = voiceErr
    ? "border-red-500/50 bg-red-500/10 text-red-400"
    : s >= 80
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
      : s >= 60
        ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
        : "border-red-500/40 bg-red-500/10 text-red-400"

  return (
    <span className="relative inline-flex">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 font-mono text-xs ${tone}`}
      >
        {voiceErr ? <XCircle className="h-3 w-3" /> : <Gauge className="h-3 w-3" />}
        {voiceErr
          ? `质检失败${voiceStage ? `·${voiceStage}` : ""}`
          : `${s}${s < 60 ? " 低质" : ""}`}
        {!voiceErr && failed.length > 0 && (
          <span className="ml-0.5 rounded bg-red-500/20 px-1 text-[10px]">✗{failed.length}</span>
        )}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-2 w-80 rounded-xl border border-border bg-card p-3 text-xs shadow-xl">
          {voiceErr && (
            <ErrorPanel title={`质检中断于【${voiceStage ?? "未知"}】`} detail={voiceErr} hint={hint} />
          )}
          {!voiceErr && score != null && (
            <p className="mb-2 font-mono text-foreground">总分 {score} / 100 · {qc.created_at ?? ""}</p>
          )}
          {Object.keys(items).length > 0 && (
            <ul className="space-y-1.5">
              {Object.entries(items).map(([k, v]) => (
                <li key={k} className="rounded-md border border-border p-2">
                  <p className={`flex items-center gap-1.5 ${v.pass ? "text-emerald-400" : "text-red-400"}`}>
                    {v.pass ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                    {QC_ITEM_LABELS[k] ?? k}
                    {v.value != null && (
                      <span className="ml-auto font-mono text-foreground/70">{String(v.value)}</span>
                    )}
                  </p>
                  <p className="mt-1 leading-5 text-muted-foreground">{v.detail}</p>
                </li>
              ))}
            </ul>
          )}
          {!voiceErr && Object.keys(items).length === 0 && (
            <p className="leading-5 text-muted-foreground">暂无分项明细。</p>
          )}
        </div>
      )}
    </span>
  )
}

// 来源角标：市场安装（由市场页在 logs/<id>/source.json 记 source=market；自训/导入无标记）
function SourceBadge({ source }: { source: string }) {
  if (source === "market") {
    return <span className="rounded-full bg-emerald-500/15 px-2 py-1 text-[10px] font-medium text-emerald-400">市场</span>
  }
  return null
}

export function VoicesPage(p: ReturnType<typeof useVoices>) {
  const mine = p.mine
  // 音色质检分数：/rvc/voices 带 qc 字段，按音色 ID 对齐到音色库卡片（随刷新重拉）
  const [qcMap, setQcMap] = useState<Record<string, VoiceQc>>({})
  useEffect(() => {
    listRvcVoices()
      .then((r) => {
        const m: Record<string, VoiceQc> = {}
        for (const v of r.voices) if (v.qc) m[v.id] = v.qc
        setQcMap(m)
      })
      .catch(() => { /* 后端未启动时忽略 */ })
  }, [p.voices])
  const [importing, setImporting] = useState(false)
  const [importMsg, setImportMsg] = useState("")
  const [importErr, setImportErr] = useState("")

  const handleImport = async (file: File | undefined, overwrite: boolean) => {
    if (!file) return
    setImporting(true)
    setImportMsg("")
    setImportErr("")
    try {
      const r = await importVoicePack(file, overwrite)
      setImportMsg(`已导入音色「${r.display_name}」${r.rvc_files ? `，并还原 ${r.rvc_files} 个 RVC 模型文件` : ""}`)
      await p.loadVoices()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes("已存在") && window.confirm(`${msg}\n是否覆盖现有档案？`)) {
        await handleImport(file, true)
      } else {
        setImportErr(msg)
      }
    } finally {
      setImporting(false)
    }
  }
  return <div className="min-h-full bg-gradient-to-br from-background via-background to-card"><header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14"><div className="mx-auto max-w-7xl"><p className="font-mono text-xs uppercase tracking-widest text-primary">STAGE 02 / VOICE LIBRARY</p><h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">音色库</h2><p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">上传视频解析后，自动从中挖掘音色候选；试听不满意就换一个，或回工坊补充视频再挖，直到满意为止。</p></div></header><main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-[minmax(0,1.6fr)_320px] lg:px-12 lg:py-14"><section className="space-y-10"><div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><p className="font-mono text-xs uppercase tracking-widest text-primary">AUTO DISCOVERY</p><h3 className="mt-2 text-lg font-semibold text-card-foreground">音色挖掘</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">把已解析的切片做转写、声纹聚类，自动筛出候选音色；每个候选会用一句与视频原话无关的新文本合成试听，真实检验音色迁移效果。</p></div><button type="button" onClick={() => void p.startMine()} disabled={mine.running || !p.backendUp} className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50">{mine.running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}{mine.running ? "挖掘中…" : mine.clusters.length ? "重新挖掘" : "开始挖掘"}</button></div><div className="mt-4 flex flex-wrap items-center gap-3 rounded-md border border-border bg-background/50 px-3 py-2.5 text-xs"><span className="font-medium text-muted-foreground">挖掘参数</span><label className="flex items-center gap-1.5 text-muted-foreground">相似度阈值<input type="number" min={0.3} max={0.8} step={0.05} value={p.mineSim} onChange={(e) => p.setMineSim(Number(e.target.value) || 0.5)} className="w-16 rounded-md border border-border bg-background px-2 py-1 text-card-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary" /></label><label className="flex items-center gap-1.5 text-muted-foreground">最小簇人数<input type="number" min={1} max={20} step={1} value={p.mineMinCluster} onChange={(e) => p.setMineMinCluster(Math.max(1, Math.round(Number(e.target.value) || 1)))} className="w-14 rounded-md border border-border bg-background px-2 py-1 text-card-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary" /></label><span className="text-muted-foreground">阈值越高挖出越多不同音色；最小人数过滤零散噪声簇</span></div><div className="mt-3 flex flex-wrap gap-2"><label className={`inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary ${p.importing || p.recording ? "pointer-events-none opacity-60" : ""}`}><FolderInput className="h-3.5 w-3.5" />{p.importing ? "批量导入中…" : "批量导入文件夹"}<input type="file" className="hidden" multiple {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} onChange={(e) => { const fs = Array.from(e.target.files ?? []); e.target.value = ""; void p.importFolder(fs) }} /></label><label className={`inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary ${p.importing || p.recording ? "pointer-events-none opacity-60" : ""}`}><FileAudio className="h-3.5 w-3.5" />选择音频/视频文件<input type="file" className="hidden" multiple accept=".mp4,.mkv,.mov,.flv,.webm,.avi,.wav,.mp3,.m4a,.flac,.ogg,.aac,.wma" onChange={(e) => { const fs = Array.from(e.target.files ?? []); e.target.value = ""; void p.importFiles(fs) }} /></label><button type="button" onClick={() => void (p.recording ? p.stopRecording() : p.startRecording())} disabled={p.importing} className={`inline-flex items-center gap-2 rounded-md border px-3 py-2 text-xs font-medium transition ${p.recording ? "border-red-500/50 bg-red-500/10 text-red-400 hover:bg-red-500/20" : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary"} disabled:pointer-events-none disabled:opacity-60`}>{p.recording ? (<><span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />停止录音（{Math.floor(p.recordSeconds / 60)}:{String(p.recordSeconds % 60).padStart(2, "0")}）</>) : (<><Mic className="h-3.5 w-3.5" />麦克风录音</>)}</button></div><p className="mt-2 text-[11px] text-muted-foreground">支持视频、音频文件与现场录音；录音/导入后会自动切片并挖掘候选音色。</p>{p.importMessage && <p className="mt-3 flex items-center gap-2 text-xs text-primary"><Loader2 className="h-3.5 w-3.5 animate-spin" />{p.importMessage}</p>}{mine.running && <p className="mt-4 flex items-center gap-2 text-xs text-primary"><Loader2 className="h-3.5 w-3.5 animate-spin" />{mine.message || "正在转写与提取声纹…"}（首次需加载模型，可能要一两分钟）</p>}{mine.stage === "error" && <ErrorPanel title="音色挖掘失败" detail={mine.message} hint="查看后端控制台输出，把报错复制给 AI 可快速定位根因" />}
          {mine.errors && mine.errors.length > 0 && <ErrorPanel title={`挖掘中有 ${mine.errors.length} 个切片处理失败`} detail={mine.errors.join("\n")} hint="这些切片被跳过，不影响其余候选；多半是音频损坏或过短" />}{mine.stage === "done" && !mine.running && <p className="mt-4 text-xs text-muted-foreground">共筛出 {mine.clusters.length} 个候选音色（有效切片 {mine.kept} 条）。试听下面的候选，满意就保存。</p>}{mine.stage === "done" && !mine.running && !!mine.clusters.length && <ul className="mt-5 space-y-3">{mine.clusters.slice(0, 6).map((c, index) => <li key={c.cluster} className="rounded-lg border border-border bg-background/60 p-4"><div className="flex flex-wrap items-center justify-between gap-3"><div className="min-w-0"><p className="flex items-center gap-2 text-sm font-medium text-card-foreground"><span className="rounded-full bg-primary/10 px-2 py-0.5 font-mono text-xs text-primary">候选 {String(index + 1).padStart(2, "0")}</span>{c.size} 段同源切片</p><p className="mt-1 truncate text-xs text-muted-foreground">代表片段原话：{c.rep.text}</p></div><div className="flex shrink-0 items-center gap-2"><button type="button" onClick={() => void p.tryPreview(c.rep.name)} disabled={!!p.previewing} className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 disabled:opacity-50">{p.previewing === c.rep.name ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}{p.previewing === c.rep.name ? "合成中…" : "试听新句"}</button><button type="button" onClick={() => void p.saveCandidate(c.rep.name, c.members)} disabled={p.busy} className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition hover:scale-105 disabled:opacity-50"><Save className="h-3.5 w-3.5" />保存为音色</button></div></div>{p.preview?.clip === c.rep.name && <div className="mt-3 rounded-md border border-border bg-card/70 p-3"><p className="text-xs text-muted-foreground">试听文本（与原视频无关）：{p.preview.text}</p><StudioAudioPlayer src={p.preview.url} label="播放试听" className="mt-2" /></div>}</li>)}</ul>}{mine.stage === "done" && !mine.clusters.length && <p className="mt-4 text-xs text-muted-foreground">没有筛出候选：切片可能太短或没有人声，回工坊换一段视频试试。</p>}</div><div className="mb-5 flex items-end justify-between"><div><p className="font-mono text-xs uppercase tracking-widest text-primary">已保存档案</p><h3 className="mt-2 text-2xl font-semibold text-foreground">可用音色</h3></div><button type="button" onClick={() => void p.loadVoices()} className="rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground shadow-md transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"><RefreshCw className="mr-2 inline h-3.5 w-3.5" />刷新</button></div>{p.loading ? <div className="space-y-3">{[1, 2, 3].map((item) => <div key={item} className="h-24 animate-pulse rounded-2xl border border-border bg-card shadow-md" />)}</div> : p.voices.length ? <div className="space-y-3">{p.voices.map((voice) => { const active = p.selectedVoiceId === voice.id; const isRvc = voice.kind === "rvc_model" || !voice.has_reference; return <article key={voice.id} className={`rounded-2xl border bg-card p-4 shadow-md transition hover:shadow-lg ${active ? "border-primary/70" : "border-border"}`}><div className="flex items-start gap-4"><button type="button" onClick={() => p.selectVoice(voice.id)} className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border ${active ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground"}`} aria-label={`选择${voice.display_name ?? voice.id}`}>{active ? <CheckCircle2 className="h-5 w-5" /> : <Crown className="h-5 w-5" />}</button><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h4 className="truncate text-sm font-semibold text-card-foreground">{voice.display_name ?? voice.id}</h4>{active && <span className="rounded-full bg-primary/10 px-2 py-1 text-xs text-primary">当前使用</span>}{isRvc && <span className="rounded-full bg-violet-500/15 px-2 py-1 text-[10px] font-medium text-violet-400">RVC 模型</span>}{voice.source && <SourceBadge source={voice.source} />}{qcMap[voice.id] && <QcBadge qc={qcMap[voice.id]} />}</div><p className="mt-1 font-mono text-xs text-muted-foreground">音色 ID：{voice.id}</p>{isRvc ? <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">{voice.model_ready ? <span className="inline-flex items-center gap-1 text-emerald-500"><CheckCircle2 className="h-3 w-3" />模型就绪</span> : voice.dataset_count ? <span className="inline-flex items-center gap-1 text-primary"><Database className="h-3 w-3" />语料 {voice.dataset_count} 条</span> : <span className="text-muted-foreground">未训练</span>}{voice.trained_at && <span>训练于 {voice.trained_at}</span>}</div> : <><p className="mt-1 font-mono text-xs text-muted-foreground">{voice.reference} · {voice.duration_s.toFixed(1)} 秒参考音频</p><StudioAudioPlayer src={p.audioUrl(voice)} label="试听参考音频" className="mt-3" /></>}{voice.preview_url && <div className="mt-2"><p className="text-[11px] text-muted-foreground">市场自动试听</p><StudioAudioPlayer src={mediaUrl(voice.preview_url)} label="播放试听" className="mt-1" /></div>}</div><div className="flex shrink-0 items-center gap-1">{!isRvc && <a href={voicePackUrl(voice.id)} download title="导出音色包（参考音频+文字稿）" className="rounded-md p-2 text-muted-foreground transition hover:bg-primary/10 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label={`导出${voice.display_name ?? voice.id}`}><Download className="h-4 w-4" /></a>}<a href={voicePackUrl(voice.id, true)} download title="导出含 RVC 模型（若已训练，导入后可直接实时变声）" className="rounded-md p-2 text-muted-foreground transition hover:bg-primary/10 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label={`导出含模型${voice.display_name ?? voice.id}`}><HardDriveDownload className="h-4 w-4" /></a><button type="button" onClick={() => void p.deleteVoice(voice)} className="rounded-md p-2 text-muted-foreground transition hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive" aria-label={`删除${voice.display_name ?? voice.id}`}><Trash2 className="h-4 w-4" /></button></div></div></article> })}</div> : <StudioEmpty title="音色库还为空" description="点击上方「开始挖掘」，从已解析的视频切片里自动筛出音色。" />}{p.voices.length >= 2 && <AbCompareCard voices={p.voices} backendUp={p.backendUp} />}{p.errorMessage && <ErrorPanel title="音色库操作失败" detail={p.errorMessage} />}{p.feedback && <div className="mt-5 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs text-primary"><CheckCircle2 className="h-4 w-4" />{p.feedback}</div>}</section><section><div className="sticky top-24 space-y-6"><div className="rounded-2xl border border-border bg-gradient-to-br from-primary/10 via-card to-card p-5 shadow-lg sm:p-6"><p className="font-mono text-xs uppercase tracking-widest text-primary">迭代筛选</p><h3 className="mt-2 text-xl font-semibold text-card-foreground">不满意？继续挖</h3><ol className="mt-4 space-y-3 text-xs leading-5 text-muted-foreground"><li className="flex gap-2"><Clock3 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />试听候选的「新句」，判断音色像不像。</li><li className="flex gap-2"><RefreshCw className="mt-0.5 h-4 w-4 shrink-0 text-primary" />不像就换下一个候选，或点「重新挖掘」。</li><li className="flex gap-2"><AudioLines className="mt-0.5 h-4 w-4 shrink-0 text-primary" />素材不够像？回「音色工坊」上传更多视频再挖。</li><li className="flex gap-2"><Save className="mt-0.5 h-4 w-4 shrink-0 text-primary" />满意就保存，之后在「文字转语音」里直接使用。</li></ol></div><div className="rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6"><p className="font-mono text-xs uppercase tracking-widest text-primary">建立新档案</p><h3 className="mt-2 text-xl font-semibold text-card-foreground">从勾选片段创建</h3><p className="mt-2 text-sm leading-6 text-muted-foreground">也可以在工作台手动勾选干净片段，手工建参考音色。</p><label className="mt-5 block text-xs font-medium text-muted-foreground" htmlFor="voice-id">音色 ID</label><input id="voice-id" value={p.voiceIdInput} onChange={(event) => p.setVoiceIdInput(event.target.value)} placeholder="例如 my_voice（字母/数字/下划线）" className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary" /><div className="mt-4 flex items-center justify-between rounded-lg border border-border bg-background/60 p-3 text-xs"><span className="flex items-center gap-2 text-muted-foreground"><Clock3 className="h-4 w-4" />已勾选片段</span><span className="font-mono text-card-foreground">{p.selectedClips.size} 段 · {p.selectedDuration.toFixed(1)}s</span></div><button type="button" onClick={() => void p.createVoice()} disabled={p.busy || !p.backendUp || !p.selectedClips.size} className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50">{p.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}{p.busy ? "创建中…" : "创建参考音色"}</button><div className="mt-5 space-y-2 border-t border-border pt-4 text-xs leading-5 text-muted-foreground"><p><WandSparkles className="mr-2 inline h-4 w-4 text-primary" />挖掘保存的音色会自动带上文字稿，克隆相似度更高。</p></div></div><div className="rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6"><p className="font-mono text-xs uppercase tracking-widest text-primary">分享与迁移</p><h3 className="mt-2 text-xl font-semibold text-card-foreground">导入音色包</h3><p className="mt-2 text-sm leading-6 text-muted-foreground">把别处导出的音色包 zip 拖进来，即可还原参考音色；含 RVC 模型的包导入后可直接实时变声。</p><label className={`mt-4 inline-flex w-full cursor-pointer items-center justify-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-4 py-2.5 text-sm font-medium text-primary transition hover:bg-primary/20 ${importing ? "pointer-events-none opacity-60" : ""}`}><FolderInput className="h-4 w-4" />{importing ? "导入中…" : "选择音色包 zip"}<input type="file" accept=".zip" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; void handleImport(f, false) }} /></label>{importMsg && <p className="mt-3 rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-xs leading-5 text-primary"><CheckCircle2 className="mr-1.5 inline h-3.5 w-3.5" />{importMsg}</p>}{importErr && <ErrorPanel title="导入音色包失败" detail={importErr} />}</div></div></section></main></div>
}

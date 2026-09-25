import { useRef, useState } from "react"
import { Check, Download, Loader2, Send, ShoppingBag, Square, Trash2, Upload, Wand2, X } from "lucide-react"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import {
  PREMIX_MODE_HINT,
  PREMIX_MODE_LABEL,
  type useSoundboard,
} from "@/pages/Tts/useSoundboard"
import { PremixTimeline } from "@/pages/Tts/PremixTimeline"

/** 出厂音效的图标（按 id；用户导入的走兜底）。 */
/**
 * 出厂素材的兜底图标（**兼容旧后端**：图标现在由 manifest 下发，见 `sfx_lib._icon`）。
 *
 * 为什么不删：前端与后端在本仓是**两条独立的同步链路**（前端 `npm run ship`、
 * 后端 `sync_backend.ps1`），完全可能碰到「新前端 + 旧后端」—— 那时 `icon` 是 undefined，
 * 没有这张表格子就会全是 🎧。它是兜底，不是第二份产品规则：真正改图标要改 manifest，
 * 这样另一处渲染层（悬浮声板窗）不用再抄一份映射。
 */
const ICONS: Record<string, string> = {
  boom: "💥",
  applause: "👏",
  alarm: "⚠️",
  riser: "🎵",
  ding: "🔔",
  weird: "👻",
}

/**
 * 特效声板格子面板（两种模式，同一份素材）。
 *
 * **实时**：点一下即出声，与人声一起被微信录走（详情见 `useSoundboard` 注释）。
 * **预混**：点一下只把音效勾上，点「混进这条语音」才离线混出一份新音频 ——
 *   发送目标随之换成它。这是"点格子会抢焦点、可能打断按住录音"那条路的兜底。
 *
 * **自门控**：整块 UI 归 `sound.fx-board` 插件，关掉它时后端 router 已卸载、
 * 端点全 404，所以这里必须 `return null` —— 这正是"宿主页托管别的插件"那一类
 * 缺口（`docs/犯错指南.md` 速查表 74 / `docs/犯错档案-工程.md` §8.40）。
 * 门控写在组件自己身上（而不是调用方），是为了让它无论被谁塞进哪个页面都安全。
 *
 * 四处使用点（同一个组件、同一份素材目录，但两个宿主页的门控写法不同）：
 *   · 微信发送页 ② 半自动（宿主 `hook.wechat`）—— `allowPremix`：TTS 播到声卡、
 *     你按住 Alt 录制的**那几秒里**点格子，或者干脆先混好再播（确定性路径）；
 *   · 微信发送页 ③ 手动（宿主 `hook.wechat`）—— 只有实时：这一档没有合成产物；
 *   · 实时变声页 RVC 实时 / 千问变声两个控制台（宿主 `sound.rvc-live`，2026-09-24 加）——
 *     只有实时，且是**主场景**（边变声边打音效）；那两处传了专属 hint，因为实时页
 *     有个别处没有的性质：自我监听回环 tap 的正是 CABLE Output，写进 CABLE 的音效
 *     用户自己耳机里也听得到（微信发送页没有这个回环，所以默认 hint 只讲对方听得到）。
 */
export function SoundboardPanel({
  sb,
  hint,
  allowPremix = false,
  sourceSeconds,
  src,
  wav,
  onPremixed,
  onSendPremixed,
}: {
  // hook 由路由层调一次（`pages/Tts/index.tsx`）往下传 —— 本组件在本页出现两回，
  // 各调一次 hook 就是两回预热/两回目录请求，而且两块的"正在播"高亮会各说各话。
  sb: ReturnType<typeof useSoundboard>
  hint?: string
  /** 是否提供"预混"模式（③ 手动档没有合成产物，只能实时）。 */
  allowPremix?: boolean
  /**
   * 预混源的人声时长（秒）—— 只用来给"第几秒"一个看得到的范围与提醒，
   * **不**用它去封顶用户的输入（封顶就等于默默改掉他填的数）。
   */
  sourceSeconds?: number
  /**
   * 预混源的**可播放地址**（`mediaUrl(...)` 拼好的）—— 波形靠它解码。
   * 缺省时波形位置退化成一行说明，秒数框与拖动以外的能力照旧。
   */
  src?: string
  /** 预混的源：要混的那条合成产物（文件名）。 */
  wav?: string
  /** 混好一份新音频时回调（调用方把发送目标换成它）。 */
  onPremixed?: (r: { wav: string; inserts: number; seconds: number }) => void
  /**
   * 混好之后**直接自动发送**（可选）。给出它时面板多一个「混好并直接发送」按钮 ——
   * 那是「文字转语音 → 微信」这条链路的一步到位：预混（离线，确定性）+
   * `send_voice`（程序点话筒起录、点绿钮发送），全程不用碰微信、不用按 Alt。
   * 不给出它时（实时页、③ 手动档）只有「混进这条语音」—— 那些地方没有"发送"这一步。
   */
  onSendPremixed?: (wav: string) => void
}) {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const on = pluginVisible(catalog, "sound.fx-board")
  const [mode, setMode] = useState<"live" | "premix">("live")
  const [done, setDone] = useState("")
  const [shelfOpen, setShelfOpen] = useState(false)
  const importInput = useRef<HTMLInputElement | null>(null)
  const packInput = useRef<HTMLInputElement | null>(null)

  if (!on) return null

  const premixUi = allowPremix && Boolean(wav)
  const inPremix = premixUi && mode === "premix"
  const picked = new Set(sb.picks.map((p) => p.sample))
  // 「我的素材」：能不能单条删由**后端**说（`removable`），不在这里拿 builtin/pack 推 ——
  // "谁能删"是会变的产品规则（比如以后允许删包内某一条），多一处推断就多一处会漂。
  const imported = sb.items.filter((i) => i.removable)
  // ⚠️ 素材动作（导入/装卸包/删素材）**需要**互斥：它们改的是目录本身，两个同时在飞
  // 会让刷新乱序，界面停在一个「少一条」的状态上。
  // 而格子（`play`）**绝不能**因这个锁变灰 —— 发送正在进行时正是要出声的时候。
  // 两个锁因此必须**分开命名**：`crossPluginGate.test.ts` 用正则守着
  // 「格子的 disabled 里不许出现 busy」，把两者合回一个表达式会让那条守卫失效。
  const materialLocked = !sb.ready || Boolean(sb.busy)

  const runPremix = async () => {
    const r = await sb.premix(wav)
    if (r) {
      setDone(r.wav)
      onPremixed?.({ wav: r.wav, inserts: r.inserts, seconds: r.seconds })
      // 勾选**不清空**：常要换个位置再混一次（改勾选后重按按钮即可）。
    }
  }

  /**
   * 混好并**立刻**用全自动链路发出去（只在 ② 半自动档出现）。
   *
   * 为什么不满足于"② 按「混进这条语音」→ ① 按「自动发送到微信」"两步：两步之间那个
   * 「发送目标已换成 `sfxmix_*`」的中间态在界面上**看不见**，用户很容易在第 ② 步之后
   * 去别处（重新合成一条）再点 ① —— 发出去的就是没混的那条，且全程无报错。
   * 这里把 wav **显式**交给发送方（不依赖 `targetWav` 的失效判定），顺序只剩一种。
   */
  const runPremixAndSend = async () => {
    const r = await sb.premix(wav)
    if (!r) return // 失败原因已在 sb.premixError 里显示，绝不装作发出去了
    setDone(r.wav)
    onPremixed?.({ wav: r.wav, inserts: r.inserts, seconds: r.seconds })
    onSendPremixed?.(r.wav)
  }

  return (
    <div className="mt-3 rounded-md border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-card-foreground">
          音效声板
          <span className="ml-2 font-normal text-muted-foreground">
            {inPremix
              ? "勾选音效后混进这条语音（不抢焦点，确定性最高）"
              : (hint ?? "点一下即出声，与人声一起被微信录走")}
          </span>
        </p>
        <div className="flex items-center gap-2">
          {premixUi && (
            <div className="inline-flex overflow-hidden rounded-md border border-border text-[11px]">
              {(["live", "premix"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={`px-2 py-1 transition ${
                    mode === m
                      ? "bg-primary/15 text-primary"
                      : "text-muted-foreground hover:text-primary"
                  }`}
                >
                  {m === "live" ? "实时" : "预混"}
                </button>
              ))}
            </div>
          )}
          {sb.playing && !inPremix && (
            <button
              type="button"
              onClick={() => void sb.stop()}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              <Square className="h-3 w-3" />停止
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-6">
        {sb.items.length === 0 && (
          <p className="col-span-3 text-[11px] text-muted-foreground sm:col-span-6">
            还没有音效素材（`sound.fx-board` 插件自带的 6 条出厂音效应在此列出）。
          </p>
        )}
        {sb.items.map((it) => {
          const active = inPremix ? picked.has(it.id) : sb.playing === it.id
          return (
            <button
              key={it.id}
              type="button"
              disabled={!sb.ready || sb.premixing}
              title={`${it.name} · ${it.duration_s}s${it.count ? ` · 用过 ${it.count} 次` : ""}${
                inPremix ? "（点一下勾选/取消）" : ""
              }`}
              onClick={() => void (inPremix ? sb.togglePick(it.id) : sb.play(it.id))}
              className={`relative flex flex-col items-center gap-1 rounded-lg border px-2 py-2.5 text-[11px] transition disabled:pointer-events-none disabled:opacity-50 ${
                active
                  ? "border-primary bg-primary/15 text-primary"
                  : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary"
              }`}
            >
              {inPremix && active && (
                <span className="absolute right-1 top-1 text-primary">
                  <Check className="h-3 w-3" />
                </span>
              )}
              <span className="text-lg leading-none">{it.icon || ICONS[it.id] || "🎧"}</span>
              <span className="max-w-full truncate">{it.name}</span>
            </button>
          )
        })}
      </div>

      {/* 预混：勾选清单 + 位置轮换 + 执行 */}
      {inPremix && (
        <div className="mt-2.5 space-y-2">
          {/* 波形：拖一下改位置 —— 用户知道的是"说到那句的时候"，那不是数出来的 */}
          <PremixTimeline
            src={src}
            seconds={sourceSeconds}
            markers={sb.picks
              .filter((p) => p.mode === "layer")
              .map((p) => ({ id: p.sample, label: ICONS[p.sample] ?? "🎧", at_s: p.at_s }))}
            onMove={sb.setPickAt}
          />
          {sb.picks.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              {sb.picks.map((p) => (
                <span
                  key={p.sample}
                  className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/10 py-0.5 pl-2 pr-1 text-[11px] text-primary"
                >
                  <button
                    type="button"
                    onClick={() => sb.cyclePickMode(p.sample)}
                    title={`${PREMIX_MODE_HINT[p.mode]}（点一下切换位置）`}
                    className="inline-flex items-center gap-1"
                  >
                    {ICONS[p.sample] ?? "🎧"}
                    {PREMIX_MODE_LABEL[p.mode]}
                  </button>
                  {/* 「第几秒」只属于叠加档：开头/结尾是拼接，位置由 mode 决定。 */}
                  {p.mode === "layer" && (
                    <span className="inline-flex items-center gap-0.5">
                      第
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        value={p.at_s}
                        onChange={(e) => sb.setPickAt(p.sample, Number(e.target.value))}
                        aria-label={`${sb.items.find((i) => i.id === p.sample)?.name ?? p.sample} 插在人声第几秒`}
                        title={
                          sourceSeconds
                            ? `插在人声的第几秒（本条人声 ${sourceSeconds.toFixed(1)}s）；也可以直接拖波形上的记号`
                            : "插在人声的第几秒（0 = 一开口就响）；也可以直接拖波形上的记号"
                        }
                        className="w-12 rounded border border-primary/40 bg-background px-1 py-0 text-[11px] text-primary"
                      />
                      秒
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => sb.togglePick(p.sample)}
                    aria-label="取消这条音效"
                    className="opacity-70 transition hover:opacity-100"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              点上面的格子勾选音效；「叠加」= 在第 N 秒同时响（0 = 一开口），「开头」= 先响一声再说话。
            </p>
          )}
          {/* 超出人声长度的提醒：后端会把它贴到末尾（并如实回报），但事先能看见更好。 */}
          {sourceSeconds !== undefined &&
            sb.picks.some((p) => p.mode === "layer" && p.at_s > sourceSeconds) && (
              <p className="text-[11px] text-yellow-600">
                有音效的秒数超出人声长度（{sourceSeconds.toFixed(1)}s）—— 它会被贴到末尾，
                而不是在你填的那一秒响。
              </p>
            )}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={!sb.ready || !sb.picks.length || sb.premixing}
              onClick={() => void runPremix()}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[11px] font-medium text-primary-foreground transition hover:scale-[1.03] disabled:pointer-events-none disabled:opacity-50"
            >
              {sb.premixing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
              {sb.premixing ? "混音中…" : "混进这条语音"}
            </button>
            {onSendPremixed && (
              <button
                type="button"
                disabled={!sb.ready || !sb.picks.length || sb.premixing}
                onClick={() => void runPremixAndSend()}
                title="混好后立刻用 ① 全自动发送到微信（程序自己点话筒起录、点发送钮），全程不用碰微信"
                className="inline-flex items-center gap-1.5 rounded-md border border-primary px-3 py-1.5 text-[11px] font-medium text-primary transition hover:bg-primary/10 disabled:pointer-events-none disabled:opacity-50"
              >
                <Send className="h-3.5 w-3.5" />
                混好并直接发送
              </button>
            )}
            {done && (
              <span className="font-mono text-[11px] text-muted-foreground">
                已混好 {done} · 改过勾选后再点一次即可
              </span>
            )}
          </div>
          {sb.premixError && <p className="text-[11px] text-destructive">{sb.premixError}</p>}
          {/* 后端的回报：读不出来的音效（少了一声）与越界的秒数（位置变了）都在这里 ——
              两者都属于"界面看不出异常"的失败，所以有就列出来。 */}
          {sb.premixNotes.length > 0 && (
            <ul className="space-y-0.5 text-[11px] text-yellow-600">
              {sb.premixNotes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 素材管理：单条导入 / 成套音效包。与"点一下响一下"是两件事，所以单列一区。 */}
      <div className="mt-3 border-t border-border pt-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-medium text-card-foreground">素材</span>
          <button
            type="button"
            disabled={materialLocked}
            onClick={() => importInput.current?.click()}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
          >
            <Upload className="h-3 w-3" />导入素材
          </button>
          <button
            type="button"
            disabled={materialLocked}
            onClick={() => packInput.current?.click()}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
          >
            <ShoppingBag className="h-3 w-3" />安装音效包
          </button>
          <button
            type="button"
            onClick={() => {
              setShelfOpen((v) => !v)
              void sb.openShelf()
            }}
            title="从清单里下载全套音效（装到本机，可随时卸载）"
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            <Download className="h-3 w-3" />
            音效包市场{sb.packs.length ? `（已装 ${sb.packs.length}）` : ""}
          </button>
          {sb.busy && (
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />{sb.busy}…
            </span>
          )}
          <span className="text-[11px] text-muted-foreground">
            共 {sb.items.length} 条（出厂 {sb.items.filter((i) => i.builtin).length}
            {sb.packSamples ? ` · 音效包 ${sb.packSamples}` : ""}
            {imported.length ? ` · 我的 ${imported.length}` : ""}）
          </span>
          {/* 两个隐式输入：accept 写宽一点（真正认不认由服务端读音频决定），
              包则只收 .zip —— 它必须是 zip，别的格式连试都不必。 */}
          <input
            ref={importInput}
            type="file"
            accept="audio/*,.wav,.flac,.ogg,.mp3"
            multiple
            hidden
            onChange={(e) => {
              const files = e.target.files
              if (files?.length) void sb.importFiles(files)
              e.target.value = "" // 清掉才能连续选同一个文件（否则第二次不触发）
            }}
          />
          <input
            ref={packInput}
            type="file"
            accept=".zip,application/zip"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void sb.installPackZip(f)
              e.target.value = ""
            }}
          />
        </div>

        {/* 我的素材：可单条删（出厂与包内素材服务端会拒，所以不在这里列出删除） */}
        {imported.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">我的：</span>
            {imported.map((it) => (
              <span
                key={it.id}
                className="inline-flex items-center gap-1 rounded-full border border-border py-0.5 pl-2 pr-1 text-[11px] text-muted-foreground"
              >
                {it.name}
                <button
                  type="button"
                  disabled={materialLocked}
                  onClick={() => void sb.removeSample(it.id)}
                  title={`删除「${it.name}」`}
                  aria-label={`删除 ${it.name}`}
                  className="opacity-70 transition hover:text-destructive hover:opacity-100"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* 已装的包：一条一个胶囊，✕ = 整包卸载（里面的素材会一起消失，标题里说清） */}
        {sb.packs.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">音效包：</span>
            {sb.packs.map((p) => (
              <span
                key={p.id}
                className={`inline-flex items-center gap-1 rounded-full border py-0.5 pl-2 pr-1 text-[11px] ${
                  p.broken ? "border-destructive/50 text-destructive" : "border-border text-muted-foreground"
                }`}
              >
                {p.name}
                <span className="opacity-60">
                  {p.broken ? `已损坏：${p.broken}` : `${p.count} 条${p.license ? ` · ${p.license}` : ""}`}
                </span>
                <button
                  type="button"
                  disabled={materialLocked}
                  onClick={() => void sb.uninstallPack(p.id)}
                  title={`卸载「${p.name}」（共 ${p.count} 条素材，会一起删掉）`}
                  aria-label={`卸载 ${p.name}`}
                  className="opacity-70 transition hover:text-destructive hover:opacity-100"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* 货架：懒加载。清单没配 / 取不到都如实说，不假装"市场里没有" */}
        {shelfOpen && (
          <div className="mt-2 rounded-md border border-border bg-background/60 p-2">
            {sb.shelf.note && (
              <p className="text-[11px] text-muted-foreground">{sb.shelf.note}</p>
            )}
            {sb.shelf.error && (
              <p className="text-[11px] text-destructive">音效包清单不可用：{sb.shelf.error}</p>
            )}
            {sb.shelf.items.length === 0 && !sb.shelf.note && !sb.shelf.error && (
              <p className="text-[11px] text-muted-foreground">清单里还没有可下载的音效包。</p>
            )}
            {sb.shelf.items.map((p) => (
              <div key={p.id} className="flex flex-wrap items-center gap-2 py-1">
                <span className="text-[11px] font-medium text-card-foreground">{p.name}</span>
                <span className="text-[11px] text-muted-foreground">
                  {p.author ? `${p.author} · ` : ""}
                  {p.license || "未标注许可"}
                  {p.downloads ? ` · 已下载 ${p.downloads}` : ""}
                </span>
                {p.installed ? (
                  <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                    <Check className="h-3 w-3" />已安装
                  </span>
                ) : (
                  <button
                    type="button"
                    disabled={materialLocked}
                    onClick={() => void sb.downloadPack(p.id)}
                    className="rounded-md border border-border px-2 py-0.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    下载并安装
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {sb.materialError && (
          <p className="mt-2 text-[11px] text-destructive">{sb.materialError}</p>
        )}
      </div>

      {sb.errorMessage && <p className="mt-2 text-[11px] text-destructive">{sb.errorMessage}</p>}
    </div>
  )
}

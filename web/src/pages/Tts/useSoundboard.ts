import { useCallback, useEffect, useRef, useState } from "react"
import {
  soundboardCatalog,
  soundboardDelete,
  soundboardImport,
  soundboardPackDownload,
  soundboardPackInstall,
  soundboardPacks,
  soundboardPacksAvailable,
  soundboardPackUninstall,
  soundboardPlay,
  soundboardPremix,
  soundboardStop,
  soundboardWarm,
  type SoundboardItem,
  type SoundboardMode,
  type SoundboardPack,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

/**
 * 预混勾选：一条音效 + 它混进去的位置 + （叠加档）**第几秒**。
 *
 * `at_s` 只对 `layer` 有意义 —— 开头/结尾是拼接，位置由 `mode` 本身决定；
 * 后端 `sfx_lib.mix_into` 就是这么解释的（它在 layer 分支里才读 `at_s`）。
 */
export type PremixPick = { sample: string; mode: SoundboardMode; at_s: number }

/** 位置的中文标签（格子/胶囊上显示；顺序即点击轮换顺序）。 */
export const PREMIX_MODE_LABEL: Record<SoundboardMode, string> = {
  layer: "叠加",
  prepend: "开头",
  append: "结尾",
}
const MODE_CYCLE: SoundboardMode[] = ["layer", "prepend", "append"]

/** 一句话解释每个位置意味着什么（用户不该靠猜"叠加"是叠在哪）。 */
export const PREMIX_MODE_HINT: Record<SoundboardMode, string> = {
  layer: "在指定秒数处同时响（0 = 一开口就响）",
  prepend: "先响一声，再说话",
  append: "说完之后来一声",
}

/**
 * 特效声板：两种用法共用一份素材目录与一份播放/混音能力。
 *
 *   · **实时**：点一下格子，把一条短音效立刻播进虚拟声卡。
 *     为什么能和"正在播的 TTS / 正在跑的实时变声"同时出声：Windows 音频默认共享模式，
 *     多路程序写同一设备由系统混音器自动混合 —— 微信从 CABLE 采集端录到的就是混好的结果。
 *     所以实时这条路径**完全不碰微信发送链路**（`_send_lock` 也不进）：发送期间点格子照样出声。
 *
 *   · **预混**：把勾选的音效离线混进一条合成产物，产出一个新 wav，发送目标换成它。
 *     存在的理由是实时那条路有一个尚未真机验证的物理限制 —— 点格子时鼠标焦点会离开微信，
 *     **按住的录音可能被取消**（设计稿 §五）。预混不依赖任何交互，所以它是那条路走不通时的兜底。
 *
 * `enabled=false`（`sound.fx-board` 被关）时：不打后端、不预热 —— 端点此时根本不存在
 * （routers 随插件卸载），请求只会 404 空转。
 */
export function useSoundboard(enabled = true) {
  const { backendUp } = useAppStore()
  const [items, setItems] = useState<SoundboardItem[]>([])
  const [playing, setPlaying] = useState("")
  const [errorMessage, setErrorMessage] = useState("")
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 预混模式的状态
  const [picks, setPicks] = useState<PremixPick[]>([])
  const [premixing, setPremixing] = useState(false)
  const [premixError, setPremixError] = useState("")
  /**
   * 后端在混音时说的话（`premix` 响应的 `skipped`）：**失败的**（音效读不出来）与
   * **提示性的**（`at_s` 越界 → 已贴到末尾）都在这里。两类都必须显示 ——
   * 前者是"少了一声"，后者是"那一声没在你说的那一秒"，都是静默失败里最难发现的那种。
   * 后端那边的文案已经自带区分（提示带"已贴到末尾"），所以不再拆成两个状态。
   */
  const [premixNotes, setPremixNotes] = useState<string[]>([])

  // 素材管理（导入 / 音效包）的状态
  const [packs, setPacks] = useState<SoundboardPack[]>([])
  const [packSamples, setPackSamples] = useState(0)
  // 货架（可下载的包）：**懒加载** —— 没点开之前不请求，也不会白碰一次网络
  const [shelf, setShelf] = useState<{
    loaded: boolean
    source: string | null
    items: SoundboardPack[]
    error: string
    note: string
  }>({ loaded: false, source: null, items: [], error: "", note: "" })
  const [busy, setBusy] = useState("") // 正在进行的素材动作（一个字符串，够用且能显示）
  // ★ 互斥的**真身**必须是 ref，不能只看 `busy` state。
  // `setBusy` 是异步的：同一个事件循环里连点两次，"第二次进入时读到的 busy 还是空串"
  // → 两个动作同时在飞，而它们都会改素材目录、各自刷新，后落库的那次可能先到
  // （界面停在"少一条"状态，手动刷一下又对了 —— 典型偶发）。ref 是同步写，当场可见。
  const busyRef = useRef(false)
  // ★ 货架的"已拿到"同样用 ref：`openShelf` 是 async，而 `useCallback([shelf.loaded])`
  // 要等**重渲染**才会换上新闭包 —— `await openShelf(); await openShelf()`（或快速连点）
  // 时第二次读到的 `shelf.loaded` 仍是 false，于是白碰一次网络。
  // 一次多花的请求不致命，但它会让"货架为什么加载中"这种排查多一个变量，
  // 而且和上面 `busyRef` 是同一类错（拿异步 state 当同步锁），统一口径。
  const shelfLoadedRef = useRef(false)
  const [materialError, setMaterialError] = useState("")

  const refresh = useCallback(async () => {
    try {
      const r = await soundboardCatalog()
      setItems(r.items ?? [])
    } catch {
      /* 服务离线时静默，下次操作再试 */
    }
  }, [])

  const refreshPacks = useCallback(async () => {
    try {
      const r = await soundboardPacks()
      setPacks(r.packs ?? [])
      setPackSamples(r.samples ?? 0)
    } catch {
      /* 同上：下一次动作会再试 */
    }
  }, [])

  useEffect(() => {
    if (!enabled || !backendUp) return
    let alive = true
    // 目录与包列表**绝不串在预热后面**：两者是无关的两件事 ——
    // `/soundboard/catalog` 是毫秒级只读接口（真机实测 25ms），而预热要拉起常驻播放器、
    // 走一次**没有超时**的就绪握手（`_spawn_worker` 里 `proc.stdout.readline()`）。
    // 一旦握手慢或卡住，`await warm` 不返回 → 目录请求永远发不出去 →
    // 面板长时间停在"共 0 条（出厂 0）"，用户以为"这功能没有"（2026-09-25 真机踩到）。
    void (async () => {
      if (!alive) return
      await refresh()
      if (!alive) return
      await refreshPacks()
    })()
    // 预热独立在后台跑：它只影响"第一次点击"的延迟（冷导入 ~2-3s），
    // 失败也很正常（设备/venv 缺失），真点播放时后端会给出可读报错。
    void soundboardWarm().catch(() => {})
    return () => {
      alive = false
    }
  }, [enabled, backendUp, refresh, refreshPacks])

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const play = useCallback(
    async (id: string) => {
      if (!enabled) return
      setErrorMessage("")
      // 乐观高亮：不等后端返回就把格子点亮 —— one-shot 要的是"点击即响"，
      // 让用户等一个 RTT 再看到反馈，手感就散了。
      const dur = items.find((i) => i.id === id)?.duration_s ?? 1
      setPlaying(id)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setPlaying(""), Math.max(300, dur * 1000))
      try {
        await soundboardPlay(id)
      } catch (error) {
        setErrorMessage(friendlyError(error, "音效播放失败"))
        setPlaying("")
        if (timer.current) clearTimeout(timer.current)
      }
    },
    [enabled, items],
  )

  const stop = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current)
    setPlaying("")
    try {
      await soundboardStop()
    } catch {
      /* 已经播完时停止失败无所谓 */
    }
  }, [])

  // ---------------- 预混 ----------------

  /** 勾上/取消一条音效（预混模式的"点格子"语义与实时模式不同：它不发声）。 */
  const togglePick = useCallback((id: string) => {
    setPicks((prev) =>
      prev.some((p) => p.sample === id)
        ? prev.filter((p) => p.sample !== id)
        : [...prev, { sample: id, mode: "layer" as SoundboardMode, at_s: 0 }],
    )
  }, [])

  /**
   * 设定"第几秒"（只对「叠加」有意义）。
   *
   * 只夹到 **≥ 0**，**不**按人声长度封顶：面板只有在拿到 `sourceSeconds` 时才真知道
   * 那有多长，缺省时把上限猜出来就等于默默改掉用户填的数；真越界由后端如实回报
   * （`premixNotes` 里那句"已贴到末尾"）。面板有转时时会额外给个琥珀色提醒 ——
   * 看得见的提醒 + 后端如实回报，两层都不靠猜。
   */
  const setPickAt = useCallback((id: string, seconds: number) => {
    const v = Number.isFinite(seconds) ? Math.max(0, seconds) : 0
    setPicks((prev) => prev.map((p) => (p.sample === id ? { ...p, at_s: v } : p)))
  }, [])

  /** 切换某条的混淆位置：叠加 → 开头 → 结尾 → 叠加（点胶囊即可轮换）。 */
  const cyclePickMode = useCallback((id: string) => {
    setPicks((prev) =>
      prev.map((p) =>
        p.sample === id
          ? { ...p, mode: MODE_CYCLE[(MODE_CYCLE.indexOf(p.mode) + 1) % MODE_CYCLE.length] }
          : p,
      ),
    )
  }, [])

  const clearPicks = useCallback(() => {
    setPicks([])
    setPremixError("")
    setPremixNotes([])
  }, [])

  /**
   * 把勾选的音效混进 `wav`（不给就由后端取最近一条合成产物）。
   * 成功返回 `{wav, seconds, inserts, skipped}`，失败返回 null 并把原因放进 `premixError`。
   *
   * 失败是**硬失败**：后端一条音效认不出来就 4xx，前端如实显示。理由是静默跳过
   * 会变成"发出去的语音少了那一声，而界面一切正常"——那类失败最难被发现。
   */
  const premix = useCallback(
    async (
      wav?: string,
    ): Promise<{ wav: string; seconds: number; inserts: number; skipped: string[] } | null> => {
      if (!enabled || picks.length === 0) return null
      setPremixing(true)
      setPremixError("")
      setPremixNotes([])
      try {
        const r = await soundboardPremix(
          wav,
          picks.map((p) => ({
            sample: p.sample,
            mode: p.mode,
            // 只给 layer 带 at_s：开头/结尾是拼接，位置由 mode 本身决定（后端也只在
            // layer 分支里读它）。带着没意义的 0 一起发，读日志的人会以为它生效了。
            at_s: p.mode === "layer" ? p.at_s : 0,
          })),
        )
        // 后端**不会**静默跳过：读不出来的音效、越界的秒数都会出现在这里。
        setPremixNotes(r.skipped ?? [])
        void refresh() // 计数变了，格子上的"用过 N 次"要跟上
        return r
      } catch (error) {
        setPremixError(friendlyError(error, "预混失败"))
        return null
      } finally {
        setPremixing(false)
      }
    },
    [enabled, picks, refresh],
  )

  // ---------------- 素材管理（导入单条 / 装包卸包） ----------------

  /**
   * 所有素材动作的公共壳：同一个 `busy` 互斥 + 成功后把目录与包列表都刷一遍。
   *
   * 为什么要互斥：这些动作都会**改素材目录**，而目录是格子面板的渲染依据。
   * 两个动作同时在飞（比如连点两次导入），后完成的那次刷新可能先落库——
   * 界面就会停在一个"少一条"的状态上，而且刷新一次就对了（典型的偶发错）。
   *
   * ⚠️ 互斥用 `busyRef`（同步）而不是 `busy`（state，异步）—— 见上面 `busyRef` 的注释。
   * 用 state 判的话，"同一帧连点两次"这个最该被挡住的现场恰好挡不住。
   */
  const withBusy = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      if (busyRef.current) return null // 同步判定：同帧连点也只能放行一次
      busyRef.current = true
      setBusy(label)
      setMaterialError("")
      try {
        const res = await fn()
        await refresh()
        await refreshPacks()
        return res
      } catch (error) {
        setMaterialError(friendlyError(error, `${label}失败`))
        return null
      } finally {
        busyRef.current = false
        setBusy("")
      }
    },
    [refresh, refreshPacks],
  )

  /** 导入自己的一条素材（可多选）。返回成功导入的条数。 */
  const importFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files)
      if (!list.length) return 0
      const res = await withBusy("导入素材", async () => {
        let ok = 0
        const failed: string[] = []
        for (const f of list) {
          try {
            await soundboardImport(f)
            ok += 1
          } catch (error) {
            failed.push(`${f.name}：${friendlyError(error, "导入失败")}`)
          }
        }
        // 多选时**逐条汇报**：一条失败不该把整批说成失败，也不该静默跳过 ——
        // 用户丢了哪一条必须能看见（同"不静默跳过"的一贯判据）。
        if (failed.length) setMaterialError(failed.join("；"))
        return ok
      })
      return typeof res === "number" ? res : 0
    },
    [withBusy],
  )

  const removeSample = useCallback(
    async (id: string) => withBusy("删除素材", () => soundboardDelete(id)),
    [withBusy],
  )

  const installPackZip = useCallback(
    async (file: File) => withBusy("安装音效包", () => soundboardPackInstall(file)),
    [withBusy],
  )

  const uninstallPack = useCallback(
    async (id: string) => withBusy("卸载音效包", () => soundboardPackUninstall(id)),
    [withBusy],
  )

  const downloadPack = useCallback(
    async (id: string) => {
      const res = await withBusy("下载音效包", () => soundboardPackDownload(id))
      return res as { id: string; sha256_verified: boolean } | null
    },
    [withBusy],
  )

  /** 打开货架（懒加载；**成功**后重复打开不重复请求）。 */
  const openShelf = useCallback(async () => {
    if (shelfLoadedRef.current) return
    try {
      const r = await soundboardPacksAvailable()
      shelfLoadedRef.current = true // 与 setShelf 同一处置位：同步写，连点也挡得住
      setShelf({
        loaded: true,
        source: r.source,
        items: r.items ?? [],
        error: r.error || "",
        note: r.note || "",
      })
    } catch (error) {
      // ★ 失败**不置 `loaded`**：否则本次会话再点开货架只会看到上一次那句错误，
      // 唯一的出路是重进页面 —— 而这类失败的常见成因（后端刚起来、网络抖一下）
      // 本来重试就好。`loaded` 的语义是"清单拿到了，别白碰一次网络"，
      // 不是"试过了"。
      shelfLoadedRef.current = false
      setShelf({
        loaded: false,
        source: null,
        items: [],
        error: friendlyError(error, "拿不到音效包清单"),
        note: "",
      })
    }
  }, [])

  return {
    items,
    playing,
    errorMessage,
    play,
    stop,
    refresh,
    ready: enabled && backendUp,
    picks,
    premixing,
    premixError,
    premixNotes,
    togglePick,
    cyclePickMode,
    setPickAt,
    clearPicks,
    premix,
    packs,
    packSamples,
    shelf,
    busy,
    materialError,
    importFiles,
    removeSample,
    installPackZip,
    uninstallPack,
    downloadPack,
    openShelf,
  }
}

import { Link } from "react-router-dom"
import { ArrowRight, Boxes, Camera, Hammer, Library, Mic2, Radio, ScanSearch, ShoppingBag, Sparkles, Volume2 } from "lucide-react"
import { Card, PageShell } from "@/components/layout/PageShell"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"

/**
 * 能力索引 —— 给那些**没有导航入口**的能力一个统一的落点。
 *
 * 为什么需要它
 * ------------
 * 调研（`docs/调研-用户需求与界面精简-2026-09-25.md`）发现：侧栏很干净（7 项），
 * 但有 **10 个能力没有任何导航入口**，只能靠"碰巧滑到某个 tab"被发现。
 *
 * 按渐进式披露的理论，这些属于**第三层功能**（永不自动露出，靠用户自己发掘）——
 * 本身不算错。但理论同时给了前提：**得有一条文档化的路径能找到它们**。
 * 现在没有，所以"找不到"就变成了"没有"。
 *
 * 本页就是那条路径：不改变任何现有入口，只做一份**可搜索、可浏览**的清单。
 * 它刻意不放进主导航 —— 索引页是"我要找某个东西"时才来的地方，
 * 常驻侧栏只会把刚做完的降噪又还回去。
 *
 * 门控：每一项都过 `pluginVisible`。关掉的能力不该在这里显示成能点的链接
 * （否则点进去就是 404）—— 这是本仓的老坑，见 `docs/犯错指南.md` 速查表。
 */
export function CapabilityIndexRoute() {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null

  /**
   * 索引项。`plugin` 为 null 表示属核心能力（core 恒可见）。
   *
   * ⚠️ 加新能力时要同步这里，否则它又变成"没有入口"。
   * 门禁 `capabilityIndex.test.ts` 会比对插件清单与这张表，确保没有能力被漏掉。
   */
  const ITEMS: { to: string; name: string; desc: string; icon: typeof Mic2; plugin: string | null }[] = [
    { to: "/audition", name: "试音间", desc: "多个音色挂上麦克风逐个点着试，比单听样本准", icon: Sparkles, plugin: "sound.audition" },
    { to: "/tts?tab=single", name: "输字变声", desc: "打字它就说，还能在中间插音效（写 [爆炸]）", icon: Volume2, plugin: "sound.tts" },
    { to: "/tts?tab=wechat", name: "微信发送", desc: "合成好的语音一键发进微信 PC 版", icon: Camera, plugin: "hook.wechat" },
    { to: "/tts?tab=book", name: "有声书", desc: "整本书长文一次性合成，自动分段", icon: Library, plugin: "sound.audiobook" },
    { to: "/offlinevc", name: "离线变声", desc: "把录好的整段音频一次性变成目标音色", icon: Hammer, plugin: "sound.offline-vc" },
    { to: "/workshop", name: "音色工坊", desc: "丢素材进来，自动切片质检，练专属音色", icon: Mic2, plugin: "sound.workshop" },
    { to: "/live?tab=rvc", name: "实时变声", desc: "开麦即变，游戏/会议/语音直接用", icon: Radio, plugin: "sound.rvc-live" },
    { to: "/voices?tab=mine", name: "我的音色", desc: "所有音色档案的管理与导出", icon: Library, plugin: null },
    { to: "/voices?tab=market", name: "音色市场", desc: "开源音色下载安装，含断点续传与回滚", icon: ShoppingBag, plugin: null },
    { to: "/pet-market", name: "桌宠与人偶市场", desc: "桌面人偶本体、皮肤市场、自定义形象", icon: Sparkles, plugin: "pet.market" },
  ]

  const visible = ITEMS.filter((it) => it.plugin === null || pluginVisible(catalog, it.plugin))

  return (
    <PageShell>
      <div className="mb-6">
        <p className="font-mono text-xs uppercase tracking-widest text-primary">工具箱 · 索引</p>
        <h1 className="mt-2 flex items-center gap-2 text-2xl font-semibold tracking-tight text-foreground">
          <Boxes className="h-6 w-6 text-primary" />
          这个应用能做的事
        </h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
          一共 {visible.length} 项。侧栏只放了最常用的几条，剩下的全在这里 ——
          点任意一张卡直接进去，不用记它在哪个页面的哪个标签里。
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {visible.map(({ to, name, desc, icon: Icon }) => (
          <Link
            key={to + name}
            to={to}
            className="group flex items-start gap-3 rounded-2xl border border-border bg-card/85 p-4 shadow-sm transition hover:-translate-y-0.5 hover:border-primary hover:shadow-lg"
          >
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary/30 bg-primary/10 text-primary">
              <Icon className="h-5 w-5" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-card-foreground">{name}</span>
              <span className="mt-1 block text-xs leading-5 text-muted-foreground">{desc}</span>
            </span>
            <ArrowRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
          </Link>
        ))}
      </div>

      <Card tone="flat" className="mt-6 flex items-start gap-3 p-4">
        <ScanSearch className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <p className="text-xs leading-5 text-muted-foreground">
          少了哪一项？多半是那个能力被关掉了。到
          <Link to="/home" className="mx-1 font-medium text-primary hover:opacity-80">设置 → 能力</Link>
          可以看到完整的开关清单，并随时打开。
        </p>
      </Card>
    </PageShell>
  )
}

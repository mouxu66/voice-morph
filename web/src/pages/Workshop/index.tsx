import { useWorkshop } from "@/pages/Workshop/useWorkshop"
import { WorkshopPage } from "@/pages/Workshop/WorkshopPage"
import { DiscoverPage } from "@/pages/Discover/DiscoverPage"
import { useFt } from "@/pages/Ft/useFt"
import { FtPage } from "@/pages/Ft/FtPage"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"

/**
 * 音色工坊（合并页）：做音色的整条链路都在这里——
 * 制作（自录/导入建音色）、发掘（内录/文件投喂素材）、微调（继续训练精修）。
 *
 * 能力门控（2026-09-23 补）：本页路由属 `sound.workshop`，但页内「发掘」与「微调」
 * 是**另外两个可关能力**（`sound.mine` / `sound.ft`）。关掉 `sound.ft` 之后
 * `finetune` / `rvc_dataset_api` 不再挂载，而 `sound.workshop` 照旧开着 ——
 * 所以「整条路由会随插件消失」并不能替这两个 tab 兜底，留着就是一路 404。
 *
 * ⚠️ 这正是 `tools/audit_endpoint_ownership.py` 旧判据的盲区：它认为非核心路由
 * 一律不需要页内门控，漏掉了「宿主开着、被托管的 tab 关着」这个可达状态。
 */
export function WorkshopRoute() {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const discoverOn = pluginVisible(catalog, "sound.mine")
  const ftOn = pluginVisible(catalog, "sound.ft")

  // hook 必须无条件调用（React 规则）；enabled=false 时它们内部直接不打后端。
  const workshop = useWorkshop()
  const ft = useFt(ftOn)

  return (
    <MergedPageTabs
      tabs={[
        { key: "make", label: "制作", content: <WorkshopPage {...workshop} /> },
        ...(discoverOn ? [{ key: "discover", label: "发掘", content: <DiscoverPage /> }] : []),
        ...(ftOn ? [{ key: "ft", label: "微调", content: <FtPage {...ft} /> }] : []),
      ]}
    />
  )
}
import { useWorkshop } from "@/pages/Workshop/useWorkshop"
import { WorkshopPage } from "@/pages/Workshop/WorkshopPage"
import { DiscoverPage } from "@/pages/Discover/DiscoverPage"
import { useFt } from "@/pages/Ft/useFt"
import { FtPage } from "@/pages/Ft/FtPage"
import { MergedPageTabs } from "@/components/MergedPageTabs"

/**
 * 音色工坊（合并页）：做音色的整条链路都在这里——
 * 制作（自录/导入建音色）、发掘（内录/文件投喂素材）、微调（继续训练精修）。
 */
export function WorkshopRoute() {
  return (
    <MergedPageTabs
      tabs={[
        { key: "make", label: "制作", content: <WorkshopPage {...useWorkshop()} /> },
        { key: "discover", label: "发掘", content: <DiscoverPage /> },
        { key: "ft", label: "微调", content: <FtPage {...useFt()} /> },
      ]}
    />
  )
}
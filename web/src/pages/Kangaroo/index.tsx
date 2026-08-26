import { useKangaroo } from "@/pages/Kangaroo/useKangaroo"
import { KangarooPage } from "@/pages/Kangaroo/KangarooPage"

export function KangarooRoute() {
  return <KangarooPage {...useKangaroo()} />
}
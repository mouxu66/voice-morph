import { useWorkshop } from "@/pages/Workshop/useWorkshop"
import { WorkshopPage } from "@/pages/Workshop/WorkshopPage"

export function WorkshopRoute() {
  return <WorkshopPage {...useWorkshop()} />
}

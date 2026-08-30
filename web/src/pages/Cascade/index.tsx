import { useCascade } from "@/pages/Cascade/useCascade"
import { CascadePage } from "@/pages/Cascade/CascadePage"

export function CascadeRoute() {
  return <CascadePage {...useCascade()} />
}

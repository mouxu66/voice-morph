import { EffectsPage } from "./EffectsPage"
import { useEffects } from "./useEffects"

export function EffectsRoute() {
  return <EffectsPage {...useEffects()} />
}

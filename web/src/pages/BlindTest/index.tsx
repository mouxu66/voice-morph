import { BlindTestPage } from "./BlindTestPage"
import { useBlindTest } from "./useBlindTest"

export function BlindTestRoute() {
  return <BlindTestPage {...useBlindTest()} />
}

import { useLive } from "@/pages/Live/useLive"
import { LivePage } from "@/pages/Live/LivePage"

/**
 * RVC 实时变声（独立页）：逐帧套音色，保留源说话习惯，适合真人音色/低延迟。
 * 要卡通/角色音色走「千问变声」（/qwen，ASR→TTS 换嗓）。
 */
export function LiveRoute() {
  return <LivePage {...useLive()} />
}
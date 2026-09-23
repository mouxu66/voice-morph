import { useCallback, useEffect, useRef, useState } from "react"
import {
  soundboardCatalog,
  soundboardPlay,
  soundboardStop,
  soundboardWarm,
  type SoundboardItem,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

/**
 * 特效声板：点一下格子，把一条短音效**实时**播进虚拟声卡。
 *
 * 为什么它能和"正在播的 TTS / 正在跑的实时变声"同时出声：Windows 音频默认共享模式，
 * 多路程序写同一设备由系统混音器自动混合 —— 微信从 CABLE 采集端录到的就是混好的结果。
 * 所以这个 hook **完全不碰微信发送链路**（`_send_lock` 也不进）：发送期间点格子照样出声。
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

  const refresh = useCallback(async () => {
    try {
      const r = await soundboardCatalog()
      setItems(r.items ?? [])
    } catch {
      /* 服务离线时静默，下次操作再试 */
    }
  }, [])

  useEffect(() => {
    if (!enabled || !backendUp) return
    let alive = true
    void (async () => {
      // 预热：把常驻播放器与素材读进内存。不预热的话第一次点击要付 ~2-3s 冷导入，
      // 用户听到的是"点了没反应"。
      try {
        await soundboardWarm()
      } catch {
        /* 设备/venv 缺失时预热失败很正常；真点播放会给出可读报错 */
      }
      if (alive) await refresh()
    })()
    return () => {
      alive = false
    }
  }, [enabled, backendUp, refresh])

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

  return { items, playing, errorMessage, play, stop, refresh, ready: enabled && backendUp }
}

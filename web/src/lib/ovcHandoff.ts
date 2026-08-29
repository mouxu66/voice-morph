// 跨页交接：把 TTS 合成结果交给离线变声页作为待转换音频。
// File/Blob 无法序列化进 sessionStorage，用内存单例直传（跳转立即生效，无需持久化）。
let pending: File[] = []

export function setOvcHandoff(files: File[]) {
  pending = files
}

export function takeOvcHandoff(): File[] {
  const files = pending
  pending = []
  return files
}

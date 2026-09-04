// 跨源资源下载：Electron/浏览器对异源 URL 会忽略 <a download> 属性导致点了没反应。
// 统一改为 fetch → Blob → objectURL（同源 blob 才尊重 download），再触发保存。
export async function downloadUrl(url: string, filename: string): Promise<void> {
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`下载失败：HTTP ${res.status}`)
  }
  const blob = await res.blob()
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = objUrl
  a.download = filename
  a.style.display = "none"
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(objUrl)
}
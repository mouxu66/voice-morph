/** 把后端/网络原始错误转成用户能看懂的话 */
export function friendlyError(error: unknown, fallback = "操作失败，请稍后重试"): string {
  const raw =
    error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const norm = raw.toLowerCase();

  if (/failed to fetch|network ?error|networkrequestfailed|econnrefused|fetch failed/i.test(norm)) {
    return "无法连接本地服务，请确认后端已启动。";
  }
  if (/加载|载入|权重|模型|weight|checkpoint|模型加载/i.test(raw)) {
    return "模型还未就绪，请稍候重试（首次使用需加载模型，可能要等一会儿）。";
  }
  if (/不存在|not found|404/i.test(norm)) {
    return raw.includes("音色") ? "找不到对应的音色档案，请先在音色库创建。" : "请求的资源不存在，请刷新页面后重试。";
  }
  if (/已存在|409|conflict/i.test(norm)) {
    return "同名文件已存在，请换个名字。";
  }
  if (/超时|timeout/i.test(norm)) {
    return "处理超时了，请重试或检查素材是否过大。";
  }
  if (/没有|为空|empty|未提供/i.test(norm)) {
    return raw || "内容为空，请检查后再试。";
  }
  if (raw) return raw;
  return fallback;
}

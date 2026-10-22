import { jsonFetch, BASE } from "../http";

// ---- 音频溯源水印（sound.watermark） ----
//
// 基于 Meta AudioSeal（MIT）：给音频嵌一个隐形的 16 位溯源码（4 位 hex），
// 听感不变；也能检测一段音频带没带我们的码。★ 口径红线（与后端一致）：
// 只说「可检出 / 可溯源」，不说「防伪 / 无法去除」—— 隐形水印是概率性取证信号。

/** 单个权重下载任务（沿用市场下载器的进度结构；total/done 为字节） */
export type WatermarkDlTask = {
  name?: string;
  filename?: string;
  status?: string; // downloading | done | failed | cancelled | idle
  total?: number | null;
  done?: number;
  error?: string;
  [k: string]: unknown;
};

export type WatermarkStatus = {
  models_dir: string;
  installed: boolean;
  size_hint_mb: number;
  download: { generator: WatermarkDlTask; detector: WatermarkDlTask };
  auto: { enabled: boolean; code: string };
};

export type WatermarkEmbedResult = {
  ok: boolean;
  code: string;
  sr: number;
  duration_s: number;
  /** 会话产物裸名（outputs/ 下）；退出即删 */
  wav: string;
  url: string;
};

export type WatermarkDetectResult = {
  ok: boolean;
  watermarked: boolean;
  prob: number;
  code: string;
  threshold: number;
};

export async function watermarkStatus(): Promise<WatermarkStatus> {
  return jsonFetch<WatermarkStatus>("/watermark/status");
}

/** 下载 AudioSeal 权重（约 90MB，两文件）。幂等：已装好回 done；在下回 downloading，前端轮询 status。 */
export async function watermarkDownload(): Promise<{
  status: string;
  installed?: boolean;
  tasks?: string[];
}> {
  return jsonFetch("/watermark/download", { method: "POST" });
}

/**
 * 植入水印 → 会话产物。file=本地文件；或 name=会话/产物裸名（如刚合成的语音）。
 * code 留空则后端随机生成（返回值里带回，用户要记下它）。
 */
export async function watermarkEmbed(
  input: { file: File } | { name: string },
  code = "",
  alpha = 1.0
): Promise<WatermarkEmbedResult> {
  const form = new FormData();
  if ("file" in input) form.append("file", input.file);
  else form.append("name", input.name);
  form.append("code", code);
  form.append("alpha", String(alpha));
  // ⚠️ 必须内联 `fetch(BASE + 字面量)`：tools/audit_endpoint_ownership.py 靠 AST
  //   从调用点抽 URL 字面量判断端点归属（同 uploadPreserveReference 的注释）。
  const res = await fetch(BASE + "/watermark/embed", { method: "POST", body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<WatermarkEmbedResult>;
}

/** 检测一段音频带没带我们的溯源码。 */
export async function watermarkDetect(
  input: { file: File } | { name: string }
): Promise<WatermarkDetectResult> {
  const form = new FormData();
  if ("file" in input) form.append("file", input.file);
  else form.append("name", input.name);
  const res = await fetch(BASE + "/watermark/detect", { method: "POST", body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<WatermarkDetectResult>;
}

export async function watermarkAutoGet(): Promise<{ enabled: boolean; code: string }> {
  return jsonFetch("/watermark/auto");
}

/** 「合成自动加标识」开关：开着时所有合成产物（含微信语音）自动嵌入该码。默认关。 */
export async function watermarkAutoSet(
  enabled: boolean,
  code = ""
): Promise<{ ok: boolean; enabled: boolean; code: string }> {
  return jsonFetch("/watermark/auto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, code }),
  });
}

import { jsonFetch, BASE } from "../http";

// ---- 讲稿录课（PPT 备注页 → 逐页旁白，sound.course） ----

export type CoursePageIn = {
  page: number;
  text: string;
  /**
   * 这页讲稿从哪来：`remark`（PPT 备注页）/ `body`（备注为空时自动取该页幻灯片正文）。
   * 贴文本的批次没有这个字段。前端据此标一句「这页用的是正文草稿」，老师可回改。
   */
  source?: "remark" | "body";
};

export type CoursePageState = {
  page: number;
  text: string;
  url: string;
  duration_s: number;
  failed: boolean;
  error: string;
};

export type CourseStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  voice_id: string;
  /**
   * 风格参考 ICL 的音色 id（空=不启用，走纯声纹克隆）。
   * ⚠️ 非空时是**整段克隆**：声音与语气都取自参考音，`voice_id` 那份音色不参与发声。
   */
  style_ref_voice: string;
  done: number;
  total: number;
  percent: number;
  current_page: number;
  current_text: string;
  job_dir: string;
  gap_ms: number;
  pages: CoursePageState[];
  error: string;
};

export type CourseParseResult = {
  source: "pptx" | "text";
  total: number;
  skipped: number;
  pages: CoursePageIn[];
  /**
   * 上传的 .pptx 在会话目录里的暂存裸名（贴文本时为空串）。
   *
   * 合成时要原样回传：成片的画面就是从这份**原课件**导出来的
   * （后端 parse → 暂存 → run 复制进产物目录 → 导出时读它）。
   */
  source_pptx: string;
};

/** 解析讲稿来源：.pptx 附件优先，否则按纯文本（`---` 分页）。二选一。 */
export async function parseCourse(
  file: File | undefined,
  text: string
): Promise<CourseParseResult> {
  const form = new FormData();
  if (file) form.append("file", file);
  form.append("text", text);
  const res = await fetch(BASE + "/course/parse", { method: "POST", body: form });
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
  return res.json();
}

export async function runCourse(
  pages: CoursePageIn[],
  voiceId: string,
  gapMs = 350,
  styleRefVoice = "",
  sourcePptx = ""
): Promise<{ ok: boolean; total: number; voice_id: string; job_dir: string }> {
  return jsonFetch("/course/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pages,
      voice_id: voiceId,
      gap_ms: gapMs,
      style_ref_voice: styleRefVoice,
      source_pptx: sourcePptx,
    }),
  });
}

export async function getCourseStatus(): Promise<CourseStatus> {
  return jsonFetch("/course/status");
}

export async function cancelCourse(): Promise<{ ok: boolean; message?: string }> {
  return jsonFetch("/course/cancel", { method: "POST" });
}

export async function retryCourse(): Promise<{
  ok: boolean;
  total: number;
  voice_id: string;
  job_dir: string;
}> {
  return jsonFetch("/course/retry", { method: "POST" });
}

/**
 * 导出成片（课件画面 + 逐页旁白 → mp4）。
 *
 * ⚠️ 画面取自学**原课件**（合成时复制进产物目录的那份），所以贴文本的批次会 400
 * —— 那句 detail 直接给用户看（"上传 .pptx 重新合成一次，成片才有画面"）。
 */
export type CourseVideoStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string; // slides | mux | ""
  percent: number;
  message: string;
  job_dir: string;
  out_url: string;
  out_name: string;
  duration_s: number;
  pages: number; // 成片用了多少页画面
  slides: number; // 课件一共导了多少页
  fps: number;
  gap_ms: number;
  error: string;
};

export type CourseVideoOptions = {
  fps?: number;
  gapMs?: number;
  forceSlides?: boolean;
  /** 指定批次（留空 = 当前这批）；pin 住它是为了"导出的一定是眼前这一批" */
  jobDir?: string;
};

export async function runCourseVideo(
  opts: CourseVideoOptions = {}
): Promise<{ ok: boolean; job_dir: string; pages: number; fps: number; gap_ms: number }> {
  return jsonFetch("/course/video", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fps: opts.fps ?? 30,
      gap_ms: opts.gapMs, // undefined 会被 JSON.stringify 丢掉 → 后端用这一批的 gap
      force_slides: opts.forceSlides ?? false,
      job_dir: opts.jobDir ?? "",
    }),
  });
}

export async function getCourseVideoStatus(): Promise<CourseVideoStatus> {
  return jsonFetch("/course/video/status");
}

export async function cancelCourseVideo(): Promise<{ ok: boolean; message?: string }> {
  return jsonFetch("/course/video/cancel", { method: "POST" });
}

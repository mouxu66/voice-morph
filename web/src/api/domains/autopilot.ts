import { jsonFetch } from "../http";

// ---- 微信全局托管（hook.wechat 的 /autopilot 页） ----
//
// 这是唯一会「替用户给别人发消息」的能力，所以接口按最小面暴露：
// 状态 / 会话列表 / 配置 / 启动 / 暂停 / 恢复 / 停止 / 测试发送。
// 启动必须带 `consent: true`（后端会 403），配置只能在停止状态改（否则 409）。

export type AutopilotConfig = {
  /** 允许托管的会话标题（用户从会话列表里挑） */
  whitelist: string[];
  /** 其中哪些是群聊：群聊里必须 @你 才会回 */
  groups: string[];
  /** 别人在群里 @ 你时用的昵称（可留空，留空就只能靠「只有 1:1 会回」） */
  me_names: string[];
  /** 只回「提问」或「@你」的消息（用户拍板的口径，默认开） */
  question_only: boolean;
  /** 免打扰会话跳过（默认开） */
  skip_muted: boolean;
  poll_s: number;
  min_gap_s: number;
  max_per_chat_per_hour: number;
  max_per_run: number;
  /** [起始小时, 结束小时)，跨零点如 [23, 7]；两值相同 = 不启用 */
  quiet_hours: [number, number];
  /** 读上下文时向上翻几页历史；0 = 不翻（翻页会抢前台） */
  history_pages: number;
  /** 模型回复的最大字数，超过就丢弃不发 */
  reply_max_chars: number;
};

export type AutopilotLogEntry = {
  ts: number;
  time: string;
  /** sent | skip | error | test */
  action: string;
  chat: string;
  reply?: string;
  why?: string;
  method?: string;
  reason?: string;
};

export type AutopilotSession = {
  chat: string;
  unread: number;
  preview: string;
  time: string;
  muted: boolean;
  pinned: boolean;
  whitelisted?: boolean;
  group?: boolean;
};

export type AutopilotStatus = "off" | "running" | "paused" | "stopping";

export type AutopilotState = {
  ok: boolean;
  server_time: number;
  status: AutopilotStatus;
  stats: { ticks: number; sent: number; skipped: number; errors: number };
  run_sent: number;
  last_error: string;
  last_tick_at: number;
  config: AutopilotConfig;
  recent: AutopilotLogEntry[];
  wechat: { enabled?: boolean; ready?: boolean; reason?: string; [k: string]: unknown };
  advisor: { ok?: boolean; reason?: string; [k: string]: unknown };
};

export async function autopilotState(): Promise<AutopilotState> {
  return jsonFetch<AutopilotState>("/autopilot/state");
}

export async function autopilotSessions(): Promise<{
  ok: boolean;
  sessions: AutopilotSession[];
  selected?: string;
  error?: string;
}> {
  return jsonFetch("/autopilot/sessions");
}

export async function autopilotSaveConfig(
  config: AutopilotConfig,
): Promise<{ ok: boolean; config: AutopilotConfig }> {
  return jsonFetch("/autopilot/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

// 每个函数把 URL 写在**自己的函数体里**，不经过 `post(path)` 辅助：
// `tools/audit_endpoint_ownership.py` 按函数体抽 URL，经变量传参的写法
// 会让这五个端点变成「抽不到 URL」的假未定位（门禁 red，2026-10-24 踩过）。

export async function autopilotStart(consent: boolean) {
  return jsonFetch<{ ok: boolean; status: AutopilotStatus }>("/autopilot/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ consent }),
  });
}

export async function autopilotPause() {
  return jsonFetch<{ ok: boolean; status: AutopilotStatus }>("/autopilot/pause", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

export async function autopilotResume() {
  return jsonFetch<{ ok: boolean; status: AutopilotStatus }>("/autopilot/resume", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

export async function autopilotStop() {
  return jsonFetch<{ ok: boolean; status: AutopilotStatus }>("/autopilot/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

/** 只在「文件传输助手」发一条，验证整条发送链路。 */
export async function autopilotTestSend(text: string) {
  return jsonFetch<{ ok: boolean; chat: string; method: string }>("/autopilot/test-send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

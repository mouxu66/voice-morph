import { jsonFetch } from "../http";

// ---- 微信语音发送（PC 微信 4.1.9+ 虚拟声卡自动灌入） ----

export type WechatLastTts = {
  ok: boolean;
  wav?: string;
  duration_s?: number;
  url?: string;
  error?: string;
};

export type WechatHistoryItem = {
  wav: string;
  duration_s: number;
  ts: number;
  outcome: string;
  /**
   * 这条现在还能不能重发/试听。
   *
   * 产物默认是**会话级**的（退出即删，见 `session_out` 模块注释里用户的原话），
   * 所以历史里多数条目在退出应用后文件已不在磁盘上。后端每次请求现算这个字段
   * （可用性是动态的）。**缺省按「不可用」处理**：宁可少给一个按钮，
   * 也不要摆一个按下去必然报错的「重发」。
   */
  available?: boolean;
  /** 长文分段发送：这一批总共几条（单条不发这个字段）。 */
  total_chunks?: number;
  /** 发送时的告警（如静音检测），落库供事后查线索。 */
  warning?: string;
};

export type WechatSendResult = {
  ok: boolean;
  wav?: string;
  duration_s?: number;
  outcome?: string;
  error?: string;
  steps?: string[];
  hint?: string;
  hint2?: string;
  warn?: string;
};

/** 最近一次 TTS 合成产物（发送前预览用） */
export async function getWechatLastTts(): Promise<WechatLastTts> {
  return jsonFetch<WechatLastTts>("/wechat/send_voice/last");
}

/** 最近发送的微信语音列表 */
export async function getWechatHistory(): Promise<{ ok: boolean; items: WechatHistoryItem[] }> {
  return jsonFetch<{ ok: boolean; items: WechatHistoryItem[] }>("/wechat/history");
}

/**
 * 全自动发送：切录音设备到 CABLE Output → 前台化微信模拟按住 Alt → 播放 → 松开发送。
 * 执行期间（约 wav 时长 + 3s）不要动键鼠。wav=outputs/ 下文件名，缺省=最近 TTS。
 */
export async function wechatSendVoice(wav?: string): Promise<WechatSendResult> {
  return jsonFetch("/wechat/send_voice", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wav: wav || null }),
  });
}

/** 半自动：播放到虚拟声卡（带静音头），用户自己在微信按住 Alt 录，录完松开发送 */
export async function wechatPlayToCable(wav?: string, leadS?: number): Promise<WechatSendResult> {
  return jsonFetch("/wechat/play_to_cable", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wav: wav || null, lead_s: leadS ?? null }),
  });
}

/** 手动实时变声模式：切微信录音到 CABLE Output + 确保实时变声运行，用户按住 Alt 说话 */
export async function wechatManualSend(): Promise<WechatSendResult> {
  return jsonFetch("/wechat/manual_send", { method: "POST" });
}

import { create } from "zustand";
import type { ClipItem, HealthInfo, VoiceInfo } from "../types";

interface AppState {
  // 服务状态
  health: HealthInfo | null;
  backendUp: boolean;
  /** 应用启动时刻（用于区分"服务启动中"与"服务离线"） */
  appStartedAt: number;
  /** 最近一次后端成功响应时刻 */
  lastOnlineAt: number | null;
  setHealth: (h: HealthInfo | null) => void;

  // 当前页面
  page: "workshop" | "voices" | "tts";
  setPage: (p: AppState["page"]) => void;

  // 音色库
  voices: VoiceInfo[];
  selectedVoiceId: string | null;
  setVoices: (v: VoiceInfo[]) => void;
  selectVoice: (id: string) => void;

  // 片段选择（勾选状态）
  clips: ClipItem[];
  selectedClips: Set<string>;
  setClips: (c: ClipItem[]) => void;
  toggleClip: (name: string) => void;
  clearSelectedClips: () => void;

  // 流水线状态
  pipelineRunning: boolean;
  pipelineMsg: string;
  setPipelineRunning: (r: boolean, msg?: string) => void;

  // 全局音频播放（同一时刻只允许一条在播，切换自动停旧的）
  playingSrc: string | null;
  setPlayingSrc: (updater: string | null | ((prev: string | null) => string | null)) => void;
}

export const useAppStore = create<AppState>((set) => ({
  health: null,
  backendUp: false,
  appStartedAt: Date.now(),
  lastOnlineAt: null,
  setHealth: (h) =>
    set(h ? { health: h, backendUp: true, lastOnlineAt: Date.now() } : { health: null, backendUp: false }),

  page: "workshop",
  setPage: (p) => set({ page: p }),

  voices: [],
  selectedVoiceId: null,
  setVoices: (v) => set({ voices: v }),
  selectVoice: (id) => set({ selectedVoiceId: id }),

  clips: [],
  selectedClips: new Set(),
  setClips: (c) => set({ clips: c }),
  toggleClip: (name) =>
    set((s) => {
      const next = new Set(s.selectedClips);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return { selectedClips: next };
    }),
  clearSelectedClips: () => set({ selectedClips: new Set() }),

  pipelineRunning: false,
  pipelineMsg: "",
  setPipelineRunning: (r, msg = "") => set({ pipelineRunning: r, pipelineMsg: msg }),

  playingSrc: null,
  setPlayingSrc: (updater) =>
    set((s) => ({
      playingSrc: typeof updater === "function" ? (updater as (p: string | null) => string | null)(s.playingSrc) : updater,
    })),
}));

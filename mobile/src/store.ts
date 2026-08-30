// 全局状态：PC 后端地址（持久化到 AsyncStorage）
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import AsyncStorage from "@react-native-async-storage/async-storage";

interface AppState {
  /** PC 后端地址，如 http://192.168.1.10:8000 */
  host: string;
  /** API Key（可选）：PC 端以 VM_API_TOKEN 启动后端时填写，本机回环请求不受影响 */
  apiKey: string;
  /** 最近一次级联变声使用的音色（跨页共享） */
  lastVoiceId: string | null;
  setHost: (host: string) => void;
  setApiKey: (apiKey: string) => void;
  setLastVoiceId: (id: string | null) => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      host: "http://192.168.1.10:8000",
      apiKey: "",
      lastVoiceId: null,
      setHost: (host) => set({ host }),
      setApiKey: (apiKey) => set({ apiKey }),
      setLastVoiceId: (lastVoiceId) => set({ lastVoiceId }),
    }),
    { name: "vm-mobile-store", storage: createJSONStorage(() => AsyncStorage) }
  )
);

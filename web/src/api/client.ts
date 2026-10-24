/** 前端 API 客户端入口（兼容 barrel）。
 *
 *  2026-10-22 起实现按域拆到 `api/domains/*.ts`，本文件只保留**原来的导出名**，
 *  所有 `import ... from "@/api/client"` 的调用方无需改动。
 *  共享设施（BASE / jsonFetch / mediaUrl）在 `api/http.ts`。
 */
export type { WarmupStatus, LanInfo } from "../types";
export { BACKEND_ORIGIN, backendPrefix, BASE, mediaUrl } from "./http";
export * from "./domains/system";
export * from "./domains/pipeline";
export * from "./domains/tts";
export * from "./domains/course";
export * from "./domains/vc";
export * from "./domains/cover";
export * from "./domains/voice";
export * from "./domains/rvc";
export * from "./domains/live";
export * from "./domains/workshop";
export * from "./domains/wechat";
export * from "./domains/sfx";
export * from "./domains/market";
export * from "./domains/library";
export * from "./domains/pet";
export * from "./domains/audition";
export * from "./domains/preserve";
export * from "./domains/watermark";
export * from "./domains/autopilot";
export * from "./domains/blindtest";

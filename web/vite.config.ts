import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// 开发模式：前端 5173 端口，代理 /api 到 FastAPI 后端 8000
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // 后端已统一挂载 /api 前缀，不再 rewrite
      },
    },
  },
  base: "./", // electron 打包后 file:// 协议下资源路径正确
});

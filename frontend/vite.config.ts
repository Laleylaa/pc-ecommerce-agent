import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    host: process.env.PC_FRONTEND_HOST || "127.0.0.1",
    port: Number(process.env.PC_FRONTEND_PORT || 5173),
    strictPort: true,
    proxy: {
      "/api": {
        target: loadEnv(mode, process.cwd(), "VITE_").VITE_DEV_PROXY_TARGET
          || process.env.PC_DEV_PROXY_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true
      }
    }
  },
  preview: { host: "127.0.0.1" }
}));

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: loadEnv(mode, process.cwd(), "VITE_").VITE_DEV_PROXY_TARGET
          || "http://127.0.0.1:8000",
        changeOrigin: true
      }
    }
  }
}));

import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(path.dirname(fileURLToPath(import.meta.url)), "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
      "/auth": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
      "/static": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
      "/landing": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
      "/favicon.svg": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
      "/vendor": {
        target: "http://localhost:9100",
        changeOrigin: true,
      },
    },
  },
});
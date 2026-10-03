import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // shadcn/ui 组件统一用 @/ 前缀引用
      "@": path.resolve(__dirname, "./src"),
    },
  },
})

import path from "node:path"
import { defineConfig } from "vitest/config"

// Minimal standalone config (not merged with vite.config.ts) so tests don't need the
// tailwind/react plugins or the dev-server proxy -- just the "@" alias used throughout
// src/.
export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  test: {
    environment: "node",
  },
})

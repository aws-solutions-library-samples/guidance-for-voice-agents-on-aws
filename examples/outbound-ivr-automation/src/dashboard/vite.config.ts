import { createRequire } from "node:module"
import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const pkg = createRequire(import.meta.url)("./package.json") as { version: string }

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  // The sidebar footer shows this (src/components/AppShell.tsx), baked in at build
  // time. It is this example's own version from package.json: the repository's git
  // tags belong to the main guidance, not to this example.
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  build: {
    outDir: "dist",
  },
  // `npm run dev` just serves the SPA -- there is no local backend to proxy to. For
  // anything beyond the login screen to work, drop a `public/config.json` matching
  // `config.ts`'s `RuntimeConfig` shape, pointed at a real deployed API Gateway +
  // Cognito + S3 stack (CDK writes that same file into the built SPA at deploy time).
})

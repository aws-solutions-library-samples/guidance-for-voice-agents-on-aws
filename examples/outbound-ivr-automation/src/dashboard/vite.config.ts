import { execSync } from "node:child_process"
import { createRequire } from "node:module"
import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const pkg = createRequire(import.meta.url)("./package.json") as { version: string }

/** The release this build came from: `git describe` against the repo's v* tags
 * ("0.7.0" on the tag, "0.7.0-2-g3b725ea" two commits after it), or package.json's
 * version when git is not available. */
function appVersion(): string {
  try {
    return execSync("git describe --tags --match 'v*'", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim()
      .replace(/^v/, "")
  } catch {
    return pkg.version
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  // The sidebar footer shows this (src/components/AppShell.tsx) — baked in at build
  // time, not fetched, since it names the SPA build itself rather than a deployment.
  define: {
    __APP_VERSION__: JSON.stringify(appVersion()),
  },
  build: {
    outDir: "dist",
  },
  // `npm run dev` just serves the SPA -- there is no local backend to proxy to. For
  // anything beyond the login screen to work, drop a `public/config.json` matching
  // `config.ts`'s `RuntimeConfig` shape, pointed at a real deployed API Gateway +
  // Cognito + S3 stack (CDK writes that same file into the built SPA at deploy time).
})

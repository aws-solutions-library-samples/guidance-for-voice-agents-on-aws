/// <reference types="vite/client" />

/** Injected by vite.config.ts's `define` at build time: `git describe` of the v* tags, or package.json's version. */
declare const __APP_VERSION__: string

interface ImportMetaEnv {
  readonly VITE_BACKEND?: "local" | "aws"
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/**
 * amazon-connect-streams has no module exports -- loading it only sets the ambient
 * global `connect` as a side effect (see ConnectCcpCard.tsx, which pulls in the real
 * ambient declarations for that with `/// <reference types="amazon-connect-streams" />`).
 * The package's own types file is a script, not a module (no top-level import/export),
 * so without this empty shim, `import("amazon-connect-streams")` fails to type-check at
 * all (TS2306) -- this just lets TypeScript treat the specifier as a module.
 */
declare module "amazon-connect-streams" {}

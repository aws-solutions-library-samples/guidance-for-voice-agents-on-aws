// Runtime config for `aws` mode, fetched once at load from `/config.json` — a file CDK
// writes at deploy time (not at build time), so the same SPA build works against any
// environment without a rebuild. Unused in `local` mode.
export type RuntimeConfig = {
  apiUrl: string
  cognitoDomain: string
  cognitoClientId: string
  cognitoRegion: string
  region: string
  // Optional: older deployments' config.json predates this -- the dashboard treats a
  // missing field the same as "off"/unset rather than requiring a redeploy to read it.
  // Empty string (or missing) means there's no Connect CCP to embed.
  ccpUrl?: string
  connectRegion?: string
}

let cached: Promise<RuntimeConfig> | null = null

export function loadConfig(): Promise<RuntimeConfig> {
  if (!cached) {
    cached = fetch("/config.json", { cache: "no-store" }).then((res) => {
      if (!res.ok) throw new Error(`could not load /config.json (${res.status})`)
      return res.json() as Promise<RuntimeConfig>
    })
  }
  return cached
}

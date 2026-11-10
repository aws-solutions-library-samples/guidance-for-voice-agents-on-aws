import { useEffect, useState } from "react"
import { loadConfig, type RuntimeConfig } from "@/lib/config"

/**
 * The deploy-time `/config.json` (lib/config.ts), as React state: `null` while it's
 * still loading. Used by the Monitor button and the Connect CCP card, both of which
 * only show up when the matching config field is set -- see pages/LiveCall.tsx.
 */
export function useRuntimeConfig(): RuntimeConfig | null {
  const [config, setConfig] = useState<RuntimeConfig | null>(null)

  useEffect(() => {
    let cancelled = false
    loadConfig()
      .then((c) => {
        if (!cancelled) setConfig(c)
      })
      .catch(() => {
        if (!cancelled) setConfig(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return config
}

// One Connect CCP for the whole app session: amazon-connect-streams' `initCCP` has no
// "destroy" and may only run once per container for the page's lifetime, and the hidden
// iframe it creates only has softphone media and fires contact events while it's
// mounted. Mounting it inside a page (ConnectCcpCard used to) meant navigating away
// unmounted the iframe -- Streams had already been initialized once, so the hook's own
// re-init guard then skipped bringing it back, leaving the card showing a stale
// "Available" state with no softphone underneath it at all: calls stopped ringing. This
// provider owns the hidden container and the hook above the router instead (see
// AppShell.tsx), so the iframe survives every page's mount/unmount.
import { createContext, useContext, type ReactNode } from "react"
import { useRuntimeConfig } from "@/lib/useRuntimeConfig"
import { useConnectSoftphone } from "@/lib/useConnectSoftphone"

type ConnectSoftphone = ReturnType<typeof useConnectSoftphone> & {
  /** True once config.json names a CCP to embed -- false before it loads. */
  enabled: boolean
}

const ConnectSoftphoneContext = createContext<ConnectSoftphone | null>(null)

export function ConnectSoftphoneProvider({ children }: { children: ReactNode }) {
  // null until config.json resolves -- useConnectSoftphone treats a null/undefined
  // ccpUrl as "no CCP to talk to yet" and skips Streams entirely (see its own doc
  // comment).
  const config = useRuntimeConfig()
  const ccpUrl = config?.ccpUrl
  // No region fallback: useConnectSoftphone already no-ops whenever ccpUrl is unset
  // (see its own doc comment), and whenever it is set, config.json always carries
  // the real deployed Region alongside it -- a hardcoded default here would risk
  // silently pointing a non-default-Region deployment's CCP at the wrong Region.
  const softphone = useConnectSoftphone({
    ccpUrl,
    connectRegion: config?.connectRegion ?? "",
  })
  const { containerRef } = softphone

  return (
    <ConnectSoftphoneContext.Provider value={{ ...softphone, enabled: !!ccpUrl }}>
      {children}
      {/*
       * The CCP iframe stays mounted and initialized for the whole app session (sign-in
       * and softphone audio depend on that) but is kept out of sight: zero size,
       * visually hidden and aria-hidden rather than `display:none` or ever unmounted --
       * see the Amazon Connect admin guide's "Embed a custom Contact Control Panel"
       * pattern referenced in useConnectSoftphone.ts.
       */}
      <div
        ref={containerRef}
        aria-hidden="true"
        className="pointer-events-none fixed h-px w-px overflow-hidden opacity-0"
      />
    </ConnectSoftphoneContext.Provider>
  )
}

export function useConnectSoftphoneContext(): ConnectSoftphone {
  const ctx = useContext(ConnectSoftphoneContext)
  if (!ctx) throw new Error("useConnectSoftphoneContext needs a ConnectSoftphoneProvider above it")
  return ctx
}

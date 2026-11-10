/// <reference types="amazon-connect-streams" />
import { useCallback, useEffect, useReducer, useRef } from "react"

/**
 * The front desk controls on top of the embedded Amazon Connect CCP (see
 * ConnectCcpCard.tsx) -- the CCP iframe stays in the DOM (sign-in and the softphone
 * audio only work while it's mounted and initialized), but it's visually hidden and
 * this hook drives a dashboard-themed card instead.
 *
 * The package only exports side effects (it sets the ambient global `connect`), so this
 * module lazy-imports it -- a plain `npm test` run (jsdom, no real Connect instance to
 * talk to) must never load it; tests inject a fake `connect` global instead (see
 * useConnectSoftphone.test.ts).
 */

// A single shared dynamic-import promise for the two effects below, rather than each
// calling `import("amazon-connect-streams")` on its own: besides being pointless (the
// module only ever needs to load once anyway), two independent dynamic imports of the
// same mocked specifier racing from different effects within one render is enough to
// confuse vi.mock's interception in tests -- see useConnectSoftphone.test.tsx -- and a
// shared promise sidesteps that regardless of its cause.
let streamsPromise: Promise<unknown> | null = null
function loadStreams(): Promise<unknown> {
  if (!streamsPromise) streamsPromise = import("amazon-connect-streams")
  return streamsPromise
}

export type ConnectPhase = "signing-in" | "ready" | "incoming" | "connected"

export type SelectableAgentState = {
  name: string
  /** True for the "Available"-type state agents are routed contacts in (AgentStateType.ROUTABLE). */
  routable: boolean
}

export type ConnectSoftphoneState = {
  phase: ConnectPhase
  /** The agent's current state name (e.g. "Available", "Offline"), once signed in. */
  agentStateName: string | null
  /** The states offered by this routing profile, for the Available/Offline toggle. */
  agentStates: SelectableAgentState[]
  /** True while a `setState` call is in flight, so the toggle can't be mashed. */
  settingState: boolean
  /** The caller's number for an incoming contact, when Connect can report one. */
  callerNumber: string | null
  muted: boolean
  callSeconds: number
  error: string | null
}

type Action =
  | { type: "signed-in"; stateName: string | null; states: SelectableAgentState[] }
  | { type: "state-changed"; stateName: string; states: SelectableAgentState[] }
  | { type: "setting-state"; settingState: boolean }
  | { type: "incoming"; callerNumber: string | null }
  | { type: "connected" }
  | { type: "ended" }
  | { type: "mute-toggled"; muted: boolean }
  | { type: "tick" }
  | { type: "error"; text: string }
  | { type: "clear-error" }

export const initialConnectSoftphoneState: ConnectSoftphoneState = {
  phase: "signing-in",
  agentStateName: null,
  agentStates: [],
  settingState: false,
  callerNumber: null,
  muted: false,
  callSeconds: 0,
  error: null,
}

export function reduceConnectSoftphone(
  state: ConnectSoftphoneState,
  action: Action,
): ConnectSoftphoneState {
  switch (action.type) {
    case "signed-in":
      return {
        ...state,
        phase: state.phase === "signing-in" ? "ready" : state.phase,
        agentStateName: action.stateName,
        agentStates: action.states,
      }
    case "state-changed":
      return { ...state, agentStateName: action.stateName, agentStates: action.states, settingState: false }
    case "setting-state":
      return { ...state, settingState: action.settingState }
    case "incoming":
      return { ...state, phase: "incoming", callerNumber: action.callerNumber, error: null }
    case "connected":
      return { ...state, phase: "connected", callSeconds: 0, muted: false }
    case "ended":
      return {
        ...state,
        phase: "ready",
        callerNumber: null,
        callSeconds: 0,
        muted: false,
      }
    case "mute-toggled":
      return { ...state, muted: action.muted }
    case "tick":
      return state.phase === "connected" ? { ...state, callSeconds: state.callSeconds + 1 } : state
    case "error":
      return { ...state, error: action.text }
    case "clear-error":
      return { ...state, error: null }
    default:
      return state
  }
}

function selectableStates(agent: connect.Agent): SelectableAgentState[] {
  return agent.getAgentStates().map((s) => ({
    name: s.name,
    routable: s.type === connect.AgentStateType.ROUTABLE,
  }))
}

export type UseConnectSoftphoneOptions = {
  // null/undefined (no deployed CCP to embed yet, or local mode) skips Streams
  // entirely -- see ConnectSoftphoneProvider, which calls this hook unconditionally
  // for the whole app session and only has a real url once config.json's ccpUrl loads.
  ccpUrl: string | null | undefined
  connectRegion: string
}

export function useConnectSoftphone({ ccpUrl, connectRegion }: UseConnectSoftphoneOptions) {
  const [state, dispatch] = useReducer(reduceConnectSoftphone, initialConnectSoftphoneState)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const agentRef = useRef<connect.Agent | null>(null)
  const contactRef = useRef<connect.Contact | null>(null)
  const initializedKeyRef = useRef<string | null>(null)
  const subscriptionsRef = useRef<connect.Subscription[]>([])

  // Init the embedded CCP exactly once per (ccpUrl, region) -- guards against both a
  // real url/region change (re-init, intentionally) and React StrictMode's double
  // mount (same key, skipped) since `connect.core.initCCP` has no "destroy" and must
  // never be called twice for the same container.
  useEffect(() => {
    const container = containerRef.current
    if (!container || !ccpUrl) return
    const key = `${ccpUrl}|${connectRegion}`
    if (initializedKeyRef.current === key) return
    initializedKeyRef.current = key
    let cancelled = false

    loadStreams()
      .then(() => {
        if (cancelled) return
        connect.core.initCCP(container, {
          ccpUrl,
          loginPopup: true,
          loginPopupAutoClose: true,
          region: connectRegion,
          softphone: { allowFramedSoftphone: true },
        })
      })
      .catch(() => {
        if (!cancelled) dispatch({ type: "error", text: "Could not load the Connect agent workspace." })
      })

    return () => {
      cancelled = true
    }
  }, [ccpUrl, connectRegion])

  // Wire up the agent and contact subscriptions once Streams is loaded. This effect
  // doesn't depend on connectRegion -- it only needs `connect` to exist, and runs once
  // per mount (subscriptions are torn down on unmount) -- but it does depend on whether
  // there's a CCP to talk to at all: no ccpUrl means the init effect above never ran,
  // so there's no agent/contact to subscribe to.
  useEffect(() => {
    if (!ccpUrl) return
    let cancelled = false
    let agentSub: connect.Subscription | null = null
    let contactSub: connect.Subscription | null = null

    loadStreams()
      .then(() => {
        if (cancelled) return

        agentSub = connect.agent((agent) => {
          if (cancelled) return
          agentRef.current = agent
          dispatch({
            type: "signed-in",
            stateName: agent.getState().name,
            states: selectableStates(agent),
          })

          const stateSub = agent.onStateChange((change) => {
            dispatch({ type: "state-changed", stateName: change.newState, states: selectableStates(change.agent) })
          })
          const muteSub = agent.onMuteToggle((status) => {
            dispatch({ type: "mute-toggled", muted: status.muted })
          })
          const errorSub = agent.onSoftphoneError((err) => {
            dispatch({ type: "error", text: softphoneErrorText(err) })
          })
          subscriptionsRef.current.push(stateSub, muteSub, errorSub)
        })

        contactSub = connect.contact((contact) => {
          if (cancelled) return
          if (contact.getType() !== connect.ContactType.VOICE) return
          contactRef.current = contact

          const onIncoming = () => {
            const initialConnection = contact.getInitialConnection()
            const endpoint = initialConnection?.getEndpoint?.()
            dispatch({ type: "incoming", callerNumber: endpoint?.phoneNumber ?? null })
          }
          const incomingSub = contact.onIncoming(onIncoming)
          const connectingSub = contact.onConnecting(onIncoming)
          const connectedSub = contact.onConnected(() => dispatch({ type: "connected" }))
          const endedSub = contact.onEnded(() => {
            contact.clear({
              success: () => {},
              failure: () => {},
            })
            contactRef.current = null
            dispatch({ type: "ended" })
          })
          subscriptionsRef.current.push(incomingSub, connectingSub, connectedSub, endedSub)
        })
      })
      .catch(() => {
        if (!cancelled) dispatch({ type: "error", text: "Could not load the Connect agent workspace." })
      })

    return () => {
      cancelled = true
      agentSub?.unsubscribe()
      contactSub?.unsubscribe()
      for (const sub of subscriptionsRef.current) sub.unsubscribe()
      subscriptionsRef.current = []
    }
    // ccpUrl starts null and flips to a real value once config.json loads (see
    // ConnectSoftphoneProvider) -- this effect must re-run that one time to pick up the
    // now-initialized CCP, not just on mount.
  }, [ccpUrl])

  // Call timer while connected.
  useEffect(() => {
    if (state.phase !== "connected") return
    const id = setInterval(() => dispatch({ type: "tick" }), 1000)
    return () => clearInterval(id)
  }, [state.phase])

  const openLogin = useCallback(() => {
    // Streams (loginPopup: true, above) opens this same ccpUrl in a popup itself as soon
    // as the hidden CCP iframe loads a sign-in page -- this button is the manual
    // fallback for when the browser blocked that popup, or the agent closed it by
    // mistake, since amazon-connect-streams exposes no typed "reopen" API.
    if (!ccpUrl) return
    window.open(ccpUrl, "ConnectLoginPopup", "width=400,height=600")
  }, [ccpUrl])

  const setAgentState = useCallback((stateName: string) => {
    const agent = agentRef.current
    if (!agent) return
    const def = agent.getAgentStates().find((s) => s.name === stateName)
    if (!def) return
    dispatch({ type: "setting-state", settingState: true })
    agent.setState(def, {
      success: () => dispatch({ type: "setting-state", settingState: false }),
      failure: () => {
        dispatch({ type: "setting-state", settingState: false })
        dispatch({ type: "error", text: `Could not change state to ${stateName}.` })
      },
    })
  }, [])

  const toggleAvailability = useCallback(() => {
    const routable = state.agentStates.find((s) => s.routable)
    const notRoutable = state.agentStates.find((s) => !s.routable)
    const isAvailable = state.agentStates.find((s) => s.name === state.agentStateName)?.routable
    const target = isAvailable ? notRoutable : routable
    if (target) setAgentState(target.name)
  }, [state.agentStates, state.agentStateName, setAgentState])

  const answer = useCallback(() => {
    contactRef.current?.accept({
      success: () => {},
      failure: () => dispatch({ type: "error", text: "Could not answer the call." }),
    })
  }, [])

  const decline = useCallback(() => {
    const contact = contactRef.current
    if (!contact) return
    contact.reject({
      success: () => {},
      failure: () => {
        // Some contact states don't support reject() -- fall back to destroying the
        // agent's own connection, which still gets the agent off the ringing contact.
        contact.getAgentConnection().destroy({ success: () => {}, failure: () => {} })
      },
    })
  }, [])

  const toggleMute = useCallback(() => {
    const agent = agentRef.current
    if (!agent) return
    if (state.muted) agent.unmute()
    else agent.mute()
  }, [state.muted])

  const hangup = useCallback(() => {
    contactRef.current?.getAgentConnection().destroy({
      success: () => {},
      failure: () => dispatch({ type: "error", text: "Could not hang up." }),
    })
  }, [])

  const clearError = useCallback(() => dispatch({ type: "clear-error" }), [])

  return {
    ...state,
    containerRef,
    openLogin,
    setAgentState,
    toggleAvailability,
    answer,
    decline,
    toggleMute,
    hangup,
    clearError,
  }
}


// Connect refuses a second media session for the same agent with a signalling handshake
// failure; the usual cause is the CCP also open in another tab.
export function softphoneErrorText(err: { errorType?: string; errorMessage?: string }): string {
  const text = `${err.errorType ?? ""} ${err.errorMessage ?? ""}`
  if (/handshake|idempotency/i.test(text)) {
    return "Another tab is using the softphone for this agent. Close other Connect or dashboard tabs, reload, then set yourself Available."
  }
  return err.errorMessage || "The Connect softphone reported an error."
}

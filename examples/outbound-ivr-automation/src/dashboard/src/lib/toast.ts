// A tiny toast bus so both the Live call page and the ⌘K command palette can surface
// "call started" / error messages without needing a shared ancestor component.

export type Toast = { id: number; kind: "ok" | "err"; text: string }

type Listener = (toasts: Toast[]) => void

let seq = 0
let toasts: Toast[] = []
const listeners = new Set<Listener>()

function emit() {
  listeners.forEach((fn) => fn(toasts))
}

export function showToast(text: string, kind: "ok" | "err" = "ok") {
  const toast: Toast = { id: ++seq, kind, text }
  toasts = [...toasts, toast]
  emit()
  setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== toast.id)
    emit()
  }, 4000)
}

export function subscribeToasts(fn: Listener): () => void {
  listeners.add(fn)
  fn(toasts)
  return () => listeners.delete(fn)
}

// Minimal, self-contained audio unlock for Monitor (src/components/MonitorButton.tsx).
// Browsers require a user gesture before they'll let audio play; useAwsMonitor.ts's
// `<audio autoplay>` elements get attached a moment after the click that turns Monitor
// on, which can land just outside that gesture. Creating (and resuming) an AudioContext
// synchronously inside the click handler -- same trick the old browser-front-desk audio
// engine used -- unlocks audio for the page so that attach doesn't get blocked.
let ctx: AudioContext | null = null

export function ensureAudioUnlocked(): void {
  if (!ctx) ctx = new AudioContext()
  if (ctx.state === "suspended") void ctx.resume()
}

# Outbound IVR's operator dashboard

React + Vite + Tailwind + shadcn/ui. One build, AWS-only: `npm run build` and
`npm run build:aws` are equivalent: see `src/lib/backend.ts`, and `src/lib/api.ts` for
the data layer that talks to the deployed API.

What it shows during a call: the live transcript, the evidence checklist filling in,
every tool call Outbound IVR made, and the outcome. Plus the controls: the Case queue to
start a call, Monitor call to listen in silently, and the Evidence pages.

## Working on it

Nothing here is run directly in normal use. `infrastructure/scripts/deploy.sh` builds this
and uploads it to S3 behind CloudFront. The root README's "Test it" section has the
commands.

For an edit-and-reload loop:

```bash
npm --prefix src/dashboard ci
npm --prefix src/dashboard run dev        # Vite dev server, SPA only -- see vite.config.ts
npm --prefix src/dashboard run build      # same as build:aws
npm --prefix src/dashboard run build:aws  # what deploy.sh runs
npm --prefix src/dashboard run test       # vitest run
```

The deployed bundle reads `config.json`, written at deploy time with the API URL, the
Cognito pool, the CCP URL and the Monitor URL; `src/lib/config.ts` is the shape. A
missing value disables the feature that needs it rather than failing the page, which is
why the softphone and Monitor can be absent without anything breaking.

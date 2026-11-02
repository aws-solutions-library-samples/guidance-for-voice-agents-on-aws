---
name: review-example
description: Reviews an example folder under examples/ against every rule in examples/AGENTS.md and reports PASS/FAIL per item with file:line evidence. Use before opening a pull request that adds or changes an example, or when replacing a coming-soon placeholder with a real example.
---

## What I Do

Check one example folder (e.g., `examples/nova-sonic-webrtc/`) against `examples/AGENTS.md` and report a PASS/FAIL checklist with `file:line` evidence for each item. I report findings; I do not fix them unless asked.

## When to Use Me

Use this skill after `create-example`, before opening a pull request that adds or changes an example, and whenever a coming-soon placeholder is replaced with a real example.

## Reference

`examples/AGENTS.md` is the source of truth. If it has rules not listed below, check those too.

## Steps

### 1. Determine the Example Type

Read the example's own `README.md`. It is a **coming-soon placeholder** when its body (after the H1) is the single line `Coming soon.`; otherwise it is a **full example**. The index in `examples/README.md` has no status column, so never infer the type from it.

### 2a. Placeholder: Check the Shape

Skip the quality bar. Check only:

| Item | PASS when |
|------|-----------|
| Placeholder shape | `README.md` has an H1 title and the single line `Coming soon.`, nothing else |
| No other files | The folder contains only `README.md` |
| Index row | `examples/README.md` has a row `[<slug>](<slug>/) \| <use case>` (Example \| Use case, no status column) and the link resolves |

### 2b. Full Example: Check Every Rule

| Item | PASS when |
|------|-----------|
| Structure | Folder is `examples/<kebab-case-slug>/`; code lives in its own `src/`, `infrastructure/`, `tests/` |
| No cross-boundary imports | Nothing imports or references code from the root `backend/` or `infrastructure/` (grep imports, relative paths like `../../backend`, and CDK asset paths) |
| README sections | Every TEMPLATE H2 section is present, in order, and filled (no leftover `<!-- -->` hints) |
| Relationship to main guidance | States Standalone, or Requires main guidance deployed and which parts |
| Mermaid | Architecture uses a Mermaid diagram; no ASCII-art diagrams in any Markdown |
| Logging | Python uses structlog with snake_case event names and keyword args (root `AGENTS.md`) |
| Container builds | Deploy uses finch (`CDK_DOCKER=finch`) |
| AWS profile and region | No hardcoded account IDs, profiles, or regions in code or infrastructure |
| Secrets | Secrets come from AWS Secrets Manager; no secrets in code; no committed `.env` (only `.env.example`) |
| Pinned dependencies | Every dependency is exact or compatible-release (`==`, `~=`, exact npm versions or `~`); no bare floors (`>=`) or unpinned names |
| Endpoint auth | Every public endpoint that triggers billable work has authentication and throttling |
| Model IDs current | Every model ID is verified against the current Amazon Bedrock model lifecycle status (check the documentation now, not from memory); no legacy or end-of-life IDs |
| IAM | Least-privilege policies; no `*FullAccess` managed policies or unscoped `"*"` actions |
| Cleanup | Cleanup removes every resource the example creates (compare against the stacks and resources defined in `infrastructure/`) |
| Cost | Real estimate with region and assumptions, not "varies" |
| Tests | Tests exist and run without AWS credentials (run them with AWS credentials unset) |
| Index row | `examples/README.md` has a row `[<slug>](<slug>/) \| <use case>` (Example \| Use case, no status column) and the link resolves |

### 3. Report

Output one line per item:

```
PASS  Structure — examples/<slug>/
FAIL  Pinned dependencies — examples/<slug>/src/requirements.txt:3 `boto3>=1.34` is a bare floor
```

End with a summary: `review-example: <slug> (<placeholder or full>): <n> PASS, <n> FAIL`. The example passes only when every item is PASS.

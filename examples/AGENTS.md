# examples/

## Scope

These rules apply to everything under `examples/`. The conventions in the root [AGENTS.md](../AGENTS.md) also apply.

## Structure

- Put each example in its own folder: `examples/<kebab-case-slug>/`.
- Keep everything the example needs inside its folder (`src/`, `infrastructure/`, `tests/`, `README.md`).
- Never import from the root `backend/` or `infrastructure/`.
- If the example reuses the main guidance, depend on it as a deployed stack and say so in the README's "Relationship to main guidance" section. Never share code across the boundary.
- Start new examples from [TEMPLATE/](TEMPLATE/) using the `create-example` skill.

## Coming soon placeholders

- A placeholder example is a `README.md` with a title and the single line "Coming soon.", and nothing else in its folder.
- List placeholders in the [index](README.md) like any other example. The index has no status column.
- The quality bar below and the `review-example` skill apply once the placeholder is replaced with a real example.

## Conventions carried over from the main guidance

- Use Mermaid for all diagrams. Never use ASCII art.
- Follow the logging conventions in the root [AGENTS.md](../AGENTS.md#logging-conventions).
- Use finch for container builds (`export CDK_DOCKER=finch`).
- Read the AWS profile and region from configuration. Never hardcode them.

## Quality bar

- Store secrets in AWS Secrets Manager. Never put secrets in code or in a committed `.env`; commit `.env.example` only.
- Pin dependency versions (exact or compatible-release). No bare floors.
- Protect any public endpoint that triggers billable work with authentication and throttling.
- Use only current model IDs, verified against the Amazon Bedrock model lifecycle status at authoring time. Never use model IDs from memory.
- Grant least-privilege IAM. Never use `*FullAccess` managed policies.
- Make Cleanup remove every resource the example creates.
- Make tests run without AWS credentials.
- Keep the TEMPLATE's shared responsibility paragraph, cost responsibility line and Notices section (shared responsibility paragraph plus the AWS notice) verbatim. Document example-specific security limitations in the README.

## Definition of done

- Tests pass.
- The README has every TEMPLATE section filled, including a real cost estimate.
- The index row is added or updated in [examples/README.md](README.md).
- `review-example` passes.

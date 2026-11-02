---
name: create-example
description: Scaffolds a new self-contained voice agent use case example under examples/ from examples/TEMPLATE/, fills the README skeleton, and adds the index row. Use when adding a new example, or a coming-soon placeholder for one, to the examples/ section.
---

## What I Do

Scaffold a new example in `examples/<slug>/`:

1. Copy `examples/TEMPLATE/` to `examples/<slug>/`
2. Fill the README skeleton from the answers below
3. Add a row to the index table in `examples/README.md`

For a **coming-soon placeholder**, write the placeholder README only -- no TEMPLATE copy.

## When to Use Me

Use this skill when adding a focused voice agent use case that lives outside the main guidance. To extend the main voice agent itself, use `create-local-tool` or `create-capability-agent` instead.

## Reference

Read `examples/AGENTS.md` before starting. Its Structure, Coming soon placeholders, Conventions, and Quality bar rules are what `review-example` checks.

## Steps

### 1. Gather Requirements

Ask the user for:
- **Slug** (kebab-case, e.g., `nova-sonic-webrtc`). This becomes the folder name. Check `examples/` to avoid conflicts; `TEMPLATE` is reserved.
- **Use case** -- one line for the index table and the Overview section.
- **Key AWS services** -- used in the Overview, Architecture, Cost, and Cleanup sections.
- **Relationship to main guidance** -- **Standalone**, or **Requires main guidance deployed** (and which stacks or parts it reuses).
- **Placeholder?** -- a full example now, or a coming-soon placeholder only.

### 2a. Placeholder: Write the Coming Soon README

Create `examples/<slug>/README.md` containing only the title and one line:

```markdown
# <Example title>

Coming soon.
```

Create nothing else in the folder. Skip to step 3.

### 2b. Full Example: Copy the Template

```bash
cp -r examples/TEMPLATE examples/<slug>
```

Then fill `examples/<slug>/README.md`:
- Replace the H1 with the example title.
- Fill every H2 section (Overview, Relationship to main guidance, Architecture, Prerequisites, Deploy, Try it, Cost, Cleanup, Troubleshooting) and remove the HTML-comment hints once filled.
- **Architecture**: a Mermaid diagram (no ASCII art).
- **Cost**: a real monthly estimate with region and usage assumptions, broken down by service -- never "varies".
- **Cleanup**: commands that remove every resource the example creates.

Put all code in the example's own `src/`, `infrastructure/`, and `tests/`. Never import from the root `backend/` or `infrastructure/`. Remove each `.gitkeep` once its folder has real files.

### 3. Add the Index Row

Add a row to the table in `examples/README.md` (columns: Example | Use case; placeholders get a row like any other example):

```markdown
| [<slug>](<slug>/) | <Use case> |
```

### 4. Review

Remind the user to run the `review-example` skill on `examples/<slug>/` before opening a pull request. Full examples must pass the full quality bar in `examples/AGENTS.md`.

### 5. Verify Checklist

- [ ] Slug is kebab-case and unique
- [ ] Placeholder: README is title + "Coming soon." only, no other files
- [ ] Full example: every TEMPLATE section is filled, including a real cost estimate
- [ ] Index row added to `examples/README.md` (Example | Use case)
- [ ] `review-example` run

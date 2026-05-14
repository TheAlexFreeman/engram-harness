---
created: '2026-05-09'
memory_influence: low
origin_session: memory/activity/2026/05/09/act-003
outcome_quality: completed
recall_events: 0
reflection_source: model
source: agent-generated
tool: harness
trust: medium
---

# Session Reflection

- **Memory retrieved:** 0 recall result(s)
- **Memory influence:** low
- **Outcome quality:** completed

## Reflection

## Session reflection — act-003 (markdown rendering)

**What went well**

- Reading the codebase before proposing anything paid off immediately. The stack turned out to be React + Tailwind (no Chakra UI despite the user profile saying Chakra), which changed the CSS approach entirely. Would have wasted time proposing Chakra-based solutions otherwise.
- Picking `react-markdown` + `remark-gfm` + `rehype-highlight` is the standard, well-maintained combination for this exact use case. No need to deliberate — it was the right call and worked first try.
- The `@tailwindcss/typography` plugin + custom CSS variable overrides is the cleanest way to style rendered markdown in a Tailwind project. Writing bespoke prose CSS by hand would have been much more fragile.
- The `@import` ordering bug (CSS imports must precede `@tailwind` directives) was caught by Vite's warning and fixed in the same session before commit. Small thing, but clean.

**Surprises**

- The user profile in memory says "React + Chakra UI" but this frontend uses plain Tailwind with no Chakra at all. The memory is describing the Better Base project, not the Engram harness frontend. These are two different codebases in the same workspace and memory conflates them. This is worth flagging.
- highlight.js adds ~350 KB to the bundle. Not a blocker for a dev tool, but worth noting — `rehype-pretty-code` with Shiki is the lighter alternative if bundle size ever matters here.

**What didn't go well**

- Nothing materially broke, but I didn't check whether `jsdom` (used by Vitest) handles `react-markdown`'s output well before running tests. Got lucky — it did. Should have checked the test setup before making DOM-touching changes.

**Knowledge gaps exposed**

- I don't have an internalized picture of which packages in the `react-markdown` ecosystem use ESM-only vs. CJS. This has historically caused Vite/Vitest config pain. It worked here without modification, but I should verify the `"type": "module"` project flag is what saved us.

**Worth remembering next time**

- Always check the actual `package.json` before assuming the stack from user profiles — the profiles describe the user's general work, not every specific project.
- `react-markdown` + `remark-gfm` + `rehype-highlight` is a proven, low-friction combination for this exact use case in a Vite/React project. No need to re-evaluate it.
- The `.agent-prose` pattern (a single CSS class with typography plugin overrides) is reusable — if there are other markdown-rendering surfaces in this project later (memory viewer, knowledge browser), the same class applies directly.
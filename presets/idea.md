# Source: an idea

`source.text` (or `source.path`) holds the idea. Nothing outside this autopilot tracks it.

## Plan (first pass, or when `plan.md` is missing)

1. Read the idea and the repo it targets.
2. Write `$AUTOPILOT_DATA/<ap>/plan.md`: 2-10 slices, each one PR: an id `IDEA-<n>`-style id unique in
   this autopilot (letters, digits, dashes), a title, what it changes, its dependencies, and how it is
   proven.
3. Add an `ask` card "Approve the plan" with options `approve` (recommended) and `change`.

## Sync (every pass)

Same as `jira-ticket.md` "Sync", without any ticket moves: `done` lives only in `state.json`.

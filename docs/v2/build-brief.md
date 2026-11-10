# v2 build brief

This is the idea the `v2` autopilot works from. The design is `docs/v2/design.md`; this file says how to build it.

## Goal

Build autopilot v2 as described in `docs/v2/design.md`, on the `v2` branch, in **shadow mode**, next to v1. Each
slice lands as one squash-merged PR into `v2`.

## Hard rules

1. **Base branch is `v2`.** Every worktree starts from `origin/v2`, every rebase is onto `origin/v2`, every PR is
   opened with `--base v2`. Nothing is pushed to, rebased onto, or opened against `main`.
2. **v1 keeps working.** v1 lives in `lib/autopilot/` and `bin/ap` and is what runs on this Mac today. v2 code goes
   in its own package, `lib/assistant/`, with its own command, `bin/ap2`. v2 may import v1 modules to reuse them.
   It changes v1 behavior only with a test proving v1 still does the same. All of `tests/` keeps passing.
3. **Nothing is installed.** No launchd job is written or loaded, `ap install` is never run, and nothing under
   `~/.claude/autopilot-data`, `~/.claude/skills` or `~/Library/LaunchAgents` is written. Tests use temporary
   folders only.
4. **Shadow mode is the default.** `mode: shadow` is the config default. No code path writes to Jira, Slack or
   GitHub unless `mode: live`. Every write path has a test showing that shadow mode refuses it.
5. **Standard library only**, like v1.
6. **Decide, do not wait.** When the design leaves a choice to the builder, or lists it under "Open questions",
   take the recommendation there (or the simplest reversible choice). Record it as a `self` decision card with
   attention, and in `docs/v2/design.md` under that question. Ask the user only for something destructive or out
   of scope.

## Carry over v1's local dev setup

v2 must run the same kinds of work v1 runs today, including apps that need emulators or simulators. Keep, with
tests:

- a port range per worker (`port_base`, 10 ports each, a parked worker keeps its range);
- single-use resources and `needs` (for example `native-dev-loop`, one emulator or simulator set at a time), so two
  tasks never hold the same one;
- per-project `health_checks`, `extra_hosts` and `mcp_servers`;
- the permission floors (`ap perms write`) for the dispatcher and for workers;
- usage parking and resume, the branch pattern, and `max_workers` across projects;
- importing a v1 project (its `config.json` and `state.json`) into v2 cards **read-only**, so v2 can show it in
  shadow mode.

## Suggested slices

The plan may change these, within 10 slices. Order by what each needs.

1. Package skeleton: `lib/assistant/`, `bin/ap2`, the data folder and `config.yaml` (with `primary_language`,
   `secondary_language`, `mode`, `keep_transcripts`), and the YAML subset the design leaves to the builder.
2. Card store: one JSON file per card, the plain-words lines required in every configured language, the audit
   trail, restore points (local restore; outside changes become proposed opposite actions), and the search index.
3. Source engine: loading source files from `<plugin>/sources/` and `<data>/sources/` (same name wins, `extends:`),
   the form, `fill: paths`, links and keys, `sort`, and `via: command`. Raw output is saved.
4. Built-in sources and scan sessions: the built-in files from the design, and the scan-session contract for
   `mcp`, `connector` and `skill` with the word-for-word check. Tests use recorded fixtures, never live calls.
5. Gate and routines: due slots, `last_slot` written after the run card, watches by interval, the heartbeat, and
   missed runs as the design's recommendation says.
6. Workers and sessions: v1's worker setup carried over (above), result files, `question.json` with the idle
   timeout, and transcript archiving under `keep_transcripts`.
7. Templates and roles: built-in templates with roles and briefs, user templates that cannot add tools.
8. Local page: a standard-library server on `127.0.0.1:4317` that serves the prototype's screens from real cards,
   including questions about a card. Read-only in shadow mode.
9. Phone page build: the page data for the last N days, with "hide what people wrote".
10. v1 import: read v1 projects into v2 cards, read-only, with a dry-run report.

## Done for tonight

Every slice that lands has tests, keeps all of `tests/` green, and works against a temporary data folder with
`bin/ap2` in shadow mode.

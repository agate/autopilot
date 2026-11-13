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

## Phase 2: switch over

Phase 1 (the slices above) is done. The user's goal now: v2 runs by itself on this Mac, does its own remaining
work, holds everything v1 and secretary recorded, and then v1's and secretary's launchd jobs are stopped. The hard
rules above still hold, with one change to rule 3:

- **Rule 3, phase 2.** Code may now install: `ap2 install` may write and load v2's own launchd job and create v2's
  real data folder, when the user runs it. A worker still never runs it, never loads, unloads or stops any launchd
  job, and never writes under `~/.claude/autopilot-data`, `~/.claude/secretary-data`, `~/.claude/skills` or
  `~/Library/LaunchAgents`. Tests use temporary folders and never call `launchctl` for real. The steps under
  "Switch-over steps" are the user's (or a talk session's, after the user says yes to that step).

### Phase 2 slices

Built by v1, in this order, so that v2 can run its own project:

1. **Gate runs the loop.** Each `ap2 gate` run also does what v1's gate does for workers and passes: it checks
   every running worker (a result file ends its round; the dead and idle rules of design 8.2), reads each due
   watch (a source that needs a scan session gets one, design 5.3), and records that a dispatcher pass is due when
   there is work for one (a ready card, a new result file, an answered `ask`), as v1's gate does.
2. **Dispatcher pass.** When a pass is due, the gate starts one short `claude --bg` pass per repo group, as v1's
   `lib/autopilot/dispatch.py` does, driven by a v2 runbook written for cards (design 8.0): start the next role of a
   ready card from its template, read hand-backs and reviews, open `ask` cards, and in live mode only apply writes
   (mark a PR ready, merge, move a Jira ticket). Reuse v1's dispatcher code where it fits.
3. **Install.** `ap2 install`, `ap2 uninstall` and `ap2 doctor`: v2's own launchd label (never v1's
   `com.autopilot.gate` or secretary's `com.secretary.gate`), `ap2 gate` every 60 seconds, its own settings.
   `doctor` checks the job, the data folder, the tools on PATH, the active profile and the mode, and prints a fix
   line for each failure.
4. **Take over a v1 project.** `ap2 import-v1 --take-over <project>` turns that project's imported cards into live
   v2 cards that v2 works on: open tasks keep their worktree, branch, PR, sessions and run folder; settled cards and
   every file the project holds come along as in slice 5. v1's folder is only read. The user pauses the project in
   v1 (`ap pause <project>`) at the same step, so v1 and v2 never both work on it.

Built by v2 itself, after the user moves the `v2` project onto v2:

5. **Every v1 record, all of it local.** For every v1 project, read-only and searchable on the page: settled cards
   with the user's answers, every run folder (briefs, `handback.json`, `evidence.json`, `review.json`,
   `prloop.json`, `journal.md`, media), specs, reports, events and the archive, and the RULES.md lines (v1's global
   and each project's). Access profiles carry over as v2 profiles. v1's folder is only read. Then fetch, once, what
   these records only link to, and save it as files under the card (see "Local copies"): each PR, each Jira
   ticket a task came from, the answers and messages stored on v1's claude.ai page, and the workers' session
   transcripts. From then on, v2 saves the same local copy when a card's work finishes.
6. **Phone page publishing.** Publish the phone page to claude.ai in live mode (design 9.2, the later work in
   section 12), and read answers from it as v1 does (design 8.3).
7. **Secretary import.** Secretary's watches (`watches.json`) become v2 watches on the built-in sources, its open
   items and PR review cards become cards, its labels and work hours carry over. `~/.claude/secretary-data` is only
   read. Each item's Slack thread and PR are saved as local copies too. Never-send stays: v2 drafts Slack replies
   and never posts them.

### Local copies

Every record v2 holds must open with no network. A link to GitHub, Jira, Slack or claude.ai is kept as a link, but
it is never the only copy of what it shows:

- A PR: its title, description, commits, diff, every comment, review and review thread, each check's result, the
  log of every failed job, and every image those texts embed (downloaded, with each link rewritten to the file).
  GitHub deletes Actions logs after about 90 days, so these are fetched first.
- A Jira ticket: its fields, description and comments, and its attachments.
- A Slack thread: every message in it, with names and times.
- A claude.ai page: the answers and messages stored on it.
- A worker's session: its transcript (`keep_transcripts`, design 3.5).

Fetching reads only (`gh`, the Jira and Slack readers v2 already has, the page's own read). It runs in shadow mode
too, since it writes nothing outside the data folder. A record that cannot be fetched (deleted, no access) gets a
note saying what was missing and why, and is fetched again on the next run.

### Switch-over steps

Each step waits for the user's yes to that step.

1. Back up `~/.claude/autopilot-data` and `~/.claude/secretary-data` (a tar file each; the folders stay).
2. `ap2 install` in shadow mode. Run it next to v1 and secretary for at least a day and compare the pages.
3. Move the `v2` project: `ap2 import-v1 --take-over v2`, `ap pause v2` in v1, live mode for that project. Slices 5
   to 7 are then built on v2.
4. When slice 7 has run a day in live mode: stop `com.secretary.gate` (`launchctl bootout`; the plist is kept, so
   it can be loaded again).
5. When every v1 project is taken over or done and slice 5 is in: stop `com.autopilot.gate` the same way. v1's and
   secretary's folders stay as they are, as the backup.

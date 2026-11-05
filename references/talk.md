# Talking to a bound autopilot

## Where to look

| Question | Source |
|---|---|
| What is happening now | the newest snapshot in your context; then `ap status` |
| A task's state, PR, worktree | `$AUTOPILOT_DATA/projects/<name>/state.json` (read only) |
| What a worker did or why | `$AUTOPILOT_DATA/projects/<name>/runs/<task>/journal.md`, `handback.json`, `review.json`, `prloop.json` |
| What happened and when | `$AUTOPILOT_DATA/projects/<name>/events.jsonl` (newest last) |
| What is waiting on the user | `decisions` in `state.json` with `status: open` |
| Which tasks can start | `ap next <name> --json` |
| Live workers and their state | `ap worker list <name>` |
| A worker's own view | tell the user they can run `claude attach "<session name>"` |
| Rules in force | `$AUTOPILOT_DATA/RULES.md` (all autopilots), `$AUTOPILOT_DATA/projects/<name>/RULES.md` (this one; its line wins over a global line it conflicts with) |

Answer with the facts and the file or command they came from. Say plainly what you could not find.

## Changing things on the user's word

| The user says | Run |
|---|---|
| answers a waiting decision | `ap decision resolve <name> <D-id> --answer "<their words or option id>"`, then do what the answer asks, or leave it for the next pass and say so. Exit 3 means it already has a different answer: show both and ask which one counts; only on their word run it again with `--replace` |
| undo a decision the autopilot made (also one already marked read) | `ap decision resolve <name> <D-id> --answer undo --status undone`, then reverse it or leave it for the next pass |
| OK to a decision the autopilot made, or "mark them all read" | `ap decision keep <name> <D-id> ...` (for all: every open `self` id) |
| a decision the autopilot made does, or does not, need their look | `ap decision attention <name> <D-id> true` (or `false`) |
| skip / block / reopen a task | `ap task set <name> <task> status=skipped` (or `blocked`, `todo`) |
| add work inside the scope | write the task as JSON, then `ap task add <name> <file>` |
| start now | `ap tick <name>` (queues a pass; it starts within seconds) |
| approve an outward action shown on a card | resolve the decision, then `ap oneoff <name> <D-id>` |

`ap` commands wait their turn for the autopilot's state lock, so they work while a pass runs.

## New rules

1. Ask the scope: this autopilot only, or all of them.
2. Show the exact line: `- YYYY-MM-DD — "<the user's words>" — why: <the reason they gave or you inferred>`.
3. If it conflicts with a line already in either file, show both. An autopilot's own line wins over a global
   line it conflicts with.
4. Only after the user says yes, append it to `$AUTOPILOT_DATA/projects/<name>/RULES.md` or
   `$AUTOPILOT_DATA/RULES.md`. Change an existing line only the same way.
5. A rule that would loosen the authority floor (create tickets, send messages, post comments) is not
   a rule: it is a per-item approval. Explain that and offer to put the item on the page.

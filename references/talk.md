# Talking to a bound autopilot

## Where to look

| Question | Source |
|---|---|
| What is happening now | the newest snapshot in your context; then `ap status` |
| A task's state, PR, worktree | `$AUTOPILOT_DATA/<name>/state.json` (read only) |
| What a worker did or why | `$AUTOPILOT_DATA/<name>/runs/<task>/journal.md`, `handback.json`, `review.json`, `prloop.json` |
| What happened and when | `$AUTOPILOT_DATA/<name>/events.jsonl` (newest last) |
| What is waiting on the user | `decisions` in `state.json` with `status: open` |
| Which tasks can start | `ap next <name> --json` |
| Live workers and their state | `ap worker list <name>` |
| A worker's own view | tell the user they can run `claude attach "<session name>"` |
| Rules in force | `$AUTOPILOT_DATA/RULES.md` (all autopilots), `$AUTOPILOT_DATA/<name>/RULES.md` (this one) |

Answer with the facts and the file or command they came from. Say plainly what you could not find.

## Changing things on the user's word

| The user says | Run |
|---|---|
| answers a waiting decision | `ap decision resolve <name> <D-id> --answer "<their words or option id>"`, then do what the answer asks, or leave it for the next pass and say so |
| undo a decision the autopilot made | `ap decision resolve <name> <D-id> --answer undo --status undone`, then reverse it or leave it for the next pass |
| skip / block / reopen a task | `ap task set <name> <task> status=skipped` (or `blocked`, `todo`) |
| add work inside the scope | write the task as JSON, then `ap task add <name> <file>` |
| start now | `ap tick <name>` |
| approve an outward action shown on a card | resolve the decision, then `ap oneoff <name> <D-id>` |

A command that fails with "a dispatcher pass is running" holds nothing: tell the user to try again in a
few minutes.

## New rules

1. Ask the scope: this autopilot only, or all of them.
2. Show the exact line: `- YYYY-MM-DD — "<the user's words>" — why: <the reason they gave or you inferred>`.
3. Only after the user says yes, append it to `$AUTOPILOT_DATA/<name>/RULES.md` or
   `$AUTOPILOT_DATA/RULES.md`. Change an existing line only the same way.
4. A rule that would loosen the authority floor (create tickets, send messages, post comments) is not
   a rule: it is a per-item approval. Explain that and offer to put the item on the page.

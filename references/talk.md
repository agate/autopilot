# Talking to a bound autopilot

## Where to look

`<ap dir>` below is the folder on the newest snapshot's `Folder:` line (`ap dir <name>` prints it too). Never
build it by hand or reuse one from earlier in the chat: an autopilot made by an earlier version sits
elsewhere until the gate moves it.

| Question | Source |
|---|---|
| What is happening now | the newest snapshot in your context; then `ap status` |
| A task's state, PR, worktree | `<ap dir>/state.json` (read only) |
| What a worker did or why | `<ap dir>/runs/<task>/journal.md`, `handback.json`, `review.json`, `prloop.json` |
| What happened and when | `<ap dir>/events.jsonl` (newest last) |
| What is waiting on the user | `decisions` in `state.json` with `status: open` |
| Which tasks can start | `ap next <name> --json` |
| Live workers and their state | `ap worker list <name>` |
| A worker's own view | tell the user they can run `claude attach "<session name>"` |
| Rules in force | `$AUTOPILOT_DATA/RULES.md` (all autopilots), `<ap dir>/RULES.md` (this one; its line wins over a global line it conflicts with) |

Answer with the facts and the file or command they came from. Say plainly what you could not find.

## Walking the user through the cards

When the talk starts (the user named this autopilot or picked it) and the newest snapshot shows cards
waiting on the user or decisions worth a look, say how many there are and start with the first one.
If the user asked something else first, answer that, then offer the walk-through in one line.

Order: open `ask` cards, oldest first (they hold up work), then open `self` cards with
`attention: true`. Routine `self` cards only when the user asks for them.

Skip a card the user already answered on the page: read the page's `replies` collection with
`ArtifactData` (`ap publish url`, with no argument, prints the page). A reply whose `decision` is
`<name>:<D-id>`, or whose `decisions` list holds it, is an answer on its way to the next pass: say so
in one line, and do not ask again. When the replies cannot be read, say that before the first card.

For each card:

1. Read the whole card in `<ap dir>/state.json` and check it is still true: open the files and lines
   it cites, and look at the PR or run it links. When something has changed since the card was
   written, say what changed and that the card may be out of date.
2. Explain it in the reader's language, in the page's order: the one sentence (`body`), the words to
   know (`terms`), the analogy, the real example with its `path:line` or link, the steps, each option
   with what it does and costs, the recommendation and its reasons (and the `related` cards: how this
   answer depends on theirs), and how to answer (`next`). A card without `explain` (written before
   cards carried one) gets the same explanation, built from its body, its links and the code.
3. Say where you are ("card 2 of 5") and stop. Wait for the user.
4. A question about it: answer from the code, the docs or the run files, citing `path:line`; never from
   memory. Say so when the answer changes the recommendation.
5. An answer: record it as the table below says, say in one line what happens next and when, then go
   to the next card.

After the last card, list what you recorded and when the next pass acts on it. "Later" or "stop" ends
the walk-through: say how many cards are left.

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
| explain an older card (one with no `explain`) on the card itself | write `{body, explain, options: {<option id>: {does, cost}}}` as for a new card, run `ap decision explain <name> <D-id> <file> --check` until it prints `ok`, then without `--check` |

`ap` commands wait their turn for the autopilot's state lock, so they work while a pass runs.

A card you add (`ap decision add`) is a full card, written as `RUNBOOK.md` "A decision card" says: the
command refuses one without its walk-through.

## New rules

1. Ask the scope: this autopilot only, or all of them.
2. Show the exact line: `- YYYY-MM-DD — "<the user's words>" — why: <the reason they gave or you inferred>`.
3. If it conflicts with a line already in either file, show both. An autopilot's own line wins over a global
   line it conflicts with.
4. Only after the user says yes, append it to `<ap dir>/RULES.md` or
   `$AUTOPILOT_DATA/RULES.md`. Change an existing line only the same way.
5. A rule that would loosen the authority floor (create tickets, send messages, post comments) is not
   a rule: it is a per-item approval. Explain that and offer to put the item on the page, as a full
   card.

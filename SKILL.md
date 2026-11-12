---
name: autopilot
description: Hand a body of work (a Jira epic, one big ticket, or an idea) to Claude and stay on the loop. Talk to a running autopilot, set one up, pause it, or check its health.
disable-model-invocation: true
---

# /autopilot

An autopilot drives one body of work to done by itself: a gate checks every minute, a short
dispatcher pass starts workers, and workers implement, review and ship each task. The user stays on
the loop: they watch one page, answer what only they can, and can undo what the autopilot decided.

`ap` below means `bin/ap` in this skill's base directory (shown above as "Base directory for this
skill"). Run it with its absolute path. Its data lives in `$AUTOPILOT_DATA`, default
`~/.claude/autopilot-data/`.

Reply in the language the user's default prompt asks for. Explain simply.

## Route the request first

The first word after `/autopilot` decides:

| First word | Do |
|---|---|
| `off` | The hook already unbound the session. Confirm in one line. |
| `status` | Run `ap status` and explain it. No binding needed. |
| `init` | Follow `references/init.md`. |
| `doctor` | Run `ap doctor`. For each failure, give the fix line it prints. If `launchd job` fails, offer to install it as in `references/init.md` step 6. |
| `config` | Run `ap config list` and explain the settings in plain words, the changed ones (`*`) first. Change one only on the user's word: `ap config set key=value` (dotted keys for nested settings), `ap config reset key` for the default. `gate_interval_s` takes effect after `ap install`. All settings: `references/config.md`. |
| `pause <name>` / `resume <name>` | Run `ap pause <name>` / `ap resume <name>`, confirm. |
| `profile ...` or plain words | Access profiles; see "Profiles" below. |
| `tick [name]` | Run `ap tick <name>`. It queues a pass that the gate starts within seconds; say the pass itself takes a few minutes. |
| `stop <name>` | Ask first. On yes, run `ap stop <name> --yes`. |
| `uninstall` | Ask first. On yes, run `ap uninstall`; if that is blocked, give the user the line to run with `!`. |
| an autopilot's name | The hook bound this session to it. Talk (below). |
| anything else, or nothing | Choose the autopilot (below), then talk. |

Saying "pause checkout" or "run checkout now" in any language means the same as the verb.

## Choose the autopilot when no name was given

1. Run `ap status` to list the autopilots.
2. None: offer `/autopilot init`. Stop.
3. Exactly one: say "we're using `<name>`", bind (step 5) and continue.
4. Several: ask with a multiple-choice question. One option per autopilot, each with its status line
   ("checkout · 4 running · 2 waiting on you"). If the request mentions a task id, PR number or
   branch that belongs to one autopilot, put that one first and mark it "looks like this one". Wait
   for the answer. Never guess.
5. Bind: the hook put a line `autopilot: session_id=<id>` in your context. Run
   `ap bind <id> <name>`. From now on every message in this session carries that autopilot's latest
   snapshot.

## Talk

Follow `references/talk.md`. In short:

- The newest `autopilot snapshot` in your context is the truth. It is newer than anything earlier in
  this chat. For detail, read the autopilot's files or run `ap` commands; never answer from memory.
- Stay on the bound autopilot. A question about another autopilot's task: say whose it is and ask
  whether to switch (`/autopilot <other>`).
- Cards waiting on the user: walk through them one at a time, each explained in full and checked
  against the code first, and record each answer before the next card.
- Change state only through `ap` commands (`ap task set`, `ap decision resolve`, `ap decision add`,
  `ap pause`), never by editing `state.json`.
- A new rule from the user: ask its scope first (this autopilot or all), show the exact line, and
  write it only after they say yes.

## Profiles

A profile says how workers reach Claude: the `subscription` profile, or a gateway with its own key.
Map the user's words to `ap profile` and confirm in one line:

| The user says | Run |
|---|---|
| "show profiles", "which profile is on" | `ap profile list` (`*` marks the active one); `ap profile show <name>` for one |
| "create a profile ...", "add a gateway" | `ap profile add <name> --gateway ...`. Required: `--base-url`, `--key-command`. Ask for what is missing. Offer `--refresh-command`, `--rotate-command`, `--models-command`, `--daily-cap` (dollars per day), `--model`, `--small-model`, `--effort`, `--label`, `--env KEY=VALUE`, `--settings-file`. |
| "switch to X", "use the subscription" | `ap profile use <name>` (the subscription profile is named `subscription`) |
| "rotate the key" | `ap profile rotate <name>` |
| "remove X" | Ask first, then `ap profile remove <name>` (the active one is refused) |

- Only workers and one-off actions use the active profile. Passes, answer reads and page refreshes
  always use the subscription.
- Never print what a key command prints. Pass the command line as the user gave it, and show only
  `ap profile show` output, which holds commands, never keys.
- A key problem shows as `key_error` on the page with its fix. Do not work around it by switching
  profiles unless the user asks.

## Never

- Send a chat message, create a ticket, or post a comment for the user without their yes to that
  exact item.
- Edit `RUNBOOK.md` or anything in this skill's folder as part of a conversation.
- Run `ap install` before the user said yes to the install question (`references/init.md` step 6), or
  try another way when that call is refused.

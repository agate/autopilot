# Autopilot v2 design

Status: draft, 2026-11-09. Built from the design conversation of 2026-11-06 to 2026-11-09 and the clickable
prototype at https://claude.ai/artifact/AmCRHr1WWaLVNgGRtjucab (UI only, no backend). The prototype is the
reference for screens; this document is the reference for behavior.

Every decision below is tagged:

- **[decided]**: the user decided it, or accepted it explicitly.
- **[proposed]**: the author's proposal. The user has not confirmed it. Build to it, but treat it as open.
- **[open]**: not decided. Listed again under "Open questions".

v2 lives on the `v2` branch. v1 on `main` keeps working unchanged until v2 replaces it.

## 1. Intent

v1 is a ticket autopilot: it takes a Jira epic or ticket, splits it into PR-sized tasks, and runs background
Claude Code workers that implement, review and loop each PR. Secretary is a separate skill that watches Slack and
GitHub, investigates questions and keeps one page of what needs the user.

v2 merges the two into one personal assistant. The user hands it work in one place, sees in one place what needs
them, what runs in the background and what is planned, and can check any step it took. It must stay safe enough to
leave running: it confirms before acting on a new request, it never sends or posts as the user, and every change it
makes can be undone.

## 2. Goals, non-goals, anti-goals

Goals:

1. One list of cards in four lanes: **Needs you**, **Running**, **Planned**, **History**.
2. Two pages over the same data: a fast local page with the full history, and a phone page on claude.ai with recent
   data only.
3. A chat box on both pages to hand over work, with priority and cancel.
4. Cards that nest (projects, tasks, steps Claude finds), link to each other, are small enough to finish in one go,
   and keep an audit trail.
5. Per-kind card templates: built in, user-made, and improved by Claude with the user's approval.
6. Sources (Slack, GitHub, Jira, and anything the user adds) defined by files, not code.

Non-goals:

- Sending or posting as the user. Claude drafts; the user sends.
- Running in the cloud. v2 runs on the user's Mac.
- Built-in support for iMessage, WhatsApp or Telegram. The source design must make them possible; v2 does not ship
  them.
- Rewriting v1 in another language.

Anti-goals (failure even if it "works"):

- A page that shows both languages on one screen.
- A source or link that an AI invents. Links and ids come from what a tool returned.
- A background process that sits open for hours waiting for a human.
- History that silently disappears (for example, transcripts deleted by Claude Code's 30-day cleanup).
- Defaults that depend on skills the user happens to have installed.

## 3. Foundations

### 3.1 Language **[decided]**

- `primary_language` is a config parameter. The default is `en`.
- `secondary_language` is optional.
  - Set: Claude writes every user-facing output in both languages, and both pages show a language switch.
  - Not set: the switch is hidden and Claude writes only the primary language, at no extra cost.
- The switch changes the **whole page**: buttons, menus, dates and content. One screen never shows both
  languages.
- Every user-facing string has one slot per language. The data model stores the secondary version only when
  `secondary_language` is set. v1 keeps page strings in `templates/labels.en.json`; v2 adds `labels.<lang>.json`
  next to it.
- **[proposed]** A reply drafted for someone else stays in the language that person wrote in. On the
  other-language page its meaning is shown separately, and Copy leaves the meaning out. A quote shows the
  translation first and the original behind a collapsed "Original" toggle.

```yaml
# <data>/config.yaml
primary_language: en        # default: en
secondary_language: zh      # optional; empty = one language, no switch
```

### 3.2 Self-contained **[decided]**

Everything v2 needs by default ships inside the v2 plugin: the engine, the templates, the source files and the
agents. At run time v2 never calls `~/.claude/skills/secretary` or any other user skill. Secretary's code is a
reference to copy from. A user can opt in to their own skills, by naming one in a source file (`read.via: skill`)
or in a rule.

### 3.3 Python **[decided]**

v2 is written in Python, standard library only, like v1. Reasons:

- The time goes to waiting on AI sessions and the network. A gate run takes about 42 ms today; Rust would save
  about 40 ms a minute, which nobody would notice.
- v1 has 4,610 lines of Python and 5,861 lines of tests to reuse.
- No build step: the plugin runs from source wherever `python3` exists.

Revisit only for one hot part (for example, search slower than 100 ms) or for users without Python.

### 3.4 Data folder **[decided]**

All data lives in one folder the user picks, outside `~/.claude` (example: `~/Documents/Assistant`). The data is
plain JSON files plus one search index, so Time Machine, a copy or a sync folder backs it up. How the folder is
found and first set up is in section 12.

```
<data>/
├── config.yaml
├── items/2026-11/     one JSON file per card
├── chat/              every request and reply
├── sessions/          AI conversations per card, gzipped (see 3.5)
├── templates/         the user's templates; they win over built-in ones
├── sources/           the user's sources; they win over built-in ones
├── routines/          one file per routine (see 7.3)
├── restore/           restore points, kept 30 days
├── index.sqlite       search, rebuilt from items/ when missing
└── attachments/
```

v1's data is in `~/.claude/autopilot-data`. Moving it is part of the migration (section 10).

### 3.5 Conversations (transcripts)

Facts, checked on 2026-11-06:

- Claude Code keeps every session in `~/.claude/projects/<cwd>/<session id>.jsonl` and deletes it after about 30
  days (`cleanupPeriodDays`).
- `claude --help` has no option that moves only the transcripts. `CLAUDE_CONFIG_DIR` moves all of `~/.claude`
  (login, settings, plugins, skills), which would cut workers off from the user's setup.
- `--resume <id>` reads only from Claude Code's own location.
- Measured: 49 background sessions took 416 MB, 8.5 MB each on average; gzip makes them about 3.4 times smaller.

Design **[decided]**:

1. While a card runs, its conversation stays where Claude Code keeps it, so it can be resumed.
2. Each time a round ends, a gzipped copy goes to `<data>/sessions/<card>/<role>-<session id>.jsonl.gz`. The card
   keeps the session id as the link.
3. When the card is done, the last copy is made and `claude rm` deletes the original.
4. Setting `keep_transcripts`: `always` (default, decided by the user) | `90d` | `never` (keep only the summary
   files: brief, journal, hand-back, review).

About 2.5 MB per conversation after gzip and 7.5 MB per code task, so 100 code tasks take about 750 MB. The Settings
page shows disk use per folder.

### 3.6 Safety rules **[decided]**

Always asks first:

- Starting any new request: the user confirms the plan.
- Anything sent or posted as the user. Claude drafts; the user sends.
- Merges, deletes, and work outside a card's scope.
- Changes to a template or a source.

Does alone, then reports:

- Reading, searching and drafting.
- Branches, commits and PRs inside a confirmed card.
- Moving its own Jira tickets as the work moves (v1 does this today).
- A restore point before every change.

What "starting" means for cards a source makes **[decided]**: reading, investigating, reviewing and drafting
start on their own (a Question gets investigated and a reply drafted; a PR review gets its reviewers; an alert gets
its analyst). That is the "does alone" list above. Anything that writes outside the data folder waits for the user.
A confirmed split (an epic's tasks, or a ticket's plan) covers starting each of its tasks; each merge still needs
its own approval.

Every change saves a restore point first (file, branch, ticket or card), and restoring saves one too, so an undo can
be undone. Restore points are kept 30 days.

What undo means **[decided]**:

- Local changes (files, cards, local branches, settings) restore right away.
- Changes outside the Mac (a pushed branch, an opened PR, a Jira status, a merge) get a **proposed opposite
  action** on a card: move the ticket back, close the PR, force-push the saved ref, or open a revert PR for a merge.
  The user confirms before it runs, because it changes a shared system.

### 3.7 Plain words on every card **[decided]**

Every card opens with an **In plain words** block, written so a high-school student could follow it: short
sentences, everyday words, one idea each, exact numbers kept. It has three lines:

1. **What this is**: the thing in everyday terms ("checkout tries a failed card payment once more, 2 seconds
   later").
2. **Where it stands**: what has happened so far ("11 new tests; reviews found nothing left to fix; every merge
   check passed").
3. **What you do**: the user's next step, or "nothing", and what happens after it.

The block sits at the top of the full card and is the same on the local page and the phone page; the phone list
shows its second line under each card's title. The writer of a card (worker, dispatcher, scan) must fill all three
lines in every configured language, and the code that adds a card refuses one without them, as v1's
`ap decision add` does for `explain`. v1's fuller walk-through (`terms`, `analogy`, `example`, `steps`, `why`,
`next`) stays for decision cards, below the block.

Because the card explains itself, there is no quick "ask Claude about this card" answer on the page. Questions that
need more go to the Mac (9.4).

## 4. Concepts

### 4.1 Card

The one unit of everything: a request, a task, a question to answer, an alert, a routine run. Every card has:

- `kind`: its template (section 6).
- `lane`: `needs`, `running`, `planned` or `done`.
- `feed`: the source that made it (section 5), shown as "from Slack mentions" or "from Jira epic".
- `parent` and links to other cards. A card can split into children, including children Claude finds while
  working ("origin: found").
- `pri` (P0 to P3), `ask` when it needs the user, and an `audit` list: who did what, when, and the restore point.

An `ask` has one of four types: **confirm** (a plan), **approve** (a merge or another risky step), **answer** (a
question or a draft to send), **fix** (something the user must repair, such as a login).

### 4.2 Template vs source **[decided]**

- A **template** says what a card *is* and how it looks: Question, Errand, Alert, Code task, Project, Research,
  Routine, Login, Step, PR review, and the user's own (example: Parcel).
- A **source** says where cards *come from*: Slack mentions, a Slack alert channel, GitHub review requests, the
  user's own PRs, a Jira epic, the chat box.
- They are independent. One Slack message can become a Question, an Errand or an Alert, or no card at all. Adding
  a source never touches a template, and the other way round.

### 4.3 Watch vs routine

- A **watch** looks at a source every few minutes and opens or updates a card only when it finds something new.
  The look itself is not logged, or History would fill with "nothing new". In this design a watch is simply a
  source with `every:`.
- A **routine** does a job at set times and files every run in History (a daily digest, a Friday worktree cleanup).
- **[proposed]** One watch per source, not one big watch: each has its own interval, fails alone, can be paused
  and scoped alone, and costs differ (`gh` costs no tokens; the Slack connector needs a small-model session). They
  all share the one gate (section 7); there is no process per watch.

## 5. Sources **[decided]**

### 5.1 One engine, one file per source

The engine is written once. It reads every source the same way:

1. **Read**: run the tool the file names: an MCP server, a claude.ai connector, a command, or one of the user's
   skills.
2. **Fill the form**: turn each item into the same fixed record (5.2).
3. **Check**: reject records that fail validation (5.3).
4. **Link and key**: build the link and the key from the record (5.4).
5. **Sort**: decide what the item becomes, then open a card, update a card, or drop it.

Each source is one YAML file. Adding or changing a source needs no code. The one case that needs code is a place no
tool can read at all; then someone writes an MCP server or a command-line tool first, outside v2.

### 5.2 The form

Every item fills the same fields:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The item's id in its own system |
| `thread_id` | no | The thread or parent it belongs to |
| `author` | no | Who wrote or raised it |
| `at` | yes | When |
| `text` | yes | What it says |
| `place` | no | Channel, repo, project |
| `status` | `brings: work` only | Its status in its own system |
| `depends_on` | no | Other items it waits for |

### 5.3 Two ways to fill it

- **`fill: paths`**: the tool returns JSON. The file gives a path per field (`.author.login`) and code fills the
  record, so no model decides any field. (`gh`, Jira MCP, a status page API.)
- **Who makes the call** depends on `read.via`, not on `fill`:
  - `command`: the gate runs it directly. No AI, no tokens.
  - `mcp` and `connector`: those tools can only be called from inside a Claude session (the login lives there).
    One small-model **scan session** per gate run makes every due call of this kind, saves the raw output, and
    ends. `fill: paths` then still means code fills the record from that saved output.
  - `skill`: the scan session runs the named skill.
- **Token cost, honestly:** only `command` reads are free, because the gate runs a plain program and calls no
  model (v1's gate already runs `gh pr view` this way in `gate.pr_fingerprint`). Every `mcp`, `connector` and
  `skill` read costs tokens. The tool's whole reply passes through the scan session, so a big reply costs more.
  `fill: paths` saves judgment, not tokens. The cost per read has not been measured yet. The only number so far is
  v1's own estimate for its Haiku answer read (about $0.0008 a run at list price, `config.py`). Measure the scan
  session before choosing default intervals, and keep each read small (a `since` filter, few fields).
- **`fill: ai`**: the tool returns text (the Slack connector). A small model fills the record from that text, and
  code then checks that every `id`, `thread_id` and `at` appears **word for word** in the raw tool output. A record
  that fails is dropped and logged. This is what keeps an AI from inventing ids.
- The scan session saves the raw tool output with the run, so a check can be repeated and a bad record traced.

### 5.4 Links and keys

- `link` is a pattern filled from the record (`"https://{site}/browse/{id}"`), a path to a link the tool returned
  (`.url`), or `none`. The engine builds it. No AI writes a link.
- Slack: `https://slack.com/archives/{channel}/p{ts without the dot}`, plus <!-- checkpkg: ignore --> `?thread_ts={thread}&cid={channel}`
  for a reply. This opens in the reader's own workspace, so no workspace name is needed (secretary does the same
  today).
- The card stores the ids (channel id, ts, thread ts, author id), not only the link, so a renamed channel still
  opens and a link can be rebuilt.
- `key` names what the card is about. The same key updates the same card: a Slack thread is
  `{channel}:{thread}`, a PR is `{repo}#{number}` (so a Slack ask about a PR and the GitHub review request become
  one card).
- Every card from a source shows a **source block** at the top: who, where, when, their words, and an "Open in
  Slack" / "Open on GitHub" / "Open in Jira" button. It is drawn from the record, so templates never hold links.
  The phone page shows the same button.

### 5.5 Sorting

`sort` maps a category to a template, `update_card` or `none`. For `fill: ai` sources the small model picks the
category from the list in the file. Example: `question → question`, `ask_to_do → errand` (or `code` when it needs a
repo), `alert → alert`, `fyi → none` (goes into the daily digest).

### 5.6 Work sources and writing back (Jira)

`brings: work` sources bring tasks, not messages. They add:

- `status_map`: their statuses to card states. The source wins over the card.
- `writes`: what the engine may change back there, and when (`on_start`, `on_pr`, `on_merge`).
- `never`: what it must not do. For Jira: `create_ticket`, `comment`. It proposes those on a card instead.
- `plan: split_here` for a big ticket that Claude splits into PR-sized tasks locally, after the user approves the
  plan.

v1's `presets/jira-epic.md`, `jira-ticket.md` and `custom.md` already follow this idea, as prose instructions for
the AI plus `ap tasks sync` validation. v2 turns them into source files (see the appendix). A source file may also
carry `instructions:` in prose for anything paths cannot express; the form check still applies.

### 5.7 Built in, the user's own, and changes **[decided]**

- Built-in source files live in `<plugin>/sources/`. The user's live in `<data>/sources/`.
- A user file with the same name wins. It can replace the built-in file, or start with `extends: <name>` and hold
  only the lines it changes.
- Plugin updates never touch the user's files. When a built-in file the user extends changes, the Sources page
  shows the change and asks whether to keep the user's lines.
- Adding a source: the user describes it in the chat box or on the Sources page; Claude drafts the file, runs a
  test read that saves nothing, shows what each item would become, and installs it only after the user confirms.
- v2 ships: Slack mentions, Slack alert channel, GitHub review requests, the user's own PRs, Jira epic, Jira
  ticket, and the chat box (built into the engine, no file).

## 6. Templates **[decided]**

- A template lists what a card shows, its buttons, and what each button does (lane change, restore point, child
  card). The prototype's Templates page shows each one as a wireframe plus its YAML.
- Templates that take outside input say `source: any` and put `source_block` first in their view.
- Built-in templates ship with the plugin; the user's own live in `<data>/templates/` and win on the same name.
- Claude suggests improvements from how the user acts (example: "you put off 6 questions this week by replying
  'later'; add a 'Remind me tomorrow' button"). The user reviews a diff and applies it as a new version; the old
  version stays.
- Claude drafts a new template from a description; it is installed only after the user checks it.

What a card *does* **[decided]**: a template names **roles**, and roles do the work. v2 ships the roles: implement,
review, PR loop, investigate, analyze, reviewer angles, and a general worker. A template lists which roles run, in
what order, and gives each a prose brief:

```yaml
kind: parcel
roles:
  - role: general            # a built-in role
    when: "delivery day 08:00"
    brief: "Check the carrier page for {tracking}. If it says out for delivery, nudge me."
```

A user template, or one Claude drafts, can combine roles and write briefs. It cannot add tools or permissions: each
role keeps the tools and permission floor it ships with. Which folder a role runs in comes from the card (its repo
worktree, or the data folder).

### 6.1 Review angles **[proposed]**

What a code review checks lives in one place, and the user can extend it without code. Today it lives in two
tables that do not know about each other: v1's PR loop (`commands/pr-loop.md`, "Roles to add beside Correctness")
and secretary's PR review (`lib/secretary/review.py`, the role lists by PR kind). v2 has one table, used by both
the PR loop on the user's own PRs and the PR review of other people's PRs.

- An **angle** is one reviewer's point of view. It has a `name`, the one `question` it answers, `when` it applies
  (path globs, a PR kind such as `ui`, `api`, `ci`, `infra`, `data`, `tests`, `docs`, or `always`), and a prose
  `brief` for the reviewer.
- Three layers, the later one winning on the same name: built in (`<plugin>/review/angles.yaml`, v1's and
  secretary's tables merged), the user's own (`<data>/review/angles.yaml`), and the repo's own
  (`<repo>/.autopilot/review.yaml`, read from the reviewed commit). A layer may add an angle, replace one, change
  only some fields with `extends:`, or turn one off with `off: true`. This follows 5.7: plugin updates never touch
  the user's or the repo's file, and the page shows when a built-in angle the user changed was updated.
- How many: Correctness always, plus the matching angles, 2 to 4 reviewers in all (the PR loop), or 2 to 3 (a PR
  review; 2 for a small PR). When more angles match than fit, the repo's come first, then the user's, then built in.
- Each round's report and each review card say which angles ran and which layer each came from.
- An angle is prose for a reviewer; it cannot add tools. Reviewers stay read-only (6, last paragraph).

```yaml
# <repo>/.autopilot/review.yaml
- name: Payments safety
  question: "Can this change charge a customer twice, or not at all?"
  when: { paths: ["billing/**", "payments/**"] }
  brief: "Read every caller of the changed charge functions. Idempotency keys must survive retries."
- name: Accessibility & UX
  off: true            # this repo has no UI
```

### 6.2 PR review cards **[proposed]**

A review request becomes a card that reviews itself, as secretary does today: the user opens it to a verdict, not a
to-do.

1. **In**: the GitHub review requests source (built in), and a Slack or other message that asks the user to review
   a PR ("PTAL", "take a look", a review-assignment bot). Both key on `{repo}#{number}` (5.4), so they land on one
   card.
2. **Start**: reviewing starts on its own (3.6). The `pr-review` role reads the PR, the ticket or the PR body as the
   requirement, the code around the change and CI at the head, then runs its angles (6.1) in parallel. Each
   finding is checked against the code before it counts.
3. **Report**: the first line is a TL;DR that leads with the verdict, `approve`, `changes` or `question` ("Approve:
   does what the ticket asks; 1 nit."). Then background, problem, solution, and the findings in four groups:
   confirmed, out of scope, rejected (with the reason) and questions for the author. A draft review comment may be
   attached, marked as not sent.
4. **Plain words** (3.7): what this is, "<who> asked you to review PR #<n>: <title>"; where it stands, the TL;DR
   line; what you do, "Read the <n> findings, then approve or ask for changes on GitHub yourself."
5. **New commits**: a delta review of only the commits since the reviewed one updates the same card; it reviews
   the whole PR again when the new commits include a merge commit or the branch was rewritten. Each earlier finding
   is marked fixed or still open.
6. **Closes** when the user has reviewed, the request is removed, or the PR is closed or merged.
7. **Never posts**: nothing is sent to GitHub or any chat in either mode. A later "post this review" button would be
   a `live`-mode write the user taps each time; it is not part of this design.

Built from secretary's `commands/review.md`, `agents/reviewer.md` and its report checks (`review roles`, `review
verdict`, `report validate`), copied, not called (3.2).

### 6.3 How the PR loop ends **[decided by the user, 2026-11-10]**

v1's PR loop may merge on its own only when it ends clean (a round with nothing to fix) or converged (two rounds in
a row with no behavior finding). When it reaches its round limit, the last round's fixes have never been reviewed,
so the loop asks the user, even when those fixes are a few small lines with tests and green CI. On 2026-11-10 this
asked the user twice for the same reason (v1 PRs #21 and #25 on `v2`). v1 keeps its rule; v2 changes it:

1. When a round's fixes would hit the round limit, the loop runs one **check of the fix**: a fresh reviewer per
   angle that found something in that round, each reading only the fix commit (its diff, the findings it answers,
   and the code it touches). It does not count toward the round limit, and it runs at most once per loop.
2. It finds nothing: the loop ends clean and may merge on its own, as any clean loop does (`ap merge-check` still
   decides).
3. It finds something: no more fixing; the loop stops and asks the user, with the check's findings on the card.
4. The card and the round report say the loop ended through a check of the fix, and link its findings.
5. A loop that stops for any other reason (not converging, the same finding twice, a design call) asks as before.

## 7. Scheduling

### 7.1 The gate **[decided, from v1]**

- launchd runs `ap gate` every 60 seconds (`StartInterval: 60`, `RunAtLoad`). Each run is a fresh, short process
  with no AI and no tokens: it reads files, decides, starts work, writes `gate.json`, and exits.
- Nothing is remembered between runs except what is on disk.
- One global lock means there is never more than one dispatcher at a time.

### 7.2 How a run knows something is due

For anything on a clock, the gate compares two things: the latest scheduled slot at or before now, and the last
slot that was handled. If the slot is newer, it is due. A run at 09:00:40 or 09:03 still sees the 09:00 slot. v1's
`gate.report_due` works this way.

Watches use elapsed time instead: due when `now - last_check_at >= every`.

### 7.3 Routines **[proposed]**

One file per routine:

```json
{"title": "Daily digest", "schedule": {"days": "weekdays", "at": ["09:00"]},
 "tz": "America/Los_Angeles", "missed_run": "skip",
 "last_slot": "2026-11-07T09:00", "paused": false}
```

Each gate run, per routine:

1. Find the latest slot at or before now. If it is not newer than `last_slot`, stop.
2. If the slot is more than a few minutes old (a grace of 5 minutes), it was missed: apply `missed_run`.
3. Create the run's card first, keyed `<routine>@<slot>`, then write `last_slot`. If the gate dies between the two,
   the next run finds the card and does not make a second one.
4. The run card goes to Running like any other card.

**[open] Missed runs.** The user first chose "skip, never catch up" when told that catching up was impossible. That
was wrong: v1's report already runs the latest missed slot once after the Mac wakes. The choices are:

- `skip` (what the prototype shows now);
- `once`: run the latest missed slot once, late (v1's report);
- `once_within: 3h`: run it late only if it is still useful (the author's recommendation).

Whatever is chosen, a new routine shows a notice about this limit once, and the user can dismiss it **[decided]**.
History marks each skipped run.

### 7.4 Heartbeat **[decided]**

The gate writes `last_run_at` on every run. The local page's top bar shows "Last check: 25s ago" and turns red after
3 minutes without a run. (Found while debugging: a two-line log without timestamps looked like a live failure for a
day. Every log line gets a time.)

## 8. Workers and sessions

### 8.0 Where judgment runs **[proposed, author's call]**

Three layers, as in v1:

1. **The gate** (code, no AI) decides *when*: what is due, what changed, whether a pass is needed.
2. **The dispatcher pass** (v1's `--bg` session per repo group, driven by `RUNBOOK.md`) decides *what next*: it
   reads hand-backs and reviews, starts the next role, applies a work source's `writes`, opens `ask` cards, and
   publishes the phone page. v2 keeps it and extends its runbook to cards from every source.
3. **Workers** do the work inside one card.

Small **scan sessions** (section 5.3) only read sources; they decide nothing about work. `every: each pass` means
"read during each dispatcher pass".

### 8.1 Background sessions **[decided, from v1]**

- A worker is a `claude --bg -n "<card> · <role>"` session started in the work's own directory (a worktree), so
  it loads that repo's CLAUDE.md and skills.
- It is resumed with `claude --bg --resume <session id>`, listed with `claude agents --json --all`, stopped with
  `claude stop`, and the user can talk to it with `claude attach`.
- `claude -p` is used only for one-off checks, not for workers.
- `max_workers` (5 in v1) limits workers across all projects. Usage limits: no new work at 85% of a usage window;
  running workers park at 90% and resume after the reset.

### 8.2 When a round ends

Facts, checked on 2026-11-06: a `--bg` session does not exit when its turn ends. It sits idle with a live process
(about 260 MB) until `claude stop`. v1 found one idle for 17.6 hours because its last message was a question,
which Claude Code labels "blocked", and v1 never stops blocked workers.

Rules:

1. **[decided, from v1]** A worker ends a round by writing its result file (`handback.json`, `review.json`,
   `prloop.json`). The gate sees the file (newer than the worker's start) and runs `worker end`: stop the process,
   save the session id on the card. Session state can be lost; the card cannot.
2. **[decided, from v1]** Dead (no session, or its process is gone, with no result file): resume once with
   "you were interrupted; check your worktree and run files first". Dead twice: stop and ask the user.
3. **[proposed]** No process waits for a human. A worker that needs the user writes `question.json` and ends its
   round; the question becomes the card's `ask`; the user's answer resumes the session with the answer in the
   prompt.
4. **[proposed]** A worker stuck at a permission prompt (state blocked, status waiting) gets a card telling the
   user to `claude attach`. After a time limit (for example 30 minutes) it is stopped and the card keeps it
   resumable. Not yet verified: whether the prompt appears again on resume.
5. **[proposed]** Fallback: any worker idle for N minutes with no result file is stopped and marked "paused, can
   resume".
6. When the card is done, its saved sessions are archived (3.5) and removed.

Streaming one long-lived process per card was rejected: it needs supervision, restart logic and a decision on how
long to keep it. A new process per round loses nothing when it dies; the cost is re-reading the conversation when it
resumes after a long gap.

### 8.3 Replies reach the worker

- Local page: **[proposed]** the local server resumes the card's session as soon as the user answers, without
  waiting for the next gate run.
- Phone page: v1 reads answers from the page every 60 seconds with a small Haiku session, except between 22:00 and
  06:00, so a phone answer starts in 1 to 2 minutes.

### 8.4 Splitting big work **[decided direction, details proposed]**

Splitting an epic or a big ticket runs in its own worker session, started in the repo's directory, never in the
assistant's main loop. To make the split reliable:

1. Rough split: read the code, cut PR-sized tasks, and state what waits for what.
2. A cold reviewer session checks the split, like a code review.
3. The user confirms.
4. Each task gets its detailed spec only when it starts, against the latest main (scope, 3 to 9 criteria, existing
   tests), as v1 does. A worker that finds a task too big splits it again into child cards.

### 8.5 Order and parallel work **[decided, from v1]**

- A task is ready when it is `todo`, everything it depends on is done, no worker holds it and no decision on it is
  open. Ready tasks sort critical first, then by phase (dependency depth).
- Starts are shared round-robin across projects, so projects run in parallel rather than one after another.
- A task that needs a single-use resource (a port, a device, a test account) does not start while another task holds
  it.

## 9. Pages

### 9.1 Local page **[decided]**

- A small server on `127.0.0.1:4317`, started at login. Only this Mac can open it.
- Every card, all history, full search (from `index.sqlite`).
- Layout from the prototype: lanes and projects on the left, the card list in the middle, the card rendered by its
  template on the right. Manage: Sources, Templates, Restore points, Settings.
- Top bar: the language switch (only when a secondary language is set), the chat box, the heartbeat, workers
  running, usage.

### 9.2 Phone page **[decided]**

- A claude.ai Artifact, published by the gate.
- Open cards always show. History is trimmed to the last N days (setting, default 3); older history stays on the Mac.
- Which lanes to include is a setting. "Hide what people wrote" shows "Priya asked a question" instead of her words.
- Tapping a card opens the **full card**, drawn by the same template as on the local page: plain words, the
  source block with its Open-in-source button, findings, plan, merge checks, children, questions and activity.
  Only transcripts and history older than the window stay off the phone. "Hide what people wrote" also hides
  their words in the full card.
- New requests go through the page's chat.

### 9.3 Chat box **[decided]**

The user types a request. Claude guesses the template, drafts a plan (what it will do, what it touches, how to undo
it), and waits for confirm. Routines get a parsed schedule and next run, plus the asleep notice. Priority can be set
and changed; any card can be cancelled. Every change saves a restore point and shows an Undo.

### 9.4 Questions about a card **[decided]**

Every full card, local or phone, has an **Ask about this card** box.

1. The question is saved on the card (on the phone, in the page's database).
2. The Mac picks it up: right away from the local page; from the phone with the next answer read (about a
   minute).
3. A **read-only** session starts in the card's folder (its worktree, or the data folder), reads what the question
   needs (code, the thread, the ticket), and writes the answer on the card in plain words, with the files or
   messages it used. It changes nothing.
4. Questions and answers stay on the card and show on both pages. Each one is also an activity entry.

**[proposed]** A follow-up question resumes the same read-only session, so it keeps its context; the session is
archived with the card.

## 10. From v1 and secretary to v2

- v1 stays on `main` and keeps running. v2 is built on `v2`.
- **[proposed, author's call]** v2 can run next to v1 on the same Mac: it has its own plugin name, launchd label,
  command name and data folder, so nothing is shared by accident.
- **Shadow mode first [decided]**: until the user switches it to live, v2 reads every source and makes cards but
  writes nothing outside its data folder (no Jira moves, no branches, no PRs, no published page other than its
  own). v1 and secretary keep doing the real work meanwhile. Shadow mode is a setting (`mode: shadow | live`), and
  every write path checks it, so it is also a safe way to try a new source later.
- Reused from v1 as is or nearly: the gate, workers and sessions, budget and parking, `nextwork`, permission
  floors, evidence checks, the PR loop agents.
- Reused from secretary by copying, not calling: the Slack and GitHub reading logic (as built-in source files and
  engine checks), the investigator, analyst and reviewer agents, the presets.
- **[open]** Moving v1's data from `~/.claude/autopilot-data` and secretary's state into the new data folder.

## 11. Open questions, most important first

1. **Missed routine runs**: `skip`, `once`, or `once_within: 3h` (7.3).
2. **Worker lifecycle**: confirm "no process waits for a human" and the idle timeout (8.2, points 3 to 5).
3. **Replies on the local page** resume the session immediately (8.3)?
4. **Drafts and quotes in two languages** (3.1, proposed part).
5. **Data migration** from v1 and secretary (10).

Calls the author made without asking (check them):

- v1's dispatcher pass stays as the layer that decides what happens next (8.0).
- MCP and connector reads go through one small scan session per gate run (5.3).
- v2 runs next to v1 under its own names (10).
- One review-angle table for both the PR loop and PR review, in three layers with the repo's winning (6.1).
- PR review cards as secretary makes them today, with the TL;DR as "where it stands" (6.2).
- The check of the fix runs once, with one reviewer per angle that found something in the last round (6.3).

## 12. Left to the builder

- The engine's internal module layout and the YAML loader (standard library only, so a small YAML subset or JSON
  with comments; pick one and document it).
  - **[decided by the builder]** A small YAML subset, read by `lib/assistant/yamlish.py`. Not JSON with comments:
    every example file in this design is YAML, and users write these files by hand.
    - In: block maps, block lists (`- item`, lists of maps, and a list written at its key's indent), flow lists
      `[a, b]` and flow maps `{a: 1, b: x}` on one line, plain / single-quoted / double-quoted strings, ints,
      floats, `true`/`false`, `null`/`~`/empty, and `#` comments (full-line and trailing; a `#` starts a comment
      only at the start of a line or after a space or tab, never inside quotes). Keys are always strings.
      `true`, `false` and `null` are read in lower, Title and UPPER case (`true`, `True`, `TRUE`); `yes`/`no`
      stay strings (YAML 1.2). A quote starts a quoted string only at the start of a value or key (the line's
      start, after the leading `- ` / `? `, after `:` and a space or tab, or inside a flow collection after `[`,
      `{`, `,`, or `:` and a space or tab, or a `:` that follows a closing quote, JSON style `"a":"b"`), so `don't`,
      `well-'known` and `Bob, 'the builder` are plain text. Only space and tab are white space (YAML 1.2), so a
      no-break space or other Unicode space is part of the text. Numbers are decimal ints and floats; hex, octal,
      `.inf` and `.nan` are refused so nothing is silently read as a string.
    - Refused, with one error class (`YamlError`) whose message starts with `line N:` and says why: tabs in
      indentation, a tab after a `-` or `?` indicator (straight after it or after its spaces), anchors `&`, aliases `*`, tags `!`, multi-line scalars `|`/`>` and multi-line plain values,
      document markers `---`/`...`, bad indentation, unclosed quotes or brackets (a flow collection must close on
      its line), duplicate keys, complex `?` keys, a quoted block key followed by a space or tab before `:`
      (`"k" : v`), a `- ` list item inside a flow collection, a plain value holding `: ` (quote it), a plain value
      starting with `@`, `` ` ``, `%`, `]`, `}`, `,` or `? ` (quote it), hex, octal, `.inf` and `.nan` numbers
      (write a decimal number or quote it), and nesting too deep to read. The top level must be a map. Refusing is safer than guessing: a file either reads exactly as written or names its bad line.
- **[decided by the builder]** The data folder and its lookup. The folder is `--data <path>` if given, else the
  environment variable `AP2_DATA`, else `~/Documents/Assistant`. A folder inside `~/.claude` is refused (3.4),
  symlinks followed (by `resolve()`) and in any letter case (the name check ignores case, so `~/.CLAUDE` is
  refused even before `~/.claude` exists; a same-folder check catches paths `resolve()` does not fold, such as
  macOS firmlinks like `/System/Volumes/Data/...` and bind mounts). The refusal also checks `~/.claude` under the
  account's home from the user database (`pwd`), so an empty or wrong `HOME` cannot hide it. With no `--data` and
  no `AP2_DATA`, an empty or relative `HOME` is refused ("set it or pass --data") instead of guessing a folder.
  `config.yaml` lives in the folder, so it has no `data_dir` key. On first use `ap2` creates the layout from 3.4
  and writes `config.yaml` with the defaults and a comment per key; it never overwrites an existing
  `config.yaml`. The search index is left to the card store. Config keys and defaults:
  `primary_language: en`, `secondary_language:` (empty = none; it may not equal the primary),
  `mode: shadow` (`shadow | live`), `keep_transcripts: always` (`always | never | <N>d`, N at least 1). A
  language is a short code: 2-3 lowercase letters, then an optional script (`zh-Hans`) and/or an uppercase
  region (`pt-BR`, `es-419`). An unknown key is refused with its name and line.
- **[decided by the builder]** Module layout so far, in `lib/assistant/`: `yamlish.py` (the subset reader),
  `config.py` (defaults, validation, and `require_live(cfg, what)`, which every write outside the data folder
  calls first and which refuses in shadow mode), `datadir.py` (lookup and layout), `cli.py` (the `ap2` command,
  run by `bin/ap2`), and the card store: `cards.py` (card shape, ids and checks), `store.py` (the `Store` API:
  add, get, update, list, restore, propose_opposite, prune, search), `restore.py` (restore points and proposed
  opposite actions) and `index.py` (`index.sqlite`); templates and roles: `templates.py` (loading, the user's
  override, validation, briefs) and `roles.py` (the shipped roles and their tools). `ap2 config [--data PATH]` prints the effective config and `data_dir` as JSON. With stdout
  closed, `ap2 config` sets up the folder, prints nothing and exits 0; when its reader goes away
  (`ap2 config | true`) it exits 1 with nothing on stderr; when a write fails (a full disk) it prints one line,
  `ap2: cannot write the output: ...`, and exits 1. `ap2` with no arguments prints the usage, listing the
  commands, to stderr and exits 2, like v1's `bin/ap`; `--help` prints to stdout and exits 0, and a bad
  argument (`ap2 nosuch`) prints argparse's usage and error to stderr and exits 2. Help, usage and these errors
  follow the same rules, with one difference: when their reader goes away (`ap2 --help | true`) they keep
  their own exit code (0 or 2) instead of 1, because help text is not a result a script reads, while
  `ap2 config`'s JSON is, and a script must see that it was cut short. A closed stream gets nothing (help is
  never sent to stderr instead) and a failed write prints the one line and exits 1 (when stderr is the stream that failed, the line is lost
  and ap2 still exits 1). The `ap2: <message>` line for a refused config or data folder follows the same
  rules: when stderr's reader is gone or its write fails, the line is lost and ap2 still exits 1, with nothing
  more. None of these prints a traceback or an `Exception ignored` line.
- **[decided by the builder]** The card store (design 3.4, 3.6, 3.7, 4.1).
  - Card id: `c-YYYYMMDD-HHMMSS-xxxxxx`, the UTC creation time to the second (ASCII digits only) and 6 random
    lowercase hex digits (a new pair is drawn if the file exists). The file is `<data>/items/<yyyy-mm>/<id>.json`, the month taken from
    the id, so a card is found without a scan and nothing but these characters reaches a path. Times are UTC text,
    `2026-11-10T14:30:05Z`. Files are UTF-8 JSON, sorted keys, indent 2, written atomically (temp file in the same
    folder, fsync, `os.replace`). Every store call holds `<data>/.store.lock` (`flock`), so two processes never lose
    each other's change.
  - The store sets `id`, `created_at`, `updated_at` and `audit`; a card that gives one is refused. Required:
    `kind` and `feed` (non-empty text), `lane`, `pri`, `origin` (the enums of 4.1), `links` (a list of card ids),
    `plain`. Optional: `parent` (a card id or null), `ask` (null or an object whose `type` is one of the four),
    `title` (text, searched). Other keys are kept as they are, for later templates. Ids in `parent` and `links` are
    checked for form only, not for existence (an import may add a child before its parent).
  - Plain words: `plain.what|where|do.<lang>` must be non-empty text for `primary_language` and, when set,
    `secondary_language`. One error class (`PlainWordsError`, a `CardError`) lists every gap, for example
    `plain.where.zh missing; plain.do.en is empty`. Restore does not re-check them: the languages may have changed
    since the point was saved.
  - Audit entry `{at, by, did, restore}`: `did` is `add`, `update: <fields>`, `restore <point id>` or
    `propose <action>`; `restore` is the point saved just before that change (null for an add). An update that
    changes nothing writes nothing. A restore puts the old card back but keeps the full audit (the current one
    plus the restore entry), so the history never shrinks.
  - The store reads a card file only when its structure is usable (`audit` a list, `plain` an object, `ask` an
    object or null, text valid Unicode); otherwise the call fails with one error naming the file, and nothing is
    written. Restore puts back any point it saved without re-checking enums, so an undo can always be undone.
    Text that is not valid Unicode (a lone surrogate, e.g. from command-line bytes that are not UTF-8) is refused
    on add, update, `--by`, `did` and search before anything is written; so are NaN and Infinity (cards are
    standard JSON) and values JSON cannot write with sorted keys (mixed key types, sets). `did`, when given, is checked like `by`: non-empty text.
  - Restore points: one file `<data>/restore/<point id>.json` with
    `{"id", "at", "kind": "card", "card_id", "card"}`, id `r-YYYYMMDD-HHMMSS-xxxxxx` (ASCII digits only). A point
    is refused, naming its file, unless its `id` is its file name and its `card_id` is its card's `id`, and its
    card passes the same structure check as a card file. `Store.prune()` removes points
    older than 30 days (kept at exactly 30); nothing calls it yet, the heartbeat (slice 5) will. A point whose time
    cannot be read is kept.
  - Proposed opposite action: `Store.propose_opposite(id, action, run, by)` sets the card's `ask` to
    `{"type": "approve", "action": <move_ticket_back|close_pr|force_push_saved_ref|revert_pr>, "run": [argv...],
    "proposed": true, "text"?: <optional note>}` and runs nothing. It is refused when the card already has an open ask (that ask is never
    overwritten).
- **[decided by the builder]** The search index schema. `<data>/index.sqlite` holds
  `meta(key TEXT PRIMARY KEY, value TEXT)` with `('version', '1')` and `cards(id TEXT PRIMARY KEY, text TEXT NOT
  NULL)`, the text being the card's plain words in every language, `title`, `kind` and `feed`, casefolded. A search
  keeps the cards whose text holds every white-space-separated word of the query as a substring (`LIKE`, with `%`,
  `_` and `\` escaped). Not FTS5, although the bundled sqlite has it: its default tokenizer cannot split Chinese
  words and its query syntax fails on input like `a-b`. Each add, update and restore rewrites the card's row; when
  that write fails for any reason (sqlite, a damaged card file met by a rebuild, the disk) the file is removed and
  the change still succeeds, because the card file is already written. Before each card file is written,
  `<data>/.index.stale` is made; it is removed once the index holds the card, so a write stopped in between (a
  crash, Ctrl-C) leaves the marker, and the next search or write rebuilds the index. A missing, unreadable,
  other-version or stale-marked file is rebuilt from `items/` (written to a temp file, then `os.replace`; the temp
  file is removed when the build fails). Results are read from the card files, never from the index, and a card is
  kept only when its file holds every word of the query, so a row that is behind its file never shows a card. A search whose rebuild meets a damaged card file
  fails with one error naming that file. Hand edits to card files that remove words are caught by this re-check,
  but added words are found only after the next rebuild (delete `index.sqlite` to force one). An index row that
  is not a card id, or a repeated row, is skipped. A month folder in `items/` that cannot be read fails the call
  with one error; it is never skipped, so a rebuild cannot silently drop its cards.
- **[decided by the builder]** Card commands: `ap2 card add <file.json|-> [--by NAME]`, `ap2 card show <id>`,
  `ap2 card list [--lane L] [--kind K] [--parent ID]`, `ap2 search <text...>`, `ap2 restore <point id> [--by NAME]`,
  each with `--data PATH`. `--by` defaults to `ap2`. `card add -` reads stdin as UTF-8, like a file, whatever the locale. Output is JSON (a card, or a list oldest first; cards made in the same second are in id order); an error is
  one line `ap2: ...` on stderr with exit 1.
- **[decided by the builder]** Templates and roles (design 4.2, 6).
  - Built-in templates live in `lib/assistant/builtin/templates/<kind>.yaml` (the plugin's folder, found from
    `templates.py`'s own path; not the repo's `templates/`, which holds v1's page labels). v2 ships `question`,
    `errand`, `alert`, `code` (Code task), `project`, `research`, `routine`, `login`, `step` and `pr-review`. The
    user's own are `<data>/templates/<kind>.yaml`; one with a built-in kind replaces it, a new kind is added. Only
    `*.yaml` files count; hidden files and folders are skipped. A bad user file fails the load with one error
    naming it (it is never skipped, so a template cannot silently fall back to the built-in one).
  - Keys: `kind` (required; lowercase letters, digits and `-`, and the file name without `.yaml`), `title` and
    `description` (optional text), `source` (`any` or `none`, default `none`), `view` (required, a non-empty
    list of block names, each once), `buttons` (required list) and `roles` (required list). An unknown key is
    refused with its line. A template with `source: any` must show `source_block` first; `source_block` is
    refused without it, because the block is drawn from the source's record (5.4).
  - Blocks: `source_block`, `plan`, `draft`, `findings`, `verdict`, `analysis`, `report`, `pr`, `merge_checks`,
    `children`, `links`, `schedule`, `runs`, `fix_steps`, `questions`, `activity`. The plain-words block (3.7) is
    not a view block: every card shows it first, above its view.
  - A button is `{id, label, <one action>}`: `lane: <needs|running|planned|done>` (a lane change),
    `restore: last` (put the card back as it was before its last change, from its restore point) or
    `child: <kind>` (a child card; the kind must be a built-in or user template). `id` is unique in the file.
    `label` is English text for now; page translations come with the pages' `labels.<lang>.json`.
  - A role entry is `{role, when?, brief}`. `when` is prose. `brief` is prose with `{field}` placeholders, filled
    from the card (`{a.b}` reads a field inside a field; `{{` and `}}` are literal braces). A placeholder the
    card lacks, or holds as null or blank text, is an error (`BriefError`) naming every such field; a value must
    be text or a number. A filled value is not read again. Built-in briefs use only `{id}`, a field every
    stored card has, because `title` is optional.
  - No added tools or permissions: a key that would widen a role is refused anywhere in the file (top level,
    a `roles` entry, a button), before any other check, with one error class `TemplateWidenError` naming the
    file, line and key. The keys, compared without letter case, `-`, `_` or spaces: `tools`, `tool`,
    `allowed_tools`, `disallowed_tools`, `permissions`, `permission`, `permission_mode`, `settings`,
    `setting_sources`, `mcp`, `mcp_servers`, `mcp_config`, `hooks`, `env`, `add_dir`, `add_dirs`,
    `dangerously_skip_permissions`, `floor`, `cwd`, `folder`, `sandbox`, `agents`, `additional_directories`,
    `default_mode`. Every template error is a `TemplateError`, a `ConfigError`, so `ap2` prints it as one
    `ap2: <file>: line N: ...` line.
  - Roles are a fixed table in code (`roles.ROLES`, read-only), not a file, so nothing in the data folder can
    change one. Each role has its tools (the only Claude Code tools its session gets) and a permission floor:
    `worker` is v1's worker floor (`lib/autopilot/perms.py`, `generate(role="worker")`: outward MCP tools, the
    protected data files, the plugin and `gh` comment and review commands denied, the park hook), plus the
    user's templates and sources (`<data>/templates/`, `<data>/sources/`) cannot be edited, because changing a
    template or a source asks first (3.6); `read-only` is that floor with no file-editing tool. The worker
    sessions (slice 6) turn a floor into settings, with Edit and Write deny rules for those two folders.
    - `implement` (`agents/implementer.md`), worker: Agent, Bash, Edit, Glob, Grep, NotebookEdit, Read, Skill,
      WebFetch, WebSearch, Write.
    - `review` (`agents/reviewer.md`), worker: Agent, Bash, Glob, Grep, Read, Skill, Write.
    - `pr-loop` (`commands/pr-loop.md`), worker: Agent, Bash, Edit, Glob, Grep, Read, Skill, Write.
    - `investigate` and `analyze` (secretary's investigator and analyst), read-only: Bash, Glob, Grep, Read,
      WebFetch, WebSearch.
    - `reviewer-angle` (secretary's reviewer; one review angle, 6.1), read-only: Bash, Glob, Grep, Read.
    - `general`, worker: Bash, Edit, Glob, Grep, Read, WebFetch, WebSearch, Write.
    - Adding a role is one more entry in the table; the PR review cards slice adds `pr-review` (6.2) and moves the
      `pr-review` template to it (it uses `reviewer-angle` until then).
  - Commands, each with `--data PATH` and JSON on stdout: `ap2 template list` (every template's kind, title,
    origin, file, and whether it replaces a built-in one), `ap2 template show <kind>` (the template in use, as
    written, and each role with its tools and floor) and `ap2 template check <file>` (validates the file and says
    what it would replace, `builtin`, `user` or null; it copies nothing). They only read: none creates the data
    folder. Errors follow the other commands (one `ap2: ...` line, exit 1).
  - Later work: template versions (6: "the old version stays") and Claude's suggested changes are not built yet.
- How the local server pushes updates to an open page (polling is fine).
- Exact limits: idle timeout, grace for missed slots, page sizes.

## Appendix: example files

### A. Slack mentions (built in) and a user's change

```yaml
# <plugin>/sources/slack-mentions.yaml
source: slack-mentions
brings: messages
read:
  via: connector             # the Slack connector on claude.ai
  tools: [slack_search_public, slack_read_thread]
  query: "<@{me}>"
  every: 15m
fill: ai
form:
  id: Message_ts             # must appear word for word in what the tool returned
  thread_id: thread_ts
  author: From
  at: Message_ts
  text: Text
  place: Channel
link: "https://slack.com/archives/{place_id}/p{id_digits}?thread_ts={thread_id}&cid={place_id}"  # checkpkg: ignore
key: "{place_id}:{thread_id}"
sort:
  question:  question
  ask_to_do: errand          # code when it needs a repo
  alert:     alert
  fyi:       none            # goes into the daily digest
```

```yaml
# <data>/sources/slack-mentions.yaml
extends: slack-mentions
read:
  every: 10m
  skip_channels: ["#random", "#social"]
```

### B. Jira epic (built in)

```yaml
source: jira-epic
brings: work
read:
  via: mcp
  server: atlassian
  tool: search_issues
  jql: "parent = {epic} AND (assignee = currentUser() OR assignee is EMPTY)"
  every: each pass
fill: paths
form:
  id: .key
  text: .fields.summary
  status: .fields.status.name
  depends_on: .fields.issuelinks[is blocked by].key
link: "https://{site}/browse/{id}"
key: "{id}"
status_map: { "To Do": todo, "In Progress": in_progress, "In Review": in_review, "Done": done }
sort: { code_work: code, discussion: question }
writes:
  on_start: { transition: "In Progress", assign: me }
  on_pr:    { transition: "In Review", remote_link: pr }
  on_merge: { transition: "Done" }
never: [create_ticket, comment]
```

### C. A user's own source, no code

```yaml
# <data>/sources/vendor-status.yaml
source: vendor-status
brings: messages
read:
  via: command
  run: curl -s https://status.example.com/api/v2/incidents/unresolved.json
  every: 5m
fill: paths
form:
  id: .incidents[].id
  at: .incidents[].created_at
  text: .incidents[].name
link: .incidents[].shortlink
key: "{id}"
sort: { all: alert }
```

### D. A source that uses the user's own skill

```yaml
# <data>/sources/team-standup.yaml
source: team-standup
brings: messages
read:
  via: skill
  skill: team-standup        # the user's skill, used because the user named it
  every: "weekdays 09:30"
fill: ai
form: { id: required, at: required, text: required }
link: none
sort: { all: question }
```

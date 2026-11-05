# Setting up an autopilot

Ask one question at a time, each as a multiple-choice question with a default. Never ask what you can
look up: read the source first (the Jira epic or ticket, the repo) and offer what you found as the
default. Write nothing until the user confirms the playback.

## Before the questions (first init only)

- Read the user's default prompt (`~/.claude/CLAUDE.md`). Take the reader's language from it, without
  asking, and run `ap config set reader_lang=<code>` (`en`, `zh`, `ja`, ...).
- Do not offer to import rules from elsewhere. The dispatcher runs in the repo and workers in its
  worktrees, and both already load the repo's `CLAUDE.md`/`AGENTS.md`, its `.claude` settings and
  skills, and the user's memory for that repo; a copy in `RULES.md` would drift from them. Rules that
  exist nowhere else come later, in talk (`references/talk.md`, "New rules").

## The questions

0. **Name.** Propose a short word from the work's title (epic "Checkout redesign" -> `checkout`):
   lowercase letters, digits and dashes, starting with a letter, not already used.
1. **Source.** Jira epic (default) / one Jira ticket / an idea / something else.
   - Jira epic or ticket: ask for the key. Read it right away.
   - Idea: ask for the text or a document path.
   - Something else: ask three more, one at a time: where can the tasks be seen? how do you know one
     can start? where is "done" marked? Keep the answers as `source.custom` with keys `where`,
     `ready_when`, `done_where`.
2. **Repo.** Default: the repo the source points to, else the current repo.
3. **Branch pattern.** Default `{prefix}.{task}.{short}`, prefix from the local part of
   `git config user.email`. Mention that some repos reject `claude/` branches.
4. **Single-use resources** that only one worker may use at a time (an emulator, a test device).
   Default: none, or what the source suggests.
5. **Authority.** Default: the table in `RUNBOOK.md` ("Authority"). The user may only tighten it.
6. **Report times.** Default 09:00, 17:00, 21:00 local.
7. **Anything to watch?** Outside events that should wake the dispatcher (a nightly run finishing).
   Propose watch commands you can infer, each `{"name", "cmd"}` where `cmd` prints something that
   changes only when the event happens (no ages or timestamps, or every gate run wakes the
   dispatcher). Default: none.
8. **Also publish to a company host?** Default no.

## Playback and writing

Read the whole configuration back in plain words. On the user's yes:

1. Write `$AUTOPILOT_DATA/<name>/config.json` from `templates/config.json` with the answers.
2. If `reader_lang` is not `en` and `$AUTOPILOT_DATA/labels.<lang>.json` does not exist, translate every
   value of `templates/labels.en.json` (keep keys and `{placeholders}`) into that file, plus
   `lang_name` in that language and `lang_short`, its one- or two-character name for the language
   switch (`中`, `日`, `FR`).
3. Run `ap perms write <name>` (it discovers this repo's tools and writes the permission floor).
4. The page, first init only (`ap publish url` prints nothing yet):
   - run `ap page build`, copy the file it prints into your scratchpad directory, and publish it with
     the Artifact tool, icon `gauge`, and
     `capabilities: {"db": {"rules": [{"path": "replies", "read": "view", "write": "owner"}]}, "user": {}, "assets": {}, "downloads": true}`
     (only the page's owner can answer cards, even after the page is shared);
   - run `ap publish url <url>`;
   - run `ap publish diff`, send its `set` rows with one `ArtifactData` batch, then `ap publish mark`.
   Later inits reuse the same page. When the package's `templates/page.html` has changed since the page
   was published, update it in place: add any key of `templates/labels.en.json` that
   `labels.<lang>.json` lacks, translated; run `ap page build`, copy the file into your scratchpad,
   read the live page with the Artifact tool, and publish the copy to the `ap publish url` page
   without `capabilities` and without `icon`, so it keeps the ones it has. Its database is untouched.
5. Sources that need a plan (`jira-ticket`, `idea`): follow the preset's "Plan" section now, so the plan
   is waiting on the page.
6. The background check. Run `ap install --check`. If it prints `installed`, go on. Otherwise ask one
   multiple-choice question: install it now? Say in plain words what it is: a login item that runs
   `ap gate` every minute and after each restart; the gate's own checks use no tokens; from 06:00 to
   22:00 it also reads the page's answers once a minute with a tiny Haiku session (about $0.0008 a read
   at list price, only while a card can be answered, so answers take effect within a minute; at night
   the 30-minute pass picks them up); it starts the passes that do use the user's Claude usage;
   `ap uninstall` removes it. Options: install now (recommended) / not now.
   - Install now: run `ap install` with the Bash tool. If Claude Code shows a permission prompt, the
     user approves it there. If the call is refused or blocked, do not retry it or find another way:
     give the user the line `ap install --print` prints, to run themselves prefixed with `!`.
   - Not now: say autopilots will not run on their own until it is installed, and that
     `/autopilot doctor` offers it again.
   Then run `ap doctor` and walk through what fails.
7. If the worker cap is below the number of active autopilots, say so ("`ap status` shows the
   warning").
8. Bind this session with `ap bind <session_id> <name>` and offer `ap tick <name>` to queue a first
   pass now.

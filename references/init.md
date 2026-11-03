# Setting up an autopilot

Ask one question at a time, each as a multiple-choice question with a default. Never ask what you can
look up: read the source first (the Jira epic or ticket, the repo) and offer what you found as the
default. Write nothing until the user confirms the playback.

## Before the questions (first init only)

- Read the user's default prompt (`~/.claude/CLAUDE.md`). Take the reader's language from it, without
  asking, and run `ap config set reader_lang=<code>` (`en`, `zh`, `ja`, ...).
- Offer to import rules the user already has (a runbook or memory files they point to). Show each
  proposed line in the format of `templates/RULES.md` and keep only the lines they say yes to.

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
   changes when the event happens. Default: none.
8. **Also publish to a company host?** Default no.

## Playback and writing

Read the whole configuration back in plain words. On the user's yes:

1. Write `$AUTOPILOT_DATA/<name>/config.json` from `templates/config.json` with the answers.
2. If `reader_lang` is not `en` and `$AUTOPILOT_DATA/labels.<lang>.json` does not exist, translate every
   value of `templates/labels.en.json` (keep keys and `{placeholders}`) into that file, plus
   `lang_name` in that language.
3. Run `ap perms write <name>` (it discovers this repo's tools and writes the permission floor).
4. The page, first init only (`ap publish url` prints nothing yet):
   - run `ap page build`, copy the file it prints into your scratchpad directory, and publish it with
     the Artifact tool, `capabilities: {"db": {}, "user": {}, "assets": {}, "downloads": true}`,
     icon `gauge`;
   - run `ap publish url <url>`;
   - run `ap publish diff`, send its `set` rows with one `ArtifactData` batch, then `ap publish mark`.
   Later inits reuse the same page.
5. Sources that need a plan (`jira-ticket`, `idea`): follow the preset's "Plan" section now, so the plan
   is waiting on the page.
6. First init only: run `ap install --print` and ask the user to run that exact line themselves,
   prefixed with `!`. It installs the login item that runs the gate every 5 minutes. Then run
   `ap doctor` and walk through what fails.
7. If the worker cap is below the number of active autopilots, say so ("`ap status` shows the
   warning").
8. Bind this session with `ap bind <session_id> <name>` and offer `ap tick <name>` for a first pass now.

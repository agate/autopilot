"""The roles v2 ships (design 6): who does a card's work, with the tools and permission floor each one keeps.

A template names roles and gives each a brief; it never changes a role's tools or floor (`templates` refuses any
key that would). The table is fixed here, in code, not in a file: nothing in the data folder can change it, and
`ROLES` is read-only. Which folder a role runs in comes from the card (its repo worktree, or the data folder).

To add a role (as the PR review cards slice added `pr-review`), add one `Role(...)` to `_SHIPPED`.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

# Permission floors. A floor is the deny list a role's session starts under; a read-only role is denied every tool
# outside its `Role.tools`, built-in or MCP, but READ_ONLY_KEPT (and every MCP server whole), so those are not given
# at all. The worker sessions (slice 6) turn a floor into settings with v1's `ap perms write` code.
FLOORS = MappingProxyType({
    "worker": "v1's worker floor (lib/autopilot/perms.py, generate(role='worker')): MCP tools that post, comment, "
              "reply, send or create are denied; the data folder's protected files and the plugin cannot be "
              "edited; `gh pr comment`, `gh pr review`, `gh issue comment`, `gh issue create` and adding PR "
              "reviewers are denied; the usage park hook runs before every tool call, and a Bash guard hook "
              "denies a shell write into the data folder (but the worker's own run directory) or the plugin, "
              "best effort, as v1's `ap hook-guard` does. Added to v1's floor: the "
              "user's templates and sources (`<data>/templates/`, `<data>/sources/`) cannot be edited, because "
              "changing a template or a source asks first (design 3.6); the worker sessions (slice 6) add these "
              "as Edit and Write deny rules.",
    "read-only": "the worker floor, and no Edit or NotebookEdit: the role reads and runs commands that change "
                 "nothing, and every gh command group a review does not need, reads included, the git commands "
                 "that push, commit or set an alias, a config value or a remote (READ_ONLY_GIT_DENIED), and "
                 "`claude` itself, which would start a session under the user's own settings, not this floor "
                 "(READ_ONLY_BASH_DENIED, the claude path the worker runs, and the other claude versions in "
                 "the folder its link leads into), every v1 `ap` command "
                 "(V1_AP_COMMANDS, V1_AP_EXTRA, and each one the `ap` on PATH declares in its modules, which "
                 "are read and never run), "
                 "and the ap2 commands a review does not run (READ_ONLY_AP2_DENIED) are denied by rule. The "
                 "session reads no global or system git config, so the user's git aliases and credential helper "
                 "are not loaded (READ_ONLY_GIT_ENV). Each of the user's own gh aliases (the `aliases:` map "
                 "in gh's config file, links followed, in block or flow style, read and never run, both the file "
                 "the daemon's env names and the one a profile's env names) is denied by rule as well, since `gh pm` with "
                 "`pm: pr merge` would merge a PR. These rules are best effort: they match "
                 "the command's text, so a program that runs `claude` or `git` for the worker (`python3 -c`, say) "
                 "gets past them, and so does a spelling that puts an option before the gh or git subcommand "
                 "(`gh -R o/r pr merge`, `gh pr -R o/r merge`, `git -C . push`); closing that needs an "
                 "allow-list Bash hook that reads the subcommand from the parsed words (guard.py's parser could "
                 "be reused) or the sandbox, both deferred to the sandbox follow-up. The investigate and analyze "
                 "roles, not startable as workers today, would need their own gh reads when they are. Every MCP tool "
                 "the session loads that the role does not list is denied, and every MCP server whole, so a "
                 "server's write tool (delete, update, submit, merge) that v1's outward list misses stays shut. "
                 "Every built-in tool the role does not list is denied too (the ones the session finds, and "
                 "READ_ONLY_BUILTIN_DENIED even when it finds none), but the tool search and Task. Its "
                 "one write is its result file: Write only there, which the worker sessions (slice 6) "
                 "add as a deny rule for every other path. A role without Write (reviewer-angle) returns its "
                 "findings to the loop that started it.",
})

# Tools a read-only role never has. Write is not here: it writes the role's result file and nothing else.
READ_ONLY_DENIED = ("Edit", "NotebookEdit")
# gh's command names, a snapshot taken from gh 2.80.0 (`gh help`, `gh pr --help`, `gh run --help`): its top-level
# commands (core, GitHub Actions, additional, and the `co` alias gh ships for `gh pr checkout`; help topics only
# print text), and the subcommands of `gh pr` and `gh run`. A prefix rule cannot say "every gh command but these",
# so a top-level command a newer gh adds is covered only once it is added here.
GH_COMMANDS = ("auth", "browse", "codespace", "gist", "issue", "org", "pr", "project", "release", "repo", "cache",
               "run", "workflow", "co", "agent-task", "alias", "api", "attestation", "completion", "config",
               "extension", "gpg-key", "label", "preview", "ruleset", "search", "secret", "ssh-key", "status",
               "variable")
# Each subcommand's aliases (the ALIASES line of `gh pr <sub> --help` and `gh run <sub> --help`) are in the snapshot
# too: `gh pr new` (create), `gh pr ls` and `gh run ls` (list).
GH_PR_COMMANDS = ("create", "list", "status", "checkout", "checks", "close", "comment", "diff", "edit", "lock",
                  "merge", "ready", "reopen", "review", "unlock", "update-branch", "view", "new", "ls")
GH_RUN_COMMANDS = ("cancel", "delete", "download", "list", "rerun", "view", "watch", "ls")
# The gh commands a review needs (`commands/pr-review.md` runs `gh pr view`, `gh pr diff`, `gh pr checks` and
# `gh api`; the rest are harmless reads): no rule here prefixes them.
READ_ONLY_GH_KEPT = ("pr", "run", "api", "search", "status", "help", "version")
READ_ONLY_GH_PR_KEPT = ("view", "diff", "checks", "list", "ls", "status")
READ_ONLY_GH_RUN_KEPT = ("view", "list", "ls", "watch")
# Bash commands a read-only role never runs (v1's `perms.BASH_DENY` rule shape): its session holds the user's gh and
# git logins, and one plain gh command can change GitHub as the user (`gh ssh-key add` gives a key push to every repo
# the user can; `gh pr update-branch` pushes a merge onto the PR's branch, moving its head, so the next check starts
# another review). So every gh command group a review does not need is denied whole, reads included, not only its
# write verbs: each top-level gh command but READ_ONLY_GH_KEPT, each `gh pr` subcommand but READ_ONLY_GH_PR_KEPT
# (the worker floor, v1's `perms.BASH_DENY`, already denies `gh pr comment` and `gh pr review`, so they are not
# repeated here), each `gh run` subcommand but READ_ONLY_GH_RUN_KEPT, the git commands in READ_ONLY_GIT_DENIED, and
# `claude` itself. `gh api` with a field or a method is not here. The investigate and analyze roles, not startable as
# workers today, would need their own gh reads (`gh issue view`, say) when they are.
_WORKER_FLOOR_GH_PR = ("comment", "review")
# Git commands a read-only role never runs: a push or a commit, and the ones that set an alias, a config value or a
# remote, or push another way (`git ps` with an alias `ps = push` gets past the `git push` rule).
READ_ONLY_GIT_DENIED = ("Bash(git push:*)", "Bash(git commit:*)", "Bash(git config:*)", "Bash(git -c:*)",
                        "Bash(git --config-env:*)", "Bash(git remote:*)", "Bash(git send-pack:*)",
                        "Bash(git subtree:*)")
# The git env a read-only session runs with (`workers.floor` sets it after a profile's env, so a profile cannot undo
# it): no global or system git config, so the user's aliases and credential helper (which pushes with the user's gh
# login) are not loaded, and no password prompt. A review's git reads (`git show`, `git log`, `git cat-file`,
# `git rev-parse`, `git ls-tree`, and `ap2 review plan --checkout`) need neither.
READ_ONLY_GIT_ENV = MappingProxyType({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                                      "GIT_TERMINAL_PROMPT": "0"})
READ_ONLY_BASH_DENIED = (tuple(f"Bash(gh {cmd}:*)" for cmd in GH_COMMANDS if cmd not in READ_ONLY_GH_KEPT)
                         + tuple(f"Bash(gh pr {sub}:*)" for sub in GH_PR_COMMANDS
                                 if sub not in READ_ONLY_GH_PR_KEPT + _WORKER_FLOOR_GH_PR)
                         + tuple(f"Bash(gh run {sub}:*)" for sub in GH_RUN_COMMANDS
                                 if sub not in READ_ONLY_GH_RUN_KEPT)
                         + READ_ONLY_GIT_DENIED + ("Bash(claude:*)",))
# v1's `ap` commands, a snapshot of v1's own list (`ap` with no arguments prints it; a test keeps the two equal). A
# review runs none, and some start a session or change v1's state (`ap oneoff` runs `claude -p` with a tool allowed,
# after `ap decision add` and `ap decision resolve`; `ap worker`, `ap tick`, `ap kick`), so a read-only role is
# denied each one, reads included. `workers.floor` writes each as `ap <command>`, `<package>/bin/ap <command>` and
# the `ap` on PATH; never a bare `ap`, which would prefix every `ap2` command.
V1_AP_COMMANDS = ("access", "archive", "bind", "budget", "check-package", "config", "decision", "dir", "doctor",
                  "event", "evidence", "gate", "hook-guard", "hook-park", "hook-prompt", "install", "kick",
                  "merge-check", "next", "oneoff", "page", "pass", "pause", "perms", "publish", "report", "resume",
                  "status", "stop", "task", "tasks", "tick", "unbind", "uninstall", "worker")
# Commands an installed v1 has that this snapshot lacks, denied as well: `ap profile add ... --models-command`
# makes v1 run a command (a nested `claude -p`, say), and `ap profile use` rewrites every v1 session's settings.
# The installed `ap` on PATH can be a separate copy, so `workers.floor` also reads its `lib/autopilot/*.py` (never
# runs it: a run migrates v1's data folder) and denies each command its `@cli.command("...")` decorators register
# (`workers._ap_commands`); this list holds when that read finds none.
V1_AP_EXTRA = ("profile", "reply-kind")
# Built-in tools a read-only role is denied even when the tool discovery does not list them, less the role's own
# tools (`workers.floor` drops those): v1's `replies.DISALLOWED` without `Task`, plus the claude.ai page tools and the
# task list. Each one acts outside the floor (a scheduled cloud agent, the user's pages, a push, a message) or is not
# a review's. Edit and NotebookEdit are READ_ONLY_DENIED.
READ_ONLY_BUILTIN_DENIED = ("Agent", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "EnterWorktree",
                            "ExitWorktree", "ListAgents", "LSP", "Monitor", "PublishPlugin", "PushNotification",
                            "Read", "RemoteTrigger", "ReportFindings", "ScheduleWakeup", "SendMessage",
                            "ShareOnboardingGuide", "Skill", "TaskStop", "WebSearch", "Workflow", "Write",
                            "ArtifactComments", "Glob", "Grep", "WebFetch", "AskUserQuestion", "EnterPlanMode",
                            "ExitPlanMode", "SendFeedback", "Artifact", "ArtifactData", "TaskCreate", "TaskUpdate",
                            "TodoWrite")
# Built-in tools a read-only role keeps though its `Role.tools` does not list them: the tool search, and `Task`, the
# Agent tool's older name in some Claude Code versions (denying it could take the Agent tool away).
READ_ONLY_KEPT = ("ToolSearch", "Task")
# The `ap2` commands a read-only role never runs: the Bash guard lets `ap2` itself write the data folder, so every
# command but the ones the pr-review command runs (`card show`, `review plan`, `review verdict`, `review check`) and
# the park (`worker park`, allowed by rule; deny would win over that allow) is denied, at the finest level a prefix
# rule allows. The hooks (`hook-*`) are left alone. `workers.floor` writes each in two spellings: `ap2 <command>` and
# `<package>/bin/ap2 <command>`. Best effort: they match the command's text, as v1's rules do.
READ_ONLY_AP2_DENIED = ("config", "search", "restore", "source", "scan", "gate", "template", "profile", "spend", "cap",
                        "serve", "phone", "import-v1", "install", "uninstall", "doctor", "card add", "card list",
                        "review apply", "review sync", "worker list", "worker start", "worker end", "worker check",
                        "worker answer", "worker finish")


@dataclass(frozen=True)
class Role:
    name: str
    does: str          # one line: what the role does
    tools: tuple       # the Claude Code tools its session gets, and no others
    floor: str         # a key of FLOORS
    from_v1: str       # what it is built from (v1 or secretary), copied, not called (design 3.2)


_READ = ("Bash", "Glob", "Grep", "Read")
_WEB = ("WebFetch", "WebSearch")
_SHIPPED = (
    Role("implement", "Implements one code task in its worktree: tests first, the repo's checks, evidence, a "
                      "hand-back.",
         ("Agent", "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Skill", "WebFetch", "WebSearch", "Write"),
         "worker", "agents/implementer.md"),
    Role("review", "Reviews one code task cold after it is handed back: criteria against evidence, a correctness "
                   "pass over the diff, a verdict with exact fixes.",
         ("Agent", "Bash", "Glob", "Grep", "Read", "Skill", "Write"), "worker", "agents/reviewer.md"),
    Role("pr-loop", "Loops on the user's own PR until CI is green and no reviewer finds anything; one fixer "
                    "pushes.",
         ("Agent", "Bash", "Edit", "Glob", "Grep", "Read", "Skill", "Write"), "worker", "commands/pr-loop.md"),
    Role("investigate", "Investigates one question (code, docs, history, trackers) and drafts a reply that is "
                        "never sent.",
         _READ + _WEB + ("Write",), "read-only", "secretary's investigator agent"),
    Role("analyze", "Analyzes one alert (still firing, how big, what changed, what the playbook says).",
         _READ + _WEB + ("Write",), "read-only", "secretary's analyst agent"),
    Role("reviewer-angle", "Reviews a PR from one review angle, adversarially, and returns findings.",
         _READ, "read-only", "secretary's reviewer agent; the PR loop's role reviewers"),
    Role("pr-review", "Reviews someone else's PR for the user: its angles in parallel, every finding checked, a "
                      "report that leads with a verdict. Posts nothing.",
         ("Agent", "Bash", "Glob", "Grep", "Read", "Write"), "read-only",
         "secretary's review command, copied into commands/pr-review.md"),
    Role("general", "Does one errand from its brief: reads, checks, drafts, and edits files only in the card's "
                    "folder.",
         ("Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write"), "worker", "v1's worker floor"),
)
ROLES = MappingProxyType({r.name: r for r in _SHIPPED})


def as_dict(role: Role) -> dict:
    return {"role": role.name, "does": role.does, "tools": list(role.tools), "floor": role.floor,
            "from_v1": role.from_v1}

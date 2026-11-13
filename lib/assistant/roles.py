"""The roles v2 ships (design 6): who does a card's work, with the tools and permission floor each one keeps.

A template names roles and gives each a brief; it never changes a role's tools or floor (`templates` refuses any
key that would). The table is fixed here, in code, not in a file: nothing in the data folder can change it, and
`ROLES` is read-only. Which folder a role runs in comes from the card (its repo worktree, or the data folder).

To add a role (as the PR review cards slice added `pr-review`), add one `Role(...)` to `_SHIPPED`.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

# Permission floors. A floor is the deny list a role's session starts under; tools outside `Role.tools` are not
# given at all. The worker sessions (slice 6) turn a floor into settings with v1's `ap perms write` code.
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
                 "nothing, and the GitHub and git write commands (READ_ONLY_BASH_DENIED) and the ap2 commands a "
                 "review does not run (READ_ONLY_AP2_DENIED) are denied by rule, best effort. Its "
                 "one write is its result file: Write only there, which the worker sessions (slice 6) "
                 "add as a deny rule for every other path. A role without Write (reviewer-angle) returns its "
                 "findings to the loop that started it.",
})

# Tools a read-only role never has. Write is not here: it writes the role's result file and nothing else.
READ_ONLY_DENIED = ("Edit", "NotebookEdit")
# Bash commands a read-only role never runs (v1's `perms.BASH_DENY` rule shape): its session holds the user's gh and
# git logins, so the write verbs are denied by rule, not only by its process's prose. `gh api` with a field or a
# method is not here.
READ_ONLY_BASH_DENIED = ("Bash(gh pr merge:*)", "Bash(gh pr close:*)", "Bash(gh pr reopen:*)", "Bash(gh pr ready:*)",
                         "Bash(gh pr edit:*)", "Bash(gh pr create:*)", "Bash(gh release:*)", "Bash(git push:*)",
                         "Bash(git commit:*)")
# The `ap2` commands a read-only role never runs: the Bash guard lets `ap2` itself write the data folder, so every
# command but the ones the pr-review command runs (`card show`, `review plan`, `review verdict`, `review check`) and
# the park (`worker park`, allowed by rule; deny would win over that allow) is denied, at the finest level a prefix
# rule allows. The hooks (`hook-*`) are left alone. `workers.floor` writes each in two spellings: `ap2 <command>` and
# `<package>/bin/ap2 <command>`. Best effort: they match the command's text, as v1's rules do.
READ_ONLY_AP2_DENIED = ("config", "search", "restore", "source", "scan", "gate", "template", "profile", "spend", "cap",
                        "serve", "phone", "import-v1", "card add", "card list", "review apply", "review sync",
                        "worker list", "worker start", "worker end", "worker check", "worker answer",
                        "worker finish")


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

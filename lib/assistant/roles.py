"""The roles v2 ships (design 6): who does a card's work, with the tools and permission floor each one keeps.

A template names roles and gives each a brief; it never changes a role's tools or floor (`templates` refuses any
key that would). The table is fixed here, in code, not in a file: nothing in the data folder can change it, and
`ROLES` is read-only. Which folder a role runs in comes from the card (its repo worktree, or the data folder).

To add a role (the PR review cards slice adds `pr-review`), add one `Role(...)` to `_SHIPPED`.
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
              "reviewers are denied; the usage park hook runs before every tool call. Added to v1's floor: the "
              "user's templates and sources (`<data>/templates/`, `<data>/sources/`) cannot be edited, because "
              "changing a template or a source asks first (design 3.6); the worker sessions (slice 6) add these "
              "as Edit and Write deny rules.",
    "read-only": "the worker floor, and no file-editing tool (Edit, Write, NotebookEdit): the role reads, runs "
                 "commands that change nothing, and reports what it found (its result file is its one write).",
})

# Tools that change files. A read-only role has none of them.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


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
         _READ + _WEB, "read-only", "secretary's investigator agent"),
    Role("analyze", "Analyzes one alert (still firing, how big, what changed, what the playbook says).",
         _READ + _WEB, "read-only", "secretary's analyst agent"),
    Role("reviewer-angle", "Reviews a PR from one review angle, adversarially, and returns findings.",
         _READ, "read-only", "secretary's reviewer agent; the PR loop's role reviewers"),
    Role("general", "Does one errand from its brief: reads, checks, drafts, and edits files only in the card's "
                    "folder.",
         ("Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write"), "worker", "v1's worker floor"),
)
ROLES = MappingProxyType({r.name: r for r in _SHIPPED})


def as_dict(role: Role) -> dict:
    return {"role": role.name, "does": role.does, "tools": list(role.tools), "floor": role.floor,
            "from_v1": role.from_v1}

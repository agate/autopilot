# Source: a Jira epic

`source.key` is the epic. Use the Atlassian tools.

## Sync (every pass)

1. Find the user's Jira account with the Atlassian user-info tool (once per pass).
2. List the epic's children: JQL `parent = <key>` with fields `summary, status, assignee, issuelinks,
   issuetype, priority`. Page through all results.
3. Keep a child only if it is unassigned or assigned to the user, and not In Progress under someone
   else. A child that is not code work (a discussion, a decision, a design review) becomes an `ask`
   card instead of a task, added once.
4. Map each kept child to a task:
   - `id`: the key. `title`: the summary, in both languages. `link`: the ticket URL. `assignee`: the
     assignee's display name or null.
   - `depends_on`: keys this ticket is blocked by ("is blocked by" links) that are also children.
   - `status`: To Do / Backlog / Selected -> `todo`; In Progress -> `in_progress`; In Review ->
     `in_review`; Done / Closed / Resolved -> `done`; Won't Do -> `skipped`; Blocked -> `blocked`. Jira
     wins over local status.
   - `needs`: single-use resources from `config.resources` that the ticket obviously needs; else `[]`.
   - `critical`: true for tickets on the longest dependency chain; `phase`: the dependency depth (1 for
     tickets with no dependencies). `origin` is set by `ap`.
5. Write the list to a file and run `ap tasks sync <ap> <file>`.

## Moving tickets

- Start: transition to In Progress and assign the user.
- PR open: transition to In Review and add the PR as a remote link.
- Merged: transition to Done with resolution Done.
- Never create tickets and never comment. Propose either on a card.

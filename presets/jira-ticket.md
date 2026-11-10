# Source: one big Jira ticket

`source.key` is the ticket. The autopilot splits it into PR-sized slices and works them locally.

## Plan (first pass, or when `plan.md` is missing)

1. Read the ticket, its links and the code it points to.
2. Write `<ap dir>/plan.md` (the folder `ap dir <ap>` prints): 2-10 slices, each one PR: an id `<key>-s<n>`, a title, what it
   changes, the slices it depends on, and how it is proven.
3. Add an `ask` card "Approve the plan" with options `approve` (recommended) and `change`, and a link
   to the ticket. It is a full card (RUNBOOK.md, "A decision card"): its walk-through explains the
   plan's slices. Add no tasks yet.

## Sync (every pass)

- While the plan card is open, do nothing else for this autopilot.
- Answer `change`: rewrite `plan.md` from the user's words and add a new card.
- Answer `approve`: map each slice to a task (`id`, `title` in both languages, `depends_on`,
  `status: todo`, `link`: the ticket URL, `needs`, `critical`, `phase`) and run `ap tasks sync`. Keep
  local statuses as they move; slices never leave `plan.md`.
- Tickets: move the ticket to In Progress when the first slice starts and to Done when every slice is
  done. Never create tickets; a slice that deserves its own ticket is a card.

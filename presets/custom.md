# Source: defined at init

`source.custom` holds the user's three answers from `init`:

- `where`: where the tasks can be seen (a board, a file, a query).
- `ready_when`: how to tell that a task can start.
- `done_where`: where and how to mark a task done.

## Sync (every pass)

1. Read the tasks from `where` with the tools that place needs.
2. Map each one to a task: a stable `id` from that place, `title` in both languages, `link`,
   `depends_on` from what `ready_when` says, `status` from that place.
3. Run `ap tasks sync <ap> <file>`.
4. When a task merges, mark it as `done_where` says. If that needs a tool the floor denies, add a card
   with an `action`.

If an answer is too vague to act on, add an `ask` card that quotes it and asks for the missing piece.

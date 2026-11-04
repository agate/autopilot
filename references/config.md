# Settings

Every global setting, its default and what it does. `ap config list` shows this install's values
next to the defaults and marks the ones you changed with `*`. `ap config set key=value` changes one
(dotted keys for nested settings, values as JSON); `ap config reset key` goes back to the default.
Only changed settings are saved, so a new default in a later version reaches you unless you changed
that setting.

| Setting | Default | What it does |
|---|---|---|
| `max_workers` | `5` | Most workers running at once, across all autopilots. |
| `budget.window_pct` | `85` | Start no new work once a usage window reaches this percent. |
| `budget.stale_min` | `60` | Usage numbers older than this many minutes count as unknown: one new worker at a time. |
| `budget.credit_pct` | `90` | Start no new work once extra credits reach this percent of the monthly limit. |
| `budget.resume_after_min` | `5` | Minutes to wait after a usage window resets before work starts again. |
| `forced_pass_min` | `30` | Run a dispatcher pass at least this often, in minutes, even when nothing changed. |
| `gate_interval_s` | `60` | How often the background gate wakes, in seconds. Takes effect after `ap install`. |
| `reply_check.every_s` | `60` | How often to read answers from the page, in seconds. 0 turns it off. |
| `reply_check.quiet` | `["22:00", "06:00"]` | No answer reads between these two times (this computer's clock); the forced pass picks answers up then. |
| `reply_check.model` | `"haiku"` | Model for the answer read. |
| `reply_check.timeout_s` | `120` | Give up on one answer read after this many seconds. |
| `report_times` | `["09:00", "17:00", "21:00"]` | When reports are written, local time. An autopilot can set its own. |
| `port_base` | `7100` | First port handed to workers; each worker gets a block of 10. |
| `archive_after_days` | `7` | Answered decisions older than this many days move to the archive. |
| `permission_mode` | `"auto"` | Claude Code permission mode for the dispatcher and workers. |
| `page_host` | `"claude.ai"` | Where the page is published. |
| `models.dispatcher` | `null` | Model for dispatcher passes; null uses Claude Code's default. |
| `models.worker` | `null` | Model for workers; null uses Claude Code's default. |
| `worker_effort` | `"xhigh"` | Effort level for workers. |
| `dispatch_timeout_s` | `1800` | Stop a dispatcher pass that runs longer than this many seconds. |
| `worktree_root` | `"~/worktrees"` | Folder where task worktrees are created. |
| `reader_lang` | `"en"` | The reader's language for the page and reports; set at the first init. |
| `tools.claude` | `null` | Path to claude that the gate uses; `ap install` records it. |
| `tools.gh` | `null` | Path to gh that the gate uses; `ap install` records it. |
| `tools.git` | `null` | Path to git that the gate uses; `ap install` records it. |
| `tools.python3` | `null` | Path to python3 that the gate uses; `ap install` records it. |

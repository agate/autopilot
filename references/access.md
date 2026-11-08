# Access checks

Before each pass the gate checks one system access, the one checked longest ago, with a 20-second
timeout. It never starts a login. A broken access opens one `system` card at the top of the page
(what broke, since when, what it blocks, the fix); the next check that passes closes it and records
`access_restored` in the events.

Checked for every autopilot:

| Check | How | Fix shown |
|---|---|---|
| `gh` | `gh auth status` exits 0 | `gh auth login` |
| `page` (when `page_host` is `claude.ai`) | `claude auth status --json` says logged in with claude.ai | `claude auth login` |
| `mcp:<name>` for each MCP server the passes use | `claude mcp get <name>` in the repo says Connected | `/mcp` → the server → Authenticate |

The MCP servers the passes use are found by `ap perms write <ap>`: the servers whose tools the
dispatcher floor allows (the tracker). Two keys in the autopilot's own `config.json` add more:

- `mcp_servers`: more server names to check, e.g. `["Docs"]`.
- `health_checks`: checks of your own, each `{"name": "...", "command": "...", "fix": "...",
  "blocks": "..."}`. `command` is a shell command run in the repo that exits 0 when access works,
  and should be quick and read-only (it has 20 seconds, and the gate's environment, so give tools
  by absolute path). `fix` is the line shown on the card; `blocks` says what stops working, in the
  reader's language (a string, or `{"en": "...", "<lang>": "..."}`). Example, a cloud role:

  ```json
  "health_checks": [
    {"name": "cloud-dev", "command": "/usr/local/bin/aws sts get-caller-identity --profile <profile>",
     "fix": "<the command that refreshes the login>", "blocks": "Deploy and log checks."}
  ]
  ```

Keep profile names and account details in your data folder's `config.json`, never in the package.
`ap access list <ap>` lists the checks; `ap access check <ap>` runs the one that is due, and
`ap access check <ap> <check>` runs that one now (exit 0 when it works). Both print the result and
open or close the card.

The gate's own page refresh (the Haiku session that keeps the page fresh between passes) opens a
`Page refresh` card the same way after 3 failed refreshes in a row. There is nothing to run: it
retries on its own after 5, 10, 20, 40, then every 60 minutes, and at once when it could not start
its session and the reply check has started one since. Passes keep publishing the page meanwhile,
and the next refresh that works closes the card.

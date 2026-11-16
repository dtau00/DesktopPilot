# MCP coverage

One file per app, `<app>.md`, for an app that `pilot` drives and that has a
Claude plugin or MCP server. It says which of the tasks we do in that app
the server can do, so an agent knows which steps go to the server and which
to `pilot`. It is not a plugin: a plugin is code that drives the GUI, and a
task the server covers needs none.

`scripts/mcp_check.py check` says when one is out of date.

## Format

```markdown
# <app>: <server>

- server: <name in the Claude Code config, or the connector's name>
- source: config | connector | builtin
- fingerprint: `sha256:...`
- tested: YYYY-MM-DD
- customer text: not allowed | allowed
- acts on: <what the server reads and writes: the open window, a cloud copy, ...>

| task | path | result | gaps |
| --- | --- | --- | --- |
| Read the open document's text | server | full | |
| Click a ribbon button | pilot | none | no UI tools |
| Fetch a fixture file | server, setup only | full | cloud copy, not the open window |
```

- **source**: `config` is a server in `.mcp.json` or `~/.claude.json`;
  `mcp_check.py` asks it itself. `connector` (claude.ai) and `builtin`
  (Claude in Chrome) keep their auth inside Claude, so an agent dumps the
  tool list for the check.
- **fingerprint**: from `mcp_check.py fingerprint`. Empty means the table
  is not tested yet.
- **customer text**: `not allowed` unless the user said a server may see
  customer documents. A server that is not allowed never gets a customer
  document, its text or its terms, in a test or anywhere else.
- **path**: `server`, `pilot`, or `server, setup only` (setup, teardown
  and checks, never the step under test).
- **result**: `full`, `partial`, `none`, or `untested`.
- **gaps**: what an agent would get wrong: it acts on a cloud copy and not
  the open window, needs a refresh before `pilot` sees the change, runs as
  another account.

The task list comes from what we do in the app, not from the server's
tools. The header list ends at the table: `mcp_check.py` reads only the
lines above it.

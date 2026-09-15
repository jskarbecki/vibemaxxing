# vibemaxxing

Manage several Claude Code accounts and see **pooled** plan usage across all of them —
how much weekly headroom you have in total, and roughly when you run dry.

macOS is first class. Linux works. Windows is unsupported in v1, and says so rather than
half-working. Python 3.11+.

```
$ vibe list
* work      jan@intra-ai.de   max
    Session                   60%
    Weekly · all models       68%
    Weekly · Fable            77%  (warning)

  personal  other@example.com max
    Session                    4%
    Weekly · all models       12%

pool  1.43 account-weeks across 2 accounts  ·  dry in 31.5h
```

## Install

```
uv tool install vibemaxxing     # or: pipx install vibemaxxing
```

Two console scripts, the same program: `vibemaxxing` and `vibe`.

## Use

```
vibe                      open the dashboard
vibe add                  adopt the login already in Claude Code (no network)
vibe add <alias>          log in to another account in the browser
vibe list                 every account, its limits, and the pooled headroom
vibe switch <alias>       make one account the active Claude Code login
vibe run <alias> -- <cmd> run one command pinned to one account
vibe remove <alias>       forget an account
vibe alias <old> <new>    rename an account
vibe usage --once         fetch once and print
vibe usage web            serve the dashboard on http://127.0.0.1:8787
```

`--json` works on every command and emits one documented envelope, the same one the web
dashboard serves at `/api/usage`. The schema is frozen in
[`docs/CONTRACT.md`](docs/CONTRACT.md) §10.

## How it works

It swaps **credentials**, nothing else. One `~/.claude` history and one MCP config,
shared by every account, exactly as if you had logged in and out by hand. On macOS the
active credential is Claude Code's own Keychain item; on Linux it is
`~/.claude/.credentials.json`.

Tokens refresh on demand about five minutes before expiry, behind a consume gate: a
refresh rotates the refresh token, so the successor is written to a stash on disk
**before** the predecessor is treated as spent. A crash mid-refresh leaves a recoverable
successor rather than a dead account.

It talks to three hosts and no others — `claude.ai`, `platform.claude.com`,
`api.anthropic.com` — and refuses to contact anything else, redirects included. No
version pings, no analytics, no telemetry.

## Known limitations

These are real and deliberate. None of them is a bug report.

**The macOS Keychain write puts the credential in `argv`.** `vibe switch` shells out to
`/usr/bin/security add-generic-password -w <blob>`, and macOS shows any local user a
process's full argv through `ps`. For the length of one `exec`, the credential is readable
cross-uid. The alternatives were measured and are worse: `security -i` silently truncates
at ~4005 bytes and stores the truncated value — the real blob here is 4877 bytes and
growing — and the Security framework refuses to read Claude Code's item from a process
that is not in its ACL, which would put an authorization prompt in front of every switch.
Multi-user macOS is out of scope for v1.

**The store is plaintext JSON at rest.** `~/.vibemaxxing/accounts/*.json` holds your
tokens, mode `0600`, directories `0700`. Anyone who can read your files can read your
tokens — which is also true of `~/.claude` itself.

**`vibe run` has two limits.** It sets `CLAUDE_CODE_OAUTH_TOKEN` in the child environment
only, so the global credential is untouched and two shells can hold two accounts at once.
But the child cannot refresh that token itself, so a session outliving the access token
needs a re-run; and the variable bypasses account OAuth entirely, so the child does not
read the Keychain at all. Every grandchild inherits it.

**The web dashboard binds `127.0.0.1` and nothing else.** No authentication, no TLS —
which is exactly why. `--host` with anything else is a hard error.

**The poll scheduler is not wired to the dashboards.** `poll.Scheduler` implements the
stagger and the `60/120/240/480` backoff, and is tested, but no live request goes through
it. The dashboards instead poll every 180 s, which keeps them inside the endpoint's
measured ~28-30 requests/identity/hour budget. See `docs/AUDIT.md`.

## Uninstall

```
uv tool uninstall vibemaxxing
rm -rf ~/.vibemaxxing
```

That is everything it creates. Your Claude Code login is untouched; whichever account was
active stays active.

## Development

```
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```

[`docs/CONTRACT.md`](docs/CONTRACT.md) is the frozen design and the authority on every
question of intent. [`docs/AUDIT.md`](docs/AUDIT.md) is the finding register.
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) is the operator's walkthrough.

MIT. Jan Skarbecki <jan@intra-ai.de>

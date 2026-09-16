# Contributing

Thanks for looking. Issues and pull requests are both welcome.

## Setup

```
git clone https://github.com/jskarbecki/vibemaxxing
cd vibemaxxing
uv sync
```

## The one command

CI runs exactly this, in this order. Run it before you open a pull request:

```
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```

Every test runs offline. There are no credentials in CI and none are needed: the HTTP
client and the keychain are both injectable ports, and the suite passes fakes.

## Before you write

Read [`docs/CONTRACT.md`](docs/CONTRACT.md). It is the frozen design, and it is the
authority on any question of intent — not preference, and not this file. It explains why
things that look odd are the way they are, which will save you writing a patch that gets
turned down for a reason nobody wrote down anywhere else.

A few rules from it that catch people out:

- **A token value lives only inside a `Secret`.** `.reveal()` may be called in exactly
  five places, listed in §5. A sixth fails the leak test.
- **The `--json` envelope is a contract.** `vibe list --json` and `GET /api/usage` emit
  the same bytes because they call the same builder. Adding a key means updating §10.
- **No new dependencies** without asking first. The runtime set is `textual` and the
  standard library, deliberately — §1.
- **`mypy --strict`, and no silencing.** No `Any`, no `type: ignore` to get a build
  green.

## Pull requests

Small and focused beats large and comprehensive. Say what changed and why in the
description; if the change touches behaviour a doc describes, update that doc in the same
pull request.

If you are unsure whether something is wanted, open an issue first and ask. That is
cheaper for both of us than a rejected branch.

## Reporting a bug

Include your OS, your Python version, `vibe --version`, and the exact command you ran.
**Never paste a token, a `~/.vibemaxxing/accounts/*.json` file, or an unredacted
`~/.claude.json`.** Output from `vibe` is scrubbed before it is printed, so pasting what
it showed you is safe; pasting the files it reads is not.

Security issues go to [`SECURITY.md`](SECURITY.md) instead, not to the issue tracker.

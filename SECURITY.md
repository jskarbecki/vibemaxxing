# Security

## Reporting

Report privately, not in the issue tracker:

- Use GitHub's [private vulnerability reporting](https://github.com/jskarbecki/vibemaxxing/security/advisories/new), or
- email **jan@intra-ai.de**.

Include what you did, what happened, and what you expected. A proof of concept helps.
Expect a first reply within a week. There is no bounty; this is a one-person project.

**Never include a real token in a report.** If a token was exposed while you were
testing, treat it as compromised and run `/login` in Claude Code to rotate it.

## What this program holds

OAuth credentials for every account you add: `~/.vibemaxxing/accounts/*.json`, mode
`0600`, inside directories mode `0700`. They are **plaintext JSON at rest**. That is a
stated design decision, not a vulnerability — see "Known limitations" in the README —
and it matches how `~/.claude` already stores the same material.

Reports that amount to "the store is not encrypted" or "the macOS Keychain write puts
the credential in argv" are already documented and will be closed as known. Both are in
the README and in [`docs/CONTRACT.md`](docs/CONTRACT.md); the argv one is there with the
measurements of why the alternatives are worse.

## What is in scope

- A token reaching stdout, stderr, a log, a traceback, the `--json` envelope, the web
  dashboard, or the SQLite history. Everything that leaves the process is scrubbed and
  there are tests for it; a path that escapes the scrubber is a real finding.
- A request to any host other than `claude.ai`, `platform.claude.com` or
  `api.anthropic.com`, including via a redirect.
- The web dashboard listening anywhere other than `127.0.0.1`.
- One account's token being written under another account's alias, or a refresh
  destroying a credential without a recoverable successor.
- Path traversal out of the store through an alias.
- A store file or directory created with permissions wider than `0600` / `0700`.

## What is not

- Anyone with read access to your user account reading your files. Same threat model as
  `~/.claude`.
- Multi-user macOS. Out of scope for v1, stated in the README.
- Anything about Claude Code or the Anthropic API itself. Report those to Anthropic.

## Supported versions

The latest release on PyPI. There are no backports.

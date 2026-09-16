# Changelog

Notable changes per release. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `vibe help`: a command that explains what each command is for and when to use it.
  Before it existed, `vibe help` answered `invalid choice: 'help'` and then listed the
  commands it had just refused to explain.
- The web dashboard shows **the week ahead** beside the account ledger: one lane per
  account, a marker where that account's weekly limit resets, seven days from right now.
  Times render in the reader's own timezone, and day boundaries are local midnights, so
  the grid does not drift across a DST change.
- Every account's plan (`max 20x`, `pro`) is shown in the dashboard's ledger.
- A top-level `resets` array in the `--json` envelope, listing each account's weekly
  rollover soonest first. Documented in `docs/CONTRACT.md` §10.

### Fixed

- **Login failed at the token exchange with HTTP 403.** Cloudflare fronts all three
  allowed hosts and answers urllib's default `Python-urllib/3.x` user agent with
  `error code: 1010` before the request reaches the API. Every request now names a real
  user agent.
- **Login failed at the Authorize click with "Invalid request format".** The PKCE
  `state` was 22 characters; the authorize endpoint accepts the consent page but rejects
  the submission below 43. The failure only surfaced at the last step, which is what made
  it look like a redirect problem.
- **`vibe switch` changed the credential but not the account.** Claude Code caches the
  profile in `~/.claude.json` under `oauthAccount` and trusts it for 24 h, so it ran on
  the new token while still showing the previous account's email, org and limits. The
  switch now repoints that object and drops `profileFetchedAt`, which makes Claude Code
  refetch the rest itself. No priming needed on a machine that has never run it.

## [0.1.0] - 2026-09-15

First release. Multi-account store, OAuth login with PKCE, credential switching through
the macOS Keychain or `~/.claude/.credentials.json`, `vibe run` for per-command accounts,
a Textual dashboard, a loopback web dashboard, and a documented `--json` envelope.

Published to TestPyPI only, to prove the release pipeline.

[Unreleased]: https://github.com/jskarbecki/vibemaxxing/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jskarbecki/vibemaxxing/releases/tag/v0.1.0

# Changelog

Notable changes per release. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Auto-switch no longer moves onto an account within 5 points of the threshold.** An account
  at 98% passed as headroom, crossed 99% minutes later, and the 15-minute cooldown then held
  you there at the limit. Such an account is not a candidate; with no other, the tick stays
  (`no account with headroom`). `CONTRACT.md` §17.
- **`vibe auto tick` no longer stays stuck when `active` and the live login disagree.**
  After a `claude /login` the pointer named an account Claude Code no longer ran, so that
  account's token was never refreshed and lapsed (`token expired - vibe leaves the active
  account's token to Claude Code`) and auto-switching never ran again. The tick now
  switches onto the live account itself, which repairs the pointer without losing the live
  login. `CONTRACT.md` §17.
- A usage `401` or `403` said `run: vibe list`, the command that had just produced it. It now
  says the token was refused and names `vibe add <alias>`.

## [0.3.0] - 2026-10-22

### Added

- **`vibe auto`: switch accounts by itself.** With `vibe auto on`, a check
  (`vibe auto tick`, the unit a scheduler calls) leaves the live account for the one with
  the most room once its session or weekly row reaches 99%. It only acts on numbers it has,
  does not switch onto an account whose login lapsed or was rejected, or onto a stale one
  or one at its limit, waits 15 minutes between switches, does not overwrite a login vibe
  does not know (while every stored account has an account id), stays while `active` and
  the live login disagree, and prints one line (`stay: …` or `switched a -> b: …`) with
  exit 0 either way. Also `status`, `exclude`/`include`, `undo`,
  `off`, and on macOS `install`/`uninstall`, which write and remove the launchd job and
  print the `launchctl` line instead of running it. Settings live in
  `~/.vibemaxxing/settings.json`; an invalid file means the defaults and never stops
  `vibe list`. `docs/CONTRACT.md` §17.
- The live account is polled about every one to two minutes, not every three, once a session
  or weekly row is at 90%, and only while no backoff is running. That can exceed the ~28-30
  requests an hour measured for one account; a 429 is obeyed.
- A `auto_switch` block in the `--json` envelope (`enabled`, `threshold`, `last_switch`),
  and a line in the TUI footer and on the web page, both read-only. Additive; the schema is
  still 1.
- `scripts/verify_auto_live.py`: a real switch between two throwaway accounts on a real
  Keychain, refusing to run without `VIBE_LIVE_OK=1`, restoring the Keychain, `active` and
  `~/.claude.json` on any failure. Never run from pytest.

### Fixed

- **`vibe switch` could overwrite a login vibe did not know and lose it.** When
  `~/.claude.json` named an account vibe had never stored (someone ran `claude /login`), the
  resync was skipped but the Keychain was still written. It now refuses, with `vibe add`,
  which adopts that login.
- **`vibe switch` could put a dead account in the Keychain and log Claude Code out.** It
  now refuses an account whose login has lapsed or was rejected, with `vibe add <name>`, and
  refreshes an expired access token first to be sure.
- **A stale `active` pointer lost the live account's rotated token.** `active` is only what
  vibe last wrote; when Claude Code ran as another stored account, the switch skipped its
  resync and overwrote it. The resync now goes to the account that owns the login, and
  `vibe switch <that account>` repairs the pointer without loss.
- **A dead refresh token was posted on every poll.** `invalid_grant` was never kept, so a
  dashboard left open spent a request a minute on each dead account. It is now recorded on
  the account and not posted again; a fresh `vibe add` clears it.

### Changed

- `vibe switch` to an account whose access token has expired makes one request, so that case
  no longer works offline.

### Not verified

- What a switch does to a `claude` session that is already running, and whether an old
  session refreshing its own token overwrites the Keychain afterwards.
- A real Keychain switch (`scripts/verify_auto_live.py` is the check) and a loaded launchd job.

## [0.2.2] - 2026-09-17

### Fixed

- **The active account showed "the login has lapsed" hours after `vibe add`, and could
  log Claude Code out.** Claude Code refreshes the active account's token in the Keychain
  and the server rotates the refresh token each time, but the dashboard kept refreshing
  its own stored copy of the same lineage. Whichever refreshed second held a spent token.
  Nothing refreshes the active account any more, checked again under the refresh lock
  that `vibe switch` now also holds. The dashboard and `vibe run` use the Keychain's token
  when `~/.claude.json` names the same account, else the stored one, until it expires.
  `vibe switch` and `vibe alias` hold the refresh locks of both accounts and refuse while
  one is held.
- **`vibe switch` could put a spent token into the Keychain.** A refresh interrupted after
  the server rotated the token leaves the live successor in a stash; switching to that
  account wrote the account file's spent predecessor instead. The stash now goes in.
  `CONTRACT.md` §8.
- A non-UTF-8 `~/.claude/.credentials.json` no longer crashes every poll on Linux, nor a
  non-UTF-8 Keychain item on macOS.

## [0.2.1] - 2026-09-16

### Fixed

- **Accounts showed `the usage endpoint answered HTTP 429` instead of their limits.**
  Every process polled on its own timer, a rate-limited account was polled again on the
  very next cycle, and a 429 threw away the numbers fetched a minute earlier. Usage
  answers now live in one cache per account under `~/.vibemaxxing/usage/`, shared by the
  web dashboard, the TUI and `vibe list`: an answer under ~3 minutes old is reused
  without a request, a 429 or 5xx backs that account off at 60/120/240/480 s (or the
  server's `Retry-After`, capped at an hour), and while it waits the account keeps showing
  its last numbers, up to an hour old, with the time they are from.
- An account without a known plan no longer asks the profile endpoint on every poll.
- A usage answer carrying a non-finite percentage no longer fails every account's view.
- **Two dashboard tabs could double the request rate.** The web cache checked freshness
  under its lock but fetched outside it, so two tabs whose timers had drifted together
  both fetched. The fetch now happens under the lock.

## [0.2.0] - 2026-09-16

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

[0.2.2]: https://github.com/jskarbecki/vibemaxxing/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jskarbecki/vibemaxxing/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jskarbecki/vibemaxxing/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jskarbecki/vibemaxxing/releases/tag/v0.1.0

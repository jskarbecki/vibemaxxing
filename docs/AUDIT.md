# Audit — Phase F

Four lenses per round, each handed the whole tree, deduplicated by a stable id of
`<lens>:<repo-relative path>:<rule-slug>`. A lens rewording a prior finding is not a new
finding. The sweep stops after two consecutive rounds that surface no previously-unseen id,
capped at four rounds.

**Rounds run: 4 of 4. Converged: NO — hit the cap.**
Distinct findings: 47 (3 high, 24 medium, 20 low).

New ids per round: round 1 = 21, round 2 = 9, round 3 = 8, round 4 = 9.
The sweep was still producing new ids in round 4, so the cap was reached rather than
convergence. Anything not marked fixed below is open, and why is stated on its row.

## Findings per lens

- **credential-leak** — 11
- **cross-platform** — 9
- **memory-and-resource** — 10
- **spec-conformance** — 17

## Register

### `credential-leak:src/vibemaxxing/httpclient.py:redirect-bypasses-host-allowlist`

| | |
|---|---|
| severity | **high** |
| location | `src/vibemaxxing/httpclient.py:81` |
| round | 1 |
| status | **FIXED** |

**UrllibClient checks ALLOWED_HOSTS only on the initial URL, and urllib's default opener follows 3xx redirects while forwarding every request header — so the Authorization: Bearer access token is re-sent to any host the upstream names, including over plaintext http://.**

*Failure scenario.* api.anthropic.com answers GET /api/oauth/usage with 302 Location: http://collector.example/x. urllib.request.urlopen (default opener, HTTPRedirectHandler installed) builds the follow-up request; in CPython 3.13.5 redirect_request strips only content-length and content-type, so Authorization survives verbatim and travels to a host that was never compared against ALLOWED_HOSTS. Proven on loopback: with ALLOWED_HOSTS = {'127.0.0.1'} and usage.USAGE_URL pointed at a 302'ing loopback server, the second, unlisted server received `Authorization: Bearer sk-ant-oat01-PROBE-SECRET-VALUE`. The contract (s5, httpclient) says "Allowed hosts, and no others"; today the allowlist is enforced for exactly one hop. The redirect is also invisible to the caller — HttpResponse carries the final 200, so nothing in the CLI, TUI or web dashboard reports that the token left the allowlist.

*Fix.* Give UrllibClient its own opener that refuses redirects instead of using the global one: `class _NoRedirect(urllib.request.HTTPRedirectHandler):` with `redirect_request` returning `None`, `_OPENER = urllib.request.build_opener(_NoRedirect)`, and call `_OPENER.open(request, timeout=timeout_s)` at line 81. A 3xx then surfaces through the existing `except urllib.error.HTTPError` arm as HTTPError(30x, body), which classify_refresh_error already treats as transient.

### `credential-leak:src/vibemaxxing/store.py:switch-resync-attributes-the-keychain-blob-to-the-active-alias`

| | |
|---|---|
| severity | **high** |
| location | `src/vibemaxxing/store.py:321` |
| round | 2 |
| status | **FIXED** |

**store.switch's resync writes whatever credential the Keychain currently holds into the account file named by the `active` pointer, with no check that the blob actually belongs to that account, so one account's tokens land in another account's file and the displaced refresh token is destroyed.**

*Failure scenario.* Two accounts: `work` (identity a@x.com, uuid-A) and `personal` (identity b@x.com, uuid-B), with `active` = personal and the Keychain holding personal's credential. The user's session lapses and they run `claude /login` outside vibe, logging back into the *work* account; the Keychain now holds work's blob while `active` still says `personal`. The next `vibe switch work` runs store.switch:321, which does `write_account(root, replace(read_account(root, 'personal'), credential=parse_blob(base_raw)))` — it writes work's accessToken and refreshToken into accounts/personal.json, next to b@x.com's identity. Reproduced against the shipped code with tests.fakes.FakeKeychain seeded with A's blob and active='personal': afterwards `personal` reads back `identity=b@x.com, refresh=A-REFRESH-TOKEN-AAAA, access=A-ACCESS-TOKEN-AAAA`, and a byte scan of the whole store for `B-REFRESH-TOKEN-BBBB` returns False — account B's refresh token exists nowhere on disk and only a fresh browser login recovers it. From then on `vibe switch personal` writes A's credential into the Keychain and `vibe run personal -- claude` runs as A, while `vibe list` and both dashboards print b@x.com's email and organisation beside A's usage — exactly the "one account's name paired with another account's token" hazard envelope._claude_identity (envelope.py:50-58) guards against on the display path but nothing guards on the write path. Contract s8 step 2 mandates the resync but only ever reasons about the case where the Keychain holds the outgoing account's own rotated token; tests/test_store.py:89 pins only that happy case (Keychain holds `a`, active is `a`), so nothing in the suite exercises a mismatch.

*Fix.* In store.switch, before the resync on line 321, skip it when the live identity is known and disagrees: read `credentials.read_claude_identity(Path.home() / '.claude.json').account_uuid` and only resync when it is None or equal to `read_account(root, outgoing).identity.account_uuid`. Known-and-disagrees is the only case suppressed, so an account whose uuid was never captured keeps today's behaviour and no rotation is lost.

### `cross-platform:src/vibemaxxing/store.py:alias-namespace-collides-on-case-insensitive-fs`

| | |
|---|---|
| severity | **high** |
| location | `src/vibemaxxing/store.py:49` |
| round | 1 |
| status | **FIXED** |

**The alias namespace is compared case-sensitively in code but is stored as a filename, so on macOS's case-insensitive APFS two aliases differing only in case are one file and the second add silently destroys the first account's refresh token.**

*Failure scenario.* Verified with the shipped code on this macOS machine (/tmp is case-insensitive; os.replace onto a differently-cased name overwrites the existing entry and keeps its name). Sequence: `vibe add jan` writes accounts/jan.json. Later a bare `vibe add` adopts a login whose ~/.claude.json emailAddress is "Jan@intra-ai.de"; cli._slug preserves case, cli._free_alias (cli.py:101) compares "Jan" against store.list_aliases() == ["jan"] case-sensitively and reports it free, and store.write_account -> fsutil.write_private -> os.replace(tmp, accounts/Jan.json) overwrites accounts/jan.json. I ran exactly this against store.write_account: after the second write, list_aliases() is still ['jan'], the only file is accounts/jan.json, and read_account(root,'jan').credential.refresh_token is the second account's. The first account's refresh token is gone with no message and no way to see two accounts ever existed; on Linux/ext4 the same commands produce two independent accounts. Second symptom from the same root cause: `vibe switch Work` succeeds on macOS (read_account opens work.json) where Linux raises NotFoundError, and write_active stores "Work" while list_aliases yields "work", so envelope.collect marks no account active - the `*` in `vibe list`, the TUI's `●` and the _claude_identity enrichment all silently disappear.

*Fix.* Make the alias namespace case-insensitive at the one boundary that owns it: compare alias.casefold() in cli._free_alias and in rename_account's `account_path(root, new).exists()` check, and have write_account refuse a new alias whose casefold matches a different existing alias. Smallest version: lowercase in cli._slug and tighten _ALIAS_RE to ^[a-z0-9][a-z0-9._-]{0,63}$, so the on-disk name is unambiguous on every filesystem.

### `credential-leak:src/vibemaxxing/cli.py:adopt-duplicates-an-already-adopted-credential`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:122` |
| round | 2 |
| status | **FIXED** |

**`vibe add` with no alias never checks whether the Keychain credential it is adopting is already stored under an existing alias, so running it twice writes the same refresh token into two account files, and the first rotation under either alias silently kills the other's login.**

*Failure scenario.* `vibe add` (bare) is the primary onboarding command (contract s15). Run it twice against the same Claude Code login: cli._adopt reads the blob, and cli.py:122 asks _free_alias for a name, which only compares the *slug* against store.list_aliases — never the credential or identity.account_uuid. Verified end-to-end through cli.main(['add']) twice with one FakeKeychain blob: aliases become ['jan', 'jan-2'], both account files hold refreshToken `A-REFRESH-TOKEN-AAAA`, both carry identity jan@intra-ai.de, and active is silently moved to jan-2. Because a refresh rotates the refresh token server-side, the twins then destroy each other: verified by seeding accounts/jan.json and accounts/jan-2.json with one shared expired credential and driving oauth.refresh(root, 'jan-2', ...) against a 200 carrying a successor — afterwards jan-2.json holds NEW-REFRESH while jan.json still holds SHARED-REFRESH-AAAA, which the server has already killed, so the next use of `jan` earns invalid_grant and envelope._usable_credential drops it to needs_login with 'run: vibe add jan'. Until that happens both aliases fetch usage with the same token, so pool_remaining sums the same real account twice and the envelope reports `"accounts": 2` with double the real account-weeks — the headline number the product exists to report.

*Fix.* In cli._adopt, before writing, scan store.list_aliases(ctx.root) and, if some existing account's credential equals the parsed one (Credential is a frozen dataclass and Secret.__eq__ compares values) or its identity.account_uuid matches, re-point `active` at that alias and emit `_action('add', alias=<existing>, adopted=True)` instead of creating a duplicate.

### `credential-leak:src/vibemaxxing/fsutil.py:shared-temp-name-breaks-atomic-write`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/fsutil.py:25` |
| round | 4 |
| status | **FIXED** |

**write_private derives its temp name deterministically from the destination and opens it without O_EXCL, so two concurrent writers of the same credential file share one inode: the loser's stale fd writes straight into the committed file and tears it, and its own os.replace then fails outside the cleanup guard.**

*Failure scenario.* Proven deterministically against the shipped helper on a tmp store. Writer A does os.open(accounts/work.json.tmp, O_WRONLY|O_CREAT|O_TRUNC), writer B opens the same name (O_TRUNC truncates A's file), A writes the rotated successor, fsyncs, and os.replace()s it into accounts/work.json. B's fd now refers to the inode that IS the committed account file, so B's write lands at offset 0 of accounts/work.json; with B's document shorter than A's the tail of A's bytes survives and the file reads back as '{"schema": 1, "alias": "work", "credential": {"refreshToken": "PRED"}}\n": "SUCCESSOR-SSS..."\n  }\n}\n'. store.read_account(root, "work") then raises StoreError '/…/accounts/work.json is not valid JSON' — the account is unusable and the credential is unrecoverable, so the only route back is a fresh browser login. B's own os.replace(tmp, path) then raises FileNotFoundError (ENOENT); line 36 sits outside the `except BaseException: tmp.unlink()` guard, so nothing cleans up and nothing retries. Stress on the shipped write_private, two threads x 400 writes to one path: 397 of 400 os.replace calls failed with FileNotFoundError. The widest product trigger is store.claim_refresh's lapsed-takeover branch (store.py:283), which the code's own comment documents as concurrent by design — both losers of the O_EXCL race read the same lapsed lease and then both call write_private on locks/<alias>.claim at the same instant; that raw OSError escapes claim_refresh (called unguarded at oauth.py:179), oauth.refresh, envelope._usable_credential and collect, so `vibe list` dies on a traceback with exit 1 instead of a §4 error. accounts/<alias>.json and stash/<alias>.json take the same hit whenever two surfaces refresh one alias on the stash-recovery branch (oauth.py:174-200), which takes no claim at all. Limit of my evidence: 60 attempts driving two threads through oauth.refresh did not land a natural collision — the write is a handful of fast syscalls — so the mechanism and the blast radius are proven but the arrival rate is not.

*Fix.* Make the temp name unique per writer and exclusive: `tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")` opened with os.O_EXCL, and move `os.replace(tmp, path)` inside the try so a failed commit still hits the `except BaseException: tmp.unlink(missing_ok=True)` arm.

### `credential-leak:src/vibemaxxing/oauth.py:stash-path-releases-a-claim-it-never-took`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/oauth.py:214` |
| round | 3 |
| status | **FIXED** |

**refresh()'s stash-recovery path never calls claim_refresh, but the finally at oauth.py:213-214 calls release_refresh unconditionally, so it deletes the in-flight claim another process is holding and re-admits a third refresher that is carrying the now-spent predecessor.**

*Failure scenario.* Reproduced end to end against a tmp store with the shipped code, and against a counterfactual for contrast.

Setup (the alias `work`, account file holding PRED-REFRESH): process B finds no stash, takes the claim (`locks/work.claim` = {"until_ms": 1757930030000}), POSTs, and writes the successor to `stash/work.json` (oauth.py:197). Contract s9 says no lock is held across the POST, so B's claim legitimately stands for its whole lease. Process C has already done its `store.read_account` for this cycle and therefore holds PRED-REFRESH in memory -- in `envelope._view` that read happens before `_usable_credential`, and `web.snapshot` runs a whole `collect()` per `GET /api/usage` on its own handler thread with nothing serialising them, so two tabs or a reload make the overlap routine.

Process A now enters `refresh` for the same alias. `store.read_stash` at oauth.py:174 returns B's successor, so the entire `if successor is None:` block -- including `claim_refresh` at line 179 -- is skipped. A falls into the try at 202, writes the account, deletes the stash, and the `finally` at 213 runs `store.release_refresh(root, alias)` on a claim A never took. Measured: `A outcome: None, requests made: 0`, and `B's claim after A runs: False`.

C then refreshes. Buggy run: `claim still held by B: False`, C sends 1 request whose body carries `refresh_token: PRED-REFRESH` -- the token the server killed when it issued B's successor -- gets HTTP 400 {"error":"invalid_grant"}, `classify_refresh_error` returns "invalid_grant", and `envelope._usable_credential` answers `the login for "work" has lapsed - run: vibe add work`, i.e. AccountState.NEEDS_LOGIN, a permanent verdict that sends the user through a fresh browser login while a perfectly good successor sits in the account file. Counterfactual run with the claim intact: `claim still held by B: True`, C sends 0 requests, verdict `could not refresh the token (busy) - run: vibe list` -- a transient ERROR retried on the next cycle. That difference is the whole point of the gate.

The trigger window is not only the sub-millisecond gap between B's write_stash and delete_stash. Contract s9's `RefreshOutcome(successor, "transient", stashed=True)` path (oauth.py:206-210) deliberately leaves the stash on disk across runs when write_account fails, so after one ENOSPC every subsequent refresh for that alias takes the no-claim path indefinitely -- both skipping the gate and stripping it from anyone else. This also defeats the mitigation the recorded TUI finding relies on ("the claim gate hands one of them busy"): with a stash present it does not.

*Fix.* Bind the release to the acquisition instead of to the function. Set `claimed = False` before line 174, `claimed = True` immediately after the successful `claim_refresh` at 179, and make the finally `if claimed: store.release_refresh(root, alias)`. The stash path then reads and consumes the durable successor without touching another process's lock.

### `credential-leak:src/vibemaxxing/oauth.py:stash-write-outside-the-release-guard`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/oauth.py:197` |
| round | 1 |
| status | **FIXED** |

**store.write_stash on line 197 sits outside both the except arms above it and the try/finally below it, so an I/O failure there loses the server-issued successor entirely and leaves the claim file held for the full 30 s lease.**

*Failure scenario.* The disk is full (or the store is on a read-only/over-quota mount) when a token refresh lands. _post_token returns a 200 carrying the successor, the server has already killed the predecessor, then write_stash raises OSError. Verified against a tmp store with write_stash stubbed to raise OSError('No space left on device'): refresh propagated the OSError, `locks/work.claim` was still present holding `{"until_ms": ...}`, `stash/work.json` did not exist, and the account file still held the now-dead predecessor. The successor exists nowhere on disk, so the next run posts the spent predecessor, earns invalid_grant, and the account drops to needs_login requiring a fresh browser login — the exact outcome contract s9 says the stash exists to prevent ("A crash at any point leaves a recoverable successor on disk"), and the alias is additionally wedged at "busy" for the lease, which is the failure the finally on line 213 was written to avoid.

*Fix.* Move line 197 inside the existing try/finally: start the `try:` before `store.write_stash(...)` so the `finally: store.release_refresh(root, alias)` on lines 213-214 covers it, and add `except OSError: return RefreshOutcome(successor, "transient", stashed=False)` so the caller gets the usable successor instead of an escaping OSError.

### `credential-leak:src/vibemaxxing/redact.py:scrub-raises-under-concurrent-registration`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/redact.py:52` |
| round | 4 |
| status | **FIXED** |

**scrub iterates the module-global _registry with no lock while Secret.__init__ inserts into and evicts from the same OrderedDict on other threads, so the one redaction helper raises RuntimeError under the concurrency the web dashboard ships with, and the fallback lands on socketserver.handle_error — the one output boundary scrub does not cover.**

*Failure scenario.* Measured: one registrar thread constructing Secrets plus two threads calling scrub() over a 200-entry registry — both scrubbers died inside 3 s with RuntimeError('OrderedDict mutated during iteration') raised by the `{form for group in _registry.values() for form in group}` comprehension at redact.py:52 (register() at redact.py:45-48 does `_registry[value] = …` and `popitem(last=False)`). The product has exactly this shape: ThreadingHTTPServer answers each GET /api/usage on its own thread, and web.snapshot runs a whole collect() per request (web.py:109-116) constructing two Secrets per account read plus one per refresh successor, while another handler thread is inside envelope.dumps -> scrub (envelope.py:213). Two browser tabs, or one tab plus a reload, is enough. web._usage catches the first failure and retries with `dumps(error_payload(_failure(exc)))` (web.py:74) — that second dumps is unguarded, so when it raises too the exception escapes do_GET into socketserver.BaseServer.handle_error, which prints an unscrubbed traceback.print_exc() to stderr and drops the connection with no response body at all. In the TUI the same collision between an overlapping _tick and action_refresh_now cycle (the to_thread worker registering while the event loop scrubs in account_content) turns a healthy poll into 'refresh failed (RuntimeError) — run: vibe list'. It fails closed today — nothing is written when scrub raises — so this is the loss of contract §5 rule 4's last line of defence rather than a leak by itself, but it is lost precisely in the multi-threaded surface where a token is likeliest to reach a string by an unanticipated route, and it routes output to the one boundary that never scrubs.

*Fix.* Add a module-level threading.Lock and take it around both the mutation in register() and the snapshot in scrub() (`with _lock: groups = list(_registry.values())`, then build `forms` outside the lock); separately, wrap web.py:74's second dumps so a scrubber failure still yields a body.

### `cross-platform:src/vibemaxxing/cli.py:ctrl-c-abandons-vibe-run`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:278` |
| round | 1 |
| status | **FIXED** |

**cmd_run lets KeyboardInterrupt escape, so the first Ctrl-C tears down the `vibe run` wrapper with a traceback instead of waiting for the child and returning its exit code.**

*Failure scenario.* Verified on macOS/CPython 3.13.5 by launching cli.main(["run","work","--",python,child.py]) in its own process group and sending SIGINT to the group, which is exactly what a terminal Ctrl-C does to a foreground group on macOS and Linux alike. The child installed its own SIGINT handler and kept working (what `claude` does on a single Ctrl-C to interrupt generation), but subprocess.run re-raised KeyboardInterrupt after _sigint_wait_secs (0.25 s), main() catches only VibeError, and the process died with a full traceback printed through the installed excepthook and an exit status of -2 (shell reports 130). Neither the child's exit code (contract s15) nor the `--json` action envelope {"action":"run","exit_code":...} (contract s10) is ever produced, and the wrapper stops supervising a child that is still alive.

*Fix.* Wait the child out instead of dying with it: replace subprocess.run with `proc = subprocess.Popen(command, env=child_env)` and loop `try: code = proc.wait(); break / except KeyboardInterrupt: pass` (the child received the same SIGINT and decides when to exit), then keep the existing envelope emit and `return code`.

### `cross-platform:src/vibemaxxing/cli.py:dashboard-launched-without-a-tty-hangs-forever`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:217` |
| round | 4 |
| status | **FIXED** |

**Bare `vibe` launches the Textual dashboard with no check that stdin is a terminal, so any non-interactive stdin (ssh without -t, cron, a pipeline, a CI step) produces a process that never exits, never reports anything, and leaves the caller's terminal in alt-screen/mouse-reporting mode.**

*Failure scenario.* Measured on this machine with the shipped console script and a throwaway HOME. `vibe` with stdin=/dev/null, and again with a closed pipe as stdin, was still running after 8 s in one probe and after 120 s in another; both times stdout was empty and stderr had received raw terminal control sequences (`\x1b[?1049h\x1b[?1000h\x1b[?1003h\x1b[?1015h\x1b[?1006h\x1b[?25l...` — alt screen on, mouse tracking on, cursor hidden) followed by the rendered `no accounts yet — run: vibe add` line. Both had to be SIGKILLed. Driving the same path through the library with two seeded accounts and file-backed stdout/stderr, the app was still alive after 12 s having written **zero** bytes to either stream and never reaching `on_mount` (no `history.db` was created; the injected client recorded 0 requests), so the hang can also occur before the first cycle with nothing at all to explain it. `cmd_dashboard` (cli.py:215-217) branches only on `args.json` — `return cmd_list(args, ctx) if args.json else tui.run(ctx)` — so the only non-TUI escape is `--json`, and that flag is silently dropped in the pre-subcommand position (recorded separately as `spec-conformance:src/vibemaxxing/cli.py:global-json-flag-silently-ignored`), leaving `vibe --json` broken as an escape too. Concretely: `ssh box vibe` (ssh without `-t` hands the child a non-tty stdin) wedges the ssh session until it is killed from another shell; the same for a Makefile target, a cron entry, a systemd unit, or a CI step that calls `vibe` bare. Because the escapes go to **stderr**, `vibe > /dev/null` does not suppress them: they still switch the calling terminal into the alternate screen and enable mouse reporting, and nothing restores it when the process is killed. Contract §4 requires every user-facing failure to name the problem and a literal recovery command; this one names nothing and never terminates.

*Fix.* Widen the guard at cli.py:217 to cover the non-interactive case: `return cmd_list(args, ctx) if args.json or not sys.stdin.isatty() else tui.run(ctx)` — a non-interactive caller then gets the one-shot listing it almost certainly wanted. Raising `UsageError("vibe needs a terminal for the dashboard", "vibe list")` instead is equally acceptable and matches §4.

### `cross-platform:src/vibemaxxing/keychain.py:mac-keychain-path-is-never-executed-by-any-check`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/keychain.py:34` |
| round | 3 |
| status | OPEN |

**MacKeychain.read/write — the only code that reads and writes the credential on the first-class platform — is executed by nothing: no test can call it (the autouse guard blocks any argv0 named `security`), and scripts/verify_switch_live.py, the live round trip contract §2 assigns to Phase G, does not exist, so the macOS CI job is green having never run a line of it.**

*Failure scenario.* `grep -rn MacKeychain tests/` matches only tests/test_keychain.py:14 (the import) and :40 (`isinstance(default_port(), MacKeychain)`); nothing anywhere calls `.read()` or `.write()`, and nothing in tests/ can: tests/conftest.py:25 sets `BLOCKED_BINARIES = ("security",)` and conftest.py:46-53 raises RealPathAccessError from the patched subprocess.run/Popen for any argv0 whose basename is `security` — behaviour tests/test_guard.py:24 pins as required. The designated escape hatch is the one file the contract names for exactly this, §2's module map line `scripts/verify_switch_live.py  G  live Keychain round trip — never run from pytest`. It does not exist: `git ls-files | grep -i script` returns nothing, `find . -name 'verify_switch_live*'` returns nothing, and `grep -rln 'verify_switch|live Keychain|never run from pytest'` over the whole tree matches only docs/CONTRACT.md itself. So `uv run pytest -q` passes 81 tests on macOS (verified, 13.57 s) and the macos-latest matrix leg differs from ubuntu-latest only in which never-executed branch `default_port()` would have returned. Contract §8 pins five exact behaviours of those unrun lines — the `find-generic-password -a <user> -w -s <service>` argv, exit 44 meaning absent, stripping exactly one trailing newline, the `add-generic-password -U … -w <blob>` write, and the AC10 resync-then-overwrite order against a real port — and none has ever been observed against /usr/bin/security; tests/test_store.py's AC10 ordering test drives tests.fakes.FakeKeychain. The gap is load-bearing, not theoretical: `cross-platform:src/vibemaxxing/keychain.py:mac-keychain-decodes-with-the-process-locale` is a defect in precisely these lines (keychain.py:85-93 passes bare `text=True`), and it shipped through v0.0.1–v0.0.5 with a green macOS matrix every time. By contrast FileKeychain, the Linux port, is exercised end to end on both OSes by tests/test_keychain.py:19-33.

*Fix.* Add scripts/verify_switch_live.py as §2 specifies: a standalone script outside `testpaths = ["tests"]`, so pytest never collects it and the autouse guard never applies, that does one MacKeychain write-then-read round trip of a throwaway blob under a throwaway service name (never `Claude Code-credentials`), asserts byte equality including a non-ASCII member, and prints what it did. Run it by hand before each release and paste its output, as §16 rule 4 requires for the check command.

### `cross-platform:src/vibemaxxing/redact.py:non-utf8-stdout-crashes-human-output`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/redact.py:92` |
| round | 2 |
| status | **FIXED** |

**Every human-mode surface writes non-ASCII glyphs (— · «» █░─) straight to sys.stdout/sys.stderr with the default strict codec, so on any host whose stdout encoding is not UTF-8 the command dies with a raw UnicodeEncodeError traceback instead of its output or its VibeError.**

*Failure scenario.* Reproduced on this machine with the shipped console script and a throwaway HOME: `HOME=$tmp LC_ALL=en_US.UTF-8 .venv/bin/vibe list` prints `no accounts yet — run: vibe add`, exit 0; `HOME=$tmp LC_ALL=en_US.ISO8859-1 .venv/bin/vibe list` prints a traceback ending `File "src/vibemaxxing/redact.py", line 92, in out / sys.stdout.write(scrub(text)) / UnicodeEncodeError: 'latin-1' codec can't encode character '—' in position 16: ordinal not in range(256)`, exit 1. The store was empty, so this is the very first command a new user runs. `LC_ALL=C` is safe (CPython turns on UTF-8 mode, verified: utf8_mode 1), but a real legacy locale is not: `LC_ALL=en_US.ISO8859-1` gives `sys.stdout.encoding = iso8859-1`, `LC_ALL=ja_JP.eucJP` gives `euc_jp`, and `PYTHONIOENCODING=ascii` gives ascii — all verified here. The blast radius is every human surface, not one string: cli.py:64/65/73 render `—` for a missing email, plan or percent; envelope.py:65 puts `—` in the needs_login message; usage.py:22 labels the weekly row `Weekly · all models`, so even a fully healthy `vibe list` fails under ascii or eucJP; tui.py:70 emits `█░─` (verified to raise under latin-1); and redact.py:21's `«redacted»` marker itself cannot be written under ascii, so the one path whose job is to report a scrubbed value is the one that cannot print. `--json` is unaffected (json.dumps defaults to ensure_ascii=True) and so is the web page (body.encode() defaults to utf-8), which is why the suite and CI never see this — every test drives the API or a capsys buffer, never a real non-UTF-8 stream. macOS forces UTF-8 for the filesystem and ships a UTF-8 terminal by default; Linux is where a legacy LANG still arrives, via ssh LANG forwarding, a CJK desktop, or an old distro image.

*Fix.* One line at the single write boundary: in `install_excepthook()` (already called first thing in `cli.main`), add `for stream in (sys.stdout, sys.stderr): stream.reconfigure(errors="backslashreplace")`. TextIOWrapper.reconfigure is stdlib and present on both platforms; the dash degrades to `—` instead of killing the command.

### `cross-platform:tests/conftest.py:guard-misses-pathlib-reads`

| | |
|---|---|
| severity | **medium** |
| location | `tests/conftest.py:62` |
| round | 1 |
| status | **FIXED** |

**The AC19 autouse guard patches builtins.open, os.open, sqlite3.connect and subprocess, but every read in the package goes through Path.read_text, which calls io.open and is never intercepted.**

*Failure scenario.* Verified: with builtins.open replaced by a recorder, Path('/tmp/x').read_text() produced no hit (pathlib calls io.open, a separate binding to the same builtin). store.read_account, store.read_active, store._claim_lapsed, credentials.read_claude_identity and keychain.FileKeychain.read all read via Path.read_text, so a test that exercises any of them without the tmp_home fixture reads the developer's real ~/.claude.json or ~/.claude/.credentials.json - two of the three BLOCKED_PATHS - and the guard stays silent. On Linux CI the same test passes because those files do not exist there, so the hole can never surface in CI; on a macOS dev box it silently reads live credentials. tests/test_guard.py only exercises builtins.open/sqlite3.connect/subprocess.run, so it passes with the hole open. No test trips it today; the defect is that the net advertised by AC19 does not catch the package's actual read path.

*Fix.* Patch io.open alongside builtins.open in guard_live_paths (save and restore both, pointing them at the same guarded wrapper) and add a `Path(...).read_text()` case to tests/test_guard.py so the hole cannot reopen.

### `memory-and-resource:src/vibemaxxing/poll.py:scheduler-never-wired-into-a-surface`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/poll.py:27` |
| round | 1 |
| status | OPEN |

**poll.Scheduler is instantiated nowhere in src/, so neither long-lived surface enforces MIN_GAP_S spacing or the 60/120/240/480 backoff that contract section 14 and AC16 define, and the web dashboard has no poll floor at all.**

*Failure scenario.* `grep -rn 'Scheduler' src/` matches only poll.py itself; `grep -rn 'import poll|poll\.' src/` matches nothing. TUI with 5 accounts: `tui.set_interval(60, _tick)` (tui.py:153) calls `envelope.collect`, which loops all 5 aliases back-to-back in one `to_thread` call (envelope.py:139-144), so 5 usage requests leave ~0.2 s apart, not the 10 s MIN_GAP_S that AC16 pins as a global guarantee. Each account then sits at 60 requests/hour, over the 28-30/hour budget section 14 measured, so 429s arrive; `envelope._view` turns each into an ERROR view and the very next tick retries the same account 60 s later — BACKOFF_S is never consulted, so a rate-limited account is re-hammered at the same cadence instead of stepping out to 480 s. The web dashboard is worse: `web.snapshot` runs a full `collect()` per `GET /api/usage` (web.py:109-116) with no floor, so two browser tabs double the per-account rate, a page reload adds another, and because index.html's `setInterval(load, 60000)` (index.html:310) never awaits the previous fetch, a cycle slower than 60 s (5 accounts x a 10 s usage timeout is 50 s before any refresh POST) leaves requests overlapping — each one its own ThreadingHTTPServer thread, socket and full fan-out.

*Fix.* Own one `poll.Scheduler` per long-lived surface (built in `tui.Dashboard.on_mount` and in `web.serve`), drive it from a tick that fetches the single alias `next_due` returns and reuses the cached view for the rest, and feed `record_success` / `record_error` / `record_exhausted` from the per-account outcome `envelope._view` already computes. If the scheduler is genuinely not wanted, delete poll.py and amend contract section 14 rather than shipping an unwired subsystem that AC16 tests in isolation.

### `memory-and-resource:src/vibemaxxing/tui.py:first-refresh-blocks-the-message-pump`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/tui.py:152` |
| round | 4 |
| status | **FIXED** |

**`on_mount` awaits the whole first account fan-out on the App's message pump before installing the timer, so for the length of that fan-out the dashboard is a blank window that queues every keystroke — including `q` and Ctrl-C — instead of acting on it.**

*Failure scenario.* Measured on this machine against the shipped code (headless `App.run_async`, 3 seeded accounts, an injected client whose `request` blocks 4 s, `tui.REFRESH_S` untouched). `on_mount` runs `self._conn = history.connect(...)` then `await self._refresh_or_report()` at tui.py:152; `envelope.collect` loops the aliases back to back, so the mount handler is held for 3 x 4 s = 12.0 s. At t = 2.0 s I sampled the live app: `app.panels == []`, `#empty` is `display:none` by the app CSS, so the screen is blank apart from the empty footer bar, and `app.is_running` is True. I posted `events.Key("q")` at t = 2.00 s; the app exited at t = 12.03 s — the key sat in Textual's queue for 10.03 s because `MessagePump._process_messages` dispatches one message at a time and `on_mount` was still awaiting. Ctrl-C is no escape either: Textual's `LinuxDriver._patch_lflag` clears `termios.ISIG` unless `TEXTUAL_ALLOW_SIGNALS` is set (verified in textual 8.2.8), so Ctrl-C arrives as a key event and queues behind the same handler; the only way out is SIGKILL from another terminal. With the shipped timeouts this is not a 12 s window: `usage.fetch_usage` defaults to `timeout_s=10.0` and `oauth._post_token` uses `httpclient.DEFAULT_TIMEOUT_S=10.0`, and `envelope.collect` is serial, so a 5-account store whose tokens are inside the 5-minute expiry buffer gives a worst case of 5 x 20 s = 100 s of a blank, uninterruptible `vibe`. The module's own comment at tui.py:183-184 states the opposite as the design — "collect() refreshes tokens and fetches usage: off the event loop, or the dashboard stops answering keys for the length of a timeout" — and `asyncio.to_thread` does deliver that for every later cycle, because `Timer._tick` awaits the callback in the timer's own task (verified in textual 8.2.8) rather than on the pump. The first cycle is the one path that never gets the guarantee, and it is the one the user meets first. No test covers it: `run_test()` and the AC14 test both wait for mount to complete before doing anything.

*Fix.* Install the timer first and start the first cycle off the pump: in `on_mount`, `self.set_interval(REFRESH_S, self._tick)` followed by `self.run_worker(self._refresh_or_report())` instead of `await self._refresh_or_report()`. `run_worker` runs the coroutine in its own task, so the mount handler returns immediately and keys are answered from the first frame, exactly as they are on every later cycle.

### `memory-and-resource:src/vibemaxxing/tui.py:history-conn-lifecycle-is-unguarded`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/tui.py:151` |
| round | 2 |
| status | **FIXED** |

**on_mount opens the one long-lived sqlite connection before installing the refresh timer and on_unmount closes it unconditionally, so any failure to open it leaves a dashboard that never refreshes and then dies on quit with an AttributeError that replaces the real cause.**

*Failure scenario.* Verified on this machine with a ~/.vibemaxxing/history.db that sqlite cannot open (I used a file holding plain text; the same class of failure is a full disk -> sqlite3.OperationalError 'database or disk is full', or an unwritable db -> I reproduced 'attempt to write a readonly database' with the file at 0o400). Sequence: tui.py:151 `self._conn = history.connect(...)` raises, so line 152's first cycle and line 153's `set_interval(REFRESH_S, self._tick)` are never reached and `self._conn` is never assigned (line 143 is a bare annotation, not an assignment). Textual stores the error in `App._exception` and keeps the app alive: measured `running=True, panels=[], has_conn=False`, an empty window with an empty footer and no timer, forever. Pressing `r` reaches refresh_cycle, which dies at tui.py:187 `history.record(self._conn, ...)` and the footer prints `refresh failed (AttributeError) - run: vibe list` - the wrong diagnosis, and the printed recovery also dies (I ran `cli.main(["list"])` on the same store: a raw sqlite3.DatabaseError escapes, since cli.main catches only VibeError). On quit, `on_unmount` (tui.py:156) runs `self._conn.close()` on the missing attribute, and the real cause is lost: `Dashboard(ctx).run()` raised `AttributeError: 'Dashboard' object has no attribute '_conn'` while `app._exception` still held `file is not a database`. The user gets a scrubbed AttributeError traceback naming nothing that points at history.db.

*Fix.* Assign `self._conn: sqlite3.Connection | None = None` in __init__ instead of the bare annotation on line 143, guard on_unmount with `if self._conn is not None`, and move `set_interval` above the first `_refresh_or_report()` (or catch the connect failure and report it through the footer) so a store that cannot be opened does not take the timer with it.

### `memory-and-resource:src/vibemaxxing/tui.py:refresh-binding-has-no-floor`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/tui.py:177` |
| round | 1 |
| status | **FIXED** |

**The `r` binding calls a full refresh cycle per key event with no debounce or in-flight guard, and Textual's message queue is unbounded, so a held key queues an arbitrary number of complete network fan-outs.**

*Failure scenario.* Measured: 5 seeded accounts, 40 `events.Key("r")` posted to the app, then one pause — 200 usage requests reached the client (5 per account per press), zero spacing, versus 5 for a normal cycle. `MessagePump._message_queue` is a bare `asyncio.Queue()` with no maxsize (textual/message_pump.py), and `App._on_key` awaits `run_action` inline on the pump, so every repeat a terminal sends while `r` is held becomes one more queued full cycle that will execute back-to-back after the key is released. Separately, `Timer._tick` awaits the callback in the timer's own task, not on the message pump, so the 60 s `_tick` and a manual `action_refresh_now` can hold two `refresh_cycle` coroutines in flight at once — both mutating `self._ctx.now_s`, both awaiting inside `_reshape`, and both driving `oauth.refresh` for the same alias through `to_thread` (where the claim gate hands one of them `busy`). A user holding `r` for three seconds on a 5-account dashboard issues several hundred requests against an endpoint budgeted at ~30/hour.

*Fix.* Guard the entry point: keep the timestamp of the last completed cycle and an `_in_flight` flag on `Dashboard`, and have `action_refresh_now` return immediately when a cycle is running or when fewer than `POLL_FLOOR_S` seconds have passed — or route both the timer and the binding through the single `poll.Scheduler` from the previous finding, which already owns `MIN_GAP_S` and the floor.

### `memory-and-resource:src/vibemaxxing/web.py:failed-fetch-recorded-as-a-real-pool-sample`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/web.py:114` |
| round | 1 |
| status | **FIXED** |

**A cycle in which accounts failed to fetch still writes `pool_weeks(views)` into the durable samples table, so a transient 429 or timeout is stored as a genuine collapse of the pool and poisons `dry_in` for as long as it stays in the window.**

*Failure scenario.* `envelope._view` maps an `HTTPError` (the 429 contract section 14 explicitly predicts under a 60 s floor) to `AccountState.ERROR` with no summary; `pool_remaining` skips every non-OK account (pool.py:30-32), so `pool_weeks` drops by up to 1.0 account-week per failed account. `web.snapshot` (web.py:114), `tui.refresh_cycle` (tui.py:187) and `cli._collect_and_record` (cli.py:181) all pass that number straight to `history.record` unconditionally. Concrete: 3 healthy accounts at 2.4 account-weeks, one minute where two of them answer 429 — the row written is 0.8. `dry_in` takes `min`/`max` by `at_s` over the returned window, so that row as the newest gives `rate = (2.4 - 0.8)/60` and a forecast of about 30 s ("dry in 0.0h") while the pool is actually untouched; as the oldest it makes `rate` negative and the forecast silently disappears. The row survives until 2000 newer samples push it out of the `SAMPLE_LIMIT` window (about 33 hours at one sample a minute) or 90 days of retention elapse. Concurrency makes it routine rather than rare: two overlapping `GET /api/usage` requests each run their own `collect()` on their own handler thread with nothing serialising them (the Recorder lock covers only the two sqlite calls), so when a token is inside the 5-minute expiry buffer one thread takes the claim and the other gets `RefreshOutcome(None, "busy")`, which `envelope._usable_credential` renders as ERROR — a pure artifact of our own concurrency, made durable.

*Fix.* Record only a sample that means what the column says: in `web.snapshot`, `tui.refresh_cycle` and `cli._collect_and_record`, skip the `history.record` call unless every view is `AccountState.OK`. A gap in the series is honest and `dry_in` already works off timestamps rather than sample spacing, so it needs no other change.

### `spec-conformance:src/vibemaxxing/cli.py:global-json-flag-silently-ignored`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:297` |
| round | 1 |
| status | **FIXED** |

**`--json` is defined on both the top-level parser and every subparser, so `vibe --json <cmd>` is accepted and then silently overwritten to False by the subparser default, emitting human text on a run the caller asked to be machine-readable.**

*Failure scenario.* `vibe --json list` exits 0 printing `no accounts yet — run: vibe add` instead of the section 10 envelope (verified). `vibe --json switch nope` prints `there is no account named "nope" / run: vibe list` on stderr and exits 4 instead of the section 10 error envelope on stdout (verified). A script doing `vibe --json list | jq .` gets a parse error with exit 0 from vibe and no signal that the flag was dropped. Contract section 15: "`--json` is accepted on every command that reports state ... and it is the only output on stdout." No test covers the flag in the pre-subcommand position.

*Fix.* Drop `_add_json(parser)` and instead put the shared flag on a `parents=[json_parser]` passed to every subparser, or have `main` read `--json` from a pre-parse (`parse_known_args`) so the parent value is not clobbered. Either way add a test asserting `vibe --json list` and `vibe list --json` emit identical stdout.

### `spec-conformance:src/vibemaxxing/cli.py:loopback-recovery-names-a-nonexistent-flag`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:211` |
| round | 1 |
| status | **FIXED** |

**The non-loopback refusal prints `vibe usage --web --host 127.0.0.1` as its recovery command, but `--web` was replaced by the `web` positional, so the command it tells the user to paste exits 2 with `unrecognized arguments: --web`.**

*Failure scenario.* `vibe usage web --host 0.0.0.0` exits 2 and prints `run: vibe usage --web --host 127.0.0.1` (verified). Pasting that line back gives `usage: vibe [-h] [--version] [--json] {add,list,switch,run,remove,alias,usage} ... / vibe: error: unrecognized arguments: --web`, exit 2 (verified). Contract section 4 requires `recovery` to be a command the user can paste, and section 15 pins `web` as a positional. Commit fcb05de shipped the `--web` spelling; the positional replaced it and this string was left behind. AC13 only asserts the word "loopback" appears, so the test passes over the broken half of the message.

*Fix.* Change the recovery string to `f"vibe usage web --host {LOOPBACK}"`, and extend the AC13 assertion to check the recovery parses, e.g. `build_parser().parse_args(recovery.split()[1:])`.

### `spec-conformance:src/vibemaxxing/cli.py:relogin-does-not-clear-the-alias-stash`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:158` |
| round | 3 |
| status | **FIXED** |

**`vibe add <alias>` over an existing alias writes the fresh credential but leaves `stash/<alias>.json` from the old lineage, so the next refresh consumes that stale successor and destroys the credential the browser login just issued.**

*Failure scenario.* Reproduced end-to-end on a tmp store. (1) `work` is expired; a refresh succeeds server-side and `write_stash` lands `S1-REFRESH`, then `store.write_account` fails with `OSError("No space left on device")` — the contract-s9 path, which returns `RefreshOutcome(successor, "transient", stashed=True)` and leaves `stash/work.json` holding `S1-REFRESH`. (2) The user re-logs in: `cli.main(["add","work"])` completes the paste-the-code flow, `_login` calls `store.write_account` at cli.py:158, and `accounts/work.json` now holds the server-issued `C2-REFRESH` / `C2-ACCESS`. The stash is untouched: `store.read_stash(root,"work").refresh_token.reveal()` still reads `S1-REFRESH`. (3) When `C2-ACCESS` next expires, `oauth.refresh` reads the stash first (oauth.py:174) and never POSTs — measured `len(client.requests) == 0`, outcome error `None` — and writes `S1-REFRESH` / `S1-ACCESS` into `accounts/work.json`, silently replacing the brand-new login with a credential from the dead lineage. `store.delete_account` (store.py:207-210) carries the comment naming exactly this hazard — "A stash left behind would be consumed as a successor if the alias were re-added, handing the new login a refresh token the server killed weeks ago" — so `remove` then `add` is safe and `add` over the same alias is not. The path is reachable rather than theoretical because the recorded finding `run-reports-a-transient-refresh-as-needs-login` prints `run: vibe add work` on exactly the transient failure that leaves the stash, walking the user into it.

*Fix.* Reset the alias's derived state in `_login` the way `delete_account` does — `store.delete_stash(ctx.root, alias)` and `store.release_refresh(ctx.root, alias)` around the `write_account` at cli.py:158 — so a fresh login never inherits the previous lineage's stashed successor or a live claim.

### `spec-conformance:src/vibemaxxing/cli.py:remove-reports-success-for-an-unknown-alias`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:238` |
| round | 3 |
| status | **FIXED** |

**`vibe remove <alias>` exits 0 and emits `"ok": true` for an alias that does not exist, where every other alias-taking command raises `NotFoundError` and exits 4.**

*Failure scenario.* Verified against the shipped code on a tmp store holding one account `work`: `cli.main(["remove","wrok"])` printed `removed "wrok"`, returned exit 0, and `store.list_aliases(root)` was still `['work']`; `cli.main(["remove","wrok","--json"])` printed the action envelope `{"schema":1,"ok":true,"action":"remove","alias":"wrok"}` and returned 0. For contrast, on the same store `cli.main(["switch","wrok"])` and `cli.main(["alias","wrok","x"])` both returned 4 with `there is no account named "wrok"`. `cmd_remove` calls `store.delete_account`, whose first line is `account_path(root, alias).unlink(missing_ok=True)`, so a typo is indistinguishable from a real removal. Contract s4 maps an unknown alias to `NotFoundError` / code `not_found` / exit 4, and s10 says the action envelope reports what the command acted on — here it names an account that never existed. A user who typos an alias is told the account is gone while its credential is still on disk at `accounts/work.json`, and a script gating on exit 0 or on `ok` records a removal that did not happen. No test covers `remove` with an unknown alias (tests/test_cli.py:146 removes an alias it just created).

*Fix.* In `cmd_remove`, resolve the alias before deleting — `store.read_account(ctx.root, args.alias)` already raises `NotFoundError` with `run: vibe list` — or have `store.delete_account` return whether a file was actually unlinked and let `cmd_remove` raise `NotFoundError` when it was not.

### `spec-conformance:src/vibemaxxing/cli.py:run-reports-a-transient-refresh-as-needs-login`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:266` |
| round | 1 |
| status | **FIXED** |

**`_fresh_token` raises `NeedsLoginError` for every unsuccessful refresh outcome, so a transient network failure or a concurrent `busy` claim exits 3 and tells the user to re-run the browser login.**

*Failure scenario.* With `work`'s access token inside the expiry buffer and the token endpoint timing out, `vibe run work -- true` prints `could not refresh "work" (transient) / run: vibe add work` and exits 3 (verified). Contract section 4 maps a transient upstream failure to `NetworkError` / code `network` / exit 6, and reserves `needs_login` / exit 3 for a dead refresh lineage or a lapsed login. Following the printed recovery starts a fresh browser login and overwrites a perfectly healthy account over a network blip. `outcome.error == "busy"` (another vibe process mid-refresh) gets the same treatment. `envelope._usable_credential` classifies the same outcomes correctly, so the CLI and the dashboards disagree about the same account.

*Fix.* Branch on `outcome.error` the way `envelope._usable_credential` already does: `NeedsLoginError` only for `invalid_grant` / `no_refresh_token`, `NetworkError` for `transient` and `busy`, and a non-account-blaming `VibeError` for `invalid_client`.

### `spec-conformance:src/vibemaxxing/cli.py:run-strips-every-separator-from-the-child-argv`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/cli.py:271` |
| round | 1 |
| status | **FIXED** |

**`cmd_run` filters every `--` out of `args.command`, not just the leading separator, so any `--` the child itself needs is deleted from its argv.**

*Failure scenario.* `vibe run work -- python -c 'print(sys.argv[1:])' a -- b` runs the child with `['a', 'b']`, the interior separator gone (verified by executing it). Real damage: `vibe run work -- git log -- src/` becomes `git log src/`, which resolves `src/` as a revision instead of a path and fails or, worse, silently means something else; `vibe run work -- claude -p -- --foo` loses the guard that stops `--foo` being parsed by claude. Contract section 15: "everything after the alias is the child's, by `argparse.REMAINDER`." `test_run_pins_the_token_to_the_child_environment_only` only passes a command with the single leading `--`.

*Fix.* Strip only a leading separator: `command = args.command[1:] if args.command and args.command[0] == "--" else list(args.command)`. Add a case to the run test asserting an interior `--` reaches the child.

### `spec-conformance:src/vibemaxxing/envelope.py:refresh-storeerror-escapes-per-account-containment`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/envelope.py:102` |
| round | 1 |
| status | **FIXED** |

**`_usable_credential` is called outside `_view`'s `try`, so a `VibeError` raised while refreshing one account (notably `store.read_stash` on an unreadable or unknown-schema stash) escapes `collect` and takes down every account's view.**

*Failure scenario.* Two accounts, `healthy` and `stale`; `stale` has an expired access token and a `stash/stale.json` carrying a schema the build does not know. `vibe list` prints only `…/stash/stale.json has store schema 99, not 1 — it is never migrated silently / run: vibe add stale` and exits 5 — the healthy account and the pool line are never rendered (verified). The TUI shows `refresh failed (StoreError)` in the footer with no panels, and `/api/usage` answers 500 for the whole page. Contract section 12: "`envelope.collect` contains **both** failure kinds per account", which is what is supposed to keep one bad account from killing `vibe list`. A `SCHEMA_VERSION` bump would put every existing stash on this path at once.

*Fix.* Widen `_view`'s guard so the whole per-account body runs inside one `except VibeError` (and the existing `except HTTPError`), returning `AccountView(..., AccountState.ERROR, exc.render(), ...)`, rather than only wrapping the initial `store.read_account`.

### `spec-conformance:src/vibemaxxing/fsutil.py:atomic-write-shares-one-tmp-name`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/fsutil.py:25` |
| round | 4 |
| status | **FIXED** |

**Contract s6 states "Writes are atomic", but write_private derives the temp file from the target name alone, so two concurrent writers of the same path share one inode and can publish a spliced, unparseable file instead of one of the two versions.**

*Failure scenario.* write_private computes `tmp = path.with_name(path.name + ".tmp")` — a name that is a pure function of the target, with no pid, no mkstemp, no O_EXCL. Two writers of the same path therefore open the SAME inode with O_TRUNC, and os.replace publishes whichever one finishes its rename first while the other's fd keeps writing into the file that rename just created. Reproduced deterministically against the shipped helper on a tmp dir, using only its own primitives in the order two processes would interleave them: writer B wrote a 266-byte account JSON, fsynced and os.replace'd it; writer A's flush then landed on the now-live file through the fd it still held, writing 86 bytes at offset 0. The published accounts/work.json read back as A's 86 bytes followed by B's 180-byte tail — `json.loads` raised `Extra data: line 2 column 1 (char 86)` — and A's own `os.replace` then failed with FileNotFoundError. store._read_json turns that file into `StoreError("… is not valid JSON", "vibe add work")`, so the account's refresh token is destroyed and only a fresh browser login recovers it. The concurrency is the concurrency the contract itself designs for, and the claim gate does not cover it: (a) `active` has no gate at all, so two simultaneous `vibe switch` runs collide on `active.tmp`; (b) on Linux FileKeychain.write goes through the same helper, so those same two switches collide on `~/.claude/.credentials.json.tmp` and can corrupt the file Claude Code itself reads; (c) store.switch writes the outgoing account file (store.py:321) with no claim held, so it races a TUI or web refresh_cycle writing that same account from a rotated successor; (d) once a stash exists, oauth.refresh skips claim_refresh entirely (oauth.py:176), so two refreshers both reach write_account for one alias. This is not an already-documented trade-off: s6 prescribes the `<name>.tmp` spelling AND asserts atomicity, and the assertion is what is false. No test drives two writers at one path.

*Fix.* Make the temp name unique per writer inside fsutil.write_private — `fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")`, fchmod it 0o600, write/fsync/os.replace that name — so every caller (store, keychain, history) gets a genuinely atomic publish from the one shared helper.

### `spec-conformance:src/vibemaxxing/poll.py:scheduler-never-wired-into-a-surface`

| | |
|---|---|
| severity | **medium** |
| location | `src/vibemaxxing/poll.py:27` |
| round | 1 |
| status | OPEN |

**`poll.Scheduler` is imported only by its own test, so contract section 14's stagger, global min-gap and error backoff do not govern any request the product actually makes.**

*Failure scenario.* `grep -rn Scheduler src tests` matches only `src/vibemaxxing/poll.py` and `tests/test_poll.py`. Both live surfaces poll on a flat timer instead: `tui.Dashboard.on_mount` does `set_interval(REFRESH_S, self._tick)` and `refresh_cycle` calls `envelope.collect`, which loops every alias back to back, and `web` serves `/api/usage` from the same `collect` on each browser poll. With five accounts the TUI therefore fires five usage requests within milliseconds of each other every 60 s — `MIN_GAP_S = 10.0` ("global spacing between consecutive requests") is never applied outside the test. More concretely, an account answering 429 is re-polled every 60 s forever instead of backing off to 60/120/240/480 s, which is the exact behaviour section 14 freezes and the exact case section 12's note says `collect` was hardened for; and `record_exhausted` is never called by anything, so an account at 100 % keeps polling on the cycle instead of at its reset. AC16 passes because it drives the `Scheduler` directly.

*Fix.* Either drive both long-lived surfaces' fetches through a `Scheduler` (the TUI tick asking `next_due` and reporting `record_success` / `record_error` / `record_exhausted` per account), or, if a flat 60 s whole-store refresh is the intended product, amend contract section 14 to say so and delete `poll.py` rather than shipping a module the product never calls.

### `credential-leak:README.md:argv-exposure-not-stated-to-users`

| | |
|---|---|
| severity | **low** |
| location | `README.md:7` |
| round | 1 |
| status | OPEN |

**Contract s8 requires the cross-uid `security -w` argv exposure to be stated in the README and docs/RUNBOOK.md; the README is seven lines that mention none of it and docs/RUNBOOK.md does not exist, so a shipped, published tool puts a credential in argv with no user-visible notice.**

*Failure scenario.* A user installs vibemaxxing 0.0.5 (publish.yml ships it) on a shared macOS box and runs `vibe switch work`. For the duration of that exec the whole 4877-byte credential blob is readable by any local uid through `ps -ww`. Contract s8 decided that exposure is acceptable only on the condition that it is "stated rather than hidden" and named the two files it must be stated in; keychain.py:57-64 carries the call-site comment, but nothing a user reads does. The same gap covers s15's two documented `vibe run` limits (the child cannot refresh the token; CLAUDE_CODE_OAUTH_TOKEN bypasses account OAuth and is inherited by every grandchild). The README's "Full documentation lands at v0.1.0" defers docs generally, which is why this is low rather than higher — but s8 singles this item out as must-state rather than must-eventually-document.

*Fix.* Add a "Known limitations" section to README.md with three bullets — the argv window on `vibe switch`, the plaintext-0600 at-rest store, and the two `vibe run` limits — and create docs/RUNBOOK.md with the same text. No code change.

### `credential-leak:src/vibemaxxing/oauth.py:stash-can-hold-the-predecessor-refresh-token`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/oauth.py:130` |
| round | 1 |
| status | OPEN |

**When the token endpoint omits refresh_token, _credential_from_token carries the predecessor's refresh token into the "successor", which store.write_stash then writes to stash/<alias>.json — contradicting the contract's stated invariant that the stash only ever holds a server-issued successor.**

*Failure scenario.* The token endpoint answers a refresh with {"access_token": ..., "expires_in": ...} and no refresh_token member. Line 127's isinstance check fails, line 130 reuses previous.refresh_token, and line 197 writes that value to the stash via credential_to_disk. Verified on a tmp store: with write_account stubbed to fail so the stash survives, stash/w2.json's credential.refreshToken read back as the seeded predecessor value PREDECESSOR-REFRESH-SENTINEL. Contract s13 pins AC12's scan on the opposite claim — "a stash only ever holds a server-issued successor, so the seeded sentinel appearing there would be a real bug" — and tests/test_leak.py::_scan_store asserts exactly that. The suite passes today only because no test drives a refresh whose response omits refresh_token; a live run against a non-rotating endpoint would fail AC12 with "stash/work.json holds a token".

*Fix.* Either make the omission an error in the refresh path — raise NetworkError("the token endpoint rotated no refresh_token", _RECOVERY) instead of falling back at lines 129-130 — or amend contract s13's AC12 exemption to name stash/*.json as carrying a legitimately-inherited predecessor. The first keeps the invariant the AC12 scan is built on.

### `credential-leak:src/vibemaxxing/redact.py:lru-evicts-by-registration-not-by-liveness`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/redact.py:41` |
| round | 2 |
| status | **FIXED** |

**The bounded registry's recency is the order of Secret construction and nothing else — reveal() never touches the LRU — so the stated guarantee that "the evicted entries are spent predecessors" is not enforced and a live token's forms can be evicted, after which scrub() silently stops redacting it.**

*Failure scenario.* redact.py:26-28 and contract s5 rule 5 both justify REGISTRY_MAX by asserting the evicted entries are spent predecessors. register() (redact.py:41-48) only does `_registry[value] = _forms(value); move_to_end(value)` and pops from the front, and Secret.reveal() (redact.py:70) — the one moment the code knows a value is live and about to cross a boundary — does not touch the registry at all, so nothing distinguishes a live token from a rotated one. Verified: construct Secret('LIVE-TOKEN-SENTINEL-0001'), then construct REGISTRY_MAX (256) further distinct Secrets; `'LIVE-TOKEN-SENTINEL-0001' in redact._registry` is False and `scrub('here is LIVE-TOKEN-SENTINEL-0001')` returns the raw token verbatim, while the Secret object is still live and still reveals that value. The reachable trigger today is narrow — envelope.collect re-reads every account each cycle, so ordinary 5-account TUI/web polling keeps every live value at the fresh end — but one collect() pass over more than 128 accounts registers 2 values per account and evicts the earliest accounts' tokens before the pass finishes, and any future code path that reveals a token without re-reading it is unguarded by construction. The consequence is the loss of the second layer of defence contract s5 rule 4 describes, not a leak on its own; it matters precisely when a primary leak (e.g. the confirmed redirect finding) puts a token into a string that scrub is the last thing standing between and the terminal.

*Fix.* Call register(self._value) from Secret.reveal() so the LRU's recency tracks use rather than construction, and correct the comment at redact.py:26-28 (and contract s5 rule 5) to say the registry evicts the least-recently-touched value, not "spent predecessors".

### `credential-leak:src/vibemaxxing/store.py:delete-account-leaves-orphaned-temp-credentials`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/store.py:207` |
| round | 4 |
| status | **FIXED** |

**delete_account removes accounts/<alias>.json, the stash and the claim but never the .tmp siblings write_private leaves behind when a process dies between os.open and os.replace, so `vibe remove` reports success while a refresh token stays on disk with nothing that will ever read or delete it.**

*Failure scenario.* Measured on a tmp store: seed stash/work.json.tmp holding refreshToken 'LEFTOVER-SUCCESSOR' (the state fsutil.write_private leaves when the process is SIGKILLed, the machine loses power, or os.replace fails for a non-ENOENT reason such as EROFS on a remounted volume), then call store.delete_account(root, 'work'). It returns cleanly — `vibe remove work` prints 'removed "work"' and exits 0 — and stash/work.json.tmp is the only file left under the root, still holding the token, verified by reading it back. It is invisible afterwards: list_aliases globs '*.json' so the alias is gone from `vibe list`, read_stash only ever opens stash/<alias>.json so it is never consumed, and nothing in the package sweeps temp files. The function's own comment at store.py:207-210 asserts the opposite — 'Everything keyed by the alias goes with it' — and AC12's _scan_store would flag exactly this file (only accounts/ is exempt), but no test ever creates one, so the suite is green with the hole open. The user's only visible signal is that a command that promises to forget an account has left a live refresh token in ~/.vibemaxxing.

*Fix.* In delete_account, unlink the temp sibling alongside each real path — `account_path(root, alias).with_name(f"{alias}.json.tmp").unlink(missing_ok=True)` and the same for stash_path and _claim_path — or, once fsutil uses unique temp names, glob and unlink `accounts/<alias>.json.*.tmp` and `stash/<alias>.json.*.tmp`.

### `cross-platform:src/vibemaxxing/cli.py:browser-open-blocks-on-a-headless-linux-console-browser`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/cli.py:151` |
| round | 4 |
| status | OPEN |

**`vibe add <alias>` calls `webbrowser.open` synchronously and before it prints the URL; on Linux CPython registers text-mode console browsers whenever `TERM` is set and `GenericBrowser.open` waits for the child, so a headless Linux login hands the terminal to lynx/w3m/links and blocks there, where the same call on macOS fires osascript and returns immediately.**

*Failure scenario.* `webbrowser.register_standard_browsers` in the CPython 3.13.5 this repo runs takes `if sys.platform == 'darwin': register("MacOSX", None, MacOSXOSAScript('default'))` on macOS — a non-blocking osascript — but on every other POSIX platform falls through to a block gated only on `if os.environ.get("TERM"):` (a sibling of the DISPLAY-gated `register_X_browsers()` call, not nested inside it) which registers `www-browser`, `links`, `elinks`, `lynx` and `w3m` as `GenericBrowser`, whose `open()` ends in `p = subprocess.Popen(cmdline, close_fds=True); return not p.wait()`. Demonstrated here by running CPython with `sys.platform` set to `"linux"`, `DISPLAY`/`WAYLAND_DISPLAY`/`BROWSER` unset, `TERM=xterm-256color`, and a stub executable named `www-browser` on `PATH`: `webbrowser._tryorder` came back `['www-browser']` and `webbrowser.open("https://claude.ai/oauth/authorize?code=true")` returned `True` only after 6.32 s — the full lifetime of the child, which had been writing to the shared terminal the whole time. In the product that call is `ctx.browser(url)` at cli.py:151, which runs *before* `err(f"opening {url}\n\nif the browser did not open, paste that URL yourself.\n")` at cli.py:152 and before `ctx.prompt(...)` at cli.py:153. So on a Debian/Ubuntu box reached over SSH with the `www-browser` alternative installed (w3m is the common default), `vibe add work` silently loses the terminal to a text browser rendering a JavaScript OAuth consent page it cannot execute, with no message on screen explaining what took over, and the paste prompt appears only once the user works out how to quit it. `DISPLAY` is absent on exactly the headless machines where paste-the-code is the only workable flow, and contract §9 ("There is no localhost callback. The flow is paste-the-code.") treats the browser call as decoration, which it is on macOS and is not on Linux. With no console browser installed and no DISPLAY the call is harmless — `_tryorder` is empty and `webbrowser.open` returns False — which is why nothing in CI sees this.

*Fix.* Swap cli.py:151 and cli.py:152 so the authorize URL is always printed before anything else can take the terminal, and make the open non-blocking on the platforms where it can block: skip `ctx.browser(url)` when `sys.platform != "darwin"` and neither `DISPLAY` nor `WAYLAND_DISPLAY` is set (the user pastes the URL that is now already on screen).

### `cross-platform:src/vibemaxxing/keychain.py:mac-keychain-decodes-with-the-process-locale`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/keychain.py:90` |
| round | 2 |
| status | **FIXED** |

**MacKeychain._run passes bare `text=True` with no `encoding=`, so `security` output is decoded with the process locale encoding while MacKeychain.write encodes the same blob into argv as UTF-8 — an asymmetric round trip that silently mangles any non-ASCII byte in the Keychain blob, while the Linux FileKeychain reads and writes explicit UTF-8.**

*Failure scenario.* Verified on this machine under `LC_ALL=en_US.ISO8859-1`: `locale.getencoding()` is ISO8859-1 while `sys.getfilesystemencoding()` is utf-8 (macOS forces it). Writing the blob `{"mcpOAuth":{"server":"Bücher"}}` puts `b'...B\xc3\xbccher...'` in argv (os.fsencode, UTF-8), but reading the identical bytes back through `subprocess.run(..., capture_output=True, text=True)` yields `'{"mcpOAuth":{"server":"BÃ¼cher"}}'` — `round-trips: False`. That matters because store.switch (store.py:311-327) reads the blob, carries its siblings through `credential_to_blob(incoming, base)` and writes the result straight back to the port: under a legacy locale every `vibe switch` re-encodes the mangled text, so the corruption is written back into the Keychain and compounds on each switch. Contract §7 states the sibling-preserving logic exists specifically so `mcpOAuth` survives a switch — this path mangles it instead. Under a CJK legacy locale the decode can also raise UnicodeDecodeError, which is neither OSError nor TimeoutExpired, so it escapes `_run`'s except clause (keychain.py:94) as a raw traceback rather than the VibeError-plus-recovery §4 requires. The Linux port is immune: FileKeychain.read/write go through `read_text(encoding="utf-8")` and `write_private`, and every one of the seven other file call sites in src/ passes `encoding="utf-8"` explicitly — this subprocess is the only reader in the package that does not. Precondition is a non-UTF-8 locale plus non-ASCII content in the blob; I could not sample a real Claude Code blob to prove non-ASCII is present, which is why this is low.

*Fix.* Replace `text=True,` with `encoding="utf-8",` in MacKeychain._run (implies text mode), matching the other seven call sites, and add UnicodeDecodeError to the `except (OSError, subprocess.TimeoutExpired)` tuple so a bad decode still renders as a VibeError.

### `cross-platform:tests/test_leak.py:loopback-requests-honour-the-macos-system-proxy`

| | |
|---|---|
| severity | **low** |
| location | `tests/test_leak.py:176` |
| round | 1 |
| status | OPEN |

**The leak tests reach the loopback dashboard with urllib.request.urlopen, which on macOS resolves proxies from the system network configuration, so a Mac with a configured HTTP proxy routes these 127.0.0.1 requests through it.**

*Failure scenario.* On darwin, urllib.request.getproxies() is `getproxies_environment() or getproxies_macosx_sysconf()` and proxy_bypass() falls through to _scproxy's exception list; I confirmed on this machine that urllib.request.proxy_bypass('127.0.0.1') is False, i.e. loopback is not bypassed by default. On an MDM-managed or corporate macOS laptop with an HTTP proxy set in System Settings > Network > Proxies and no no_proxy env var, tests/test_leak.py::test_web_dashboard_serves_no_token and ::test_a_failing_envelope_serves_no_token send `GET http://127.0.0.1:<ephemeral>/api/usage` to that proxy and fail with URLError or a proxy 403, while the identical checkout passes on Linux, where only http_proxy/no_proxy env vars are consulted. tests/test_web.py already avoids this by driving the same routes with http.client.HTTPConnection.

*Fix.* Use http.client.HTTPConnection(host, port) in tests/test_leak.py the way tests/test_web.py::get already does, or build the request through urllib.request.build_opener(urllib.request.ProxyHandler({})).

### `memory-and-resource:src/vibemaxxing/history.py:connect-leaks-the-connection-when-the-schema-fails`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/history.py:39` |
| round | 2 |
| status | **FIXED** |

**connect() opens the sqlite connection on line 39 and applies the schema on lines 40-44 with no guard, so a failure there leaks the connection and its file descriptor and lets a raw sqlite3.Error escape where every caller expects a VibeError.**

*Failure scenario.* Two triggers reproduced on this machine against a tmp store. (1) history.db holds bytes sqlite cannot read (partial restore from a backup tool, a file another program wrote): `cur.execute(_SCHEMA)` on line 41 raises `sqlite3.DatabaseError: file is not a database`. (2) history.db is present but unwritable - I set it 0o400; a full disk gives the same class: `sqlite3.OperationalError: attempt to write a readonly database`. In both cases `conn` (line 39) is never closed on the way out, and the exception is not a VibeError, so `cli.main`'s `except VibeError` does not see it: I ran `cli.main(["list"], context=ctx)` against case (1) and the sqlite3.DatabaseError escaped main entirely, so the user gets a scrubbed traceback through redact._excepthook and exit 1 instead of contract s4's problem-plus-recovery-command. Same path for `vibe usage web` (web.Recorder.__init__) and `vibe` (tui.on_mount). The leaked connection is bounded in practice because CPython collects it and the process is exiting, but nothing in the function releases it.

*Fix.* Wrap lines 40-44 in `try: ... except sqlite3.Error as exc: conn.close(); raise StoreError(f"{path} is not a usable history database ({exc.__class__.__name__})", "vibe list") from None` - errors is a leaf module, so history may import StoreError without breaking the import direction in contract s2.

### `memory-and-resource:src/vibemaxxing/oauth.py:stash-path-releases-a-claim-it-never-took`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/oauth.py:213` |
| round | 3 |
| status | **FIXED** |

**`refresh`'s `finally: store.release_refresh(...)` runs on the stash-recovery path too, where `claim_refresh` was never called — so a process that finds a stash deletes whatever claim another process is currently holding across its POST.**

*Failure scenario.* Verified against the shipped code on a tmp store: process A calls `store.claim_refresh(root, 'work')` (True; `locks/work.claim` holds `{"until_ms": 1757930030000}`) and is inside its POST; A has already written `stash/work.json`. Process B calls `oauth.refresh` for the same alias — line 174's `read_stash` returns the successor, so the whole `if successor is None:` block including `claim_refresh` is skipped, B goes straight to lines 202-212, and the unconditional `finally` at 213-214 unlinks A's claim. Measured output: `B posted: 0 requests`, `A's claim file still there: False`, `C can claim (A is still mid-POST): True`. Concrete damage needs a third caller: with `vibe usage web` serving two browser tabs on their own `ThreadingHTTPServer` handler threads plus a `vibe` TUI on the same store, thread C can hold the predecessor credential it read a moment before B's `write_account`, find `read_stash` empty (B deleted it at line 211), take the claim B just freed, and POST the predecessor the server already killed — earning a real `invalid_grant`, which `envelope._usable_credential` (envelope.py:79-80) turns into `needs_login` and `the login for "work" has lapsed — run: vibe add work` on all three surfaces, plus a 0 contribution to the pool for that cycle. Without the spurious release C would have got `busy` instead, which renders as a transient `error`, not "go and log in again". The window is sub-millisecond, which is why this is low; the mechanical defect — a lease released by a caller that never took it — is not narrow at all, and it also silently shortens A's `CLAIM_LEASE_S` protection to zero on every stash recovery.

*Fix.* Track whether this call took the claim and release only then, e.g. bind `claimed = False` before the `if successor is None:` block, set it to `True` right after `claim_refresh` returns True, and make the `finally` read `if claimed: store.release_refresh(root, alias)`. The stash path needs no release because it never acquired anything.

### `memory-and-resource:src/vibemaxxing/tui.py:collect-thread-outlives-the-app-on-quit`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/tui.py:185` |
| round | 4 |
| status | **FIXED** |

**`refresh_cycle` runs `envelope.collect` on `asyncio.to_thread`'s default executor, whose threads are non-daemon and are joined by `asyncio.run`'s teardown, so `q` tears the dashboard down and then leaves the process alive and unresponsive for the remainder of the in-flight fan-out.**

*Failure scenario.* Measured on this machine against the shipped code: 3 seeded accounts, `tui.REFRESH_S` set to 1.0 so a cycle starts promptly, an injected client whose `request` blocks 6 s from the fourth call on. With a slow cycle in flight I posted `events.Key("q")` at t = 1.45 s. `app.run_async` returned at t = 1.46 s — Textual had already stopped the timer (`Timer.stop` cancels the task, and the `CancelledError` passes straight through `_refresh_or_report`'s `except Exception` because it is a `BaseException`) and restored the terminal — but the process did not become free until t = 19.03 s, 17.6 s after the app was gone, because the worker thread was still inside the blocking `collect()` (3 accounts x 6 s) and `asyncio.run` ends in `Runner.close()` -> `loop.shutdown_default_executor()`, which joins it. `tui.run` (tui.py:211-212) calls `Dashboard(ctx).run()`, and `App.run` uses `asyncio.run(run_app())` on Python >= 3.10 (verified in textual 8.2.8), so this is the shipped path, not a test artifact. With the shipped timeouts — `usage.fetch_usage` 10 s plus a 10 s refresh POST per account, serial in `envelope.collect` — a 5-account store on a dead network leaves the shell sitting with no dashboard and no prompt for up to 100 s after `q`. Same shape for the in-flight cycle that a plain window close starts. Nothing bounds the wait and the thread cannot be cancelled: it is parked in a blocking socket read.

*Fix.* Hand the fan-out to a thread the process is willing to abandon instead of `asyncio.to_thread`'s non-daemon default executor: run `envelope.collect` on a `threading.Thread(..., daemon=True)` and deliver the result to an `asyncio.Future` via `loop.call_soon_threadsafe`. A daemon thread is not joined at interpreter exit, so `q` frees the shell as soon as the app is down.

### `memory-and-resource:src/vibemaxxing/web.py:recorder-leaked-when-bind-fails`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/web.py:120` |
| round | 1 |
| status | **FIXED** |

**`serve` opens the Recorder's sqlite connection before the try block and builds the server outside it, so a bind failure leaks the connection and escapes as a raw traceback instead of a VibeError.**

*Failure scenario.* `recorder = Recorder(...)` at web.py:119 opens the history connection; `server = build_server(port, ...)` at web.py:120 sits outside the `try` whose `finally` (web.py:128-129) is the only `recorder.close()`. Run `vibe usage web` while another dashboard already holds the default port 8787: `ThreadingHTTPServer((LOOPBACK, port), Handler)` raises `OSError: [Errno 48] Address already in use`, `recorder.close()` never runs, and because `cli.main` catches only `VibeError`, the OSError reaches `redact._excepthook` — the user sees a scrubbed traceback rather than the "problem plus literal recovery command" contract section 4 requires, and the process exits 1 with no defined exit code for the condition. Same path on any other bind refusal (a privileged port, an unavailable interface).

*Fix.* Move `build_server` inside the try (or construct the server first and the Recorder second), and wrap the bind in `except OSError` raising `VibeError(f"port {port} is already in use", f"vibe usage web --port {port + 1}")` so the failure carries a recovery command and the connection is released on every path.

### `memory-and-resource:tests/test_tui.py:ac14-never-exercises-the-rebuild-path`

| | |
|---|---|
| severity | **low** |
| location | `tests/test_tui.py:228` |
| round | 3 |
| status | OPEN |

**AC14 feeds a byte-identical envelope on all 200 cycles, so `_reshape` short-circuits every time and the widget teardown/rebuild path the test exists to guard is executed exactly once, at mount — and on that path the dashboard exceeds AC14's own 1 MB threshold by ~10x.**

*Failure scenario.* `_queue(client, 5, payload)` re-queues one parsed fixture for every cycle, so `_shape(view)` (tui.py:107) is identical on all 200 cycles and `refresh_cycle` takes the `shapes == self._shapes` early return at tui.py:200 on every one of them. Both assertions then measure only the repaint path: `marks[200][1] == marks[50][1]` is vacuous because that path never touches the tree, and `marks[200][0] - marks[50][0] < 1_000_000` measures a path that allocates 165 bytes/cycle (measured: 2,803,530 -> 2,828,282 traced bytes over cycles 50-200). Drive the identical harness with one account's `limits` list one entry shorter on odd cycles — a shape change, which is what an account flapping between OK and a 429 ERROR message produces every 60 s, since `_shape` includes `view.message is not None` and every row label — and the same assertion reads 6,052,810 -> 15,747,984, i.e. 9.7 MB across cycles 50-200, 64.6 KB and 486 retained objects per cycle. AC14 as written therefore certifies a bound the shipped dashboard does not meet on its other path. (The growth is not in fact unbounded: carried to 1600 cycles, RSS goes 63.3 -> 67.3 MiB and the retained `StylesCache` count plateaus at 1026 by cycle 400 — a bounded Textual render cache, ~+15 MiB over the stable baseline. So this is a test-soundness defect, not a leak; but a real leak of up to 6.6 KB/cycle would also pass the current 1 MB/150-cycle threshold unnoticed.) Same module docstring, tui.py:5-7, states the guarantee the test does not exercise: "a tree rebuilt every minute is a leak with a nice render (AC14)".

*Fix.* Make the fixture change shape at least once inside the measured window — e.g. serve one account an `HTTPError(429, ...)` on odd cycles, or drop a `limits` entry — so `_reshape`'s remove/mount_all path is what is measured, and compare two late windows (say cycles 400->1600) instead of 50->200, since only a late-window delta distinguishes a bounded cache fill from a leak. Separately, `_reshape` tears down and rebuilds every panel when any one account's shape changes; keying panels by alias and reusing the existing `Static` for an alias that is still present would keep the flapping-account case on the repaint path entirely.

### `spec-conformance:README.md:argv-and-run-limitations-are-undocumented`

| | |
|---|---|
| severity | **low** |
| location | `README.md:7` |
| round | 1 |
| status | OPEN |

**Contract sections 8 and 15 state that the Keychain argv cross-uid exposure and `vibe run`'s two limits are documented in the README and `docs/RUNBOOK.md`; the README is a six-line stub and `docs/RUNBOOK.md` does not exist.**

*Failure scenario.* `ls docs/` returns `CONTRACT.md` only, and README.md ends at "Full documentation lands at `v0.1.0`." Section 8 says of the argv write path: "What argv genuinely adds is **cross-uid** reach for one exec. That goes in the README and in `docs/RUNBOOK.md` as a known limitation" — only the third of the three required disclosures (the call-site comment in `keychain.MacKeychain.write`) shipped. Section 15 likewise says `vibe run`'s two limits (the child cannot refresh its own token; `CLAUDE_CODE_OAUTH_TOKEN` bypasses account OAuth entirely) are "documented in the README and the runbook rather than hidden". A user installing v0.0.5 gets no notice that a `vibe switch` briefly exposes their credential to any local uid via `ps`.

*Fix.* Either add the two sections to README.md and create `docs/RUNBOOK.md`, or amend contract sections 8 and 15 to say the disclosure lands at v0.1.0 — as written the contract asserts a security disclosure exists when it does not.

### `spec-conformance:src/vibemaxxing/cli.py:add-json-prompt-pollutes-stdout`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/cli.py:153` |
| round | 2 |
| status | **FIXED** |

**`vibe add <alias> --json` writes the interactive paste prompt to stdout ahead of the action envelope, so stdout is not parseable JSON, breaking contract s15's "it is the only output on stdout".**

*Failure scenario.* `_login` sends the authorize URL to stderr via `err()` (cli.py:152) but then calls `ctx.prompt("paste the code shown in the browser: ")`, whose default is the builtin `input`, and `input` writes its prompt to `sys.stdout`. Verified by driving `cli.main(["add","work","--json"])` with `prompt=input`, a stubbed client and a piped stdin/stdout: stdout came back as `'paste the code shown in the browser: {\n  "schema": 1,\n  "ok": true,\n  "action": "add",\n  "alias": "work",\n  "adopted": false\n}\n'` and `json.loads` on it raised `Expecting value: line 1 column 1`. Exit code was 0 and the account was written, so a script running `vibe add work --json | jq .` gets a parse error with no signal that the login actually succeeded. Contract s15: "`--json` is accepted on every command that reports state. It emits the s10 envelope on success and the s10 error envelope on failure, and it is the only output on stdout"; s10 names `add` as one of the action-envelope commands. No test covers the `add <alias>` login path in `--json` mode (tests/test_leak.py only drives bare `add`/`add --json`, and every test context overrides `prompt` with a lambda, so the real `input` is never exercised).

*Fix.* Print the prompt text through `err()` and hand `ctx.prompt` an empty string, so the only thing `input` can write to stdout is nothing: `err("paste the code shown in the browser: "); paste = ctx.prompt("").strip()`.

### `spec-conformance:src/vibemaxxing/cli.py:adopt-slug-is-not-guaranteed-a-valid-alias`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/cli.py:95` |
| round | 1 |
| status | **FIXED** |

**`_slug` keeps a leading underscore, which `valid_alias` rejects, so `vibe add` (bare adopt) aborts with a usage error for any Claude login whose email local part starts with `_`.**

*Failure scenario.* With `~/.claude.json` naming `_jan@intra-ai.de`, `vibe add` exits 2 with `'_jan' is not a usable alias: letters, digits, dot, dash and underscore only, up to 64 characters, starting with a letter or digit / run: vibe list` (verified). `_slug` maps disallowed characters to `-` and then `.strip("-.")`, which never removes the underscore that `^[A-Za-z0-9]` forbids in the first position. Contract section 15 says the alias is "derived from the identity's email local part, slugified to `valid_alias`". The printed recovery (`vibe list`) does not help, and the offline-adopt path is the primary onboarding command. A 64-character local part hits the same wall on collision, where `_free_alias` appends `-2` and pushes the candidate past the 64-character limit.

*Fix.* Make `_slug` produce a value `valid_alias` accepts — strip leading characters outside `[A-Za-z0-9]` as well (`slug.lstrip("-._")`) and have `_free_alias` truncate `wanted` before appending the numeric suffix — then assert `store.valid_alias(alias)` before writing.

### `spec-conformance:src/vibemaxxing/cli.py:usage-once-flag-is-never-read`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/cli.py:336` |
| round | 3 |
| status | OPEN |

**`--once`, the flag contract s15 puts in the documented `vibe usage --once` invocation, is defined on the parser and read by nothing, so it silently does nothing in the one place it would change behaviour.**

*Failure scenario.* `grep -n 'args\.once\|\.once' src/vibemaxxing/*.py` returns no match: `use.add_argument("--once", action="store_true", help="fetch once and print")` at cli.py:336 sets `args.once` and `cmd_usage` (cli.py:220-224) branches only on `args.mode`. Verified with the real parser: `vibe usage web --once` parses to `mode='web' once=True`, and `cmd_usage` takes the `web` branch and calls `web.serve`, which blocks on `serve_forever` until ctrl-c. So `vibe usage web --once --json | jq .` hangs forever and emits no JSON, while the help text for the flag the user passed promises "fetch once and print". The mirror case is just as visible: `vibe usage` with no flag at all behaves identically to `vibe usage --once`, so the documented flag is satisfied by accident rather than by code. No test passes `--once` to anything except `usage --once` (tests/test_leak.py:47-48), where it happens not to matter.

*Fix.* Either make the flag load-bearing — in `cmd_usage`, refuse `--once` together with `mode == "web"` as a `UsageError` naming `vibe usage --once` as the recovery — or drop the argument and let the bare `vibe usage` be the documented one-shot form.

### `spec-conformance:src/vibemaxxing/credentials.py:blob-parse-recovery-reruns-the-failing-command`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/credentials.py:104` |
| round | 4 |
| status | OPEN |

**A Keychain blob with no `claudeAiOauth` member makes `vibe add` exit 5 with the recovery `vibe add` — the command that just failed — where the sibling branch three lines earlier reports the same real condition as NeedsLoginError with `claude /login`.**

*Failure scenario.* credentials._RECOVERY is the bare string "vibe add" (credentials.py:21) and both parse_blob (104) and parse_blob_members (95, 97) use it. cli._adopt calls parse_blob at cli.py:119. Verified end to end through cli.main(["add"]) on a tmp HOME with FakeKeychain seeded `{"mcpOAuth": {...}}`: exit 5, stderr `the stored credential blob has no 'claudeAiOauth' object / run: vibe add`. Pasting that recovery reproduces the identical error forever. Same with a non-JSON blob: exit 5, `the stored credential blob is not JSON / run: vibe add`. The condition is reachable without corruption — on Linux `~/.claude/.credentials.json` holds `mcpOAuth` alongside `claudeAiOauth`, so a user who has authorized an MCP server but never completed a Claude Code login has exactly this file. Two contract rules are broken at once. s4/s16.8: "recovery is a command the user can paste" — this one is the failing command. And s4's taxonomy: the user-visible condition is "there is no Claude Code login here to adopt", which cli.py:115-118 already maps correctly to NeedsLoginError / code needs_login / exit 3 with `claude /login` for the `blob is None` case, so `vibe add` reports one condition two incompatible ways depending on whether the port returns None or a blob missing one member. No test covers a blob that parses as JSON but carries no claudeAiOauth.

*Fix.* In cli._adopt, catch StoreError from credentials.parse_blob and re-raise the branch that already exists four lines above — `NeedsLoginError("Claude Code has no usable login on this machine to adopt", "claude /login")` — so the adopt path has one condition, one code and one pasteable recovery.

### `spec-conformance:src/vibemaxxing/index.html:absent-severity-colours-elevated`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/index.html:255` |
| round | 2 |
| status | **FIXED** |

**A row whose `severity` is null renders elevated (orange) on the web page but calm in the TUI and the CLI, so the three surfaces disagree about the same envelope.**

*Failure scenario.* `usage._text` yields `None` when a `limits` entry carries no `severity` member, and `envelope.account_entry` emits `"severity": null`. index.html:255 maps that with `row.severity === 'normal' ? 'normal' : 'warn'`, so the fill gets `data-severity="warn"` and `background: var(--warn)` (orange, index.html:107). `tui._row_line` (tui.py:80) and `cli._render_accounts` (cli.py:74) both test `row.severity in (None, "normal")` and treat the same row as calm. Concrete: AC7's payload, `{"kind":"monthly_experimental","percent":12}` — test_usage.py:52 asserts `row.severity is None` for exactly this row — paints orange on `http://127.0.0.1:8787` and plain in `vibe` and `vibe list` for the same account in the same minute. Contract s10 says the three surfaces "agree by construction rather than by discipline", and s12's frozen rule is "treating `normal` as the calm state and **everything else** as elevated", which the two typed surfaces do not follow for an absent value. No test pins the page's severity mapping (tests/test_web.py only checks the design properties), so nothing catches the split.

*Fix.* Pick one rule and apply it in all three places. Smallest change, matching the two typed surfaces and avoiding a false warning colour on every unknown-kind row: at index.html:255 use `row.severity === 'normal' || row.severity == null ? 'normal' : 'warn'`. (The alternative, following s12 literally, is to drop `None` from the calm tuple at tui.py:80 and cli.py:74.)

### `spec-conformance:src/vibemaxxing/index.html:web-who-line-ignores-display-name`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/index.html:181` |
| round | 1 |
| status | **FIXED** |

**The web page's who-line falls back `email` then `organization`, never reading `display_name` — the field contract section 10 says was added to the envelope specifically so the page could do better than the alias.**

*Failure scenario.* An account added through `vibe add <alias>` whose token response carried `account.display_name` but no `email_address` (the parse is opportunistic, so `email` stays null) renders on the web dashboard as the bare alias plus, at best, its organization name, while the TUI's `tui._who` renders the person's name. Contract section 10: "the surfaces fall back `email` → `display_name` → `alias` for the who-line, and without it in the envelope the web page could only fall back to the alias." `display_name` is in `ENVELOPE_ACCOUNT_KEYS` and in every payload, so the test that pins the envelope keys passes while the one consumer that motivated the field never reads it.

*Fix.* `var who = account.email || account.display_name || account.organization;` and add the label to the shape signature in `render()` so a change to it triggers a rebuild.

### `spec-conformance:src/vibemaxxing/keychain.py:mac-keychain-has-no-execution-coverage`

| | |
|---|---|
| severity | **low** |
| location | `src/vibemaxxing/keychain.py:57` |
| round | 3 |
| status | OPEN |

**Neither `MacKeychain.read` nor `MacKeychain.write` is ever executed by anything in the tree: AC19 forbids pytest from running `security`, and `scripts/verify_switch_live.py`, the live round trip contract s2 reserves for exactly this, does not exist.**

*Failure scenario.* `ls -la scripts/` shows the directory exists and is empty; `find . -name 'verify_switch_live*'` returns nothing, while contract s2's module map lists `scripts/verify_switch_live.py  G  live Keychain round trip — never run from pytest`. tests/test_keychain.py imports `MacKeychain` only for two `isinstance` assertions in `test_default_port_picks_by_platform_and_refuses_windows` and never constructs a call, and the conftest guard blocks any `security` invocation by design (tests/conftest.py:25, tests/test_guard.py:24). So on the default platform the whole macOS port runs unverified: the exact `find-generic-password -a <user> -w -s <service>` argv, the `returncode == 44` means-absent mapping, the strip-exactly-one-trailing-newline rule at keychain.py:55, and the `add-generic-password -U … -w <blob>` argv at keychain.py:65-76. That is the path contract s8 chose *because* both alternatives silently corrupt — `security -i` truncating a 4576-byte command line to 4005 bytes and storing the truncated credential, the promptless `-w` truncating at 128 — and the 4877-byte live blob it must carry through argv is the case most likely to hit a limit. A regression in any of those four details would ship green on CI (Linux takes `FileKeychain`) and first show up as a user's `mcpOAuth` logins disappearing after a `vibe switch`.

*Fix.* Add the `scripts/verify_switch_live.py` the module map already names: read the port, write it back unchanged, re-read and byte-compare, with a guard that refuses to run under pytest — the one artifact that turns the argv write path from unverified into verified.

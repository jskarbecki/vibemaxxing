# vibemaxxing — frozen contract

Every writer reads this file before touching code. The integrator resolves conflicts
**against this document**, not by preference. A decision that is not written here is
not frozen; ask the integrator rather than inventing one.

Version: 1 (frozen 2026-09-15, Phase A). 0.3.0 (2026-10-21) adds §17 and amends §2, §6,
§8, §9, §10, §14 and §15 for automatic switching; each amendment says "(0.3.0)".

---

## 0. The check command

Run unchanged after every implementing step. Never suppressed, never narrowed.

```
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```

A writer runs it over its own slice. The integrator runs it over the merged tree.

---

## 1. Dependency set — frozen, with pins

**No writer edits `pyproject.toml` or `uv.lock`.** A writer needing something outside
this set asks the integrator, who alone regenerates the lock.

| Kind | Package | Pin | Why |
|---|---|---|---|
| runtime | `textual` | `>=8.2.8,<8.3` | TUI dashboard, single minor |
| dev | `ruff` | `>=0.16.7,<0.17` | lint + format |
| dev | `mypy` | `>=2.3.1,<3` | `--strict` |
| dev | `pytest` | `>=9.1.1,<10` | suite |
| dev | `pytest-asyncio` | `>=1.4.0,<2` | Textual `App.run_test()` |

That is the whole set. Everything else is the standard library, deliberately:

- HTTP — `urllib.request`, behind the injectable `HttpClient` port (§5).
- Web server — `http.server.ThreadingHTTPServer`.
- History — `sqlite3`.
- Keychain — `/usr/bin/security` via `subprocess`.
- TOML — `tomllib` (3.11+).

`requires-python = ">=3.11"`. Build backend: `hatchling`.
Console scripts: `vibemaxxing` and `vibe`, both `vibemaxxing.cli:main`.
License MIT, author `Jan Skarbecki <jan@intra-ai.de>`.

---

## 2. Module map and ownership

```
src/vibemaxxing/
  __init__.py      B   __version__ from importlib.metadata
  __main__.py      B   python -m vibemaxxing
  models.py        B   AccountState, Sample      [shared, frozen]
  errors.py        B   error taxonomy, exit codes [shared, frozen]
  redact.py        B   Secret, scrub, excepthook  [shared, frozen]
  httpclient.py    B   HttpClient port, HTTPError [shared, frozen]
  fsutil.py        C   private_dir, write_private, create_private_file [shared]
  store.py         C1  on-disk store, stash, active pointer, claim
  credentials.py   C1  Credential, Identity, blob (de)serialisation
  keychain.py      C1  KeychainPort, MacKeychain, FileKeychain
  oauth.py         C1  PKCE, authorize URL, exchange, refresh consume gate
  usage.py         C2  usage client + summarize
  pool.py          C2  pool_remaining, dry_in; decide, Settings, LastSwitch (0.3.0)
  history.py       C3  SQLite samples, prune
  poll.py          C3  Scheduler (floor, stagger, backoff)
  envelope.py      D   the --json envelope, shared by CLI and web
  auto.py          H   settings.json, auto.json, tick, undo, status, notify, launchd job
  cli.py           D   argparse dispatch
  tui.py           E1  Textual dashboard
  web.py           E2  web dashboard server
  index.html       E2  web dashboard page (package data)
tests/
  conftest.py      B   autouse real-path guard (AC19), tmp_home fixture
  fakes.py         B   FakeHttpClient, FakeKeychain    [shared, frozen]
  fixture_usage.json B copied verbatim from claude-usage-dashboard
scripts/
  verify_switch_live.py  G  live Keychain round trip — never run from pytest
  verify_auto_live.py    H  live switch between two throwaway accounts — never run from pytest
```

Letters are phases: **B** solo skeleton, **C1/C2/C3** the three Phase-C writers,
**D** solo CLI, **E1/E2** the two Phase-E writers, **G** solo hardening, **H**
automatic switching (0.3.0).

**A writer touches only the files its slice owns.** Phase-B files are frozen: a
writer that needs a change there asks the integrator.

Import direction is one-way, no cycles:

```
models, errors, redact, httpclient, fsutil   (leaves — import nothing from the package)
      ^          ^          ^
credentials -> keychain -> store -> oauth
      ^                              ^
    usage -> pool                  history, poll
                 ^
              envelope
                 ^
               auto
                 ^
         cli -> tui, web
```

`auto` (0.3.0) sits above `envelope`, which it asks for the numbers, and below `cli`,
`tui` and `web`, which only read its settings and its last-switch record. The decision
itself, `pool.decide`, stays in `pool` and imports no I/O: `pool` is below `envelope` and
cannot name `AccountView`, so `auto` hands it `pool.Reading`s built from the views.

---

## 3. Shared value types — `models.py`

```python
class AccountState(StrEnum):
    OK = "ok"
    NEEDS_LOGIN = "needs_login"
    ERROR = "error"
```

Three states, no more. `ok` — usable credential. `needs_login` — the refresh lineage
is dead or the login itself lapsed; only a fresh `/login` fixes it. `error` — the last
fetch failed transiently; retry later.

An account in `needs_login` or `error` contributes `0` to the pool but still counts
toward the displayed account total (AC8).

---

## 4. Error taxonomy and exit codes — `errors.py`

```python
class VibeError(Exception):
    exit_code: ClassVar[int] = 1
    code: ClassVar[str] = "error"
    def __init__(self, message: str, recovery: str | None = None) -> None: ...
    message: str
    recovery: str | None
    def render(self) -> str: ...        # "message\n  run: recovery"  (recovery omitted if None)
```

| Class | `code` | exit | Raised for |
|---|---|---|---|
| `VibeError` | `error` | 1 | base; unclassified runtime failure |
| `UsageError` | `usage` | 2 | bad arguments, non-loopback `--host` |
| `PasteFormatError` | `paste_format` | 2 | pasted code has no `#` (AC3) |
| `StateMismatchError` | `state_mismatch` | 2 | pasted state half ≠ expected nonce (AC3) |
| `NeedsLoginError` | `needs_login` | 3 | dead refresh lineage, lapsed login, no credential |
| `NotFoundError` | `not_found` | 4 | unknown alias |
| `StoreError` | `store` | 5 | store unreadable / unwritable / malformed |
| `NetworkError` | `network` | 6 | transient upstream failure |

`PasteFormatError` and `StateMismatchError` subclass `UsageError`.

**Every user-facing error names the problem *and* the literal recovery command.**
`recovery` is a command the user can paste, e.g. `vibe add work`. `render()` is the
only human formatter; `--json` mode uses the error envelope in §10.

Exit `0` is success. Nothing else is defined.

---

## 5. Shared leaves — frozen signatures

### `redact.py` — the one redaction helper

```python
REDACTED: Final = "«redacted»"
MIN_SECRET_LEN: Final = 8          # shorter values are never registered
REGISTRY_MAX: Final = 256          # bounded LRU; a rotated predecessor ages out

class Secret:
    __slots__ = ("_value",)
    def __init__(self, value: str) -> None: ...   # registers literal/base64/json forms
    def reveal(self) -> str: ...
    def __str__(self) -> str: ...                 # REDACTED
    def __repr__(self) -> str: ...                # "Secret(«redacted»)"
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...
    def __bool__(self) -> bool: ...

def scrub(text: str) -> str: ...
def install_excepthook() -> None: ...             # sys.excepthook -> scrubbed traceback
```

Rules, in force everywhere:

1. A token value lives **only** inside a `Secret`. `Credential.access_token` and
   `.refresh_token` are `Secret`, never `str`.
2. `Secret` is not JSON-serialisable. `json.dumps` on one raises `TypeError` — loud,
   by design.
3. `.reveal()` may be called in **exactly five places**, and nowhere else:
   - `credentials.credential_to_disk()` — writing the account/stash file
   - `credentials.credential_to_blob()` — writing the Keychain / credentials file
   - `usage._get()` — the `Authorization` header of both OAuth API GETs
   - `oauth.refresh()` and `oauth.exchange_code()` — the POST body
   - `cli` — the `CLAUDE_CODE_OAUTH_TOKEN` value in the child env of `vibe run`
   The Phase-F credential-leak lens greps for every other `.reveal(`.
4. `scrub()` is applied at **every** output boundary: the stdout/stderr writer, the
   `--json` dumper, `VibeError.render()`, the excepthook, and every history write.
   It replaces each registered secret's literal, base64, and JSON-escaped form.
5. The registry is a bounded LRU of `REGISTRY_MAX` entries. Values shorter than
   `MIN_SECRET_LEN` are never registered (they would nuke unrelated output).

### `httpclient.py` — the one injectable client

```python
@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    def json(self) -> dict[str, object]: ...   # raises NetworkError on a non-object body

class HTTPError(Exception):
    def __init__(self, status: int, body: bytes) -> None: ...
    status: int
    body: bytes

class HttpClient(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None = None, timeout_s: float = 10.0) -> HttpResponse: ...

class UrllibClient:                            # the only real implementation
    def request(...) -> HttpResponse: ...      # raises HTTPError on 4xx/5xx, NetworkError otherwise
```

**Every** outbound request in the package goes through an injected `HttpClient`.
No module constructs a `UrllibClient` at import time; `cli` builds the one instance and
passes it down. No test may reach the network.

Allowed hosts, and no others: `claude.ai`, `platform.claude.com`, `api.anthropic.com`.
No version pings, no analytics, no telemetry.

Every request carries `User-Agent: vibemaxxing/<version>` unless the caller sets its own.
Cloudflare fronts all three hosts and rejects urllib's default `Python-urllib/3.x` with a
403 `error code: 1010` before the request reaches the API -- with no agent named, the token
exchange at the end of `vibe add` fails every time. This is the one thing `httpclient`
takes from the package: `__version__` off `vibemaxxing/__init__`, which imports no
submodule, so the leaf stays acyclic.

### `models.py`

`AccountState` (§3) and `Sample`:

```python
@dataclass(frozen=True)
class Sample:
    at_s: float
    pool: float
```

`Sample` lives here, not in `history.py` or `pool.py`, because `history` produces it and
`pool.dry_in` consumes it — the two are separate Phase-C slices and neither may depend
on the other.

---

## 6. On-disk store — `store.py` (C1)

Root is `Path.home() / ".vibemaxxing"`. No environment override: tests set `HOME`.

```
~/.vibemaxxing/                 0700
  accounts/<alias>.json         0600   the account, including its credential
  stash/<alias>.json            0600   a successor not yet in its account file
  locks/<alias>.claim           0600   refresh in-flight claim, with a lease
  usage/<alias>.json            0600   last usage answer and backoff, shared by every process
  active                        0600   one line: the alias whose credential is live
  settings.json                 0600   automatic switching: on/off, threshold, cooldown, exclusions
  auto.json                     0600   the last switch, whoever made it (§17)
  history.db                    0600   the sample series
```

`settings.json` and `auto.json` are small and carry no token. Their shapes are in §17.

**AC11**: every file `0o600`, every directory `0o700`, created that way, never widened.
Writes are atomic: write `<name>.tmp` with `os.open(..., 0o600)`, `fsync`, `os.replace`.

That is implemented **once**, in `fsutil.py` — `private_dir`, `write_private`,
`create_private_file` — and `store`, `keychain` and `history` all call it. Two copies of
a `0600` write in a credential tool is one copy that can drift, and `keychain` cannot
import it from `store` because `store` imports `keychain`.

### `accounts/<alias>.json`

```json
{
  "schema": 1,
  "alias": "work",
  "added_at": 1757930000.0,
  "state": "ok",
  "message": null,
  "identity": {
    "email": "jan@intra-ai.de",
    "account_uuid": "…",
    "organization_name": "…",
    "organization_uuid": "…",
    "seat_tier": "…",
    "billing_type": "…",
    "display_name": "…"
  },
  "credential": {
    "accessToken": "…",
    "refreshToken": "…",
    "expiresAt": 1757930000000,
    "refreshTokenExpiresAt": 1760522000000,
    "scopes": ["user:profile", "…"],
    "subscriptionType": "max",
    "rateLimitTier": "…"
  }
}
```

`credential` uses **Claude Code's own key names**, so `credential_to_blob()` is a
straight wrap into `{"claudeAiOauth": {...}}` with no translation layer.
`expiresAt` and `refreshTokenExpiresAt` are epoch **milliseconds**.

`schema` is the store version. A file with an unknown `schema` raises `StoreError`
naming the file and the recovery command — it is never silently migrated.

### `stash/<alias>.json`

```json
{"schema": 1, "alias": "work", "stashed_at": 1757930000.0, "credential": { … }}
```

### `locks/<alias>.claim`

```json
{"until_ms": 1757930030000}
```

### `usage/<alias>.json`

```json
{"schema": 1, "added_at": 1757930000.0, "fetched_at": 1757930000.0, "payload": { … },
 "retry_at": null, "failures": 0, "message": null}
```

A cache, not a record. `payload` is the usage endpoint's response exactly as received and
holds no token. `fetched_at` and `payload` are both present or both `null`. A missing,
unreadable or unknown-schema file reads as no cache and is overwritten on the next fetch,
never raised. `added_at` is the owning account's: a file whose `added_at` differs from
`accounts/<alias>.json` belongs to an earlier login under the same alias (`vibe add` over
it, or a fetch that landed after `vibe remove`) and is ignored. `vibe remove` deletes the
file, `vibe alias` moves it with the account. §14 says how it is used.

### Public signatures

```python
SCHEMA_VERSION: Final = 1
CLAIM_LEASE_S: Final = 30.0

def store_root() -> Path: ...
def account_path(root: Path, alias: str) -> Path: ...
def stash_path(root: Path, alias: str) -> Path: ...
def history_path(root: Path) -> Path: ...

@dataclass(frozen=True)
class Account:
    alias: str
    credential: Credential
    identity: Identity
    state: AccountState
    message: str | None
    added_at: float

def list_aliases(root: Path) -> list[str]: ...
def read_account(root: Path, alias: str) -> Account: ...          # NotFoundError / StoreError
def write_account(root: Path, account: Account) -> None: ...      # atomic, 0600
def delete_account(root: Path, alias: str) -> None: ...
def rename_account(root: Path, old: str, new: str) -> None: ...

def read_stash(root: Path, alias: str) -> Credential | None: ...
def write_stash(root: Path, alias: str, credential: Credential) -> None: ...
def delete_stash(root: Path, alias: str) -> None: ...

def read_active(root: Path) -> str | None: ...
def write_active(root: Path, alias: str) -> None: ...

def live_alias(root: Path, port: KeychainPort) -> str | None: ...          # 0.3.0, §8
def live_owner(root: Path, *, prefer: str | None) -> str | None: ...       # 0.3.0, §8
def require_usable(account: Account) -> None: ...                          # 0.3.0, §8

def claim_refresh(root: Path, alias: str, *, now_s: float) -> bool: ...   # False if one is live
def release_refresh(root: Path, alias: str) -> None: ...

def valid_alias(alias: str) -> bool: ...   # ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

`valid_alias` is the second validation boundary (a file the user named). An invalid
alias raises `UsageError`. Path traversal via alias is impossible by construction.

---

## 7. Credentials and identity — `credentials.py` (C1)

```python
EXPIRY_BUFFER_MS: Final = 5 * 60 * 1000

@dataclass(frozen=True)
class Credential:
    access_token: Secret
    refresh_token: Secret
    expires_at_ms: int
    refresh_token_expires_at_ms: int | None
    scopes: tuple[str, ...]
    subscription_type: str | None
    rate_limit_tier: str | None

def parse_credential(raw: Mapping[str, object]) -> Credential: ...     # StoreError on shape
def parse_blob(raw: str) -> Credential: ...                            # {"claudeAiOauth": {…}}
def credential_to_disk(c: Credential) -> dict[str, object]: ...        # reveal() site
def credential_to_blob(c: Credential,
                       base: Mapping[str, object] | None = None) -> str: ...   # reveal() site
def is_expired(c: Credential, *, now_ms: int, buffer_ms: int = EXPIRY_BUFFER_MS) -> bool: ...
def login_lapsed(c: Credential, *, now_ms: int) -> bool: ...           # refreshTokenExpiresAt

@dataclass(frozen=True)
class Identity:
    email: str | None
    account_uuid: str | None
    organization_name: str | None
    organization_uuid: str | None
    seat_tier: str | None
    billing_type: str | None
    display_name: str | None

EMPTY_IDENTITY: Final = Identity(None, None, None, None, None, None, None)

def read_claude_identity(path: Path) -> Identity: ...   # ~/.claude.json oauthAccount; EMPTY if absent
```

Identity comes from `~/.claude.json` → `oauthAccount`. **No request is spent on
identity.**

### The credential blob has siblings — measured live, 2026-09-15

The blob Claude Code stores is **not** `{"claudeAiOauth": …}` alone. On this machine
its top-level keys are `["claudeAiOauth", "mcpOAuth"]`, and more may appear.

`credential_to_blob(c, base)` therefore **replaces only the `claudeAiOauth` member of
`base`** and carries every sibling key through unchanged. Writing a bare
`{"claudeAiOauth": …}` would delete the user's MCP server logins on the first switch.
This is exactly the frozen isolation decision — one shared `~/.claude` history and MCP
config, credentials swapped and nothing else — so MCP OAuth state must survive a switch
rather than travel with an account.

`base` is the blob currently in the Keychain, read immediately before the write. With
`base=None` the result is `{"claudeAiOauth": …}` alone, which is correct only when the
port holds nothing yet.

The live `claudeAiOauth` carries exactly the seven documented fields:
`accessToken`, `refreshToken`, `expiresAt`, `refreshTokenExpiresAt`, `scopes`,
`subscriptionType`, `rateLimitTier`. `parse_blob` ignores unknown members rather than
rejecting them — the server adds fields without warning (see §12).

`login_lapsed` is what the UI surfaces *before* it happens: `refreshTokenExpiresAt` is
when the login itself dies, and only a fresh `/login` fixes it.

---

## 8. Keychain port — `keychain.py` (C1)

```python
CLAUDE_CODE_KEYCHAIN_SERVICE: Final = "Claude Code-credentials"
SECURITY_BIN: Final = "/usr/bin/security"          # absolute: no PATH hijack
SECURITY_TIMEOUT_S: Final = 10.0

class KeychainPort(Protocol):
    def read(self) -> str | None: ...              # None when absent
    def write(self, blob: str) -> None: ...

class MacKeychain:                                 # service above, account = getpass.getuser()
class FileKeychain:                                # ~/.claude/.credentials.json, 0600

def default_port() -> KeychainPort: ...            # MacKeychain on darwin, else FileKeychain
```

- `MacKeychain.read` → `security find-generic-password -a <user> -w -s <service>`;
  exit code 44 means "absent" and returns `None`; strip exactly one trailing newline.
- `MacKeychain.write` → `security add-generic-password -U -a <user> -s <service> -w <blob>`,
  with the blob in **argv**. One path, no fallback.

### Why argv, measured 2026-09-15

All three alternatives were tried on this machine and rejected:

| Mechanism | Result |
|---|---|
| `security -i` (stdin command mode) | **Silently truncates at ~4005 bytes and stores the truncated value.** A 4576-byte command line wrote a 4005-byte credential and then misparsed the remainder as a second command. |
| `security add-generic-password … -w` with no value | Prompts twice on the terminal (`password data for new item:` / `retype`) and truncates the answer at **128 bytes**. |
| `Security.framework` via `ctypes` | Writes a 4883-byte item exactly, nothing in argv — but only for items **this process created**. Reading Claude Code's item with user interaction disallowed returns `errSecAuthFailed` (**-25293**): our process is not in that item's ACL. Using it would put a GUI authorization prompt in front of every `vibe switch`. |

The live blob on this machine is **4877 bytes** and `mcpOAuth` only grows, so `security -i`
would corrupt the credential on every write rather than protect it. A code path that can
only ever corrupt is worse than no code path, so it is not written at all.

That leaves argv, and its exposure is stated rather than hidden. Apple's own help text
says `Use of the -p or -w options is insecure`, and it is: macOS exposes a process's full
argv to **any** local user through `ps` — verified here by reading root-owned and
`_windowserver`-owned processes' arguments as an unprivileged user. For the duration of
one `exec`, the credential is readable cross-uid.

Two things bound it, and neither is an excuse:

- Multi-user macOS is out of scope for v1.
- The store already keeps the same tokens as plaintext JSON at `0600`, by frozen product
  decision, so a **same-uid** attacker gains nothing from argv they did not already have.

What argv genuinely adds is **cross-uid** reach for one exec. That goes in the README and
in `docs/RUNBOOK.md` as a known limitation, and the call site carries a comment saying so.
`MacKeychain.read` is unaffected: `find-generic-password -w` puts the secret on our own
stdout pipe, never in argv.
- Windows: **no code path**. `default_port()` on `win32` raises `VibeError`.
- `CLAUDE_CONFIG_DIR` is never read, never set, never honoured. With it set Claude Code
  scopes the item to a hashed service name and `NO_KEYCHAIN=1` does not reliably
  produce a plaintext file — measured. We swap credentials, we do not isolate config
  dirs, so only the fixed-name item matters.

**Switch order is fixed** (AC10), and every step is load-bearing:

-1. (0.3.0) Refuse a dead target before anything else, with `NeedsLoginError` and recovery
   `vibe add <alias>`: `store.require_usable` raises for an account whose refresh token
   has lapsed (`login_lapsed`, against the real clock) or whose file records state
   `needs_login`. It touches neither the port nor a claim. Exempt: the one account whose
   stored copy the resync in step 2 replaces from the port, the owner of the live login
   (below) when one is named, else the account `active` names. A stale `active` is not
   exempt while another account owns the login: its stored copy is not replaced, so a dead
   one would reach the port. An access token that has merely expired is
   not refused here — `store` cannot refresh, since `oauth` imports it — and is Claude
   Code's to refresh once active; `oauth.ensure_usable` (below) proves the lineage alive
   first, wherever there is an `HttpClient`.
0. Take the refresh claim (`locks/<alias>.claim`) of the incoming account, the outgoing
   account and, (0.3.0) when it is a third account, the **owner** of the port's login,
   held until the switch returns, then re-read `active`. A claim already held, or an
   `active` that moved since it was first read, fails the switch with `UsageError` and
   recovery `vibe switch <alias>`: a refresh in flight would otherwise finish after its
   spent predecessor was copied into the port, and a switch that just ran would have the
   resync below write one account's tokens into another's file. `vibe alias` holds the
   claims of both names the same way, so no poll sees a not-yet-active copy of the active
   account. The owner is read from `~/.claude.json` before the claims it joins, with no
   port read, so a refused switch touches nothing.
1. Read the port once. Keep the whole blob — it has siblings (§7).
1b. (0.3.0) **Refuse a login vibe does not know.** If the port holds a login (a blob whose
   `claudeAiOauth` parses, not an empty member), `~/.claude.json` names an account uuid,
   and **every** stored account has a known uuid and none equals it, the port holds the
   only copy of that refresh token (someone ran `claude /login`). Raise `UsageError` with
   recovery `vibe add`, which adopts it. The port was read and nothing was written. An
   account stored without a uuid might be the live one, so with any such account (or an
   unreadable one) this check abstains and the old rule below applies.
2. Parse its `claudeAiOauth` and write it back to the account that **owns** it: the stored
   account whose uuid `~/.claude.json` names (preferring the outgoing one on a tie), else
   the outgoing account when no uuid settles it. The resync clears a `needs_login` record
   on that account, since Claude Code holds a working login for it. Claude Code rotates the
   active token behind our back; skipping this resync loses that rotation and strands the
   owner on a spent token. (Before 0.3.0 the resync went only to the outgoing account and
   was skipped when the uuids disagreed, but the port was written anyway: a stale `active`
   pointer lost the live account's rotation.)
3. If the incoming account has a stash (and is neither the outgoing account nor the owner,
   whose port copy wins), write it into its account file and delete it.
   A stash is a refresh the server completed that the file never got, and once active
   the account is never refreshed, so it would never be consumed. Build the incoming
   blob with `credential_to_blob(incoming, base=the blob from step 1)` so `mcpOAuth` and
   any future sibling survive.
4. Write the port.
5. Repoint `oauthAccount` in `~/.claude.json` at the incoming account and delete its
   `profileFetchedAt`. Claude Code caches the logged-in profile there and skips the
   refetch for 24h while that stamp is fresh, so a credential-only switch leaves it
   running on the incoming token while naming the **outgoing** account — measured on
   2.1.272. Dropping the stamp makes Claude Code refetch with the incoming token and
   fill the members we cannot know (`billingType`, `seatTier`, the trial flags), so a
   machine that has never run `claude` needs no priming. Every other key in the file is
   preserved; a missing file is created, a corrupt one is left alone.
6. Update the `active` pointer.

Step 5 happens **after** step 4: the identity file only says who the credential belongs
to, so it must never name the incoming account while the port still holds the outgoing
one. The resync in step 2 happens **before** the overwrite in step 4. A test with an
in-memory fake port asserts that ordering.

### The active account's token belongs to Claude Code (0.2.2)

The refresh token rotates on every refresh, so a lineage survives only one refresher.
For the active account that is Claude Code, whether or not a session is running right
now. Nothing in `vibe` refreshes it, and only `switch` step 2 writes a credential taken
from the port:

- `oauth.refresh` returns error `active` for the active alias, checked once on entry,
  which also keeps it from consuming the active alias's stash, and again after taking the
  refresh claim. `switch` and `vibe alias` hold that claim (step 0), so a poll that read
  `active` before either cannot refresh the lineage in the port. An empty or unparsable
  claim file counts as lapsed only once it is older than the lease, since that is what a
  claim looks like before its write lands. A holder stalled past the 30 s lease, such as
  a laptop asleep mid-refresh, can still be taken over.
- A poll uses `envelope.active_token` for the active account, and `vibe run <active>`
  uses it with the 5-minute expiry buffer, since the child cannot refresh. It picks the
  port's token when `~/.claude.json` and the account name the same uuid, both known, and
  that token is not another account's stored access token (`switch` writes the port a
  moment before `~/.claude.json` and `active`). Otherwise the stored access token. Either
  only until it expires.
- When neither is usable, a poll makes no request and shows the cached answer under §14's
  rules with a message naming why: `token expired - vibe leaves the active account's token
  to Claude Code`, the Keychain's own error when the port could not be read, or `no
  account id stored to match Claude Code's login` for an account whose login returned no
  uuid (switching away is the only way to refresh that one). `state` becomes `error` once
  no answer an hour old or younger remains. `vibe run` fails with recovery `claude`.
- The plan backfill never runs for the active account: it rewrites the account file, and
  a `switch` resync landing inside that read-write would be undone.

Before 0.2.2 the stored copy was refreshed. Whichever of the two refreshers went second
held a spent token: when that was us, the account read `login lapsed` hours after
`vibe add`; when it was Claude Code, its own session was logged out.

### Who is live, and is the target alive (0.3.0)

`active` is only what vibe last wrote. A `claude /login` since then moves the real login
without touching it, and it was observed disagreeing. Anything that acts on "the account
in use" therefore asks `store.live_alias(root, port)`: the stored account whose identity
uuid equals the one `~/.claude.json` names, when the port holds a login; `None` for
anything it cannot tell (no uuid, a login nobody stored, an unreadable port, two aliases
for one uuid with neither `active`). `read_active` keeps its meaning for every existing
caller.

`store.live_owner(root, prefer=active)` is the owner the switch resync writes into. It is
`None` when `~/.claude.json` names no uuid, **or when the active account was stored without
one**: vibe writes `~/.claude.json` from what it has, so after a switch onto an account with
no uuid the file still names the account that was left, and reading it would name the wrong
owner. `store.switch` takes `now_s` so a caller that already holds a clock reading checks
the lapse against the same instant.

`oauth.ensure_usable(root, alias, client, now_ms=...)` is the half of the usability check
that needs a request. The account the resync replaces from the port (the owner, else
`active`) is exempt: Claude Code holds that lineage. Any other account gets
`require_usable`, then a refresh of an access token inside the expiry buffer (vibe is the
one refresher of that lineage). A successor that was neither written to the account file
nor stashed exists only in this process, so the switch would still write the spent
predecessor; that counts as a failure, not a success. A lineage the server rejects
raises `NeedsLoginError` with `vibe add <alias>`; a refresh that merely could not be
confirmed (a 5xx, a busy claim, `invalid_client`) raises `NetworkError` with
`vibe switch <alias>`, because a browser login is the wrong advice for an upstream
hiccup. `vibe switch` and the auto-switch tick both call it before `store.switch`, so
`vibe switch` to an account with an expired access token now makes one request and no
longer works offline for that account.

---

## 9. OAuth — `oauth.py` (C1)

```python
AUTHORIZE_URL: Final = "https://claude.ai/oauth/authorize"
TOKEN_URL:     Final = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID:     Final = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI:  Final = "https://platform.claude.com/oauth/code/callback"
SCOPES: Final = ("org:create_api_key", "user:profile", "user:inference",
                 "user:sessions:claude_code", "user:mcp_servers", "user:file_upload")
```

Nothing else. No endpoint, client id, grant type, scope or header outside this list is
ever constructed.

### Authorize URL (AC2)

`build_authorize_url(verifier: str, state: str) -> str` produces exactly these query
parameters, and no others:

| key | value |
|---|---|
| `code` | `true` |
| `client_id` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| `response_type` | `code` |
| `redirect_uri` | `https://platform.claude.com/oauth/code/callback` |
| `scope` | the six scopes, space-joined, in the order above |
| `code_challenge` | unpadded base64url SHA-256 of the verifier |
| `code_challenge_method` | `S256` |
| `state` | the nonce |

There is **no localhost callback**. The flow is paste-the-code.

### Paste parsing (AC3)

```python
def parse_pasted_code(paste: str, expected_state: str) -> tuple[str, str]: ...
```

Phase A named PKCE for this module but specified no generator, so Phase D added
`new_verifier()` (`secrets.token_urlsafe(32)`, 43 chars, inside RFC 7636's 43-128) and
`new_state()` (`secrets.token_urlsafe(32)`, 43 chars). The authorize endpoint
validates the state's length: a 22-char state renders the consent page but fails the
Authorize click with "Invalid request format".

Split on the **first** `#`. No `#` → `PasteFormatError`. State half ≠ `expected_state` →
`StateMismatchError`. Returns `(code, state)`.

### Refresh error classification (AC4)

```python
def classify_refresh_error(exc: HTTPError) -> str: ...
```

Permanent **only** when the status is 400/401/403 **and** the top-level `error` member
of the JSON body is `invalid_grant` or `invalid_client`. Returns that literal string.
Everything else — other statuses, other members, an unparseable body — returns
`"transient"`. A substring scan is forbidden: the marker can appear inside another
envelope's detail text, and a false permanent verdict quarantines a live account.

`invalid_grant` — this refresh lineage is dead; the account goes `needs_login` with the
literal recovery command `vibe add <alias>` in its message. (0.3.0) The verdict is
**recorded**, not just returned: `oauth.refresh` writes the account file with state
`needs_login` and that message — only while the stored credential still holds the refresh
token that was refused, so a `vibe add` that landed meanwhile is not marked dead — and a
later call for the same refresh token returns `invalid_grant` without a request. Before
this, every poll re-posted a dead token for as long as the account existed. The on-disk
`schema` stays 1: `state` and `message` were already fields. `envelope.collect` reads a
non-active account whose file records `needs_login` as `needs_login` with no request; a
successful refresh or a switch resync writes state `ok` again, and `vibe add <alias>`
already writes a fresh `ok` account.
`invalid_client` — our client id was rejected; systemic, **not** the account's fault,
so it lands no strike against the account.

### The consume gate (AC5)

```python
@dataclass(frozen=True)
class RefreshOutcome:
    credential: Credential | None
    error: str | None        # None | "invalid_grant" | "invalid_client"
                             #      | "no_refresh_token" | "transient" | "busy"
    stashed: bool = False

def refresh(root: Path, alias: str, credential: Credential, client: HttpClient,
            *, now_ms: int) -> RefreshOutcome: ...

def ensure_usable(root: Path, alias: str, client: HttpClient,
                  *, now_ms: int) -> None: ...          # 0.3.0, §8
```

A refresh rotates the refresh token: the server issues a successor and kills the
predecessor. **The successor must be durable before the predecessor is treated as
spent**, and only one refresh per alias is in flight at a time.

Exact order:

1. `read_stash(root, alias)` — a present stash means the last run was interrupted.
   Use it instead of POSTing a token that is already spent, and jump to **step 5**.
   (Phase A wrote "step 6" here. Taken literally that deletes the durable successor
   without ever writing it to the account file, so the next run POSTs the spent
   predecessor and earns `invalid_grant` — the exact failure the stash exists to
   prevent. Corrected 2026-09-15; the implementation was always 5 → 6 → 7.)
2. `claim_refresh(root, alias)` — `False` → return `RefreshOutcome(None, "busy")`.
3. **Unlock, then POST.** `{"grant_type": "refresh_token", "refresh_token": …,
   "client_id": …}`, JSON body, no auth header.
4. `write_stash(root, alias, successor)` — the successor is now durable.
5. `write_account(root, account_with_successor)`.
6. `delete_stash(root, alias)` — only once the account file holds it.
7. `release_refresh(root, alias)`.

A crash at any point leaves a recoverable successor on disk. If step 5 raises, the
outcome is `RefreshOutcome(successor, "transient", stashed=True)`: the caller may use
the credential but must not claim the account file was updated.

**No file lock is ever held across network I/O.** The in-flight exclusion is the claim
file with a `CLAIM_LEASE_S` lease, taken and released under the lock, never held during
the POST. A crashed claimer ages out.

### Exchange

```python
def exchange_code(client: HttpClient, *, code: str, verifier: str,
                  state: str) -> tuple[Credential, Identity]: ...
```

It returns an `Identity` as well as a `Credential`. The token response optionally carries
`account` / `organization` objects naming who the token belongs to, and without them a
freshly logged-in account would render as its bare alias until Claude Code next rewrote
`~/.claude.json` — which, for an account that is not active, may be never. The parse is
opportunistic: anything malformed yields `EMPTY_IDENTITY` rather than failing the login.

`{"grant_type": "authorization_code", "code": …, "code_verifier": …, "state": …,
"client_id": …, "redirect_uri": …}`. **Unverified until a live 200 is pasted.** Nothing
may depend on it before that evidence exists.

---

## 10. The `--json` envelope — `envelope.py` (D), reused verbatim by the web dashboard

`vibe <cmd> --json` and `GET /api/usage` emit **the same document**. One builder, one
schema. AC17 pins it.

```json
{
  "schema": 1,
  "generated_at": "2026-09-15T10:40:00+00:00",
  "accounts": [
    {
      "alias": "work",
      "active": true,
      "state": "ok",
      "message": null,
      "email": "jan@intra-ai.de",
      "display_name": "Jan",
      "organization": "Intra AI",
      "plan": "max 20x",
      "updated_at": "2026-09-15T10:39:12+00:00",
      "rows": [
        {"kind": "session", "label": "Session", "percent": 0,
         "severity": "normal", "resets_at": "2026-09-15T10:40:00.190984+00:00"}
      ],
      "breakdown": [{"label": "Claude Code", "percent": 100}]
    },
    {
      "alias": "old",
      "active": false,
      "state": "needs_login",
      "message": "login lapsed for \"old\" — run: vibe add old",
      "email": "other@example.com",
      "display_name": null,
      "organization": null,
      "plan": null,
      "updated_at": null,
      "rows": [],
      "breakdown": []
    }
  ],
  "pool": {
    "accounts": 2,
    "remaining_account_weeks": 0.44,
    "dry_in_seconds": null
  },
  "resets": [
    {"alias": "work", "at": "2026-09-19T13:00:00+00:00"},
    {"alias": "old", "at": "2026-09-22T21:00:00+00:00"}
  ],
  "auto_switch": {
    "enabled": false,
    "threshold": 99,
    "last_switch": {
      "from": "work", "to": "side", "at": "2026-09-15T10:20:00+00:00",
      "reason": "work session at 99%; side is at 10%"
    }
  }
}
```

`auto_switch` (0.3.0) is **additive**: `ENVELOPE_SCHEMA` stays 1 and no existing key moved.
`enabled` and `threshold` are the effective settings (§17), so a missing or invalid
`settings.json` shows the defaults, `false` and `99`, and never an error. `last_switch` is
the record in `auto.json` as `{from, to, at, reason}`, or `null` when there is none; the
internal `until` field is not part of the envelope. `vibe list --json` and `GET /api/usage`
build the block the same way from the same two reads. The block is present in every
envelope, whatever the settings say.

`updated_at` is when that account's numbers were fetched, which can be earlier than
`generated_at`: §14's shared cache serves an answer up to `USAGE_FRESH_S` old without a
request. After a 429, a 5xx or a network failure, an account whose last answer is under
`USAGE_WAIT_CAP_S` (1 h) old keeps it, with `state` `ok` and a `message` that names the
problem and the time of the numbers shown (`rate limited - next try 23:10 - showing usage
from 23:02`). With no such answer it is `state` `error` with the message minus that last
clause. A 401, 403 or other 4xx is `state` `error` with no numbers, as it always was.

`resets` is every account's **weekly rollover**, soonest first, and `[]` when no account
reports one. The `weekly_all` row only: the session row rolls over every few hours and
would bury the weekly ones it overlaps, and `weekly_scoped` rolls over within a minute of
`weekly_all` on the same account. An account that is not `ok`, or whose `resets_at` does
not parse, contributes no entry rather than one at a guessed moment.

**Every documented key is present on every entry**, whatever the state — absent data is
`null` or `[]`, never a missing key. The single list of those keys lives in
`tests/fakes.py` as `ENVELOPE_ACCOUNT_KEYS`, so the CLI's and the web dashboard's tests
cannot disagree about what the envelope promises.

`display_name` was added in Phase E: the surfaces fall back `email` → `display_name` →
`alias` for the who-line, and without it in the envelope the web page could only fall back
to the alias while the TUI, which renders from the typed objects, could do better. A `needs_login` entry's `message` contains the
literal string `vibe add`.

No token value appears in this document, in any form, ever. The `--json` dumper runs
`scrub()` over the serialised text before it is written.

`list` and `usage --once` emit the account envelope above. `add`, `switch`, `remove`,
`alias` and `run` change state rather than report it, so in `--json` mode they emit the
action envelope instead — reporting a fetched account list after a `switch` would mean a
network round trip the command did not need:

```json
{"schema": 1, "ok": true, "action": "switch", "alias": "work"}
```

`action` is the subcommand name; the remaining keys name what it acted on (`alias`, or
`old`/`new` for `alias`, plus `exit_code` for `run` and `adopted` for `add`). (0.3.0) The
`vibe auto` subcommands use the same envelope with `action` `"auto <sub>"`: `auto tick`
adds `switched` and `line`, `auto status` adds `enabled`, `threshold`, `cooldown_s`,
`excluded`, `live`, `active`, `last_switch` and `next`, `auto install` adds `plist` and
`load`, `auto uninstall` adds `removed` and `unload`, and `auto exclude|include` add
`alias` and `excluded`.

`envelope.py` also owns `collect()`, which does the reads, the refreshes and the usage
fetches and returns `AccountView`s, and `pool_weeks()`, which is the single place the
pooled number is computed. The CLI, the TUI and the web dashboard all call those two and
`build()`, which is what makes the three surfaces agree by construction rather than by
discipline.

Error envelope, for a command that fails in `--json` mode:

```json
{"schema": 1, "error": {"code": "needs_login", "message": "…", "recovery": "vibe add work"}}
```

`code` is the `code` class attribute from §4. The process exit code is that class's
`exit_code`.

---

## 11. Web dashboard route table — `web.py` (E2)

Binds `127.0.0.1` **only**. `--host` with anything other than the literal `127.0.0.1`
is a hard error: `UsageError` naming loopback, exit `2` (AC13).

| Method | Path | Status | Content-Type | Body |
|---|---|---|---|---|
| GET | `/` | 200 | `text/html; charset=utf-8` | `index.html` package data |
| GET | `/api/usage` | 200 | `application/json` | the §10 envelope |
| GET | `/api/usage` | 500 | `application/json` | `{"schema":1,"error":{…}}` |
| GET | anything else | 404 | `text/plain; charset=utf-8` | `not found\n` |

`ThreadingHTTPServer` answers each request on its own thread, so the process-wide history
connection is opened with `check_same_thread=False` and every use is serialised by
`web.Recorder`'s lock. **That lock never covers the usage fetch** — only the two sqlite
calls — which is the same rule as §9's consume gate.

No other route. No authentication, no TLS — both out of scope, and both are why the
bind is loopback-only. `Cache-Control: no-store` on every response.

```python
def build_server(port: int, *, envelope: Callable[[], dict[str, object]]) -> ThreadingHTTPServer: ...
```

`build_server(0)` must yield a socket whose `getsockname()[0]` is `"127.0.0.1"` (AC13).

`index.html` is ported from `claude-usage-dashboard` and **keeps** its design
properties: zero `box-shadow`, every colour a `:root` custom property, `tabular-nums`,
relative reset times, both colour schemes, and `transition: width 240ms
cubic-bezier(0.23, 1, 0.32, 1)` as the only motion. It rebuilds DOM only when the
account *shape* changes and otherwise repaints in place — that is also what keeps a
page left open for days from growing. Colour comes from the payload's `severity`, never
from an invented percentage threshold. The row list is rendered from `rows`, never from
a hardcoded three-row layout.

Exactly **ten** hex literals, five per scheme, all of them inside `:root`. Every other
colour on the page — hairlines, tracks, the badge border, the scrollbar — is
`color-mix`ed from those five, so the palette has five decisions in it and both schemes
stay in step by construction. `tests/test_web.py` counts them.

The page is a **split**: the account ledger on the left, the week on the right,
collapsing to one column under 1080px. The rail renders `resets` as inline SVG with no
library and no build step — the page is package data served over loopback and must work
with no network at all.

The week runs from **this instant** to seven days out, one lane per account, a dot where
that account's weekly limit rolls over. The left edge is now, so the whole thing slides
on the same 30 s timer that repaints the relative reset times.

Every rendered time goes through `toLocaleDateString` / `toLocaleTimeString`, which read
the machine's own zone — a reader in New York reads New York time with no setting to
find — and the rail names the zone it resolved so that is visible rather than assumed.
Day boundaries are **local midnights**, walked with `setDate`, not fixed 24 h steps from
now: a fixed step drifts a day's worth of gridline across a DST change. A weekday name
is centred over the day it names, not over the midnight that starts it, and is dropped
when its column is under 20px — which is the part-day at each end, and the left one is
already called "now".

Every limit row is drawn the same: one bar height, one weight, one colour rule. Colour
comes from the payload's `severity` and nothing else. Which row matters is the reader's
call, not something typography decides for them.

---

## 12. Usage and pool — `usage.py`, `pool.py` (C2)

```python
USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL: Final = "https://api.anthropic.com/api/oauth/profile"
BETA_HEADER: Final = "oauth-2025-04-20"

def fetch_usage(client: HttpClient, token: Secret, *, timeout_s: float = 10.0) -> dict[str, object]: ...
def fetch_plan(client: HttpClient, token: Secret,
               *, timeout_s: float = 10.0) -> tuple[str | None, str | None]: ...

@dataclass(frozen=True)
class Row:
    kind: str
    label: str
    percent: int | None
    severity: str | None
    resets_at: str | None

@dataclass(frozen=True)
class BreakdownRow:
    label: str
    percent: int | None

@dataclass(frozen=True)
class Summary:
    rows: tuple[Row, ...]
    breakdown: tuple[BreakdownRow, ...]

def summarize(payload: Mapping[str, object]) -> Summary: ...
```

`envelope.collect` contains **both** failure kinds per account: `VibeError`, and
`httpclient.HTTPError`, which is not a `VibeError` and is what `UrllibClient` raises on
every 4xx and 5xx — including the 429 §14's own note predicts under a 60 s floor.
Containing it in `collect` rather than in each surface is what makes `vibe list`, the TUI
and the web page behave identically on a bad day; before Phase E it escaped `collect` and
`vibe list` died on a traceback instead of an exit code.

`fetch_usage` sends `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`,
and nothing else. Measured 2026-09-15: those two headers alone return
`HTTP/1.1 200 OK` with `Content-Type: application/json`, so no `Accept` header is added.

`fetch_plan` reads `organization.organization_type` and `organization.rate_limit_tier`
off the profile endpoint and drops the `claude_` prefix, so the stored value is the one
Claude Code writes (`max`, `pro`) and a switch ports a blob in Claude Code's own
vocabulary. It exists because the plan is in neither of the two places a poll already
looks: the usage response names no plan (measured 2026-09-16: 22 top-level keys, none of
them a plan), and the token endpoint answers a `vibe add` login without one, so an
account we logged in ourselves showed no plan at all while an imported one showed
`max`. `envelope._fill_plan` therefore fetches it **once**, on the first poll of an
account whose credential has no `subscriptionType`, and writes it back to the account
file; every later poll reads it off disk. A failing profile endpoint costs the plan
label and nothing else — the account keeps its usage rows and its `ok` state.

The envelope's `plan` is that label, not the raw field: `subscriptionType` plus the
multiplier parsed off the tier (`default_claude_max_20x` → `max 20x`), because two Max
accounts on different multipliers have very different weekly budgets and the raw `max`
answers "which plan" only halfway. An unrecognised tier renders the bare subscription
type rather than a guessed number.

`summarize` renders from the **`limits` array**, never from the sibling `five_hour` /
`seven_day` objects. `kind` values are open-ended. Labels:

| `kind` | label |
|---|---|
| `session` | `Session` |
| `weekly_all` | `Weekly · all models` |
| `weekly_scoped` | `Weekly · ` + `scope.model.display_name` |
| anything else | `kind.replace("_", " ").capitalize()` |

An unknown kind renders and raises nothing (AC7). A `weekly_scoped` row without a
display name falls through to the generic rule.

**Measured live, 2026-09-15.** `severity` is not a two-valued flag: the live response
carried `"warning"` alongside `"normal"`. Colour is chosen by treating `"normal"` as the
calm state and **everything else** as elevated — never by inventing a percentage
threshold. The live response also carried **22 top-level keys** where the fixture has 9,
including a dozen codenamed members (`amber_ladder`, `tangelo`, `seven_day_opus`, …).
`summarize` reads `limits` and `seven_day_breakdown` and ignores the rest, which is why
an endpoint that grows new fields cannot break it. `seven_day_breakdown` carried
`window_started_at`, absent from the fixture — same rule.

```python
WEEKLY_ALL_KIND: Final = "weekly_all"

@dataclass(frozen=True)
class AccountUsage:
    state: AccountState
    rows: tuple[Row, ...]

def pool_remaining(accounts: Iterable[AccountUsage]) -> float: ...
```

`pool_remaining` = `Σ (100 − clamp(percent, 0, 100)) / 100` over the **`weekly_all` row
only**, in account-weeks. An account in `needs_login` or `error` contributes `0`; an
account with no `weekly_all` row contributes `0`. Five `ok` accounts at
`[0, 56, 66, 100, 20]` give `2.58` (AC8).

```python
def dry_in(samples: Sequence[Sample]) -> timedelta | None: ...   # Sample from models
```

Measured from the **newest** sample's timestamp, using the oldest and newest samples:
`rate = (oldest.pool − newest.pool) / (newest.at_s − oldest.at_s)`. Returns `None` when
fewer than two samples, when the timestamps are equal, or when the pool is equal or
increasing (`rate <= 0`). Otherwise `timedelta(seconds=newest.pool / rate)`. The result
is **never clamped at a reset boundary** — a forecast that silently stops at the next
reset is a forecast that lies. `(t=0, 3.0)` and `(t=3600, 2.5)` give `timedelta(hours=5)`
(AC9).

---

## 13. History — `history.py` (C3)

History exists for exactly one consumer: the burn-rate forecast. Historical charts
beyond it are out of scope, so there is one table and it holds the pooled series only.
No per-account rows, no token columns, no request bodies.

```sql
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS samples (
    at_s REAL PRIMARY KEY,
    pool REAL NOT NULL
);
```

```python
SCHEMA_VERSION: Final = 1
RETENTION_DAYS: Final = 90
SAMPLE_LIMIT: Final = 2000          # bounded read; dry_in needs oldest + newest only

def connect(path: Path, *,
            check_same_thread: bool = True) -> sqlite3.Connection: ...  # 0600, applies schema
def record(conn: sqlite3.Connection, *, at_s: float, pool: float) -> None: ...
def prune(conn: sqlite3.Connection, *, now_s: float,
          retention_days: int = RETENTION_DAYS) -> int: ...     # returns rows deleted
def samples(conn: sqlite3.Connection, *, since_s: float,
            limit: int = SAMPLE_LIMIT) -> list[Sample]: ...
```

`record` prunes on write (AC15). Rows older than `RETENTION_DAYS` are deleted; rows
inside the window are untouched. Every cursor is closed; the connection is opened once
per process, not per refresh.

**No token value, in any form, is ever written to this database.** AC12 scans its raw
bytes.

`connect` creates its parent directory `0700` and the file `0600` before sqlite touches
it — sqlite would create the file `0644`, and the store tree does not exist until an
account has been written, so a dashboard opened on a fresh machine would otherwise die on
a missing directory.

### AC12's one exemption, settled with the owner 2026-09-15

AC12 says the sentinel must appear in "no byte of ... any file under the temp
`~/.vibemaxxing`". Taken literally that is unsatisfiable: the frozen at-rest decision
stores the credential as plaintext JSON in `accounts/<alias>.json`, so seeding the store
with the sentinel as the access token puts it there by construction. The owner settled it:
**`accounts/*.json` is the single exemption**, and the test additionally asserts it is
`0600`. Everything else is scanned — the stash, the claim, the active pointer, the raw
bytes of `history.db`, both streams, and the forced tracebacks. The stash check stays
meaningful because AC12 seeds the sentinel as the **access** token, and a stash only ever
holds an access token the server just issued.

(Phase A claimed a stash holds *nothing* from the predecessor. That was wrong: RFC 6749
makes `refresh_token` optional in a refresh response, so a server that does not rotate it
leaves the predecessor's refresh token in the successor and therefore in the stash. That
is correct behaviour, and it does not weaken AC12, whose sentinel is the access token.)

---

## 14. Poll scheduler — `poll.py` (C3)

```python
POLL_FLOOR_S: Final = 60.0          # never below this
MIN_GAP_S: Final = 10.0             # global spacing between consecutive requests
BACKOFF_S: Final = (60.0, 120.0, 240.0, 480.0)

@dataclass(frozen=True)
class Due:
    alias: str
    at_s: float
    backoff: bool                   # True => a retry, excluded from the window cap

class Scheduler:
    def __init__(self, aliases: Sequence[str], *, start_s: float) -> None: ...
    def next_due(self, *, now_s: float) -> Due | None: ...
    def record_success(self, alias: str, *, now_s: float) -> None: ...
    def record_error(self, alias: str, *, now_s: float) -> None: ...
    def record_exhausted(self, alias: str, *, now_s: float, resets_at_s: float) -> None: ...
```

Accounts are **staggered**: alias *i* of *n* starts at `start_s + i * (POLL_FLOOR_S / n)`
and repeats every `POLL_FLOOR_S`. With five accounts that is one request every 12 s, so
every half-open `[t, t+60)` window holds at most five scheduled requests and consecutive
scheduled requests are at least `MIN_GAP_S` apart globally (AC16). When `n` is large
enough that `POLL_FLOOR_S / n < MIN_GAP_S`, the stagger widens to `MIN_GAP_S` and the
cycle lengthens accordingly — the floor and the gap are both hard.

An account that errors retries at `60, 120, 240, 480` seconds, then holds at `480`.
Backoff retries do **not** count toward the window cap. A success resets the backoff.
An exhausted account (limits at 100 %) polls at its reset time, not on the cycle.

**What governs live requests (0.2.1).** `claude-swap` measured the usage endpoint's
budget at ~28–30 requests per identity per rolling hour, and the budget is per identity,
not per process. The `Scheduler` above is still not driven by any surface. Live requests
are governed instead by the shared cache in `usage/<alias>.json`, consulted per account
inside `envelope.collect`, so the CLI, the TUI and the web dashboard all obey it:

- An answer younger than `USAGE_FRESH_S` (`DASHBOARD_INTERVAL_S - MIN_GAP_S`, 170 s) is
  served without a request, whichever process fetched it. One account costs at most one
  request per ~3 min however many dashboards and `vibe list` runs are going; two
  processes whose caches expire in the same instant can each spend one.
- A 429 or 5xx sets `retry_at` on the `BACKOFF_S` ladder above (60/120/240/480 s, then
  holds at 480), or at `Retry-After` when the server names a longer wait, never more than
  `USAGE_WAIT_CAP_S` (3600 s) ahead. Until then no request goes out for that account. The
  ladder lives on disk, so a restart does not reset it, and a `retry_at` further ahead than
  the cap is not obeyed. A success clears it.
- A 401, 403 or other 4xx sets no backoff and shows no numbers: the token was refused, and
  freezing the last answer as `ok` would hide that. A 401 or 403 says `the usage endpoint
  answered HTTP <status> - the token was refused, run: vibe add <alias>`; another 4xx names
  no recovery.
- A network failure or a body that is not JSON sets no backoff; the next attempt still
  waits for `USAGE_FRESH_S` to run out like any other.
- A cached answer is neither fresh nor shown when its `fetched_at` is in the future (the
  clock stepped back) or more than `USAGE_WAIT_CAP_S` old.
- The profile request that fills in a missing plan goes out only on a cycle that is
  already fetching usage.
- (0.3.0) **Faster polling of the live account.** The auto-switch tick (§17) names the live
  account to `envelope.collect(..., fast=...)` when its cached answer already has a
  `session` or `weekly_all` row at 90% or more whose reset is still ahead or unknown. For
  that account only, an answer is reused for `POLL_FLOOR_S` (60 s) instead of
  `USAGE_FRESH_S`, so it is asked at most once a minute. This is one parameter (`fresh_s`)
  on the existing `_usage_view` path, not a second fetcher, and the backoff check still
  comes before any fetch, so a 429 outranks it. Every other surface passes nothing and
  behaves as before.
  **Effective cadence is 60 to 120 s, not 60 s.** An answer is reused while `age < 60`, so
  a tick that a scheduler fires even slightly early reuses it and the next request waits
  for the tick after: between one and two intervals, by scheduler jitter. Up to 60
  requests an hour for the hot account at the fast end, against the ~28-30 an hour measured
  for one identity (above), for as long as it stays at 90% or more. A 429 puts it on the
  backoff ladder, which is obeyed. This is a known cost of being fast where it matters,
  not a guarantee that the budget holds; it was not measured against the live endpoint.

---

## 15. CLI surface — `cli.py` (D)

```
vibe --version                          AC1: matches importlib.metadata and pyproject
vibe                                    opens the Textual dashboard
vibe add [alias]                        bare: adopt the login already in Claude Code
                                        with alias: fresh browser login (paste-the-code)
vibe list [--json]
vibe switch <alias> [--json]
vibe run <alias> -- <cmd> [args…]
vibe remove <alias> [--json]
vibe alias <old> <new> [--json]
vibe usage --once [--json]
vibe usage web [--host 127.0.0.1] [--port N]
vibe auto on|off [--json]                (0.3.0, §17)
vibe auto status [--json]
vibe auto exclude <alias> [--json]
vibe auto include <alias> [--json]
vibe auto undo [--json]
vibe auto tick [--json]
vibe auto install [--json]               macOS only
vibe auto uninstall [--json]             macOS only
```

`web` is a positional, which is what AC13 is literally written against. `--json` on `run`
must precede the alias (`vibe run --json work -- claude`): everything after the alias is
the child's, by `argparse.REMAINDER`.

`Context` carries a `clock: Callable[[], float] = time.time` alongside `now_s`. A one-shot
command reads `now_s` and never calls the clock; the two surfaces that stay up for days
read the clock each cycle. Without it the web dashboard would evaluate every token's
expiry against process start, and neither long-lived surface could be driven by a fake
clock in a test.

`--json` is accepted on every command that reports state. It emits the §10 envelope on
success and the §10 error envelope on failure, and it is the only output on stdout.

**`vibe add` (bare)** adopts the credential already in the Keychain plus the identity in
`~/.claude.json`. It makes **no network request**, so it works offline. The alias is
derived from the identity's email local part, slugified to `valid_alias`, with a
numeric suffix on collision.

**`vibe run <alias> -- <cmd>`** refreshes the alias if it is inside the expiry buffer,
then runs `<cmd>` with `CLAUDE_CODE_OAUTH_TOKEN` set to that alias's access token in the
**child environment only**. The global Keychain credential is untouched, so concurrent
shells on different accounts do not collide. Decided by the owner, 2026-09-15. Two
limits are documented in the README and the runbook rather than hidden: the child cannot
refresh the token itself, so a session outliving the access token needs a re-run; and the
variable bypasses account OAuth, so the child does not read the Keychain at all.

**`vibe switch <alias>`** performs the §8 resync-then-overwrite order and updates the
`active` pointer. (0.3.0) It first calls `oauth.ensure_usable` (§8), so an account whose
login is dead is refused with `vibe add <alias>` before the port is touched, and a login
vibe does not know is refused with `vibe add` (§8, step 1b). There is no `--force`: the
existing tests stay green without one, and an override is how a login gets lost.

---

## 16. Rules for writers

1. Read this file first. Resolve every question against it.
2. Touch only the files your slice owns (§2). `pyproject.toml` and `uv.lock` are the
   integrator's alone — a writer that edits either breaks `uv sync --locked` for
   everyone.
3. Write the failing test first, paste the failure, commit it, then write the minimum
   that passes.
4. Run the §0 check command over your slice and **paste its raw output**. A claim of
   "tests pass" without output above it does not count.
5. Never `Any`, never `# type: ignore`, never `cast` to silence the checker.
6. Validate at two boundaries only: JSON from the network, and files the user wrote.
   No defensive checks on internal call sites.
7. No abstraction with one implementation, no config for a value that never changes,
   no plugin system, no feature flags.
8. Every user-facing error names the problem **and** the literal recovery command.
9. Comments only where the reasoning is not in the code. No docstrings on lines you did
   not change.
10. No test reaches the network, writes the real Keychain, or touches the real
    `~/.claude` or `~/.vibemaxxing`. The autouse guard in `tests/conftest.py` enforces
    this and fails the offending test by name (AC19).
11. Use the shared test doubles in `tests/fakes.py` — `FakeHttpClient` and
    `FakeKeychain` — and the `tmp_home` fixture in `tests/conftest.py`. Do not write a
    second fake for a port that already has one; three incompatible fakes is a merge
    conflict the integrator cannot resolve against anything.
12. Hand off a summary and a path. Never a file dump, never a red slice. Writers never
    merge.

---

## 17. Automatic switching — `auto.py`, `pool.decide` (0.3.0)

Off until `vibe auto on`. When it is on and the live account's `session` or `weekly_all`
row reaches the threshold, a tick switches Claude Code to the eligible account with the
most room. The unit a scheduler calls is `vibe auto tick`; nothing in the package runs it
by itself.

### `settings.json`

```json
{"schema": 1, "auto_switch": false, "threshold": 99, "cooldown_s": 900, "excluded": []}
```

Written with `fsutil.write_private` (`0600`). `threshold` is an integer 50..100,
`cooldown_s` an integer >= 0, `excluded` a list of valid aliases. `auto.read_settings` is
the one place the file is validated, and a file that is missing, unreadable, of another
`schema`, or wrong **anywhere** is the defaults **as a whole** — never an error, so it
cannot stop `vibe list`, and never the valid half, so a hand-edit that is wrong in one
place leaves auto-switch off rather than on. A missing member takes its default.

Reading a bad file as the defaults is right for a read and wrong for a write: `on`, `off`,
`exclude` and `include` go through `auto.settings_for_edit`, which refuses a file that
exists but is invalid (`UsageError`, recovery `rm <path>`) instead of saving the defaults
over what someone wrote by hand. `vibe alias` renames an alias inside `excluded` and inside
`auto.json` (`from`, `to`) with the account, so an exclusion does not silently stop
applying.

### `auto.json`

```json
{"schema": 1, "from": "work", "to": "side", "at": 1757930000.0,
 "reason": "work session at 99%; side is at 10%", "until": 1758102800.0}
```

The last switch, made by a tick or by `vibe auto undo`. `until` is when the rows that
limited `from` have all reset, or `null` when that was not known; the account just left
stays out of the running until then, whatever a fresh reading of it says. An unreadable
file is no record, and so is one whose `at` is not a time a clock could have written
(`auto.read_last` validates both times, because `vibe list`, the TUI and the web page all
format `at` and one absurd value would stop every one of them). `until` that is absurd is
read as unknown. An `at` far in the future is no cooldown at all, rather than one that
lasts until then; a few seconds of clock skew still counts as just switched. `history.db` holds only the pooled series (§13) and has no place for a
switch without a schema change, so it records nothing.

### The decision — `pool.decide`

Pure, in `pool.py`, importing no I/O:

```python
decide(readings: Sequence[Reading], *, settings: Settings, live: str | None,
       now_s: float, last: LastSwitch | None) -> Decision      # action, target, reason
next_candidate(...) -> str | None          # where a switch would go, trigger or not
```

`Reading` is `(alias, state, rows | None, updated_at, identified)`; `rows` is `None` when
there is no usable answer. `identified` is false for an account stored without a uuid:
after landing on one, `~/.claude.json` still names the account that was left, so vibe could
not tell who is live. Such an account is never chosen as a target. Rules, in order; the
first that applies wins:

1. `auto_switch` is false: stay, `auto-switch is off`.
2. The live alias is unknown: stay, `live account unknown`.
3. The **only** trigger is the live account's `session` or `weekly_all` row at or above
   the threshold; `weekly_scoped` and every other kind never trigger. No usable answer for
   the live account: stay, `no fresh numbers for <alias>`; below the threshold: stay,
   `<alias> is below <n>%`. A reading is a floor until its own row resets, since usage
   only falls when the window rolls over: a trigger row whose reset is still ahead (or
   unknown) counts however old the reading is, and one whose reset has passed says nothing
   about now, so it reads as `no fresh numbers`. Nothing is ever switched on a guess.
4. Within `cooldown_s` of the last switch: stay, `cooldown: last switch <n>s ago`.
5. A candidate is state `ok` (for a non-active account, `collect` has already refused a
   lapsed login and refreshed an expiring token, so `ok` is the usability check), not
   excluded, `identified`, not the live account, with an answer fetched within `USAGE_FRESH_S` and not
   from the future, with a known percentage on every `session`/`weekly_all` row and none
   within 5 points of the threshold (at or above `threshold - 5`), and not the account just left before its `until`.
6. The candidate with the lowest `max(session, weekly_all)` wins, ties by alias; none:
   stay, `no account with headroom`. Percentages are compared across accounts here and
   nowhere else.

### The tick — `auto.tick`

```python
tick(root, *, client, port, now_s, notify) -> TickResult        # line, switched
```

Order: settings (off: stay, before anything is fetched) → `store.live_alias` (unknown:
stay) → the pointer check → `collect` with the live account `fast` when its cached answer
is at 90% on a trigger row (§14) → `pool.decide` → `oauth.ensure_usable` → a re-check that
the live account and `active` are still the ones decided about → `store.switch` →
`auto.json` → the notification. It prints one line, `stay: <reason>` or
`switched <from> -> <to>: <reason>`, and exits 0 in every case that is not a bug.

- **The pointer check.** If `active` is not the live account the tick runs `store.switch`
  onto the live account first, which resyncs the live login into its file and writes the
  pointer, then carries on. Left as it was, polling would treat `active` as the account in
  use and the live one as any other, whose expired token it would then refresh behind
  Claude Code's back (§8), while `active` itself was never refreshed and lapsed. A refused
  repair (`UsageError`, `NeedsLoginError`, `NotFoundError`) is `stay: Claude Code runs as
  <live> but vibe's active account is <active> and repairing it failed: <message> - run:
  vibe switch <live>`.
- **A refused switch is a stay.** `UsageError`, `NeedsLoginError`, `NetworkError` and
  `NotFoundError` from `ensure_usable` or `store.switch` — a claim held, a login vibe does
  not know, a target that turned out dead or was removed — become `stay: <message>` and are
  not retried in the same tick. A `StoreError`, or the plain `VibeError` the Keychain port
  raises when `security` fails, is not caught here: it reaches `cli.main`'s handler, which
  prints the message and its recovery and exits non-zero, so a scheduler sees a port or
  store that is broken rather than a quiet stay. Anything else is a bug and propagates.
- **The live account is re-checked before the write.** `collect` and a refresh can take
  seconds, and a person may have run `vibe switch` meanwhile. If `store.live_alias` or
  `active` no longer names the account the decision was about, the tick stays
  (`the live account changed while checking, so <target> was left alone`).
- **A switch that landed but could not be recorded** still notifies and still counts as
  switched; the line ends `(not recorded: <ErrorClass>)`, and the cost is that no cooldown
  or `until` exists for it.
- **The Keychain** is written only by `store.switch`, which holds the claims (§8). There is
  no other writer.
- **The notification** runs only after a switch landed: `/usr/bin/osascript`, the message
  passed as an argument of the script and never inside its text, a timeout, every failure
  swallowed, silent off macOS. `cli.Context.notify` carries it so a test never raises one.

### The other commands

- `vibe auto on|off` flips `auto_switch`. `exclude <alias>` needs an existing account
  (`NotFoundError`, `vibe list`); `include <alias>` always succeeds.
- `vibe auto status` prints the state, threshold, cooldown, exclusions, the live account
  (and, when it differs, the `active` pointer and the `vibe switch` that repairs it), the
  last switch and `next`, where a switch would go. It collects only when the pointer agrees
  with the live account, for the reason above.
- `vibe auto undo` switches back to `from` of the last record through `ensure_usable` and
  `store.switch`, and records itself as a switch (`reason` `undo`), so the cooldown starts
  again and the next tick does not walk back out. With no record: `UsageError`,
  `vibe auto status`.
- `vibe auto install` (macOS only; otherwise `UsageError`, `vibe auto tick`) writes
  `~/Library/LaunchAgents/de.intra-ai.vibemaxxing-auto.plist`, label
  `de.intra-ai.vibemaxxing-auto`, running `<this interpreter> -m vibemaxxing auto tick`
  with `StartInterval` 60, and **prints** `launchctl bootstrap gui/<uid> <plist>`. It never
  runs `launchctl`: loading a job is the operator's. `uninstall` removes the file and prints
  `launchctl bootout gui/<uid>/<label>`; it does not unload anything either.

### Not verified, and not claimed

`scripts/verify_auto_live.py` performs a real switch between two throwaway accounts on a
real Keychain and asserts that the port holds the incoming blob; it refuses to run without
`VIBE_LIVE_OK=1` and never runs from pytest. Nothing here has been run against a real
Keychain, a running `claude` or a loaded launchd job. In particular **untested**: whether
a `claude` session that is already running picks up a switch, and whether an old session
refreshing its own token afterwards overwrites the Keychain with the outgoing account.

### Known limits

- The owner of the port's login is read before the claims it joins (§8); a `claude /login`
  in that instant resyncs into an account whose claim is not held.
- Marking a lineage dead (§9) is a compare-then-write with no lock; a `vibe add` landing in
  the microseconds between the two is overwritten.
- The unknown-login check abstains when any stored account has no uuid (§8, step 1b). So
  "never overwrites a login vibe does not know" holds only while every stored account has a
  uuid.
- Auto-switch needs the active account's stored uuid: without it `store.live_owner`, and so
  `store.live_alias`, cannot name the live account and every tick stays at `live account
  unknown`. It is also never switched onto an account stored without one.
- "Never switches onto a dead account" is proven for a lapsed login, one the server already
  rejected, and an expired access token that fails to refresh. A refresh token the server
  would reject but has not been asked about passes until it is.
- A tick stays while `active` disagrees with the live account (`vibe switch <live>`
  repairs it). Until then no automatic switch happens.
- Polls (`collect`, so `vibe list`, the TUI and the web page) treat the account `active`
  names as the active one. If Claude Code runs another account, that account's expired
  token can still be refreshed by one of those polls. The tick stays rather than poll in
  that state; the other surfaces do not.
- A refresh whose response is lost (a POST that times out after the server rotated the
  token) still loses its successor. Auto-switch neither causes nor fixes that.

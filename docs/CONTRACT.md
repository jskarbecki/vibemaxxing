# vibemaxxing — frozen contract

Every writer reads this file before touching code. The integrator resolves conflicts
**against this document**, not by preference. A decision that is not written here is
not frozen; ask the integrator rather than inventing one.

Version: 1 (frozen 2026-09-15, Phase A).

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
  pool.py          C2  pool_remaining, dry_in
  history.py       C3  SQLite samples, prune
  poll.py          C3  Scheduler (floor, stagger, backoff)
  envelope.py      D   the --json envelope, shared by CLI and web
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
```

Letters are phases: **B** solo skeleton, **C1/C2/C3** the three Phase-C writers,
**D** solo CLI, **E1/E2** the two Phase-E writers, **G** solo hardening.

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
         cli -> tui, web
```

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
   - `usage.fetch_usage()` — the `Authorization` header
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
  active                        0600   one line: the alias whose credential is live
  history.db                    0600   the sample series
```

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

1. Read the port once. Keep the whole blob — it has siblings (§7).
2. Parse its `claudeAiOauth` and write it back to the **outgoing** account's file.
   Claude Code rotates the active token behind our back; skipping this resync loses that
   rotation and strands the outgoing account on a spent token.
3. Build the incoming blob with `credential_to_blob(incoming, base=the blob from step 1)`
   so `mcpOAuth` and any future sibling survive.
4. Write the port.
5. Update the `active` pointer.

The resync in step 2 happens **before** the overwrite in step 4. A test with an
in-memory fake port asserts that ordering.

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
`new_state()` (`secrets.token_urlsafe(16)`).

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
literal recovery command `vibe add <alias>` in its message.
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
      "plan": "max",
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
  }
}
```

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
`old`/`new` for `alias`, plus `exit_code` for `run` and `adopted` for `add`).

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

---

## 12. Usage and pool — `usage.py`, `pool.py` (C2)

```python
USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER: Final = "oauth-2025-04-20"

def fetch_usage(client: HttpClient, token: Secret, *, timeout_s: float = 10.0) -> dict[str, object]: ...

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
meaningful because a stash only ever holds a server-issued successor, so the seeded
sentinel appearing there would be a real bug.

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

> Note for the record, not a change: `claude-swap` measured the usage endpoint's budget
> at ~28–30 requests per identity per rolling hour. A 60 s floor per account is 60/hour
> and can therefore draw 429s under sustained polling. The 60 s floor is a frozen
> product decision and AC16 tests it; this note exists so the number is not mistaken for
> an oversight.

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
`active` pointer.

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

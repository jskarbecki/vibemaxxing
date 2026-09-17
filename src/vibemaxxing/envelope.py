"""The one snapshot every surface renders: the CLI, the TUI and the web page.

`collect` does the I/O and `build` turns the result into the JSON document frozen
in docs/CONTRACT.md section 10. `vibe list --json` and `GET /api/usage` emit the
same bytes because they call the same two functions.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from vibemaxxing import credentials, oauth, store, usage
from vibemaxxing.credentials import EMPTY_IDENTITY, Credential, Identity
from vibemaxxing.errors import StoreError, VibeError
from vibemaxxing.httpclient import HttpClient, HTTPError
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState
from vibemaxxing.poll import BACKOFF_S, USAGE_FRESH_S, USAGE_WAIT_CAP_S
from vibemaxxing.pool import WEEKLY_ALL_KIND, AccountUsage, pool_remaining
from vibemaxxing.redact import scrub
from vibemaxxing.usage import Summary

ENVELOPE_SCHEMA: Final = 1

# The refresh lineage is dead or the login itself lapsed. Only a fresh /login
# fixes either, so the recovery command is the same one.
_DEAD_REFRESH: Final = ("invalid_grant", "no_refresh_token")


@dataclass(frozen=True)
class AccountView:
    alias: str
    active: bool
    state: AccountState
    message: str | None
    identity: Identity
    plan: str | None
    summary: Summary | None
    updated_at: float | None


def _iso(at_s: float | None) -> str | None:
    if at_s is None:
        return None
    return datetime.fromtimestamp(at_s, UTC).isoformat(timespec="seconds")


def _claude_identity(stored: Identity) -> Identity:
    """Fill in an active account's identity from ~/.claude.json.

    Only when the uuids already agree: a fresh login that Claude Code has never
    run would otherwise adopt whichever account happens to be named in that file,
    which pairs one account's name with another account's token silently.
    """
    live = credentials.read_claude_identity(Path.home() / ".claude.json")
    if live.account_uuid is not None and live.account_uuid == stored.account_uuid:
        return live
    return stored


def _fill_plan(root: Path, alias: str, credential: Credential, client: HttpClient) -> str | None:
    """The plan label, fetching and storing it once for an account that has none.

    An account imported from Claude Code arrives with subscriptionType already
    set; one added through our own login arrives without it, which is why some
    cards showed a plan and some showed nothing. The profile endpoint knows, so
    the first fetch after a login backfills the credential and every later poll
    reads it off disk — no extra request per poll, and no plan-less card.
    """
    if credential.subscription_type is not None:
        return credentials.plan_label(credential.subscription_type, credential.rate_limit_tier)
    try:
        subscription, tier = usage.fetch_plan(client, credential.access_token)
    except (VibeError, HTTPError):
        # Decoration. A profile endpoint having a bad day must not cost the
        # account its usage rows.
        return None
    if subscription is None:
        return None
    try:
        # Re-read rather than reuse the account this view opened with: a refresh
        # may have rotated the token since, and writing the stale one back would
        # hand the next run a token the server has already killed.
        # ponytail: no lock, so a refresh landing inside this window loses its
        # plan backfill and refetches on the next poll. Cheap, and never a token.
        stored = store.read_account(root, alias)
        store.write_account(
            root,
            replace(
                stored,
                credential=replace(
                    stored.credential, subscription_type=subscription, rate_limit_tier=tier
                ),
            ),
        )
    except (VibeError, OSError):
        pass
    return credentials.plan_label(subscription, tier)


def _needs_login(alias: str) -> str:
    return f'the login for "{alias}" has lapsed — run: vibe add {alias}'


def _usable_credential(
    root: Path, alias: str, credential: Credential, client: HttpClient, now_ms: int
) -> tuple[Credential | None, str | None]:
    """Return the credential to use, or a message saying why there is none."""
    if credentials.login_lapsed(credential, now_ms=now_ms):
        return None, _needs_login(alias)
    if not credentials.is_expired(credential, now_ms=now_ms):
        return credential, None
    outcome = oauth.refresh(root, alias, credential, client, now_ms=now_ms)
    if outcome.credential is not None:
        return outcome.credential, None
    if outcome.error in _DEAD_REFRESH:
        return None, _needs_login(alias)
    if outcome.error == "active":
        # A `vibe switch` to this account landed mid-poll; the next poll reads it
        # as the active account.
        return None, "this account just became active - run: vibe list"
    if outcome.error == "invalid_client":
        # Systemic: our client id was rejected. Not this account's fault, so it
        # must not read as "go and log in again".
        return None, "the OAuth client id was rejected — run: vibe --version and report it"
    return None, f"could not refresh the token ({outcome.error}) — run: vibe list"


def _hhmm(at_s: float) -> str:
    # Local time: every surface that shows this runs on the same machine as the
    # process that wrote it, and an absolute time never goes stale on screen.
    return datetime.fromtimestamp(at_s).strftime("%H:%M")


def _transient(status: int) -> bool:
    # A 429 or a 5xx says "not now"; the account's last numbers are still its
    # numbers. A 401 or 403 says the token is refused, and freezing the old
    # numbers as ok would hide that for as long as the backoff runs.
    return status == 429 or status >= 500


def _usage_view(
    root: Path,
    account: store.Account,
    credential: Credential,
    client: HttpClient,
    now_s: float,
    base: AccountView,
    *,
    can_fetch_why: str = "",
) -> AccountView:
    """Serve an account's usage from the shared cache, the endpoint, or the last answer.

    Fresh cache: no request. Inside a backoff: no request, last answer shown with
    its time. Otherwise fetch; a 429 or 5xx starts or extends the backoff (the
    60/120/240/480 s ladder of CONTRACT section 14, or Retry-After if longer,
    never past USAGE_WAIT_CAP_S) and, when an answer under USAGE_WAIT_CAP_S old
    exists, still shows it rather than blanking the account.
    """
    alias = account.alias
    cached = store.read_usage_cache(root, alias)
    # Another login under the same alias -- `vibe add` over it, or a fetch that
    # landed after `vibe remove` -- has another added_at. Its numbers are not ours.
    if cached is not None and cached.added_at != account.added_at:
        cached = None
    kept_at = cached.fetched_at if cached is not None else None
    kept = cached.payload if cached is not None else None
    # Negative when the clock has stepped back past the fetch: that answer's age is
    # unknown, so it is neither fresh nor shown.
    age_s = None if kept_at is None else now_s - kept_at

    def last_answer(problem: str, recovery_text: str) -> AccountView:
        if kept is None or kept_at is None or age_s is None or not 0 <= age_s <= USAGE_WAIT_CAP_S:
            return replace(base, state=AccountState.ERROR, message=recovery_text)
        return replace(
            base,
            message=f"{problem} - showing usage from {_hhmm(kept_at)}",
            summary=usage.summarize(kept),
            updated_at=kept_at,
        )

    if kept is not None and age_s is not None and 0 <= age_s < USAGE_FRESH_S:
        return replace(base, summary=usage.summarize(kept), updated_at=kept_at)
    wait_until = cached.retry_at if cached is not None else None
    # A retry_at past the cap was not written by this code, so it is not obeyed.
    if (
        cached is not None
        and wait_until is not None
        and now_s < wait_until <= now_s + USAGE_WAIT_CAP_S
    ):
        problem = cached.message or f"backing off - next try {_hhmm(wait_until)}"
        return last_answer(problem, problem)

    if can_fetch_why:
        return last_answer(can_fetch_why, can_fetch_why)

    # Only on a poll that is about to spend a request anyway: a plan-less account
    # would otherwise ask the profile endpoint on every cached or waiting cycle.
    # Never for the active account: the backfill rewrites the account file, and a
    # `vibe switch` resync landing between its read and write would be undone.
    if not base.active:
        base = replace(base, plan=_fill_plan(root, alias, credential, client) or base.plan)

    # ponytail: no cross-process lock, so two processes whose caches expire in the
    # same instant both fetch once. Bounded at one extra request per process per
    # interval; a claim file like locks/<alias>.claim if that ever shows in a 429.
    try:
        payload = usage.fetch_usage(client, credential.access_token)
    except VibeError as exc:
        # Offline, or a body that is not JSON: no backoff. The next cycle may try
        # again, and USAGE_FRESH_S already bounds how often that is.
        return last_answer(scrub(exc.message), exc.render())
    except HTTPError as exc:
        # UrllibClient raises HTTPError, which is not a VibeError, on every 4xx
        # and 5xx. Containing it here rather than in each surface is what makes
        # the CLI, the TUI and the web page behave the same on a bad day.
        if not _transient(exc.status):
            return replace(
                base,
                state=AccountState.ERROR,
                message=f"the usage endpoint answered HTTP {exc.status} - run: vibe list",
            )
        failures = (cached.failures if cached is not None else 0) + 1
        step_s = BACKOFF_S[min(failures, len(BACKOFF_S)) - 1]
        retry_at = now_s + min(max(step_s, exc.retry_after_s or 0.0), USAGE_WAIT_CAP_S)
        # No "run: vibe list": another request is what drew this, and the backoff
        # already decides when the next one goes out.
        what = "rate limited" if exc.status == 429 else f"usage endpoint answered HTTP {exc.status}"
        problem = f"{what} - next try {_hhmm(retry_at)}"
        # Re-read: another process may have landed a newer answer while this one
        # was in flight, and writing ours back would throw it away.
        latest = store.read_usage_cache(root, alias)
        if (
            latest is not None
            and latest.added_at == account.added_at
            and latest.fetched_at is not None
            and (kept_at is None or latest.fetched_at > kept_at)
        ):
            kept_at, kept = latest.fetched_at, latest.payload
            age_s = now_s - kept_at
        store.write_usage_cache(
            root,
            alias,
            store.UsageCache(
                added_at=account.added_at,
                fetched_at=kept_at,
                payload=kept,
                retry_at=retry_at,
                failures=failures,
                message=problem,
            ),
        )
        return last_answer(problem, problem)
    store.write_usage_cache(
        root,
        alias,
        store.UsageCache(
            added_at=account.added_at,
            fetched_at=now_s,
            payload=payload,
            retry_at=None,
            failures=0,
            message=None,
        ),
    )
    return replace(base, summary=usage.summarize(payload), updated_at=now_s)


def active_token(
    root: Path, account: store.Account, port: KeychainPort, *, now_ms: int, buffer_ms: int = 0
) -> tuple[Credential | None, str]:
    """A token for the active account that needs no refresh, or why there is none.

    The refresh token rotates on every refresh, so a lineage survives one
    refresher, and for the active account that is Claude Code. A second one here
    is how an account read "login lapsed" the morning after `vibe add`, and the
    other way round it logs Claude Code out. So nothing here refreshes and nothing
    here writes (oauth.refresh refuses the active alias too): `vibe switch` is the
    one place the store is resynced from the port.

    The port's token is borrowed only when ~/.claude.json and the account name the
    same uuid, both known, and the token is not a copy of another account's stored
    one. Unknown is not agreement, and `vibe switch` writes the port a moment
    before it writes ~/.claude.json and `active`: in between, the port holds the
    incoming account's token under the outgoing account's name. Otherwise the
    stored access token, until it expires.
    """
    read_problem = None
    live = None
    try:
        raw = port.read()
    except VibeError as exc:
        raw, read_problem = None, exc.message
    stored_uuid = account.identity.account_uuid
    if raw is not None and stored_uuid is not None:
        live_uuid = credentials.read_claude_identity(Path.home() / ".claude.json").account_uuid
        if live_uuid == stored_uuid:
            with contextlib.suppress(StoreError):
                live = credentials.parse_blob(raw)
    if live is not None and _stored_elsewhere(root, account.alias, live):
        live = None
    for candidate in (live, account.credential):
        if candidate is not None and now_ms + buffer_ms < candidate.expires_at_ms:
            return candidate, ""
    if read_problem is not None:
        return None, f"token expired and Claude Code's credential is unreadable: {read_problem}"
    if stored_uuid is None:
        # Nothing ties Claude Code's token to this account, so its refreshes can
        # never be borrowed. Switching away lets vibe refresh this account again.
        return None, "token expired - no account id stored to match Claude Code's login"
    return None, "token expired - vibe leaves the active account's token to Claude Code"


def _stored_elsewhere(root: Path, alias: str, live: Credential) -> bool:
    for other in store.list_aliases(root):
        if other == alias:
            continue
        with contextlib.suppress(VibeError):
            if store.read_account(root, other).credential.access_token == live.access_token:
                return True
    return False


def _view(
    root: Path,
    alias: str,
    *,
    active: bool,
    client: HttpClient,
    port: KeychainPort,
    now_s: float,
    fetch: bool,
) -> AccountView:
    now_ms = int(now_s * 1000)
    try:
        account = store.read_account(root, alias)
    except VibeError as exc:
        return AccountView(
            alias, active, AccountState.ERROR, exc.render(), EMPTY_IDENTITY, None, None, None
        )

    identity = _claude_identity(account.identity) if active else account.identity
    plan = credentials.plan_label(
        account.credential.subscription_type, account.credential.rate_limit_tier
    )

    if active:
        token, problem = active_token(root, account, port, now_ms=now_ms)
        base = AccountView(alias, active, AccountState.OK, None, identity, plan, None, None)
        if not fetch:
            return base
        return _usage_view(
            root, account, token or account.credential, client, now_s, base, can_fetch_why=problem
        )

    try:
        # Inside the guard: read_stash on an unreadable or unknown-schema stash
        # raises StoreError, and one broken account must not blank every other
        # account's view.
        credential, message = _usable_credential(root, alias, account.credential, client, now_ms)
    except VibeError as exc:
        return AccountView(
            alias, active, AccountState.ERROR, exc.render(), identity, plan, None, None
        )
    if credential is None:
        dead = message is not None and "vibe add" in message
        state = AccountState.NEEDS_LOGIN if dead else AccountState.ERROR
        return AccountView(alias, active, state, message, identity, plan, None, None)

    if not fetch:
        return AccountView(alias, active, AccountState.OK, None, identity, plan, None, None)

    base = AccountView(alias, active, AccountState.OK, None, identity, plan, None, None)
    return _usage_view(root, account, credential, client, now_s, base)


def collect(
    root: Path, *, client: HttpClient, port: KeychainPort, now_s: float, fetch: bool = True
) -> list[AccountView]:
    active = store.read_active(root)
    return [
        _view(
            root,
            alias,
            active=alias == active,
            client=client,
            port=port,
            now_s=now_s,
            fetch=fetch,
        )
        for alias in store.list_aliases(root)
    ]


def account_entry(view: AccountView) -> dict[str, object]:
    summary = view.summary
    return {
        "alias": view.alias,
        "active": view.active,
        "state": str(view.state),
        "message": view.message,
        "email": view.identity.email,
        "display_name": view.identity.display_name,
        "organization": view.identity.organization_name,
        "plan": view.plan,
        "updated_at": _iso(view.updated_at),
        "rows": [
            {
                "kind": row.kind,
                "label": row.label,
                "percent": row.percent,
                "severity": row.severity,
                "resets_at": row.resets_at,
            }
            for row in (summary.rows if summary else ())
        ],
        "breakdown": [
            {"label": row.label, "percent": row.percent}
            for row in (summary.breakdown if summary else ())
        ],
    }


def pool_weeks(views: Sequence[AccountView]) -> float:
    return round(
        pool_remaining(
            AccountUsage(state=v.state, rows=v.summary.rows if v.summary else ()) for v in views
        ),
        2,
    )


def _epoch(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        # An unparseable stamp drops that one reset off the list. The server owns
        # this string's format, and guessing at a new one would place a marker at
        # an invented moment rather than omit it.
        return None


def resets(views: Sequence[AccountView]) -> list[dict[str, object]]:
    """Each account's weekly rollover, soonest first.

    The ``weekly_all`` row only. The session row rolls over every few hours and
    would bury the weekly ones it overlaps, and ``weekly_scoped`` rolls over
    within a minute of ``weekly_all`` on the same account.
    """
    found: list[tuple[float, dict[str, object]]] = []
    for view in views:
        if view.state is not AccountState.OK or view.summary is None:
            continue
        for row in view.summary.rows:
            if row.kind != WEEKLY_ALL_KIND:
                continue
            at_s = _epoch(row.resets_at) if row.resets_at else None
            if at_s is not None:
                found.append((at_s, {"alias": view.alias, "at": _iso(at_s)}))
            break
    return [entry for _, entry in sorted(found, key=lambda pair: pair[0])]


def build(
    views: Sequence[AccountView], *, now_s: float, dry_in: timedelta | None = None
) -> dict[str, object]:
    return {
        "schema": ENVELOPE_SCHEMA,
        "generated_at": _iso(now_s),
        "accounts": [account_entry(v) for v in views],
        "pool": {
            "accounts": len(views),
            "remaining_account_weeks": pool_weeks(views),
            "dry_in_seconds": None if dry_in is None else int(dry_in.total_seconds()),
        },
        "resets": resets(views),
    }


def error_payload(error: VibeError) -> dict[str, object]:
    return {
        "schema": ENVELOPE_SCHEMA,
        "error": {
            "code": error.code,
            "message": scrub(error.message),
            "recovery": error.recovery,
        },
    }


def dumps(payload: Mapping[str, object]) -> str:
    # Last line of defence: a token that reached the payload by some route nobody
    # anticipated still does not leave this function.
    return scrub(json.dumps(payload, indent=2, sort_keys=False))

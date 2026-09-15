"""The one snapshot every surface renders: the CLI, the TUI and the web page.

`collect` does the I/O and `build` turns the result into the JSON document frozen
in docs/CONTRACT.md section 10. `vibe list --json` and `GET /api/usage` emit the
same bytes because they call the same two functions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from vibemaxxing import credentials, oauth, store, usage
from vibemaxxing.credentials import EMPTY_IDENTITY, Credential, Identity
from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HttpClient
from vibemaxxing.models import AccountState
from vibemaxxing.pool import AccountUsage, pool_remaining
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
    if outcome.error == "invalid_client":
        # Systemic: our client id was rejected. Not this account's fault, so it
        # must not read as "go and log in again".
        return None, "the OAuth client id was rejected — run: vibe --version and report it"
    return None, f"could not refresh the token ({outcome.error}) — run: vibe list"


def _view(
    root: Path, alias: str, *, active: bool, client: HttpClient, now_s: float, fetch: bool
) -> AccountView:
    now_ms = int(now_s * 1000)
    try:
        account = store.read_account(root, alias)
    except VibeError as exc:
        return AccountView(
            alias, active, AccountState.ERROR, exc.render(), EMPTY_IDENTITY, None, None, None
        )

    identity = _claude_identity(account.identity) if active else account.identity
    plan = account.credential.subscription_type

    credential, message = _usable_credential(root, alias, account.credential, client, now_ms)
    if credential is None:
        dead = message is not None and "vibe add" in message
        state = AccountState.NEEDS_LOGIN if dead else AccountState.ERROR
        return AccountView(alias, active, state, message, identity, plan, None, None)

    if not fetch:
        return AccountView(alias, active, AccountState.OK, None, identity, plan, None, None)

    try:
        payload = usage.fetch_usage(client, credential.access_token)
    except VibeError as exc:
        return AccountView(
            alias, active, AccountState.ERROR, exc.render(), identity, plan, None, None
        )
    return AccountView(
        alias, active, AccountState.OK, None, identity, plan, usage.summarize(payload), now_s
    )


def collect(
    root: Path, *, client: HttpClient, now_s: float, fetch: bool = True
) -> list[AccountView]:
    active = store.read_active(root)
    return [
        _view(root, alias, active=alias == active, client=client, now_s=now_s, fetch=fetch)
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

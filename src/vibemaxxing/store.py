"""The on-disk store: accounts, stashed successors, the active pointer, the claim.

Root is ``~/.vibemaxxing``. There is no environment override — tests set ``HOME``.
Every file is created 0600 and every directory 0700, and every write is atomic:
a fresh temp file, fsynced, then ``os.replace``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from vibemaxxing.credentials import (
    Credential,
    Identity,
    credential_to_blob,
    credential_to_disk,
    parse_blob,
    parse_blob_members,
    parse_credential,
    read_claude_identity,
    write_claude_identity,
)
from vibemaxxing.errors import NotFoundError, StoreError, UsageError
from vibemaxxing.fsutil import private_dir, write_private
from vibemaxxing.keychain import KeychainPort
from vibemaxxing.models import AccountState

SCHEMA_VERSION: Final = 1
CLAIM_LEASE_S: Final = 30.0

# Lower case only. An alias is a filename, and macOS's APFS is case-insensitive:
# with a case-sensitive comparison in code, `vibe add Jan` reports itself free
# against an existing `jan`, then os.replace overwrites accounts/jan.json and the
# first account's refresh token is gone with nothing said. Rejecting the mixed
# case outright is the only version of this that is unambiguous on every
# filesystem, and it is louder than silently folding the name the user typed.
_ALIAS_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Account:
    alias: str
    credential: Credential
    identity: Identity
    state: AccountState
    message: str | None
    added_at: float


def valid_alias(alias: str) -> bool:
    return _ALIAS_RE.match(alias) is not None


def _checked(alias: str) -> str:
    # The single boundary between a user-supplied alias and a path. Guarding here
    # rather than in each caller is what makes traversal impossible by construction.
    if not valid_alias(alias):
        folded = re.sub(r"[^a-z0-9._-]", "-", alias.lower()).strip("-.") or "account"
        raise UsageError(
            f"{alias!r} is not a usable alias: lower-case letters, digits, dot, dash "
            "and underscore only, up to 64 characters, starting with a letter or digit. "
            f"Try {folded!r}.",
            "vibe list",
        )
    return alias


def store_root() -> Path:
    return Path.home() / ".vibemaxxing"


def account_path(root: Path, alias: str) -> Path:
    return root / "accounts" / f"{_checked(alias)}.json"


def stash_path(root: Path, alias: str) -> Path:
    return root / "stash" / f"{_checked(alias)}.json"


def history_path(root: Path) -> Path:
    return root / "history.db"


def usage_path(root: Path, alias: str) -> Path:
    return root / "usage" / f"{_checked(alias)}.json"


def _claim_path(root: Path, alias: str) -> Path:
    return root / "locks" / f"{_checked(alias)}.claim"


def _ensure_parent(path: Path, root: Path) -> None:
    private_dir(root)
    if path.parent != root:
        private_dir(path.parent)


def _write_json(path: Path, root: Path, payload: Mapping[str, object]) -> None:
    _ensure_parent(path, root)
    write_private(path, json.dumps(payload, indent=2) + "\n")


def _read_json(path: Path, alias: str) -> dict[str, object]:
    recovery = f"vibe add {alias}"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFoundError(f'there is no account named "{alias}"', "vibe list") from None
    except OSError as exc:
        raise StoreError(f"{path} is unreadable: {exc.__class__.__name__}", recovery) from None

    parsed: object
    try:
        parsed = json.loads(text)
    except ValueError:
        raise StoreError(f"{path} is not valid JSON", recovery) from None
    if not isinstance(parsed, dict):
        raise StoreError(f"{path} is not a JSON object", recovery)

    raw = {str(key): value for key, value in parsed.items()}
    if raw.get("schema") != SCHEMA_VERSION:
        raise StoreError(
            f"{path} has store schema {raw.get('schema')!r}, "
            f"not {SCHEMA_VERSION} — it is never migrated silently",
            recovery,
        )
    return raw


def _credential_from(raw: Mapping[str, object], path: Path, alias: str) -> Credential:
    member = raw.get("credential")
    if not isinstance(member, dict):
        raise StoreError(f"{path} has no credential object", f"vibe add {alias}")
    try:
        return parse_credential({str(key): value for key, value in member.items()})
    except StoreError as exc:
        raise StoreError(f"{path}: {exc.message}", f"vibe add {alias}") from None


def _optional_str(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def list_aliases(root: Path) -> list[str]:
    try:
        names = [path.stem for path in (root / "accounts").glob("*.json")]
    except OSError:
        return []
    return sorted(name for name in names if valid_alias(name))


def read_account(root: Path, alias: str) -> Account:
    path = account_path(root, alias)
    raw = _read_json(path, alias)

    identity_raw = raw.get("identity")
    identity_map = (
        {str(key): value for key, value in identity_raw.items()}
        if isinstance(identity_raw, dict)
        else {}
    )
    state_raw = raw.get("state")
    state = next((known for known in AccountState if known == state_raw), None)
    if state is None:
        raise StoreError(f"{path} has unknown state {state_raw!r}", f"vibe add {alias}")

    added_at = raw.get("added_at")
    return Account(
        alias=alias,
        credential=_credential_from(raw, path, alias),
        identity=Identity(
            email=_optional_str(identity_map, "email"),
            account_uuid=_optional_str(identity_map, "account_uuid"),
            organization_name=_optional_str(identity_map, "organization_name"),
            organization_uuid=_optional_str(identity_map, "organization_uuid"),
            seat_tier=_optional_str(identity_map, "seat_tier"),
            billing_type=_optional_str(identity_map, "billing_type"),
            display_name=_optional_str(identity_map, "display_name"),
        ),
        state=state,
        message=_optional_str(raw, "message"),
        added_at=float(added_at) if isinstance(added_at, (int, float)) else 0.0,
    )


def write_account(root: Path, account: Account) -> None:
    identity = account.identity
    _write_json(
        account_path(root, account.alias),
        root,
        {
            "schema": SCHEMA_VERSION,
            "alias": account.alias,
            "added_at": account.added_at,
            "state": str(account.state),
            "message": account.message,
            "identity": {
                "email": identity.email,
                "account_uuid": identity.account_uuid,
                "organization_name": identity.organization_name,
                "organization_uuid": identity.organization_uuid,
                "seat_tier": identity.seat_tier,
                "billing_type": identity.billing_type,
                "display_name": identity.display_name,
            },
            "credential": credential_to_disk(account.credential),
        },
    )


@dataclass(frozen=True)
class UsageCache:
    """The last usage answer for one account, shared by every process on the machine.

    The endpoint's budget is per identity, not per process: a web dashboard, a TUI
    and `vibe list` in three terminals each polling on their own timer spend one
    budget three times over. ``payload`` is the server's JSON exactly as received
    and holds no token. ``retry_at`` and ``failures`` carry the backoff across
    processes, so a restart does not reset it and hammer an account mid-429.
    """

    added_at: float
    fetched_at: float | None
    payload: dict[str, object] | None
    retry_at: float | None
    failures: int
    message: str | None


def _epoch(value: object) -> float | None:
    # bool is an int; a hand-edited `true` must not read as a timestamp of 1.0.
    # No range check here: envelope only uses a time that sits within an hour of
    # now, which is what keeps inf, nan and year 10000 away from datetime.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def read_usage_cache(root: Path, alias: str) -> UsageCache | None:
    """None for a missing, unreadable or foreign file: a cache is never an error."""
    try:
        parsed: object = json.loads(usage_path(root, alias).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA_VERSION:
        return None
    added_at = _epoch(parsed.get("added_at"))
    if added_at is None:
        return None
    fetched_at = _epoch(parsed.get("fetched_at"))
    payload = parsed.get("payload")
    failures = parsed.get("failures")
    message = parsed.get("message")
    # A payload without its timestamp could be shown as fresh forever, so the two
    # are only ever kept together.
    usable = isinstance(payload, dict) and fetched_at is not None
    return UsageCache(
        added_at=added_at,
        fetched_at=fetched_at if usable else None,
        payload={str(key): value for key, value in payload.items()}
        if usable and isinstance(payload, dict)
        else None,
        retry_at=_epoch(parsed.get("retry_at")),
        failures=failures if isinstance(failures, int) and not isinstance(failures, bool) else 0,
        message=message if isinstance(message, str) else None,
    )


def write_usage_cache(root: Path, alias: str, cache: UsageCache) -> None:
    # A cache that cannot be written costs the next poll one request. It must not
    # cost this poll the numbers it already has.
    with contextlib.suppress(OSError):
        _write_json(
            usage_path(root, alias),
            root,
            {
                "schema": SCHEMA_VERSION,
                "added_at": cache.added_at,
                "fetched_at": cache.fetched_at,
                "payload": cache.payload,
                "retry_at": cache.retry_at,
                "failures": cache.failures,
                "message": cache.message,
            },
        )


def delete_account(root: Path, alias: str) -> None:
    path = account_path(root, alias)
    if not path.exists():
        raise NotFoundError(
            f'there is no account named "{alias}"',
            "vibe list",
        )
    path.unlink(missing_ok=True)
    # A .tmp sibling is what write_private leaves when a process dies between
    # os.open and os.replace. It holds a whole credential and nothing else will
    # ever read or remove it, so `vibe remove` must not report success over one.
    path.with_name(path.name + ".tmp").unlink(missing_ok=True)
    # Everything keyed by the alias goes with it. A stash left behind would be
    # consumed as a successor if the alias were re-added, handing the new login a
    # refresh token the server killed weeks ago.
    delete_stash(root, alias)
    usage_path(root, alias).unlink(missing_ok=True)
    release_refresh(root, alias)
    if read_active(root) == alias:
        (root / "active").unlink(missing_ok=True)


def rename_account(root: Path, old: str, new: str) -> None:
    account = read_account(root, old)
    if account_path(root, new).exists():
        raise UsageError(f'there is already an account named "{new}"', "vibe list")

    write_account(root, replace(account, alias=new))
    # Carried, not dropped: a rename inside a 429 backoff would otherwise reset the
    # ladder and fire at an endpoint that just asked us to wait.
    with contextlib.suppress(OSError):
        usage_path(root, old).replace(usage_path(root, new))
    stashed = read_stash(root, old)
    if stashed is not None:
        # An orphaned successor would leave the renamed account on a spent token.
        write_stash(root, new, stashed)
        delete_stash(root, old)
    # Move the pointer before the delete: delete_account clears an active pointer
    # that still names the old alias.
    if read_active(root) == old:
        write_active(root, new)
    delete_account(root, old)


def read_stash(root: Path, alias: str) -> Credential | None:
    path = stash_path(root, alias)
    if not path.exists():
        return None
    raw = _read_json(path, alias)
    return _credential_from(raw, path, alias)


def write_stash(root: Path, alias: str, credential: Credential) -> None:
    _write_json(
        stash_path(root, alias),
        root,
        {
            "schema": SCHEMA_VERSION,
            "alias": alias,
            "stashed_at": time.time(),
            "credential": credential_to_disk(credential),
        },
    )


def delete_stash(root: Path, alias: str) -> None:
    stash_path(root, alias).unlink(missing_ok=True)


def read_active(root: Path) -> str | None:
    try:
        alias = (root / "active").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return alias if valid_alias(alias) else None


def write_active(root: Path, alias: str) -> None:
    private_dir(root)
    write_private(root / "active", _checked(alias) + "\n")


def claim_refresh(root: Path, alias: str, *, now_s: float) -> bool:
    path = _claim_path(root, alias)
    _ensure_parent(path, root)
    payload = json.dumps({"until_ms": int((now_s + CLAIM_LEASE_S) * 1000)})
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not _claim_lapsed(path, now_s):
            return False
        # ponytail: two processes can both take over the same lapsed claim; the
        # loser then posts a spent refresh token and gets one invalid_grant. Swap
        # the O_EXCL create for an flock if that ever shows up in practice.
        write_private(path, payload)
        return True
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _claim_lapsed(path: Path, now_s: float) -> bool:
    parsed: object
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(parsed, dict):
        return True
    until_ms = parsed.get("until_ms")
    if not isinstance(until_ms, (int, float)):
        return True
    return now_s * 1000 >= until_ms


def release_refresh(root: Path, alias: str) -> None:
    _claim_path(root, alias).unlink(missing_ok=True)


def switch(root: Path, alias: str, port: KeychainPort) -> None:
    read_account(root, alias)  # an unknown alias must fail before the port is touched

    base_raw = port.read()
    base = parse_blob_members(base_raw) if base_raw is not None else None

    outgoing = read_active(root)
    if base_raw is not None and outgoing is not None and account_path(root, outgoing).exists():
        # Claude Code rotates the live token behind our back; without this resync
        # the outgoing account is stranded on a token the server already killed.
        #
        # But the blob in the port is only the outgoing account's if nobody ran
        # `claude /login` behind us. When they have, the port holds a DIFFERENT
        # account and copying it here writes one account's tokens into another
        # account's file, destroying the displaced refresh token. Identity is
        # free to check, so check it: resync only when the live account uuid is
        # unknown or agrees.
        account = read_account(root, outgoing)
        live_uuid = read_claude_identity(Path.home() / ".claude.json").account_uuid
        stored_uuid = account.identity.account_uuid
        if live_uuid is None or stored_uuid is None or live_uuid == stored_uuid:
            write_account(root, replace(account, credential=parse_blob(base_raw)))

    # Re-read: switching to the account that was already active must keep the
    # credential the resync just wrote, not the copy read before it.
    incoming = read_account(root, alias)
    port.write(credential_to_blob(incoming.credential, base))
    # After the port, never before: the identity file only describes who the
    # credential belongs to, so it must not name the incoming account while the
    # port still holds the outgoing one.
    write_claude_identity(Path.home() / ".claude.json", incoming.identity)
    write_active(root, alias)

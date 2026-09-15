#!/usr/bin/env python3
"""The one live Keychain round trip. Run it by hand, never from pytest.

pytest has to pass on Linux, where there is no Keychain at all, and the autouse
guard in tests/conftest.py fails any test that spawns `security`. So the real
write path to the user's live credential has no automated coverage anywhere --
this script is its only evidence.

    uv run python scripts/verify_switch_live.py --restore <path> --a <alias> --b <alias>

It switches to <b>, verifies, switches back to <a>, verifies, and on any failure
restores the Keychain from --restore and stops. No token value is ever printed:
credentials are compared and reported by SHA-256 prefix only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from vibemaxxing import store
from vibemaxxing.credentials import parse_blob
from vibemaxxing.keychain import CLAUDE_CODE_KEYCHAIN_SERVICE, MacKeychain, default_port


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def token_digest(blob: str) -> str:
    return hashlib.sha256(parse_blob(blob).access_token.reveal().encode()).hexdigest()[:16]


def restore(port: MacKeychain, path: Path) -> None:
    saved = path.read_text().rstrip("\n")
    port.write(saved)
    back = port.read()
    ok = back is not None and json.loads(back) == json.loads(saved)
    print(f"  restored from {path}: {'verified' if ok else 'FAILED TO VERIFY'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restore", required=True, type=Path, help="backup taken before any switch"
    )
    parser.add_argument("--a", required=True, help="the alias that is active now")
    parser.add_argument("--b", required=True, help="the alias to switch to and back from")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("this script only means anything on macOS")
        return 2
    if not args.restore.is_file():
        print(f"no restore file at {args.restore} -- take the backup first:")
        print(
            f'  /usr/bin/security find-generic-password -a "$(whoami)" -w '
            f'-s "{CLAUDE_CODE_KEYCHAIN_SERVICE}" > {args.restore}'
        )
        return 2

    port = default_port()
    if not isinstance(port, MacKeychain):
        print("the default port is not the macOS Keychain; refusing")
        return 2

    root = store.store_root()
    before = port.read()
    if before is None:
        print("the Keychain holds no Claude Code credential; nothing to verify")
        return 2

    print(f"restore file : {args.restore}")
    print(f"blob before  : sha256 {digest(before)}  ({len(before)} bytes)")
    print(f"token before : sha256 {token_digest(before)}")
    print(f"siblings     : {sorted(json.loads(before))}")

    try:
        print(f"\nswitch -> {args.b}")
        store.switch(root, args.b, port)
        mid = port.read()
        if mid is None:
            raise AssertionError("the Keychain came back empty after the switch")
        # Hashed on the spot: the plaintext never lands in a local that a
        # traceback could format.
        want_b = hashlib.sha256(
            store.read_account(root, args.b).credential.access_token.reveal().encode()
        ).hexdigest()[:16]
        got = token_digest(mid)
        print(f"  token now  : sha256 {got}")
        if got != want_b:
            raise AssertionError(f"the Keychain does not hold {args.b}'s access token")
        if sorted(json.loads(mid)) != sorted(json.loads(before)):
            raise AssertionError("a sibling member of the blob was dropped by the switch")
        print(f"  siblings   : {sorted(json.loads(mid))}  (preserved)")
        print(f"  active     : {store.read_active(root)}")

        print(f"\nswitch -> {args.a}")
        store.switch(root, args.a, port)
        after = port.read()
        if after is None:
            raise AssertionError("the Keychain came back empty after switching back")
        print(f"  token now  : sha256 {token_digest(after)}")
        if json.loads(after) != json.loads(before):
            raise AssertionError("the blob did not come back byte-for-byte equal")
        print(f"  blob equals the original: yes  (sha256 {digest(after)})")
        print(f"  active     : {store.read_active(root)}")
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        print("restoring the Keychain from the backup")
        restore(port, args.restore)
        return 1

    print("\nround trip verified. Claude Code sees the same credential it started with.")
    print(f"delete the restore file when you are done with it:\n  rm {args.restore}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

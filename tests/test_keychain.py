from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from vibemaxxing.errors import VibeError
from vibemaxxing.keychain import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_BIN,
    FileKeychain,
    MacKeychain,
    default_port,
)


def test_file_keychain_round_trip_is_owner_only(tmp_home: Path) -> None:
    port = FileKeychain()
    assert port.read() is None

    port.write('{"claudeAiOauth": {}}')
    assert port.read() == '{"claudeAiOauth": {}}'

    path = tmp_home / ".claude" / ".credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    port.write('{"claudeAiOauth": {"accessToken": "second"}}')
    assert port.read() == '{"claudeAiOauth": {"accessToken": "second"}}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_default_port_picks_by_platform_and_refuses_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(default_port(), MacKeychain)

    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(default_port(), FileKeychain)

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(VibeError) as excinfo:
        default_port()
    assert excinfo.value.recovery is not None

    # No PATH hijack, and the fixed item name is what Claude Code actually uses.
    assert SECURITY_BIN == "/usr/bin/security"
    assert CLAUDE_CODE_KEYCHAIN_SERVICE == "Claude Code-credentials"

from __future__ import annotations

import base64
import json
import sys

import pytest

from vibemaxxing.errors import NeedsLoginError
from vibemaxxing.redact import REDACTED, REGISTRY_MAX, Secret, install_excepthook, scrub

SENTINEL = "VMXSENTINEL0000000000"


def test_secret_never_renders_its_value() -> None:
    secret = Secret(SENTINEL)
    assert str(secret) == REDACTED
    assert repr(secret) == f"Secret({REDACTED})"
    assert f"{secret}" == REDACTED
    assert SENTINEL not in f"{secret!r} {secret!s}"
    assert secret.reveal() == SENTINEL


def test_secret_is_not_json_serialisable() -> None:
    with pytest.raises(TypeError):
        json.dumps({"token": Secret(SENTINEL)})


def test_scrub_replaces_literal_base64_and_json_escaped_forms() -> None:
    Secret(SENTINEL)
    encoded = base64.b64encode(SENTINEL.encode()).decode()
    text = f"literal={SENTINEL} base64={encoded} json={json.dumps(SENTINEL)}"
    scrubbed = scrub(text)
    assert SENTINEL not in scrubbed
    assert encoded not in scrubbed


def test_short_values_are_never_registered() -> None:
    Secret("abc")
    assert scrub("abc") == "abc"


def test_registry_is_bounded() -> None:
    from vibemaxxing import redact

    for index in range(REGISTRY_MAX + 50):
        Secret(f"token-value-{index:06d}")
    assert len(redact._registry) <= REGISTRY_MAX


def test_error_render_names_problem_and_recovery() -> None:
    error = NeedsLoginError('login lapsed for "work"', "vibe add work")
    assert error.render() == 'login lapsed for "work"\n  run: vibe add work'


def test_error_render_is_scrubbed() -> None:
    Secret(SENTINEL)
    assert SENTINEL not in NeedsLoginError(f"boom {SENTINEL}").render()


def test_excepthook_scrubs_tracebacks(capsys: pytest.CaptureFixture[str]) -> None:
    Secret(SENTINEL)
    install_excepthook()
    try:
        raise RuntimeError(f"exploded with {SENTINEL}")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err
    assert REDACTED in captured.err

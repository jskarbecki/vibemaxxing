"""The one redaction helper. Every output path in the package goes through it.

A token value lives only inside a ``Secret``. ``Secret`` renders as ``REDACTED``
in every string context and is not JSON-serialisable, so the common ways a token
leaks — an f-string, a ``repr`` in a traceback, ``json.dumps`` of a dataclass —
fail closed rather than print. ``scrub`` is the second layer: it replaces any
registered secret that reached a string by some other route, in its literal,
base64 and JSON-escaped forms.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from collections import OrderedDict
from types import TracebackType
from typing import Final

REDACTED: Final = "«redacted»"
MIN_SECRET_LEN: Final = 8
REGISTRY_MAX: Final = 256

# Bounded LRU keyed by the secret, valued by every form of it worth replacing.
# A refresh rotates the refresh token on every call, so an unbounded registry in
# a dashboard left open for days is a slow leak; the evicted entries are spent
# predecessors.
_registry: OrderedDict[str, tuple[str, ...]] = OrderedDict()


def _forms(value: str) -> tuple[str, ...]:
    candidates = (
        value,
        base64.b64encode(value.encode()).decode(),
        json.dumps(value)[1:-1],
    )
    return tuple(dict.fromkeys(c for c in candidates if len(c) >= MIN_SECRET_LEN))


def register(value: str) -> None:
    # A short value would match unrelated output and redact the whole page.
    if len(value) < MIN_SECRET_LEN:
        return
    _registry[value] = _forms(value)
    _registry.move_to_end(value)
    while len(_registry) > REGISTRY_MAX:
        _registry.popitem(last=False)


def scrub(text: str) -> str:
    if not _registry:
        return text
    forms = {form for group in _registry.values() for form in group}
    # Longest first: a short form that is a substring of a longer one must not
    # shred the longer one into an unrecognisable partial match.
    for form in sorted(forms, key=len, reverse=True):
        if form in text:
            text = text.replace(form, REDACTED)
    return text


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value
        register(value)

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


def out(text: str) -> None:
    sys.stdout.write(scrub(text))


def err(text: str) -> None:
    sys.stderr.write(scrub(text))


def _excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    sys.stderr.write(scrub("".join(traceback.format_exception(exc_type, exc, tb))))


def install_excepthook() -> None:
    sys.excepthook = _excepthook

from __future__ import annotations

import argparse
from collections.abc import Sequence

from vibemaxxing import __version__
from vibemaxxing.redact import install_excepthook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe",
        description="Manage several Claude Code accounts and see pooled plan usage.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    install_excepthook()
    build_parser().parse_args(argv)
    return 0
